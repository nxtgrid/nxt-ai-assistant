"""
FastAPI application wrapper for the serverless handler.

This module provides a FastAPI app that wraps the existing serverless
handler.main() function for deployment on App Platform.
"""

# Import the serverless handler
import asyncio
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import quote

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from orchestrator.services.ticketing.alert_facts import AlertFacts, derive_severity
from shared.utils.gdrive_doc_fetcher import GoogleDriveDocFetcher
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None
agent_worker = None  # AgentWorker instance (if persistent agents enabled)

# --- Graceful shutdown: track all active Telegram workflow tasks ---
# asyncio.Tasks created here (not BackgroundTasks) so they are independently
# cancellable and trackable during SIGTERM-triggered shutdown.
# NOTE: Only safe at instance_count=1. If scaling to multiple instances,
# use a distributed lock (see docs/VALKEY_CHECKPOINTING_REFERENCE.md) to
# prevent two instances racing to recover the same packet.
_active_workflow_tasks: set[asyncio.Task] = set()
_shutdown_in_progress = False

# Add parent directory to path to import handler
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from handler import async_main

if TYPE_CHECKING:
    from shared.auth.auth_service import GridNotificationTarget


def get_api_key():
    """Get API key from environment."""
    return os.getenv("API_KEY", "")


def get_auth_method(request: Request) -> str:
    """
    Determine authentication method from request headers.

    Returns:
        "api" if X-Api-Key header matches
        "telegram" if X-Telegram-Bot-Api-Secret-Token header matches

    Raises:
        HTTPException 401 if no valid auth found
    """
    api_key = request.headers.get("X-Api-Key")
    telegram_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    expected_key = get_api_key()

    # Log headers for debugging
    logger.info(
        f"Auth check: X-Api-Key={bool(api_key)}, "
        f"X-Telegram-Bot-Api-Secret-Token={bool(telegram_secret)}, "
        f"API_KEY configured={bool(expected_key)}"
    )

    if not expected_key:
        raise HTTPException(
            status_code=401,
            detail="API_KEY not configured on server",
        )

    if api_key and api_key == expected_key:
        logger.info("Auth method: api (X-Api-Key header)")
        return "api"
    elif telegram_secret and telegram_secret == expected_key:
        logger.info("Auth method: telegram (X-Telegram-Bot-Api-Secret-Token header)")
        return "telegram"
    else:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "API key"},
        )


app = FastAPI(
    title="Anansi Chat Orchestrator",
    description="Chat orchestration service with Gemini and MCP tool integration",
    version="1.0.0",
)

# Enable CORS — restrict to known origins. The mini app is served from the same
# origin so same-origin requests need no CORS. Telegram and API key callers are
# server-to-server and don't use CORS. CORS_ALLOWED_ORIGINS can be overridden
# via env var (comma-separated) for dev or multi-domain setups.
_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", os.getenv("APP_URL", "http://localhost:8501"))
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Api-Key", "X-Telegram-Bot-Api-Secret-Token", "Content-Type"],
)


@app.middleware("http")
async def https_redirect_and_hsts(request: Request, call_next):
    """Redirect HTTP → HTTPS and add HSTS header.

    DigitalOcean / Cloudflare terminates TLS, so check X-Forwarded-Proto.
    Telegram Desktop may load web_app URLs over HTTP; this catches that.
    """
    proto = request.headers.get("x-forwarded-proto", "https")
    if proto == "http" and request.url.hostname not in ("localhost", "127.0.0.1"):
        from starlette.responses import RedirectResponse

        https_url = str(request.url).replace("http://", "https://", 1)
        return RedirectResponse(https_url, status_code=301)

    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Mount Mini App API router
if os.getenv("MINI_APP_FORMS_ENABLED", "false").lower() == "true":
    from orchestrator.mini_app.router import router as mini_app_router

    app.include_router(mini_app_router)
    # Serve built mini app static files at /mini-app/
    # Docker: /app/mini_app/dist, Local dev: ../../mini_app/dist relative to chat_orchestrator/
    mini_app_dist = Path("/app/mini_app/dist")
    if not mini_app_dist.is_dir():
        mini_app_dist = Path(__file__).parent.parent.parent.parent / "mini_app" / "dist"
    if mini_app_dist.is_dir():
        app.mount(
            "/mini-app", StaticFiles(directory=str(mini_app_dist), html=True), name="mini-app"
        )
        logger.info("Mini App mounted at /mini-app/ (static) + /api/mini-app/ (API)")
    else:
        logger.warning("Mini App dist not found at %s — static files not served", mini_app_dist)


async def _handle_sigterm() -> None:
    """SIGTERM handler: cancel active workflow tasks and wait for cleanup.

    Called when DigitalOcean App Platform signals the old container to shut down
    during a rolling deployment.  Active Telegram workflow tasks are cancelled so
    their CancelledError handlers can write 'interrupted' state before the process
    exits.  APScheduler is stopped first to prevent new jobs from starting.

    Uses asyncio.wait() with a shared 60-second budget (not per-task sequential
    waits) so the total wait is bounded regardless of how many tasks are active.
    """
    logger.info("SIGTERM received — beginning graceful workflow shutdown")

    # Stop scheduler before cancelling tasks to prevent new jobs starting
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

    if _active_workflow_tasks:
        # Cancel all active workflow tasks
        for task in list(_active_workflow_tasks):
            task.cancel()

        # Wait for all tasks concurrently within the shared budget.
        # Tasks that finish write their 'interrupted' state via CancelledError handler.
        # Tasks that don't finish in time are covered by the startup recovery scan.
        done, pending = await asyncio.wait(list(_active_workflow_tasks), timeout=60)
        if pending:
            logger.warning(
                f"SIGTERM: {len(pending)} task(s) did not complete cleanup within 60s "
                "(startup recovery scan will catch these on next boot)"
            )

    logger.info("SIGTERM: workflow shutdown complete")


async def warmup_services():
    """Pre-load MCP tools and Google Docs to avoid cold start latency.

    This runs in the background after startup to warm caches without
    blocking the health check or delaying server readiness.
    """
    import asyncio

    # Small delay to let the server fully start first
    await asyncio.sleep(2)

    logger.info("Starting service warmup...")
    start_time = asyncio.get_running_loop().time()

    try:
        # 1. Pre-load MCP tools (imports all server modules and caches tool definitions)
        from orchestrator.models.schemas import UserContext
        from orchestrator.services.user_permissions import UserPermissionsService

        permissions_service = UserPermissionsService()
        # Create a minimal context for warmup (staff to get all tools)
        warmup_context = UserContext(
            user_id="warmup",
            user_email="warmup@system",
            session_id="warmup",
            is_staff=True,
        )
        tools = await permissions_service.get_available_tools(warmup_context)
        logger.info(f"Warmup: Loaded {len(tools)} MCP tools")

        # 2. Pre-fetch all Google Docs (system instructions for both modes)
        from orchestrator.services.artifacts_provider import ArtifactsProvider

        provider = ArtifactsProvider()

        # NOTE: EXPERT_INSTRUCTIONS_DOC_ID is intentionally NOT warmed here. The
        # artifacts provider parses a flat "system instructions" section, but the
        # expert doc uses per-expert "# Expert:" headers, so this parser always
        # returns 0 sections — logging a misleading "failed after 3 attempts"
        # error and wasting ~6s every startup. The expert doc is loaded and
        # cached correctly by expert_instructions_provider instead.
        docs_to_cache = [
            ("STAFF_SUPPORT_DOC_ID", "staff instructions"),
            ("CUSTOMER_SUPPORT_DOC_ID", "customer instructions"),
            ("VERIFICATION_DOC_ID", "verification criteria"),
        ]

        loop = asyncio.get_running_loop()
        for env_var, description in docs_to_cache:
            doc_id = os.getenv(env_var)
            if doc_id:
                try:
                    await loop.run_in_executor(None, provider._fetch_google_doc_sections, doc_id)
                    logger.info(f"Warmup: Cached {description} doc")
                except Exception as e:
                    logger.warning(f"Warmup: Failed to cache {description}: {e}")

        elapsed = asyncio.get_running_loop().time() - start_time
        logger.info(f"Service warmup complete in {elapsed:.1f}s")

    except Exception as e:
        logger.warning(f"Warmup failed (non-fatal): {e}")


