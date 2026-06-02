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
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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

        docs_to_cache = [
            ("STAFF_SUPPORT_DOC_ID", "staff instructions"),
            ("CUSTOMER_SUPPORT_DOC_ID", "customer instructions"),
            ("EXPERT_INSTRUCTIONS_DOC_ID", "expert definitions"),
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

    # Use the SAME verification service as response verification
    async with ResponseVerificationService() as service:
        result = await service.verify_response(
            original_message="[Broadcast message - admin composing announcement]",
            response_text=body.message,
            verification_instructions=criteria,
            conversation_context=context,
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