async def _run_startup_recovery() -> None:
    """Kick off the startup recovery scan after a brief delay (let the server fully start)."""
    await asyncio.sleep(3)
    try:
        from orchestrator.services.startup_recovery_service import recover_orphaned_packets

        count = await recover_orphaned_packets()
        if count:
            logger.info(f"Startup recovery: re-enqueued {count} interrupted packet(s)")
    except Exception:
        logger.exception("Startup recovery scan failed (non-fatal)")


@app.on_event("startup")
async def startup_event():
    """Initialize scheduled tasks on application startup."""
    global scheduler

    # Register SIGTERM handler for graceful workflow shutdown during deployments
    loop = asyncio.get_running_loop()

    def _schedule_sigterm():
        global _shutdown_in_progress
        if _shutdown_in_progress:
            logger.warning("SIGTERM received again — shutdown already in progress, ignoring")
            return
        _shutdown_in_progress = True
        asyncio.create_task(_handle_sigterm())

    loop.add_signal_handler(signal.SIGTERM, _schedule_sigterm)

    # Start warmup in background (don't block startup)
    asyncio.create_task(warmup_services())

    # Run startup recovery scan: finds packets orphaned by previous deployment crashes
    asyncio.create_task(_run_startup_recovery())

    # Check if any scheduled services are enabled
    metrics_enabled = os.getenv("METRICS_ENABLED", "true").lower() == "true"
    grafana_enabled = os.getenv("GRAFANA_ACTIONS_ENABLED", "false").lower() == "true"

    if not metrics_enabled and not grafana_enabled:
        logger.info(
            "All scheduled services disabled (METRICS_ENABLED and GRAFANA_ACTIONS_ENABLED are false)"
        )

    if metrics_enabled or grafana_enabled:
        # Initialize scheduler
        scheduler = AsyncIOScheduler()

        # Get schedule configuration
        schedule_timezone = os.getenv("METRICS_TIMEZONE", "UTC")

        # Schedule metrics job if enabled
        if metrics_enabled:
            metrics_hour = int(os.getenv("METRICS_SCHEDULE_HOUR", "9"))
            logger.info(
                f"Setting up metrics scheduler to run weekly on Monday at {metrics_hour:02d}:00 {schedule_timezone}"
            )

            # Import metrics service (lazy import to avoid circular dependencies)
            from orchestrator.services.metrics_service import MetricsService

            metrics_service = MetricsService()

            # Schedule weekly metrics job (runs every Monday)
            scheduler.add_job(
                metrics_service.send_weekly_metrics,
                trigger=CronTrigger(
                    day_of_week="mon", hour=metrics_hour, minute=0, timezone=schedule_timezone
                ),
                id="weekly_metrics",
                name="Send Weekly Metrics to Telegram",
                replace_existing=True,
            )
            logger.info("Metrics scheduler configured")

        # Schedule Grafana indexing job if enabled
        if grafana_enabled:
            grafana_hour = int(os.getenv("GRAFANA_SYNC_HOUR", "2"))
            logger.info(
                f"Setting up Grafana indexer to run daily at {grafana_hour:02d}:00 {schedule_timezone}"
            )

            # Define async wrapper for Grafana indexer
            async def run_grafana_indexer():
                """Run Grafana panel indexing."""
                try:
                    logger.info("Starting scheduled Grafana panel indexing...")
                    # Run indexer in thread pool since it's CPU-bound
                    import asyncio
                    import os
                    import sys

                    # Add rag_pipeline to path
                    rag_pipeline_path = os.path.join(
                        os.path.dirname(__file__), "../../../rag_pipeline/ingestion"
                    )
                    if rag_pipeline_path not in sys.path:
                        sys.path.insert(0, rag_pipeline_path)

                    from grafana_indexer_incremental import index_all_grafana_panels

                    result = await asyncio.get_event_loop().run_in_executor(
                        None, index_all_grafana_panels, False
                    )

                    if result.get("status") == "completed":
                        logger.info(
                            f"Grafana indexing completed: {result.get('panels_indexed', 0)} panels indexed"
                        )
                    else:
                        logger.error(
                            f"Grafana indexing failed: {result.get('message', 'Unknown error')}"
                        )

                except Exception as e:
                    logger.error(f"Error during scheduled Grafana indexing: {e}", exc_info=True)

            # Schedule nightly Grafana indexing job
            scheduler.add_job(
                run_grafana_indexer,
                trigger=CronTrigger(hour=grafana_hour, minute=0, timezone=schedule_timezone),
                id="grafana_indexer",
                name="Index Grafana Dashboard Panels",
                replace_existing=True,
            )
            logger.info("Grafana indexer scheduler configured")

        scheduler.start()
        logger.info("Scheduler started successfully")

    # Initialize persistent agent worker if enabled
    agents_enabled = os.getenv("PERSISTENT_AGENTS_ENABLED", "false").lower() in ("true", "1", "yes")
    if agents_enabled:
        try:
            from orchestrator.services.agent_worker import AgentWorker

            global agent_worker
            supabase_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
            supabase_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
            agent_worker = AgentWorker(supabase_url=supabase_url, supabase_key=supabase_key)
            await agent_worker.start()

            # Ensure scheduler exists for agent jobs
            if scheduler is None:
                scheduler = AsyncIOScheduler()

            # Safety poll: process batch every 15 minutes (fallback for missed NOTIFY)
            # PG LISTEN/NOTIFY handles near-instant wake; this is just a fallback.
            scheduler.add_job(
                agent_worker.process_batch,
                trigger="interval",
                seconds=900,
                max_instances=1,
                coalesce=True,
                id="agent_batch_poll",
                name="Agent Event Safety Poll",
                replace_existing=True,
            )

            # Scheduled wakes: check cron schedules every 15 minutes
            scheduler.add_job(
                agent_worker.queue_scheduled_wakes,
                trigger="interval",
                seconds=900,
                max_instances=1,
                coalesce=True,
                id="agent_scheduled_wakes",
                name="Agent Scheduled Wakes",
                replace_existing=True,
            )

            # Reconciliation: auto-provision/terminate persistent agent instances every 5 minutes
            scheduler.add_job(
                agent_worker.reconcile_instances,
                trigger="interval",
                minutes=5,
                max_instances=1,
                coalesce=True,
                id="reconcile_agents",
                name="Reconcile Persistent Agent Instances",
                replace_existing=True,
            )

            if not scheduler.running:
                scheduler.start()

            logger.info("Persistent agent worker initialized with scheduler jobs")
        except Exception as e:
            logger.error(f"Failed to start persistent agent worker: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Escalation Jira sweep — runs daily at 9am WAT (08:00 UTC, WAT is UTC+1,
    # no DST).  Registered unconditionally so it fires even when METRICS_ENABLED
    # and GRAFANA_ACTIONS_ENABLED are both false.
    # -------------------------------------------------------------------------
    from orchestrator.services.escalation_service import EscalationService

    _escalation_svc = EscalationService()

    # Startup orphan recovery always runs regardless of JIRA_SWEEP_ENABLED —
    # manual Track button clicks also create claims that can be orphaned by SIGTERM.
    async def _startup_orphan_recovery():
        await asyncio.sleep(5)
        try:
            await _escalation_svc.recover_orphaned_claims()
        except Exception:
            logger.exception("Escalation orphan recovery failed (non-fatal)")

    asyncio.create_task(_startup_orphan_recovery())

    jira_sweep_enabled = os.getenv("JIRA_SWEEP_ENABLED", "true").lower() == "true"
    if jira_sweep_enabled:

        async def _run_escalation_jira_sweep():
            """Daily sweep: auto-file Jira tickets for stale unclaimed escalations."""
            logger.info("Starting daily escalation Jira sweep")
            start = time.monotonic()
            try:
                summary = await _escalation_svc.run_escalation_jira_sweep()
                logger.info(
                    "Escalation sweep complete in %.1fs: %s",
                    time.monotonic() - start,
                    summary,
                )
            except Exception:
                logger.exception("Escalation Jira sweep job failed")

        if scheduler is None:
            scheduler = AsyncIOScheduler()

        scheduler.add_job(
            _run_escalation_jira_sweep,
            trigger=CronTrigger(hour=8, minute=0, timezone="UTC"),  # 9am WAT = 8am UTC
            id="escalation_jira_sweep",
            name="Daily Escalation Jira Sweep",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        if not scheduler.running:
            scheduler.start()

        logger.info("Escalation Jira sweep scheduled (daily 08:00 UTC)")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up agent worker and scheduler on application shutdown."""
    # Stop agent worker
    global agent_worker
    if agent_worker:
        try:
            await agent_worker.stop()
            logger.info("Agent worker stopped")
        except Exception as e:
            logger.warning(f"Agent worker shutdown failed (non-fatal): {e}")
        agent_worker = None

    global scheduler
    if scheduler:
        scheduler.shutdown()
        logger.info("Metrics scheduler shut down")

    # Flush pending Langfuse traces
    from shared.utils.langfuse_utils import LANGFUSE_ENABLED

    if LANGFUSE_ENABLED:
        try:
            import asyncio

            from langfuse import get_client

            client = get_client()
            await asyncio.to_thread(client.shutdown)
            logger.info("Langfuse client shut down")
        except Exception as e:
            logger.warning(f"Langfuse shutdown failed (non-fatal): {e}")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "chat-orchestrator"}


@app.get("/api/v1/jobs")
async def list_scheduled_jobs(request: Request):
    """Return all registered APScheduler jobs with next run time.

    Authentication:
        - X-Api-Key header required
    """
    get_auth_method(request)
    if scheduler is None:
        return JSONResponse({"jobs": []})
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
        )
    return JSONResponse({"jobs": jobs})


async def _handle_jira_webhook(payload: dict) -> None:
    """Dispatch Jira webhook events to EscalationService handlers."""
    from orchestrator.services.escalation_service import EscalationService

    svc = EscalationService()
    event = payload.get("webhookEvent", "")
    issue_event = payload.get("issue_event_type_name", "")

    if event == "comment_created":
        await svc.handle_jira_comment(payload)
    elif event == "jira:issue_updated" and issue_event != "issue_commented":
        # Guard: Jira fires both comment_created AND jira:issue_updated for new comments.
        # Filtering by issue_event_type_name prevents double-processing.
        await svc.handle_jira_issue_updated(payload)
    else:
        logger.debug("Ignoring Jira webhook event=%s issue_event=%s", event, issue_event)


@app.post("/webhook/jira")
async def jira_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """Receive Jira webhook events (comment_created, jira:issue_updated).

    Authentication: Jira Cloud signs the request body with HMAC-SHA256 using the
    webhook secret and sends the digest in the X-Hub-Signature header as
    "sha256=<hex_digest>".  Set JIRA_WEBHOOK_SECRET to the same value configured
    in the Jira webhook settings.

    The endpoint is fail-closed: if JIRA_WEBHOOK_SECRET is not configured it
    rejects all requests rather than accepting them unauthenticated.
    """
    import hashlib
    import hmac

    secret = os.getenv("JIRA_WEBHOOK_SECRET", "")
    if not secret:
        logger.error("JIRA_WEBHOOK_SECRET not configured — rejecting Jira webhook request")
        raise HTTPException(status_code=401, detail="Webhook authentication not configured")

    body_bytes = await request.body()
    expected_sig = "sha256=" + hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    sig_header = request.headers.get("X-Hub-Signature", "")
    if not hmac.compare_digest(sig_header, expected_sig):
        logger.warning("Jira webhook HMAC mismatch")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        import json

        payload = json.loads(body_bytes)
    except Exception as e:
        logger.error("Failed to parse Jira webhook body: %s", e)
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON"})

    background_tasks.add_task(_handle_jira_webhook, payload)
    return JSONResponse(status_code=200, content={"ok": True})


async def _run_telegram_workflow(body: dict, chat_id: str, topic_id: int | None) -> None:
    """Run a Telegram webhook workflow and send an error message if it fails."""
    try:
        await async_main(body)
    except Exception as e:
        logger.error(f"Telegram workflow failed for chat {chat_id}: {e}", exc_info=True)
        if chat_id:
            try:
                from shared.utils.telegram_send import send_telegram_message

                bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                await send_telegram_message(
                    bot_token,
                    chat_id,
                    "Something went wrong — please try again.",
                    topic_id=topic_id,
                )
            except Exception:
                pass


async def _is_staff_for_disabled_check(body: dict, auth_method: str) -> bool:
    """Determine if a request originates from a staff context.

    Used by the BOT_ENABLED=false branch to decide whether to surface a
    visible "disabled" notice (staff) or stay silent (customers).
    """
    metadata = body.get("metadata") or {}
    if metadata.get("staff_group_auth"):
        return True
    if metadata.get("scheduled_is_staff"):
        return True
    if metadata.get("is_staff"):
        return True

    if auth_method == "telegram":
        tg_msg = body.get("message") or body.get("edited_message") or {}
        tg_chat = tg_msg.get("chat") or {}
        chat_id = str(tg_chat.get("id") or "").strip()
        topic_id = tg_msg.get("message_thread_id")
        tg_user_id = str((tg_msg.get("from") or {}).get("id") or "").strip()
    else:
        chat_id = str(body.get("chat_id") or "").strip()
        topic_id = body.get("topic_id")
        tg_user_id = str(body.get("user_id") or "").strip()

    if not chat_id:
        return False

    try:
        from shared.auth import get_auth_service

        perms = await get_auth_service().resolve_permissions_from_chat(
            chat_id=chat_id,
            topic_id=topic_id,
            user_id=tg_user_id or "disabled-check",
            telegram_id=tg_user_id or None,
        )
        return bool(perms and perms.is_staff)
    except Exception as e:
        logger.warning(f"is_staff lookup failed during BOT_ENABLED check: {e}")
        return False


async def _send_telegram_disabled_notice(chat_id: str, topic_id, reply_to_message_id=None) -> None:
    """Send a 'Bot is currently disabled' notice via Telegram Bot API."""
    import httpx

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token or not chat_id:
        return
    payload: dict = {"chat_id": chat_id, "text": "Bot is currently disabled."}
    if topic_id is not None:
        try:
            payload["message_thread_id"] = int(topic_id)
        except (TypeError, ValueError):
            pass
    if reply_to_message_id is not None:
        try:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        except (TypeError, ValueError):
            pass
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=payload,
            )
    except Exception as e:
        logger.warning(f"Failed to send BOT_ENABLED=false notice to Telegram: {e}")


@app.post("/")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Handle incoming webhook requests.

    Accepts both Telegram native format and internal webhook format.

    Authentication:
        - X-Api-Key: Returns response in HTTP body
        - X-Telegram-Bot-Api-Secret-Token: Sends response via Telegram Bot API
    """
    # Verify authentication and get method
    auth_method = get_auth_method(request)

    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse JSON body: {e}")
        logger.error(f"Content-Type: {request.headers.get('content-type')}")
        try:
            raw_body = await request.body()
            logger.error(f"Raw body (first 200 chars): {raw_body[:200]}")
        except Exception:
            pass
        return JSONResponse(
            status_code=400, content={"success": False, "error": f"Invalid JSON body: {str(e)}"}
        )

    # When BOT_ENABLED=false, staff get a visible "disabled" notice;
    # customers and unknown audiences get silence (no delivery, no Telegram message).
    bot_enabled = os.getenv("BOT_ENABLED", "true").lower() in ("true", "1", "yes")
    if not bot_enabled:
        is_staff = await _is_staff_for_disabled_check(body, auth_method)
        logger.info(
            f"Bot is disabled via BOT_ENABLED flag - is_staff={is_staff}, auth={auth_method}"
        )

        if auth_method == "telegram":
            if is_staff:
                tg_msg = body.get("message") or body.get("edited_message") or {}
                tg_chat_id = str((tg_msg.get("chat") or {}).get("id") or "")
                tg_topic_id = tg_msg.get("message_thread_id")
                tg_msg_id = tg_msg.get("message_id")
                await _send_telegram_disabled_notice(tg_chat_id, tg_topic_id, tg_msg_id)
            return JSONResponse(status_code=200, content={"success": True})

        # API key path (direct callers / scheduler)
        if is_staff:
            # success=True so the scheduler delivers the notice to staff
            return JSONResponse(
                status_code=200,
                content={"success": True, "message": "Bot is currently disabled."},
            )
        # Customer / unknown: success=False so the scheduler's safety filter
        # treats this as a failed run and delivers nothing.
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "error": "Bot is currently disabled",
                "message": "Bot is currently disabled",
            },
        )

    # Add auth method to body so handler knows how to respond
    body["_auth_method"] = auth_method

    if auth_method == "telegram":
        # Return 200 immediately to prevent Telegram from retrying the webhook.
        # Telegram retries if no 200 is received within ~5s; long-running workflows
        # (e.g. embedding 1000+ chunks) exceed this, causing duplicate executions.
        # Telegram sends its response via the Bot API anyway, not the HTTP body.
        #
        # Use asyncio.create_task (not BackgroundTasks) so the workflow is a real
        # asyncio.Task that can be tracked in _active_workflow_tasks and cancelled
        # cleanly during SIGTERM-triggered graceful shutdown.
        tg_msg = body.get("message") or body.get("edited_message") or {}
        tg_chat_id = str(tg_msg.get("chat", {}).get("id", ""))
        tg_topic_id = tg_msg.get("message_thread_id")
        task = asyncio.create_task(_run_telegram_workflow(body, tg_chat_id, tg_topic_id))
        _active_workflow_tasks.add(task)
        task.add_done_callback(_active_workflow_tasks.discard)
        return JSONResponse(status_code=200, content={"success": True})

    # For API key auth, caller is waiting for the response in the HTTP body
    result = await async_main(body)
    status_code = result.pop("statusCode", 200)
    return JSONResponse(status_code=status_code, content=result)


@app.post("/chat")
async def handle_chat(request: Request, background_tasks: BackgroundTasks):
    """
    Alternative endpoint for chat requests.

    Same as root endpoint but with explicit /chat path.

    Authentication:
        - X-Api-Key: Returns response in HTTP body
        - X-Telegram-Bot-Api-Secret-Token: Sends response via Telegram Bot API
    """
    # Delegate to handle_webhook — including BOT_ENABLED handling, which is
    # staff-aware (visible notice for staff, silent for customers).
    return await handle_webhook(request, background_tasks)


# ============================================================================
# External Notification Passthrough (n8n / VRM / Grafana → Telegram)
# ============================================================================


class NotifyRequest(BaseModel):
    """Payload for an external notification forwarded to a grid's Telegram group.

    n8n (or any authorized upstream) composes the message and posts it here with a
    grid name only. Anansi resolves the grid to its internal Telegram group (chat +
    topic) via the Auth DB — the single source of truth — and performs the send, so
    callers never handle raw chat/topic IDs and outbound formatting, retry, and
    logging stay consistent with chat replies.
    """

    source: str = Field(..., description="Origin system, e.g. 'vrm', 'grafana', 'n8n'.")
    grid_name: str = Field(
        ...,
        min_length=1,
        description="Target grid name. Resolved server-side (fuzzy, 80% threshold) to the "
        "grid's internal Telegram group chat + topic. Undeliverable if no confident match.",
    )
    text: str = Field(..., description="Message body (GitHub-flavoured markdown accepted).")
    parse_mode: Optional[str] = Field(
        default="Markdown",
        description="Telegram parse mode. 'Markdown' converts + falls back to plain text; "
        "pass null/empty to send verbatim plain text.",
    )
    dedup_key: Optional[str] = Field(
        default=None, description="Optional idempotency hint (logged, not enforced here)."
    )
    ticket_id: Optional[str] = Field(
        default=None,
        description="Ticketing hint. Omit for a pure passthrough alert (today's behavior, "
        "unchanged). Empty string ('') to FILE a new ticket from this notification -- "
        "response returns {ok, ticket_ref}. 'auto' to let Anansi decide whether this alert "
        "is new, relates to an existing open ticket on this grid (amend), or is an exact "
        "re-fire (duplicate) -- see ALERT_CORRELATION_ENABLED; response additionally returns "
        "{decision, correlated_with, confidence, decided_by}. A ref (e.g. 'TKT-000123' or "
        "'OPS-55') to append this notification as a comment/update to that existing ticket -- "
        "response returns {ok, ticket_ref: ticket_id}. An unresolvable ref returns 404.",
    )
    close: Optional[bool] = Field(
        default=False,
        description="With a populated ticket_id, also transition that ticket to done "
        "after the comment is added. Ignored when ticket_id is omitted, blank, or 'auto'.",
    )
    alert: Optional[AlertFacts] = Field(
        default=None,
        description="Structured alert facts for ticket_id='auto' correlation (subject, "
        "alert_type, details, severity, component_kind/key/label, fired_at, rule_id). "
        "Every field is independently derivable from `text`/`subject` when omitted -- pass "
        "what you already have (e.g. n8n's extracted MPPT/DCU id) and Anansi fills the rest. "
        "Ignored for any ticket_id other than 'auto'.",
    )


async def _log_notification_to_chat_db(
    body: "NotifyRequest",
    chat_id: str,
    topic_id: Optional[str],
    telegram_message_id: int,
    ticket_ref: Optional[str] = None,
) -> None:
    """Best-effort: record a forwarded notification in the chat's existing session.

    Gives Anansi context when a user later replies to the alert in the group.
    Never creates a session (that would pollute session state for chats that have
    never talked to the bot) and never raises — logging failures are non-fatal.

    When ``ticket_ref`` is set (this notification created or updated a ticket),
    the saved message is also tagged via ``tag_message_as_ticket_comment`` so it
    shows up in that ticket's comment timeline (``get_ticket_comments``), mirroring
    how forwarded escalation replies are tagged.
    """
    try:
        from orchestrator.models.schemas import ConversationMessage
        from orchestrator.services.supabase_client import get_supabase_client

        client = get_supabase_client()
        session = await client.get_session_by_chat_id(
            source="telegram",
            chat_id=str(chat_id),
            topic_id=str(topic_id) if topic_id else None,
        )
        if not session or session.id is None:
            return

        message = ConversationMessage(
            role="model",
            content=body.text,
            timestamp=datetime.now(timezone.utc).isoformat(),
            telegram_message_id=telegram_message_id,
            metadata={
                "channel": "notify_endpoint",
                "notification_source": body.source,
                "grid_name": body.grid_name,
                **({"dedup_key": body.dedup_key} if body.dedup_key else {}),
            },
        )
        saved = await client.save_messages(session.id, [message], from_chat_id=str(chat_id))
        if ticket_ref and saved:
            await client.tag_message_as_ticket_comment(saved[0].id, ticket_ref)
    except Exception as e:
        logger.warning("Notify: chat-db logging failed (non-fatal): %s", e)


async def _deliver_notification(
    body: "NotifyRequest",
    target: "GridNotificationTarget",
    ticket_ref: Optional[str] = None,
    delivery: "Optional[NotificationDelivery]" = None,
) -> None:
    """Convert, send, and log a notification to an already-resolved grid target.

    Grid resolution (and, if requested, ticket creation/comment/close) happens
    synchronously in the handler so failures are reported to the caller rather
    than silently dropped here; this runs in the background and only covers the
    Telegram send + best-effort session logging/tagging.

    ``delivery`` (populated only for ``ticket_id="auto"``, see
    ``_resolve_notify_ticket_auto``) can suppress the send entirely (a silent
    "duplicate" between roll-ups), override the text (a short amend/roll-up
    reply instead of the full alert), and/or thread the send as a reply to an
    earlier message. ``delivery=None`` (every other ``ticket_id`` path) is
    exactly today's behavior: the full alert text, unthreaded.
    """
    from shared.utils.telegram_markdown import convert_github_to_telegram_markdown
    from shared.utils.telegram_send import send_telegram_message_with_fallback

    if delivery is not None and delivery.suppress:
        logger.info(
            "Notify: delivery suppressed source=%s grid=%s ticket_ref=%s",
            body.source,
            target.grid_name,
            ticket_ref,
        )
        return

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        logger.error("Notify: TELEGRAM_BOT_TOKEN not configured — dropping notification")
        return

    parse_mode = (body.parse_mode or "").strip() or None
    ticketed_delivery = delivery is not None and delivery.ticket is not None
    if ticketed_delivery:
        # Ticket references are deliberately rendered as Telegram Markdown
        # links, so a caller-provided plain/HTML mode cannot make the link
        # literal or parse it under the wrong grammar.
        parse_mode = "Markdown"
    raw_text = (delivery.text_override if delivery is not None and delivery.text_override else body.text)
    if ticketed_delivery:
        raw_text = (
            _format_ticket_update_notification(
                raw_text, delivery.ticket, urgent=delivery.top_level
            )
            if delivery.text_override
            else _format_ticket_notification(body, delivery.ticket)
        )
    text = raw_text
    if parse_mode and parse_mode.lower().startswith("markdown"):
        text = convert_github_to_telegram_markdown(raw_text)

    reply_to_message_id = (
        delivery.reply_to_message_id if delivery is not None and not delivery.top_level else None
    )

    message_id = await send_telegram_message_with_fallback(
        bot_token,
        target.chat_id,
        text,
        parse_mode=parse_mode,
        topic_id=target.topic_id,
        reply_to_message_id=reply_to_message_id,
    )
    if message_id is None:
        logger.warning(
            "Notify: delivery failed source=%s grid=%s chat=%s",
            body.source,
            target.grid_name,
            target.chat_id,
        )
        return

    logger.info(
        "Notify: forwarded source=%s grid=%s (fuzzy=%s) chat=%s message_id=%s",
        body.source,
        target.grid_name,
        target.was_fuzzy,
        target.chat_id,
        message_id,
    )
    await _log_notification_to_chat_db(
        body, target.chat_id, target.topic_id, message_id, ticket_ref=ticket_ref
    )

    if delivery is not None and delivery.record_message_id_for_ticket_ref:
        try:
            from orchestrator.services.ticketing.correlation_store import CorrelationStore

            store = CorrelationStore(get_client=_raw_supabase_client)
            await store.record_message_id(delivery.record_message_id_for_ticket_ref, message_id)
        except Exception:
            logger.warning(
                "Notify: failed to record telegram_message_id for %r",
                delivery.record_message_id_for_ticket_ref,
                exc_info=True,
            )


def _raw_supabase_client() -> Optional[Any]:
    """Raw postgrest client (``.table()``/``.rpc()``) for the correlation-layer
    services (``CorrelationStore``) -- mirrors ``TicketService``'s own
    ``_raw_client()`` accessor, which does the same
    ``get_supabase_client()._get_client()`` unwrap."""
    from orchestrator.services.supabase_client import get_supabase_client

    wrapper = get_supabase_client()
    return wrapper._get_client() if wrapper else None


# Per-grid in-process locks serializing alert-correlation decisions for a
# grid, so N alerts arriving in the same second don't each see "no open
# candidate" and each file their own ticket. Correct only for the current
# single-process deployment (chat_orchestrator/Dockerfile runs uvicorn with
# no --workers); at instance_count > 1 this stops serializing across
# processes -- the follow-up is a `grid_correlation_leases` table with a
# short-TTL lease (see the plan's "Concurrency" section). Module-level dict
# keyed by resolved grid name; safe to grow/read without an extra guard
# since asyncio is single-threaded and nothing awaits between the .get()
# and the .setdefault() below.
_grid_correlation_locks: Dict[str, asyncio.Lock] = {}


def _get_grid_correlation_lock(grid_name: str) -> asyncio.Lock:
    lock = _grid_correlation_locks.get(grid_name)
    if lock is None:
        lock = asyncio.Lock()
        _grid_correlation_locks[grid_name] = lock
    return lock


@asynccontextmanager
async def _acquire_grid_correlation_lock(grid_name: str, timeout_seconds: float):
    """Yields ``True`` if the per-grid lock was acquired within
    ``timeout_seconds``, ``False`` on timeout (never raises) -- callers must
    treat a timeout the same as any other correlation failure: fall through
    to filing a plain new ticket rather than blocking the request."""
    lock = _get_grid_correlation_lock(grid_name)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


async def _create_notify_ticket(
    body: "NotifyRequest",
    target: "GridNotificationTarget",
    backend_override: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
) -> "tuple[Optional[Any], Optional[JSONResponse]]":
    """File a new notify-originated ticket. Shared by the plain ``ticket_id=""``
    path and every ``"auto"`` fallback (flag off, lock timeout, correlation
    failure, decided "new") -- all of them file exactly the same way.

    Returns ``(TicketResult, None)`` on success or ``(None, response)`` on
    failure (500 -- ticket creation failing is the one /notify-ticketing
    failure mode that must reach the caller, same as before this task).
    """
    from orchestrator.services.supabase_client import get_supabase_client
    from orchestrator.services.ticketing.backend import TicketBackendError, TicketCreateRequest
    from orchestrator.services.ticketing.service import TicketService

    ticket_service = TicketService(get_supabase_client=get_supabase_client)

    if summary is None:
        summary = _notify_ticket_subject(body)
    if description is None:
        description = body.text

    try:
        result = await ticket_service.create_ticket(
            TicketCreateRequest(
                summary=summary,
                description=description,
                grid_name=target.grid_name,
                source="notify",
            ),
            backend_override=backend_override,
        )
    except TicketBackendError as e:
        logger.error("Notify: ticket creation failed source=%s: %s", body.source, e)
        return None, JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"Ticket creation failed: {e}"},
        )
    return result, None


def _notify_ticket_subject(body: "NotifyRequest") -> str:
    """Return the supplied alert subject or a safe legacy fallback for a ticket."""
    if body.alert and body.alert.subject.strip():
        return body.alert.subject.strip()[:120]
    return next((line.strip() for line in body.text.splitlines() if line.strip()), "Notification")[:120]


async def _record_new_correlation(
    store: Any,
    target: "GridNotificationTarget",
    alert: "AlertFacts",
    result: Any,
    root_cause_kind: Optional[str],
    summary: str,
    description: str,
) -> None:
    """Seed the ``ticket_correlations`` row for a freshly-filed notify ticket
    (plain "new", or a newly-created root-cause parent). Best-effort -- a
    failure here means the ticket exists but alert correlation won't find it
    as a candidate later (degrades to filing another new ticket next time,
    not a lost alert)."""
    try:
        signatures = [alert.signature] if alert.signature else []
        affected_keys: List[Dict[str, Any]] = []
        if alert.component_kind and alert.component_key:
            affected_keys = [
                {
                    "kind": alert.component_kind,
                    "key": alert.component_key,
                    "label": alert.component_label,
                    "first_seen": alert.fired_at,
                    "last_seen": alert.fired_at,
                    "count": 1,
                }
            ]
        await store.upsert_correlation(
            ticket_ref=result.ref,
            ticket_backend=result.backend,
            grid_name=target.grid_name,
            organization_id=None,
            root_cause_kind=root_cause_kind,
            primary_signature=alert.signature or "",
            signatures=signatures,
            affected_keys=affected_keys,
            summary_base=summary,
            description_base=description,
            severity=alert.severity,
            telegram_chat_id=target.chat_id,
            telegram_topic_id=target.topic_id,
        )
    except Exception:
        logger.warning(
            "Notify: failed to seed correlation row for %r", result.ref, exc_info=True
        )


@dataclass(frozen=True)
class NotificationTicket:
    """Backend-neutral ticket data needed to render a notification."""

    ref: str
    backend: str
    url: Optional[str] = None


def _notification_ticket_from_result(result: Any) -> NotificationTicket:
    return NotificationTicket(ref=result.ref, backend=result.backend, url=result.url)


def _ticket_notification_url(ticket: NotificationTicket) -> Optional[str]:
    """Return the public browse URL for a ticket, if its backend has one."""
    if ticket.url:
        return ticket.url
    if ticket.backend == "internal":
        app_url = os.getenv("APP_URL", "").rstrip("/")
        if app_url:
            return f"{app_url}/tickets/{quote(ticket.ref, safe='-')}"
        return None
    if ticket.backend == "jira":
        jira_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
        if jira_url:
            return f"{jira_url}/browse/{quote(ticket.ref, safe='-')}"
    return None


def _ticket_notification_link(ticket: NotificationTicket) -> str:
    """Return a Telegram-Markdown-safe ticket reference link or bold fallback."""
    from shared.utils.telegram_markdown import escape_markdown

    ticket_url = _ticket_notification_url(ticket)
    escaped_ref = escape_markdown(ticket.ref)
    if not ticket_url:
        return f"*{escaped_ref}*"
    safe_url = quote(ticket_url, safe=":/?&=%#")
    return f"[{escaped_ref}]({safe_url})"


def _format_ticket_notification(body: "NotifyRequest", ticket: NotificationTicket) -> str:
    """Render a newly-filed or updated ticket alert as Telegram Markdown."""
    from shared.utils.telegram_markdown import escape_markdown

    subject = body.alert.subject.strip() if body.alert and body.alert.subject.strip() else ""
    if not subject:
        subject = _notify_ticket_subject(body)

    severity = body.alert.severity.strip().lower() if body.alert and body.alert.severity else ""
    if not severity:
        severity = derive_severity(subject)

    ticket_link = _ticket_notification_link(ticket)
    urgent_prefix = "🔴 " if severity == "urgent" else ""
    return "\n".join(
        (
            f"{urgent_prefix}*{escape_markdown(subject)}*",
            f"📍 Grid: {escape_markdown(body.grid_name)}",
            f"🎫 Ticket: {ticket_link}",
        )
    )


def _format_ticket_update_notification(
    update: str, ticket: NotificationTicket, *, urgent: bool = False
) -> str:
    """Render a concise factual correlation update with its ticket reference linked."""
    from shared.utils.telegram_markdown import escape_markdown

    ticket_link = _ticket_notification_link(ticket)
    prefix = "🔴" if urgent else "↻"
    return f"{prefix} {ticket_link} — {escape_markdown(update)}"


@dataclass(frozen=True)
class NotificationDelivery:
    """How ``_deliver_notification`` should actually post (or suppress) this
    alert to Telegram -- computed once ticket correlation has decided
    new/amend/duplicate. ``None`` (the default used by every non-"auto"
    ``ticket_id`` path) means "send ``body.text`` in full, no reply,
    exactly as before this existed".
    """

    suppress: bool = False
    text_override: Optional[str] = None
    reply_to_message_id: Optional[int] = None
    top_level: bool = False  # escalation: force a fresh (non-reply) post
    record_message_id_for_ticket_ref: Optional[str] = None
    ticket: Optional[NotificationTicket] = None


def _new_ticket_delivery(ticket: NotificationTicket) -> NotificationDelivery:
    """A freshly-filed ticket (plain "new", flag-off, or any fallback path)
    posts the alert in full, unthreaded, and remembers the resulting
    message_id against the ticket so a later amend can reply to it."""
    return NotificationDelivery(record_message_id_for_ticket_ref=ticket.ref, ticket=ticket)


def _amend_delivery(
    decision: Any, amendment: Any, ticket: NotificationTicket
) -> NotificationDelivery:
    label = (decision.affected_key or {}).get("label") or "another component"
    count = amendment.affected_keys_count if amendment is not None else 1
    message = f"{label} also affected ({count} component{'s' if count != 1 else ''})"
    if amendment is not None and amendment.escalated:
        return NotificationDelivery(text_override=message, top_level=True, ticket=ticket)
    reply_to = amendment.telegram_message_id if amendment is not None else None
    return NotificationDelivery(text_override=message, reply_to_message_id=reply_to, ticket=ticket)


def _duplicate_delivery(amendment: Any, ticket: NotificationTicket) -> NotificationDelivery:
    """Silent by default (that's the whole point of "duplicate") -- except
    every ``ALERT_CORRELATION_ROLLUP_EVERY``-th occurrence, which gets one
    reply so a long-running issue doesn't vanish from the topic entirely."""
    from shared.config import flag_registry as fr

    if amendment is None:
        return NotificationDelivery(suppress=True)
    rollup_every = int(fr.get("ALERT_CORRELATION_ROLLUP_EVERY"))
    if rollup_every > 0 and amendment.occurrence_count % rollup_every == 0:
        message = f"still firing — {amendment.occurrence_count} occurrences"
        return NotificationDelivery(
            text_override=message,
            reply_to_message_id=amendment.telegram_message_id,
            ticket=ticket,
        )
    return NotificationDelivery(suppress=True)


async def _resolve_notify_ticket_auto(
    body: "NotifyRequest", target: "GridNotificationTarget", backend_override: str
) -> "tuple[Optional[str], Optional[JSONResponse], Optional[Dict[str, Any]], Optional[NotificationDelivery]]":
    """``ticket_id == "auto"``: smart alert correlation (see
    docs/superpowers/plans/2026-07-27-smart-alert-correlation-notify.md).

    Fails open at every step -- ``ALERT_CORRELATION_ENABLED`` off, a grid-lock
    timeout, or the correlator/executor raising all fall back to filing a
    plain new ticket via ``_create_notify_ticket`` (the exact same path as
    ``ticket_id=""``). An alert is never dropped; correlation only ever adds
    grouping on top, never a new way to fail.
    """
    from shared.config import flag_registry as fr

    if not fr.get("ALERT_CORRELATION_ENABLED"):
        result, error = await _create_notify_ticket(body, target, backend_override)
        if error is not None:
            return None, error, None, None
        return (
            result.ref,
            None,
            {"decision": "new", "correlated_with": None, "confidence": None, "decided_by": "flag_off"},
            _new_ticket_delivery(_notification_ticket_from_result(result)),
        )

    from orchestrator.services.supabase_client import get_supabase_client
    from orchestrator.services.ticketing.alert_facts import enrich_alert_facts
    from orchestrator.services.ticketing.correlation_render import apply_amendment
    from orchestrator.services.ticketing.correlation_store import CorrelationStore
    from orchestrator.services.ticketing.correlator import AlertCorrelator
    from orchestrator.services.ticketing.service import TicketService

    if body.alert is not None:
        base_alert = body.alert
    else:
        first_line = next(
            (line.strip() for line in body.text.splitlines() if line.strip()), ""
        )
        base_alert = AlertFacts(subject=first_line, details=body.text)
    alert = enrich_alert_facts(base_alert, grid_name=target.grid_name)

    timeout_seconds = float(fr.get("ALERT_CORRELATION_TIMEOUT_SECONDS"))

    async with _acquire_grid_correlation_lock(target.grid_name, timeout_seconds) as acquired:
        if not acquired:
            logger.warning(
                "Notify: grid-correlation lock timeout for %r -- filing plain ticket",
                target.grid_name,
            )
            result, error = await _create_notify_ticket(body, target, backend_override)
            if error is not None:
                return None, error, None, None
            return (
                result.ref,
                None,
                {"decision": "new", "correlated_with": None, "confidence": None, "decided_by": "fallback"},
                _new_ticket_delivery(_notification_ticket_from_result(result)),
            )

        store = CorrelationStore(get_client=_raw_supabase_client)
        ticket_service = TicketService(get_supabase_client=get_supabase_client)
        correlator = AlertCorrelator(store=store, ticket_service=ticket_service)

        try:
            decision = await correlator.decide(
                target.grid_name, alert, dedup_key=body.dedup_key, backend_override=backend_override
            )
        except Exception:
            logger.exception(
                "Notify: correlator.decide() raised for grid %r -- filing plain ticket",
                target.grid_name,
            )
            decision = None

        if decision is None:
            result, error = await _create_notify_ticket(body, target, backend_override)
            if error is not None:
                return None, error, None, None
            return (
                result.ref,
                None,
                {"decision": "new", "correlated_with": None, "confidence": None, "decided_by": "fallback"},
                _new_ticket_delivery(_notification_ticket_from_result(result)),
            )

        try:
            if decision.decision == "new" or (
                decision.decision == "amend" and decision.needs_root_cause_ticket
            ):
                if decision.decision == "amend" and decision.needs_root_cause_ticket:
                    # Root-cause-first: file the parent ticket now (this
                    # alert becomes its first attached child, below), rather
                    # than amending onto whatever ticket the model picked.
                    # Delivery-wise this is a brand-new ticket too -- there is
                    # no prior message to reply to, so post it in full.
                    root_summary = (
                        f"! Urgent: {target.grid_name} root cause ({decision.root_cause_kind}) — "
                        f"dependent equipment alerts are being grouped here !"
                    )
                    root_description = (
                        decision.reason
                        or "Filed automatically to group alerts sharing this root cause."
                    )
                    result, error = await _create_notify_ticket(
                        body, target, backend_override, summary=root_summary, description=root_description
                    )
                    if error is not None:
                        return None, error, None, None
                    await _record_new_correlation(
                        store, target, alert, result, decision.root_cause_kind, root_summary, root_description
                    )
                    import dataclasses

                    await apply_amendment(
                        store=store,
                        ticket_service=ticket_service,
                        ticket_ref=result.ref,
                        alert=alert,
                        decision=dataclasses.replace(decision, ticket_ref=result.ref),
                        raw_text=body.text,
                    )
                    return (
                        result.ref,
                        None,
                        {
                            "decision": "amend",
                            "correlated_with": None,
                            "confidence": decision.confidence,
                            "decided_by": decision.decided_by,
                        },
                        _new_ticket_delivery(_notification_ticket_from_result(result)),
                    )

                summary = _notify_ticket_subject(body)
                result, error = await _create_notify_ticket(
                    body, target, backend_override, summary=summary, description=body.text
                )
                if error is not None:
                    return None, error, None, None
                await _record_new_correlation(
                    store, target, alert, result, decision.root_cause_kind, summary, body.text
                )
                return (
                    result.ref,
                    None,
                    {
                        "decision": "new",
                        "correlated_with": None,
                        "confidence": decision.confidence,
                        "decided_by": decision.decided_by,
                    },
                    _new_ticket_delivery(_notification_ticket_from_result(result)),
                )

            # amend (onto an existing ticket) or duplicate.
            amendment = await apply_amendment(
                store=store,
                ticket_service=ticket_service,
                ticket_ref=decision.ticket_ref,
                alert=alert,
                decision=decision,
                raw_text=body.text,
            )
            if amendment is None:
                # Correlation row vanished between decide() and here (store
                # outage) -- the target ticket still exists, so at minimum
                # comment on it rather than silently dropping the alert.
                # No reply-target context survives this, so deliver nothing
                # rather than risk a misdirected/noisy post.
                await ticket_service.add_comment(decision.ticket_ref, body.text, public=False)
                ref = decision.ticket_ref
                delivery = NotificationDelivery(suppress=True)
            else:
                ref = amendment.ticket_ref
                ticket = NotificationTicket(
                    ref=ref, backend=await ticket_service.get_backend_name(ref)
                )
                delivery = (
                    _amend_delivery(decision, amendment, ticket)
                    if decision.decision == "amend"
                    else _duplicate_delivery(amendment, ticket)
                )
            return (
                ref,
                None,
                {
                    "decision": decision.decision,
                    "correlated_with": decision.ticket_ref,
                    "confidence": decision.confidence,
                    "decided_by": decision.decided_by,
                },
                delivery,
            )
        except Exception:
            logger.exception(
                "Notify: correlation execution raised for grid %r -- filing plain ticket",
                target.grid_name,
            )
            result, error = await _create_notify_ticket(body, target, backend_override)
            if error is not None:
                return None, error, None, None
            return (
                result.ref,
                None,
                {"decision": "new", "correlated_with": None, "confidence": None, "decided_by": "fallback"},
                _new_ticket_delivery(_notification_ticket_from_result(result)),
            )


async def _resolve_notify_ticket_full(
    body: "NotifyRequest", target: "GridNotificationTarget"
) -> "tuple[Optional[str], Optional[JSONResponse], Optional[Dict[str, Any]], Optional[NotificationDelivery]]":
    """Resolve ``body.ticket_id`` into a ticket ref per the /notify ticketing contract.

    Returns ``(ticket_ref, None, extra, delivery)`` on success -- ``ticket_ref``
    is ``None`` when ``body.ticket_id`` was omitted (pure passthrough,
    unchanged behavior) -- or ``(None, response, None, None)`` when the
    request must fail fast with ``response`` before any delivery is
    scheduled. ``extra`` (decision/correlated_with/confidence/decided_by) and
    ``delivery`` (how ``_deliver_notification`` should post/suppress/reply)
    are only ever populated for ``ticket_id="auto"`` -- every other path
    returns ``None`` for both, so neither the response body nor the Telegram
    send behavior changes for existing callers.

    Runs synchronously in the handler (not the background delivery task) so
    ticket failures reach the caller in the HTTP response, same rationale as
    the existing synchronous grid resolution.

    Notify-originated tickets use NOTIFY_TICKETS_BACKEND (default 'internal'),
    independent of TICKET_BACKEND_OVERRIDE (which only governs customer
    escalations) -- so Grafana/n8n/VRM alerts never land in the Jira OPS
    project unless an operator explicitly opts them into 'auto'.
    """
    if body.ticket_id is None:
        return None, None, None, None

    from shared.config import flag_registry as fr

    backend_override = fr.get("NOTIFY_TICKETS_BACKEND") or "internal"
    normalized = body.ticket_id.strip().lower()

    if body.ticket_id == "":
        result, error = await _create_notify_ticket(body, target, backend_override)
        if error is not None:
            return None, error, None, None
        ticket = _notification_ticket_from_result(result)
        return ticket.ref, None, None, _new_ticket_delivery(ticket)

    if normalized == "auto":
        return await _resolve_notify_ticket_auto(body, target, backend_override)

    # Populated ticket_id: comment on (and optionally close) an existing ticket.
    from orchestrator.services.supabase_client import get_supabase_client
    from orchestrator.services.ticketing.service import TicketService

    ticket_service = TicketService(get_supabase_client=get_supabase_client)
    ticket_ref = body.ticket_id
    status = await ticket_service.get_status(ticket_ref)
    if status is None:
        logger.warning("Notify: unresolvable ticket_id=%r (source=%s)", ticket_ref, body.source)
        return None, JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Unknown or unresolvable ticket_id: {ticket_ref!r}"},
        ), None, None
    commented = await ticket_service.add_comment(ticket_ref, body.text, public=False)
    if not commented:
        logger.warning(
            "Notify: add_comment reported failure for ticket_ref=%r (source=%s)",
            ticket_ref,
            body.source,
        )
    if body.close:
        await ticket_service.transition_to_done(ticket_ref)
    ticket = NotificationTicket(
        ref=ticket_ref,
        backend=await ticket_service.get_backend_name(ticket_ref),
    )
    return ticket_ref, None, None, NotificationDelivery(ticket=ticket)


async def _resolve_notify_ticket(
    body: "NotifyRequest", target: "GridNotificationTarget"
) -> "tuple[Optional[str], Optional[JSONResponse]]":
    """Backward-compatible 2-tuple wrapper over ``_resolve_notify_ticket_full``
    for callers that only care about ``(ticket_ref, error_response)`` -- the
    contract this function had before ``ticket_id="auto"`` existed."""
    ticket_ref, error_response, _extra, _delivery = await _resolve_notify_ticket_full(body, target)
    return ticket_ref, error_response


@app.post("/chat/notify")
async def handle_notify(
    request: Request, body: NotifyRequest, background_tasks: BackgroundTasks
) -> JSONResponse:
    """Forward an externally-composed notification to a Telegram group.

    Authentication: a shared secret in the ``X-Notify-Secret`` header must match
    ``NOTIFY_SHARED_SECRET``. Fail-closed — if the secret is not configured the
    endpoint rejects all requests.

    Gating: ``NOTIFY_ENDPOINT_ENABLED`` must be true (toggle on the admin settings
    page). When off the endpoint returns 503 so the caller can decide what to do;
    Anansi neither sends nor queues.

    Resolution is synchronous so failures reach the caller: an unresolvable
    ``grid_name`` returns 404 (caller should not retry) and a resolution infra
    error returns 503 (caller may retry). Only the Telegram send + logging run in
    the background, after which the endpoint has already returned 202.

    Verification bypass: these are internal operational alerts, not customer chat,
    so they intentionally skip ``ResponseVerificationService`` (see CLAUDE.md
    "Outgoing Message Verification"). The enable toggle is the operator kill switch.
    """
    import hmac

    from shared.auth import get_auth_service

    secret = os.getenv("NOTIFY_SHARED_SECRET", "")
    if not secret:
        logger.error("NOTIFY_SHARED_SECRET not configured — rejecting notify request")
        raise HTTPException(status_code=401, detail="Notify endpoint not configured")

    provided = request.headers.get("X-Notify-Secret", "")
    if not hmac.compare_digest(provided, secret):
        logger.warning("Notify: secret mismatch from source=%s", body.source)
        raise HTTPException(status_code=401, detail="Unauthorized")

    if os.getenv("NOTIFY_ENDPOINT_ENABLED", "false").lower() not in ("true", "1", "yes"):
        return JSONResponse(
            status_code=503, content={"ok": False, "error": "Notify endpoint disabled"}
        )

    # Resolve synchronously so an unknown grid is reported to the caller rather than
    # silently dropped in the background. Distinguish "no such grid" (404, terminal)
    # from an infrastructure failure (503, retryable).
    try:
        target = await get_auth_service().resolve_grid_notification_target(body.grid_name)
    except Exception:
        logger.exception(
            "Notify: grid resolution failed for %r (source=%s)", body.grid_name, body.source
        )
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "Grid resolution temporarily unavailable"},
        )
    if target is None:
        logger.warning("Notify: unresolvable grid_name=%r (source=%s)", body.grid_name, body.source)
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": f"Unknown or unresolvable grid_name: {body.grid_name!r}",
            },
        )

    # Ticket resolution (if requested) is synchronous, same rationale as grid
    # resolution above: a 404/500 must reach the caller, not be dropped in the
    # background. body.ticket_id is None -> ticket_ref stays None -> the
    # response below is byte-identical to today's passthrough-only behavior.
    ticket_ref, error_response, extra, delivery = await _resolve_notify_ticket_full(body, target)
    if error_response is not None:
        return error_response

    # Return fast; the send + logging happen in the background (mirrors the
    # Telegram-webhook pattern — responses go out via the Bot API, not this body).
    background_tasks.add_task(_deliver_notification, body, target, ticket_ref, delivery)
    response_content: Dict[str, Any] = {"ok": True}
    if ticket_ref:
        response_content["ticket_ref"] = ticket_ref
    if extra:
        response_content.update(extra)
    return JSONResponse(status_code=202, content=response_content)


@app.post("/api/v1/metrics/test")
async def test_metrics(request: Request, date: Optional[str] = None):
    """
    Test endpoint to manually trigger metrics collection and posting.

    Args:
        date: Optional date in YYYY-MM-DD format. If not provided, uses yesterday.

    Example:
        POST /api/v1/metrics/test
        POST /api/v1/metrics/test?date=2025-12-03

    Authentication:
        - X-Api-Key header required
    """
    # Verify authentication
    get_auth_method(request)

    # Import metrics service
    from orchestrator.services.metrics_service import MetricsService

    metrics_service = MetricsService()

    if not metrics_service.is_enabled():
        return JSONResponse(
            status_code=503,
            content={"success": False, "error": "Metrics service not enabled or configured"},
        )

    try:
        # Parse date if provided, otherwise use yesterday
        if date:
            try:
                target_date = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "Invalid date format. Use YYYY-MM-DD"},
                )
        else:
            # Use yesterday
            from datetime import timedelta

            target_date = datetime.now(timezone.utc) - timedelta(days=1)

        # Send metrics
        result = await metrics_service.send_metrics_for_date(target_date)

        if result.get("success"):
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": f"Metrics sent for {target_date.strftime('%Y-%m-%d')}",
                    "date": target_date.strftime("%Y-%m-%d"),
                },
            )
        else:
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": result.get("error", "Failed to send metrics"),
                },
            )

    except Exception as e:
        logger.exception(f"Error in metrics test endpoint: {e}")
        return JSONResponse(
            status_code=500, content={"success": False, "error": f"Internal error: {str(e)}"}
        )


# ============================================================================
# Broadcast Verification Endpoint
# ============================================================================


class BroadcastVerifyRequest(BaseModel):
    """Request model for broadcast verification."""

    message: str = Field(..., description="The broadcast message to verify")
    target_groups: List[str] = Field(
        default_factory=list, description="Optional list of target group names for context"
    )


class BroadcastVerifyResponse(BaseModel):
    """Response model for broadcast verification."""

    passed: bool
    feedback: str = ""
    categories: List[str] = Field(default_factory=list)
    error: Optional[str] = None


# Cache for verification criteria (fetched from Google Doc)
_verification_criteria_cache: Optional[str] = None


def _get_verification_criteria() -> str:
    """
    Get verification criteria from the same Google Doc used for response verification.

    Uses VERIFICATION_DOC_ID - the same document used for verifying customer responses.
    This ensures consistent quality standards across all verification use cases.

    Returns cached criteria if available.
    """
    global _verification_criteria_cache

    if _verification_criteria_cache is not None:
        return _verification_criteria_cache

    # Use the SAME verification doc as response verification
    doc_id = os.getenv("VERIFICATION_DOC_ID", "")

    if doc_id:
        try:
            fetcher = GoogleDriveDocFetcher()
            doc_content = fetcher.fetch_document(doc_id)
            if doc_content:
                _verification_criteria_cache = doc_content
                logger.info(f"Loaded verification criteria from doc {doc_id}")
                return _verification_criteria_cache
        except Exception as e:
            logger.warning(f"Failed to fetch verification doc {doc_id}: {e}")

    # Default criteria if no doc configured or fetch failed
    _verification_criteria_cache = """
You are a message quality checker for a utility/energy company.

Evaluate messages for quality before they are sent to customers.

PASS the message if it:
- Is professional and appropriate for business communication
- Does not contain sensitive information (passwords, API keys, internal URLs)
- Is clear and understandable
- Has correct grammar and spelling

FAIL the message if it:
- Contains inappropriate content, profanity, or unprofessional language
- Includes internal information not meant for customers
- Is confusing, ambiguous, or poorly written
- Could cause unnecessary alarm or panic
"""
    return _verification_criteria_cache


@app.post("/api/v1/verify/broadcast", response_model=BroadcastVerifyResponse)
async def verify_broadcast(request: Request, body: BroadcastVerifyRequest):
    """
    Verify a broadcast message before sending.

    Uses the same ResponseVerificationService as response verification,
    with the same Google Doc (VERIFICATION_DOC_ID), same model, and same LLM path.

    Args:
        body: The verification request with message and optional target groups

    Returns:
        BroadcastVerifyResponse with pass/fail status and feedback

    Authentication:
        - X-Api-Key header required
    """
    from orchestrator.services.verification_service import ResponseVerificationService

    # Verify authentication
    get_auth_method(request)

    # Check if verification is enabled (uses global VERIFICATION_ENABLED toggle)
    verification_enabled = os.getenv("VERIFICATION_ENABLED", "false").lower() == "true"
    if not verification_enabled:
        return BroadcastVerifyResponse(
            passed=True,
            feedback="Verification disabled",
        )

    # Get verification criteria (same doc as response verification)
    criteria = _get_verification_criteria()
    if not criteria:
        return BroadcastVerifyResponse(
            passed=True,
            feedback="Verification skipped: no criteria configured",
        )

    # Build context with target groups if provided
    context = None
    if body.target_groups:
        group_list = ", ".join(body.target_groups[:5])
        if len(body.target_groups) > 5:
            group_list += f" ...and {len(body.target_groups) - 5} more"
        context = f"This is a broadcast message being sent to: {group_list}"

    # Use the SAME verification service as response verification, but in
    # "broadcast" mode so the judge does not expect the text to answer a specific
    # customer question. The message arrives already enriched (placeholders
    # substituted for a sample recipient) so the judge sees what customers receive.
    async with ResponseVerificationService() as service:
        result = await service.verify_response(
            original_message=(
                "[One-way broadcast announcement — not a reply to a customer question]"
            ),
            response_text=body.message,
            verification_instructions=criteria,
            conversation_context=context,
            mode="broadcast",
        )

    logger.info(
        f"Broadcast verification result: passed={result.passed}, categories={result.categories}"
    )

    return BroadcastVerifyResponse(
        passed=result.passed,
        feedback=result.feedback,
        categories=result.categories,
        error=None,
    )
