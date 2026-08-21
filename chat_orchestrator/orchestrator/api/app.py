"""
FastAPI application wrapper for the serverless handler.

This module provides a FastAPI app that wraps the existing serverless
handler.main() function for deployment on App Platform.
"""

# Import the serverless handler
import asyncio
import dataclasses
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from orchestrator.services.urgent_alert_context import (
    UrgentAlertContext,
    build_urgent_alert_context,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None

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


def get_identity_assertion_key() -> str:
    """A secret distinct from API_KEY, held only by callers trusted to assert
    an arbitrary user_email when auth-database lookup misses.

    API_KEY alone must not grant this: it is shared by every "api"
    auth_method caller (n8n, the scheduler, direct API integrations, the
    skill builder), any of whom could otherwise impersonate any account by
    setting user_email directly in the request body -- get_user_email's DB
    lookup failing is exactly what reaches that fallback in handler.py's
    _handle_webhook / _handle_webhook_async. See Phase 4 of
    docs/superpowers/plans/2026-08-06-user-designed-skills.md, "Identity over
    the API channel."
    """
    return os.getenv("IDENTITY_ASSERTION_KEY", "")


def is_identity_trusted_caller(request: Request) -> bool:
    """True only when the caller holds IDENTITY_ASSERTION_KEY.

    Fails closed: unconfigured (no key set) or mismatched both return False.
    A deployment that never sets IDENTITY_ASSERTION_KEY never honors a
    caller-supplied user_email from anyone -- there is no default-trust
    fallback to accidentally leave open.
    """
    import hmac

    expected = get_identity_assertion_key()
    if not expected:
        return False
    provided = request.headers.get("X-Identity-Assertion-Key", "")
    return bool(provided) and hmac.compare_digest(provided, expected)


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
        logger.warning("Mini App dist not found at {} — static files not served", mini_app_dist)


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

        # 2. Pre-fetch every doc-backed prompt through the shared library, so
        # the first real request doesn't pay the Google Doc fetch cost.
        # Previously this went through ArtifactsProvider._fetch_google_doc_sections
        # directly, keyed by env var; that parser assumes a flat "system
        # instructions" section and always returned 0 sections for the
        # experts doc (per-expert "# Expert:" headers), so it was excluded
        # with a ~6s wasted-attempt cost every startup. PROMPTS.text() doesn't
        # parse sections at all -- it's just cached raw text -- so there's no
        # reason to exclude experts.definitions anymore.
        from shared.prompts import PROMPTS

        loop = asyncio.get_running_loop()
        for prompt_id in (
            "staff.system",
            "customer.system",
            "verification.criteria",
            "experts.definitions",
        ):
            try:
                await loop.run_in_executor(None, PROMPTS.text, prompt_id)
                logger.info(f"Warmup: Cached {prompt_id}")
            except Exception as e:
                logger.warning(f"Warmup: Failed to cache {prompt_id}: {e}")

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

    # Check if the metrics scheduled service is enabled.
    #
    # Grafana's nightly indexing used to be scheduled here too (a
    # run_grafana_indexer job gated on GRAFANA_ACTIONS_ENABLED), but it could
    # never actually succeed: it imported grafana_indexer_incremental from a
    # `rag_pipeline/ingestion` path that has only ever contained README.md
    # and __init__.py -- the real module has always lived in
    # anansi_app/scripts/, which this service's own Dockerfile never copies
    # into the image at all. Every nightly attempt raised ImportError,
    # silently caught by the job's own `except Exception`, so it failed once
    # a night, indefinitely, with nothing surfacing the failure. That job now
    # lives in anansi_app itself (scripts/grafana_scheduler.py, started
    # alongside broadcast_scheduler.py in start.sh) -- the process that
    # actually has the indexer script, its dependencies, and Supabase write
    # access, and that the "Sync Now" button already runs it from
    # successfully.
    metrics_enabled = os.getenv("METRICS_ENABLED", "true").lower() == "true"

    if not metrics_enabled:
        logger.info("All scheduled services disabled (METRICS_ENABLED is false)")

    if metrics_enabled:
        # Initialize scheduler
        scheduler = AsyncIOScheduler()

        # Get schedule configuration
        schedule_timezone = os.getenv("METRICS_TIMEZONE", "UTC")

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

        scheduler.start()
        logger.info("Scheduler started successfully")

    # -------------------------------------------------------------------------
    # Escalation Jira sweep — runs daily at 9am WAT (08:00 UTC, WAT is UTC+1,
    # no DST).  Registered unconditionally so it fires even when METRICS_ENABLED
    # is false.
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
                    "Escalation sweep complete in %.1fs: {}",
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
    """Clean up scheduler on application shutdown."""
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
    from orchestrator.services.ticketing.correlation_store import failures_last_hour

    return {
        "status": "healthy",
        "service": "chat-orchestrator",
        "correlation_store_failures_last_hour": failures_last_hour(),
    }


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


@app.post("/internal/tickets/{ticket_ref}/close")
async def close_ticket_internal(ticket_ref: str, request: Request) -> JSONResponse:
    """Close a ticket through TicketService, callable from the tools-service process.

    The Jira MCP server's change_status action used to close internal tickets
    by writing the `tickets` table directly, bypassing TicketRepository
    (documented as the sole writer) and, now that TicketService.transition_to_done
    posts the update card itself, meaning a bot-initiated close was the one
    transition that never reached Telegram. Routing it here restores both.

    Authentication:
        - X-Api-Key header required (same as /api/v1/jobs above)
    """
    get_auth_method(request)

    from orchestrator.services.supabase_client import get_supabase_client
    from orchestrator.services.ticketing.service import TicketService

    try:
        closed = await TicketService(get_supabase_client=get_supabase_client).transition_to_done(
            ticket_ref
        )
    except Exception as exc:
        logger.exception("Internal close failed for {!r}", ticket_ref)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
    if not closed:
        # transition_to_done() returning False (not raising) means the close
        # itself genuinely didn't happen -- e.g. Jira refused the transition.
        # Distinct from the 500 above: the request was handled fine, the
        # ticket just isn't closed.
        return JSONResponse(
            status_code=200, content={"ok": False, "ticket_ref": ticket_ref, "closed": False}
        )
    return JSONResponse(status_code=200, content={"ok": True, "ticket_ref": ticket_ref})


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
        logger.debug("Ignoring Jira webhook event={} issue_event={}", event, issue_event)


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
        logger.error("Failed to parse Jira webhook body: {}", e)
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
    identity_trusted = is_identity_trusted_caller(request)

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
    # Whether this caller may assert an arbitrary user_email when auth-DB
    # lookup misses -- see is_identity_trusted_caller's docstring.
    body["_identity_trusted"] = identity_trusted

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
# Skill builder support (Phase 4 of
# docs/superpowers/plans/2026-08-06-user-designed-skills.md)
#
# The builder (anansi_app/nicegui_app/pages/skill_builder.py) sends chat
# turns via POST /chat like any other "api" caller; these two endpoints
# wrap the two Phase 2/3 functions that had no HTTP caller until now --
# validate_skill_steps (pure, no LLM) and generate_skill_summary (one LLM
# call). Builder-only tooling: no Telegram-format path, no BOT_ENABLED
# handling, no side effects on chat_messages/skills.
# ============================================================================


class SkillStepPayload(BaseModel):
    """One authored step, mirroring the stored skills.steps shape -- see
    skill_validation.py's module docstring for the canonical shape.

    name and instruction are optional (not just "may be empty") because a
    P3 `kind="function"` step carries neither: its display name is `handler`
    and it has no instruction text to render. Both stay required-by-
    convention (never actually None) for the pre-P3 default kind="llm"
    shape -- skill_validation.py's own field defaults (`step.get("name") or
    ...`) already tolerate their absence either way.

    result_preview is optional and builder-only: what the step's tools
    actually returned (skill_builder.py's _step_response_text, truncated),
    used solely to enrich /skills/summarize's prompt (see skill_summary.py's
    _build_summary_prompt) -- /skills/validate ignores it entirely.
    """

    index: int
    name: Optional[str] = None
    instruction: Optional[str] = None
    output_var: Optional[str] = None
    allow_write: bool = False
    is_response_step: bool = False
    kind: Optional[str] = None
    handler: Optional[str] = None
    result_preview: Optional[str] = None


class SkillValidationErrorPayload(BaseModel):
    step_index: int
    step_name: str
    message: str
    severity: str


class SkillValidateRequest(BaseModel):
    steps: List[SkillStepPayload]
    declared_inputs: Optional[List[str]] = Field(
        default=None,
        description="The skill's own input names (Phase 3 concept). Omit until that exists.",
    )


class SkillValidateResponse(BaseModel):
    errors: List[SkillValidationErrorPayload]


@app.post("/skills/validate", response_model=SkillValidateResponse)
async def validate_skill(request: Request, body: SkillValidateRequest) -> SkillValidateResponse:
    """Inline + save-time validation for the skill builder.

    Pure computation, no LLM call, no DB read/write -- see
    skill_validation.py's validate_skill_steps. The builder calls this after
    every new step and again immediately before Save.
    """
    get_auth_method(request)  # 401s on missing/invalid key

    # orchestrator.experts.__init__ already imports orchestrator.experts.handlers
    # (see that package's own __init__), which is what runs every
    # @register_step(...) decorator -- importing skill_validation below,
    # itself inside orchestrator.experts, already guarantees that happened,
    # so builder_exposed_handlers() reflects every real opt-in with no
    # separate "make sure handlers are loaded" step needed here.
    from orchestrator.experts.skill_validation import validate_skill_steps
    from orchestrator.experts.step_registry import get_step_registry

    registry = get_step_registry()
    # Task 5.4 (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-
    # tools.md): handlers that mutate but have no MockSpec -- a step naming
    # one of these can't be saved with mock enabled.
    unmockable_handlers = {
        handler_name
        for handler_name in registry.list_handlers()
        if (contract := registry.get_contract(handler_name)) is not None
        and contract.mutates
        and contract.mock is None
    }

    errors = validate_skill_steps(
        [step.model_dump() for step in body.steps],
        declared_inputs=body.declared_inputs,
        exposed_handlers=registry.builder_exposed_handlers(),
        unmockable_handlers=unmockable_handlers,
    )
    return SkillValidateResponse(
        errors=[
            SkillValidationErrorPayload(
                step_index=e.step_index,
                step_name=e.step_name,
                message=e.message,
                severity=e.severity,
            )
            for e in errors
        ]
    )


class SkillSummarizeRequest(BaseModel):
    steps: List[SkillStepPayload]
    title: str = ""


class SkillSummarizeResponse(BaseModel):
    summary: str


@app.post("/skills/summarize", response_model=SkillSummarizeResponse)
async def summarize_skill(
    request: Request, body: SkillSummarizeRequest
) -> SkillSummarizeResponse:
    """Auto-generate a skill's catalog summary for the builder's Save panel.

    One LLM call -- see skill_summary.py. The result is a starting point;
    MAX_SUMMARY_CHARS-capped, quote-stripped, but never validated for
    accuracy. The author edits it before Save persists whatever text is in
    the field at that point, generated or not.
    """
    get_auth_method(request)

    from orchestrator.experts.skill_summary import generate_skill_summary

    summary = await generate_skill_summary(
        [step.model_dump() for step in body.steps], title=body.title
    )
    return SkillSummarizeResponse(summary=summary)


class SkillDispatchScheduleRequest(BaseModel):
    schedule_id: str


class SkillDispatchScheduleResponse(BaseModel):
    dispatched: int
    skipped: int
    failed: int
    reason: Optional[str] = None


@app.post("/skills/dispatch-schedule", response_model=SkillDispatchScheduleResponse)
async def dispatch_skill_schedule_endpoint(
    request: Request, body: SkillDispatchScheduleRequest
) -> SkillDispatchScheduleResponse:
    """Fan a due skill schedule out across every eligible entity (Phase 5 of
    docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 1).

    Called by anansi_app/scripts/broadcast_scheduler.py when it finds a due
    user_schedules row with skill_id set, instead of that scheduler's own
    single-chat command dispatch -- see skill_schedule_dispatch.py's module
    docstring for why the entity fan-out and authorization work can only
    happen here (direct Auth DB access), not in the scheduler's own process.

    Synchronous: the scheduler waits for this to finish rather than
    polling, since a skill run's own per-entity delivery already happens
    inside this call (skill_runner.py's _ResponseBuffer sends success
    messages as steps complete; failures are routed before this returns
    too) -- there is nothing left to deliver asynchronously afterward.
    """
    get_auth_method(request)

    from orchestrator.experts.skill_schedule_dispatch import dispatch_skill_schedule

    result = await dispatch_skill_schedule(body.schedule_id)
    return SkillDispatchScheduleResponse(**result)


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
    can be associated with the canonical ticket timeline, mirroring how
    forwarded escalation replies are tagged.

    ``group_id`` must be passed to ``save_messages`` -- without it the bot's
    own alert posts are invisible to ``chat_messages`` reads keyed by
    ``group_id`` (notably ``ChatWatermarkRepository``'s topic-scoped scroll
    count, plan B6), even though this is exactly a group notification.
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
        saved = await client.save_messages(
            session.id, [message], from_chat_id=str(chat_id), group_id=str(chat_id)
        )
        if ticket_ref and saved:
            await client.tag_message_as_ticket_comment(saved[0].id, ticket_ref)
    except Exception as e:
        logger.warning("Notify: chat-db logging failed (non-fatal): {}", e)


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
    from shared.utils.telegram_send import (
        edit_telegram_message,
        send_telegram_message_with_fallback,
    )

    if delivery is not None and delivery.suppress:
        logger.info(
            "Notify: delivery suppressed source={} grid={} ticket_ref={}",
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
    alert_context = (
        delivery.alert_context
        if delivery is not None and delivery.alert_context is not None
        else _build_notify_alert_context(body, target)
    )
    ticket_summary = delivery.ticket_summary if delivery is not None else ""
    stored_ticket_severity = delivery.stored_ticket_severity if delivery is not None else ""
    urgent = _is_effectively_urgent(alert_context, ticket_summary, stored_ticket_severity)
    if delivery is not None and delivery.top_level:
        urgent = True
    live_output_line = await alert_context.telegram_output_line() if urgent else None
    if ticketed_delivery:
        # Ticket references are deliberately rendered as Telegram Markdown
        # links, so a caller-provided plain/HTML mode cannot make the link
        # literal or parse it under the wrong grammar.
        parse_mode = "Markdown"
    raw_text = (delivery.text_override if delivery is not None and delivery.text_override else body.text)
    if ticketed_delivery:
        raw_text = (
            _format_ticket_update_notification(
                raw_text, delivery.ticket, urgent=urgent, live_output_line=live_output_line
            )
            if delivery.text_override
            else _format_ticket_notification(
                body, delivery.ticket, urgent=urgent, live_output_line=live_output_line
            )
        )
    elif live_output_line:
        raw_text = f"{raw_text.rstrip()}\n{live_output_line}"
    if delivery is not None and delivery.site_status:
        raw_text = f"{raw_text.rstrip()}\n{_render_alert_site_status(delivery.site_status)}"
    text = raw_text
    if parse_mode and parse_mode.lower().startswith("markdown"):
        text = convert_github_to_telegram_markdown(raw_text)

    reply_to_message_id = (
        delivery.reply_to_message_id if delivery is not None and not delivery.top_level else None
    )

    if delivery is not None and delivery.edit_message_id:
        edited = await edit_telegram_message(
            bot_token,
            target.chat_id,
            delivery.edit_message_id,
            text,
            parse_mode=parse_mode,
        )
        if edited:
            logger.info(
                "Notify: edited existing message_id={} source={} grid={} chat={}",
                delivery.edit_message_id,
                body.source,
                target.grid_name,
                target.chat_id,
            )
            # The ticket's telegram_message_id is unchanged (still the
            # message we just edited), so there's no new message_id to
            # record -- skip straight past the send/record-receipt paths.
            #
            # Note: this delivery runs as an independent background task per
            # request. Two amendments to the same ticket in quick succession
            # are serialized during the *decision* phase (same grid lock),
            # but their edits here are not -- if the later request's HTTP
            # call happens to land first, an older edit can overwrite a
            # newer one on Telegram (message text only; the ticket backend's
            # state is unaffected). Self-healing: the next amendment's edit
            # corrects it. Same best-effort, single-process posture already
            # accepted elsewhere in this module.
            return
        logger.warning(
            "Notify: edit of message_id={} failed, falling back to a new send "
            "source={} grid={} chat={}",
            delivery.edit_message_id,
            body.source,
            target.grid_name,
            target.chat_id,
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
            "Notify: delivery failed source={} grid={} chat={}",
            body.source,
            target.grid_name,
            target.chat_id,
        )
        return

    logger.info(
        "Notify: forwarded source={} grid={} (fuzzy={}) chat={} message_id={}",
        body.source,
        target.grid_name,
        target.was_fuzzy,
        target.chat_id,
        message_id,
    )
    await _log_notification_to_chat_db(
        body, target.chat_id, target.topic_id, message_id, ticket_ref=ticket_ref
    )
    try:
        from orchestrator.services.ticketing.notify_alert_delivery_repository import (
            NotifyAlertDeliveryRepository,
        )

        ticket = delivery.ticket if delivery is not None else None
        await NotifyAlertDeliveryRepository(get_client=_raw_supabase_client).record_success(
            grid_name=target.grid_name,
            external_chat_id=target.chat_id,
            external_topic_id=target.topic_id,
            external_message_id=message_id,
            source=body.source,
            dedup_key=body.dedup_key,
            ticket_id=ticket.ticket_id if ticket is not None else None,
            ticket_ref=ticket_ref,
            rendered_text=text,
            alert=body.alert.model_dump() if body.alert is not None else {"text": body.text},
        )
    except Exception:
        logger.warning("Notify: successful-delivery ledger write failed", exc_info=True)

    if delivery is not None and delivery.ticket is not None and delivery.ticket.ticket_id:
        try:
            from orchestrator.services.ticketing.delivery_repository import DeliveryRepository

            receipts = DeliveryRepository(get_client=_raw_supabase_client)
            await receipts.record(
                ticket_id=delivery.ticket.ticket_id,
                escalation_id=None,
                purpose="update" if delivery.text_override else "notification",
                external_chat_id=str(target.chat_id),
                external_topic_id=str(target.topic_id) if target.topic_id is not None else None,
                external_message_id=int(message_id),
            )
        except Exception:
            logger.warning("Notify: failed to record delivery receipt", exc_info=True)


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
    alert_context: Optional[UrgentAlertContext] = None,
) -> "tuple[Optional[Any], Optional[str]]":
    """File a new notify-originated ticket. Shared by the plain ``ticket_id=""``
    path and every ``"auto"`` fallback (flag off, lock timeout, correlation
    failure, decided "new") -- all of them file exactly the same way.

    Returns ``(TicketResult, None)`` on success or ``(None, error)`` when
    neither backend can create a ticket.  Callers must still forward the base
    alert to Telegram; a ticketing outage is not allowed to lose an alert.
    """
    from orchestrator.services.supabase_client import get_supabase_client
    from orchestrator.services.ticketing.backend import TicketCreateRequest
    from orchestrator.services.ticketing.service import TicketService

    ticket_service = TicketService(get_supabase_client=get_supabase_client)

    if summary is None:
        summary = _notify_ticket_subject(body)
    if description is None:
        description = body.text

    # Internal ticket creation has no LLM consumer. Inspect the primary so
    # both an explicit `jira` override and `auto`→Jira receive live facts
    # before the Jira type selector runs, while an internal primary avoids a
    # needless telemetry lookup. TicketService resolves again for the actual
    # create so a transient health-check error remains fail-open.
    llm_context: Dict[str, Any] = {}
    if alert_context is not None:
        try:
            primary_backend = await ticket_service.resolve_backend(override=backend_override)
            if primary_backend.name == "jira":
                llm_context = await alert_context.llm_facts()
        except Exception:
            logger.warning(
                "Notify: unable to resolve ticket backend before telemetry enrichment",
                exc_info=True,
            )
    try:
        outcome = await ticket_service.create_ticket_with_internal_fallback(
            TicketCreateRequest(
                summary=summary,
                description=description,
                grid_name=target.grid_name,
                source="notify",
                llm_context=llm_context,
                severity=body.alert.severity if body.alert is not None else "",
            ),
            backend_override=backend_override,
        )
    except Exception as exc:
        logger.exception("Notify: ticket creation crashed source={}", body.source)
        return None, f"Ticket creation failed: {exc}"

    if outcome.result is None:
        error = outcome.error or "Ticket creation failed in both configured backends"
        logger.error("Notify: ticket creation failed source={}: {}", body.source, error)
        return None, f"Ticket creation failed: {error}"
    if outcome.fallback_used:
        logger.warning(
            "Notify: Jira ticket creation failed; created internal fallback {} ({})",
            outcome.result.ref,
            outcome.error,
        )
    return outcome.result, None


def _notify_ticket_subject(body: "NotifyRequest") -> str:
    """Return the supplied alert subject or a safe legacy fallback for a ticket."""
    if body.alert and body.alert.subject.strip():
        return body.alert.subject.strip()[:120]
    return next((line.strip() for line in body.text.splitlines() if line.strip()), "Notification")[:120]


def _single_affected_key(alert: "AlertFacts") -> List[Dict[str, Any]]:
    """The one affected-component entry for a freshly-filed ticket's seed
    state, or ``[]`` for a grid-level alert with no identifiable component.

    Shared between ``_seed_description`` (the initial rendered ticket
    description) and ``_record_new_correlation`` (the correlation row seeded
    right after) so both agree on the ticket's affected-equipment state from
    the very first alert.
    """
    if not (alert.component_kind and alert.component_key):
        return []
    return [
        {
            "kind": alert.component_kind,
            "key": alert.component_key,
            "label": alert.component_label,
            "first_seen": alert.fired_at,
            "last_seen": alert.fired_at,
            "count": 1,
        }
    ]


def _seed_description(
    alert: "AlertFacts", raw_text: str, root_cause_kind: Optional[str] = None
) -> str:
    """Render a freshly-filed ticket's initial description the same way
    ``apply_amendment`` will re-render it on the ticket's next alert
    (``render_description``), so the description's shape -- affected-
    equipment block or bare text -- doesn't change between a ticket's first
    and second alert (plan B5). A grid-level alert with no identifiable
    component still renders bare -- ``render_description`` omits the marker
    block entirely when there's nothing to list.
    """
    from orchestrator.services.ticketing.correlation_render import render_description

    return render_description(
        {
            "description_base": raw_text,
            "affected_keys": _single_affected_key(alert),
            "occurrence_count": 1,
            "root_cause_kind": root_cause_kind,
        }
    )


async def _record_new_correlation(
    store: Any,
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
    not a lost alert).

    Current ref/backend/grid/summary and Telegram delivery coordinates are
    not this row's concern post-0005b -- they live on ``tickets`` (already
    written by ``TicketService.create_ticket_with_internal_fallback``) and
    ``message_deliveries`` respectively.
    """
    ticket_id = getattr(result, "ticket_id", None)
    if not ticket_id:
        logger.warning(
            "Notify: cannot seed correlation row for {!r} -- ticket creation "
            "returned no canonical ticket_id",
            result.ref,
        )
        return
    try:
        signatures = [alert.signature] if alert.signature else []
        affected_keys = _single_affected_key(alert)
        await store.upsert_correlation(
            ticket_id=ticket_id,
            root_cause_kind=root_cause_kind,
            primary_signature=alert.signature or "",
            signatures=signatures,
            affected_keys=affected_keys,
            summary_base=summary,
            description_base=description,
            severity=alert.severity,
        )
    except Exception:
        logger.warning(
            "Notify: failed to seed correlation row for {!r}", result.ref, exc_info=True
        )


async def _file_uncorrelated_ticket(
    body: "NotifyRequest",
    target: "GridNotificationTarget",
    backend_override: str,
    alert: "AlertFacts",
    alert_context: UrgentAlertContext,
    store: Any,
    decided_by: str,
) -> "tuple[Optional[str], Optional[JSONResponse], Optional[Dict[str, Any]], Optional[NotificationDelivery]]":
    """File a plain ticket on a fail-open path *and* record its correlation row.

    Without the row the ticket is invisible to ``open_candidates_for_grid``
    forever, so the next identical alert cannot correlate with it and files yet
    another ticket. Recording is best-effort inside ``_record_new_correlation``
    -- a store outage still leaves the ticket filed and the alert delivered.

    Returns the same 4-tuple shape as ``_resolve_notify_ticket_auto`` itself
    (``response`` is always ``None`` here -- no path through this helper
    produces one) so callers can return its result directly.
    """
    summary = _notify_ticket_subject(body)
    result, error = await _create_notify_ticket(
        body,
        target,
        backend_override,
        description=_seed_description(alert, body.text),
        alert_context=alert_context,
    )
    if error is not None:
        return None, None, {"ticket_error": error}, _ticket_failure_delivery(alert_context)
    await _record_new_correlation(store, alert, result, None, summary, body.text)
    if body.dedup_key and result.ticket_id:
        await store.record_event_ticket_id(body.dedup_key, result.ticket_id)
    return (
        result.ref,
        None,
        {
            "decision": "new",
            "correlated_with": None,
            "confidence": None,
            "decided_by": decided_by,
        },
        _new_ticket_delivery(_notification_ticket_from_result(result), alert_context),
    )


@dataclass(frozen=True)
class NotificationTicket:
    """Backend-neutral ticket data needed to render a notification."""

    ref: str
    backend: str
    url: Optional[str] = None
    ticket_id: Optional[str] = None


def _notification_ticket_from_result(result: Any) -> NotificationTicket:
    return NotificationTicket(
        ref=result.ref,
        backend=result.backend,
        url=result.url,
        ticket_id=getattr(result, "ticket_id", None),
    )


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


def _format_ticket_notification(
    body: "NotifyRequest",
    ticket: NotificationTicket,
    *,
    urgent: Optional[bool] = None,
    live_output_line: Optional[str] = None,
) -> str:
    """Render a newly-filed or updated ticket alert as Telegram Markdown."""
    from shared.utils.telegram_markdown import escape_markdown

    subject = body.alert.subject.strip() if body.alert and body.alert.subject.strip() else ""
    if not subject:
        subject = _notify_ticket_subject(body)

    ticket_link = _ticket_notification_link(ticket)
    if urgent is None:
        severity = body.alert.severity.strip().lower() if body.alert and body.alert.severity else ""
        urgent = (severity or derive_severity(subject)) == "urgent"
    lines = [f"{'🔴 ' if urgent else ''}*{escape_markdown(subject)}*"]
    if live_output_line:
        lines.append(live_output_line)
    lines.extend((f"📍 Grid: {escape_markdown(body.grid_name)}", f"🎫 Ticket: {ticket_link}"))
    return "\n".join(lines)


def _format_ticket_update_notification(
    update: str,
    ticket: NotificationTicket,
    *,
    urgent: bool = False,
    live_output_line: Optional[str] = None,
) -> str:
    """Render a concise factual correlation update with its ticket reference linked."""
    from shared.utils.telegram_markdown import escape_markdown

    ticket_link = _ticket_notification_link(ticket)
    prefix = "🔴" if urgent else "↻"
    message = f"{prefix} {ticket_link} — {escape_markdown(update)}"
    return f"{message}\n{live_output_line}" if live_output_line else message


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
    edit_message_id: Optional[int] = None  # amend: edit this message instead of replying
    top_level: bool = False  # escalation: force a fresh (non-reply) post
    ticket: Optional[NotificationTicket] = None
    alert_context: Optional[UrgentAlertContext] = None
    ticket_summary: str = ""
    stored_ticket_severity: str = ""
    site_status: str = ""


def _render_alert_site_status(status: str) -> str:
    return {
        "on": "🟢 Site status: On",
        "isolated": "🔌 Site status: Isolated",
        "off": "🔴 Site status: Off",
    }.get(status, "Ⅹ Site status: Unknown")


def _build_notify_alert_context(
    body: "NotifyRequest", target: "GridNotificationTarget"
) -> UrgentAlertContext:
    """Create a lazy context without performing telemetry I/O."""
    severity = body.alert.severity if body.alert and body.alert.severity else ""
    return build_urgent_alert_context(
        subject=_notify_ticket_subject(body),
        incoming_severity=severity,
        grid_name=target.grid_name,
    )


def _is_effectively_urgent(
    context: UrgentAlertContext, ticket_summary: str = "", stored_ticket_severity: str = ""
) -> bool:
    return bool(
        context.is_incoming_urgent()
        or derive_severity(ticket_summary) == "urgent"
        or stored_ticket_severity.strip().lower() == "urgent"
    )


def _new_ticket_delivery(
    ticket: NotificationTicket,
    alert_context: Optional[UrgentAlertContext] = None,
    ticket_summary: str = "",
) -> NotificationDelivery:
    """A freshly-filed ticket (plain "new", flag-off, or any fallback path)
    posts the alert in full, unthreaded. ``_deliver_notification`` records
    the resulting message_id as a ``message_deliveries`` receipt (keyed by
    ``ticket.ticket_id``) so a later amend's ``DeliveryRepository.latest_for_ticket``
    lookup finds it as the reply/edit anchor."""
    return NotificationDelivery(
        ticket=ticket,
        alert_context=alert_context,
        ticket_summary=ticket_summary,
    )


def _ticket_failure_delivery(alert_context: UrgentAlertContext) -> NotificationDelivery:
    """A terse unlinked fallback alert when every ticket backend is unavailable."""
    return NotificationDelivery(text_override=alert_context.subject, alert_context=alert_context)


async def _ticket_summary(ticket_service: Any, ticket_ref: str) -> str:
    """Read a ticket's current summary without letting a status outage block delivery."""
    try:
        status = await ticket_service.get_status(ticket_ref)
        return status.summary if status is not None else ""
    except Exception:
        logger.warning("Notify: failed to read current ticket summary for {!r}", ticket_ref, exc_info=True)
        return ""


def _amend_delivery(
    decision: Any,
    amendment: Any,
    ticket: NotificationTicket,
    reply_to_message_id: Optional[int] = None,
    ticket_summary: str = "",
) -> NotificationDelivery:
    """Post only what an operator needs to act on.

    An amend that merely re-listed a component already on the ticket changed
    nothing operationally -- the ticket still records the occurrence and the
    raw alert comment, but Telegram stays quiet. Only a component genuinely
    joining the ticket, an escalation, or a power-chain cascade fold is
    worth a message.

    A cascade fold (``decision.root_cause_kind == "power_chain"``) is never
    suppressed even when it happened to add no new *keyed* component (a
    root-cause kind recurring under a blank affected_key still folds in) --
    linking two pings that would otherwise look unrelated into one thread is
    the entire point of that rung, so silence here would be worse than the
    noise it replaces. Its message prefers the LLM's own ``update_message``
    (written specifically for this Telegram topic) over the generic
    rendered summary, which for a mixed-kind ticket just reads "root summary
    -- +N dependent alert(s) (...)" and does not say what changed.

    ``reply_to_message_id`` is resolved by the caller via
    ``DeliveryRepository.latest_for_ticket`` (message_deliveries no longer
    lives on the correlation row post-0005b) -- unused when escalating,
    which always posts fresh rather than replying. For a cascade fold this
    anchor is the *root* ticket's own latest delivery, since ``amendment``
    always describes the root ticket being amended. ``ticket_summary`` is
    the ticket's current live summary (fetched by the caller only when this
    delivery will actually notify) -- the escalation branch's fallback when
    ``amendment.rendered_summary`` is blank.
    """
    escalated = bool(amendment is not None and amendment.escalated)
    component_added = bool(amendment is not None and amendment.component_added)
    cascade_symptom = bool(
        decision is not None and getattr(decision, "root_cause_kind", None) == "power_chain"
    )

    if not (component_added or escalated or cascade_symptom):
        return NotificationDelivery(suppress=True)

    if escalated:
        content = (amendment.rendered_summary or "").strip() if amendment is not None else ""
        if not content:
            content = ticket_summary.strip()
        # _format_ticket_update_notification (in _deliver_notification) adds
        # its own leading emoji for a top-level/urgent post -- an escalated
        # rendered/live summary already starts with "🔴 " (apply_amendment
        # prefixes it), so strip ours first or the pair doubles up into
        # "🔴 OPS-3428 — 🔴 ! Urgent: ...".
        content = content.lstrip("🔴").strip()
        message = f"escalated to urgent — {content}" if content else "escalated to urgent"
    elif cascade_symptom:
        rendered_summary = (amendment.rendered_summary or "").strip() if amendment is not None else ""
        label = (decision.affected_key or {}).get("label") or "a dependent alert"
        message = (
            (decision.update_message or "").strip()
            or rendered_summary
            or f"Folded in as a power_chain symptom: {label}"
        )
    else:
        rendered_summary = (amendment.rendered_summary or "").strip() if amendment is not None else ""
        if rendered_summary:
            message = rendered_summary
        else:
            # Rendered summary is blank on paths that never compute a full
            # ticket summary (e.g. the Jira-only-seed path) -- fall back to
            # the older short phrasing rather than posting/editing to blank.
            label = (decision.affected_key or {}).get("label") or "a new component"
            count = amendment.affected_keys_count if amendment is not None else 1
            message = f"Added {label} ({count} affected component{'s' if count != 1 else ''})"

    if escalated:
        # A fresh top-level post, not an edit -- and it becomes the new edit
        # target for any subsequent amend, so the edit target moves off the
        # stale original message instead of staying pinned to it forever.
        return NotificationDelivery(
            text_override=message,
            top_level=True,
            ticket=ticket,
        )
    return NotificationDelivery(
        text_override=message,
        reply_to_message_id=reply_to_message_id,
        edit_message_id=reply_to_message_id,
        ticket=ticket,
    )


def _duplicate_delivery(amendment: Any, ticket: NotificationTicket) -> NotificationDelivery:
    """Duplicates amend ticket history but never create Telegram noise."""
    return NotificationDelivery(suppress=True)


def _candidate_summaries_from_store_rows(rows: List[Dict[str, Any]]) -> List[Any]:
    """Convert raw ``ticket_correlations`` rows into ``CandidateSummary``
    instances, mirroring ``AlertCorrelator._assemble_candidates``'s store-row
    conversion (correlator.py) -- minus the ``TicketService.find_open_by_grid``
    merge and the live status-confirmation pass, both of which are exactly the
    I/O this lock-free path exists to avoid. Trusts the correlation store's
    own "open" bookkeeping instead of re-confirming against the ticket
    backend, which is an acceptable approximation only for this narrow,
    best-effort timeout fallback -- the lock-*held* path still does the full,
    confirmed assembly."""
    from orchestrator.services.ticketing.correlator import CandidateSummary, _age_hours

    now = datetime.now(timezone.utc)
    candidates: List[CandidateSummary] = []
    for row in rows:
        ref = row.get("ticket_ref")
        ticket_id = row.get("ticket_id")
        if not ref or not ticket_id:
            continue
        candidates.append(
            CandidateSummary(
                ref=ref,
                ticket_id=ticket_id,
                backend=row.get("ticket_backend") or "",
                summary=row.get("summary_current") or row.get("summary_base") or "",
                age_hours=_age_hours(row.get("created_at"), now),
                root_cause_kind=row.get("root_cause_kind"),
                affected_keys=row.get("affected_keys") or [],
                occurrence_count=row.get("occurrence_count") or 1,
                status=row.get("status") or "",
                signatures=row.get("signatures") or [],
                severity=row.get("severity") or "",
            )
        )
    candidates.sort(key=lambda c: c.age_hours if c.age_hours is not None else 0.0)
    return candidates


async def _finalize_correlation_decision(
    body: "NotifyRequest",
    target: "GridNotificationTarget",
    alert: "AlertFacts",
    alert_context: UrgentAlertContext,
    store: Any,
    ticket_service: Any,
    decision: Any,
) -> "tuple[Optional[str], Optional[JSONResponse], Optional[Dict[str, Any]], Optional[NotificationDelivery]]":
    """Execute an "amend"/"duplicate" ``CorrelationDecision`` via
    ``apply_amendment`` and turn the result into the ``(ref, response, extra,
    delivery)`` 4-tuple ``_resolve_notify_ticket_auto`` returns.

    Shared by ``_resolve_notify_ticket_auto``'s own lock-held amend/duplicate
    branch and ``_attempt_lock_free_signature_correlation``'s lock-free
    match -- both need identical post-decision plumbing once a
    ``CorrelationDecision`` names an existing ``ticket_ref`` to amend/dup,
    regardless of how that decision was reached (LLM+lock vs.
    deterministic+lock-free).
    """
    from orchestrator.services.ticketing.correlation_render import apply_amendment

    amendment = await apply_amendment(
        store=store,
        ticket_service=ticket_service,
        ticket_ref=decision.ticket_ref,
        ticket_id=decision.ticket_id,
        alert=alert,
        decision=decision,
        raw_text=body.text,
        grid_name=target.grid_name,
    )
    if amendment is None:
        # Correlation row vanished between the decision and here (store
        # outage) -- the target ticket still exists, so at minimum comment
        # on it rather than silently dropping the alert. No reply-target
        # context survives this, so deliver nothing rather than risk a
        # misdirected/noisy post.
        await ticket_service.add_comment(decision.ticket_ref, body.text, public=False)
        ref = decision.ticket_ref
        delivery = NotificationDelivery(
            suppress=True,
            alert_context=alert_context,
            stored_ticket_severity=decision.ticket_severity,
        )
    else:
        ref = amendment.ticket_ref
        ticket = NotificationTicket(
            ref=ref,
            backend=await ticket_service.get_backend_name(ref),
            ticket_id=amendment.ticket_id,
        )
        reply_to_message_id: Optional[int] = None
        if amendment.decision == "amend" and not amendment.escalated:
            from orchestrator.services.ticketing.delivery_repository import DeliveryRepository

            try:
                deliveries = DeliveryRepository(get_client=_raw_supabase_client)
                anchor = await deliveries.latest_for_ticket(amendment.ticket_id)
            except Exception:
                logger.warning(
                    "Notify: failed to resolve delivery anchor for {!r}", ref, exc_info=True
                )
                anchor = None
            if anchor:
                reply_to_message_id = anchor.get("external_message_id")
        if amendment.decision == "amend":
            # Fetched here (rather than after _amend_delivery, as before)
            # because the escalation branch needs it as a fallback when
            # amendment.rendered_summary is blank -- same "only when this
            # will actually notify" gate _amend_delivery itself applies
            # (component_added or escalated), just hoisted one level up so
            # the value can be threaded into that call.
            ticket_summary = (
                await _ticket_summary(ticket_service, ref)
                if (amendment.component_added or amendment.escalated)
                else ""
            )
            delivery = _amend_delivery(
                decision, amendment, ticket, reply_to_message_id, ticket_summary
            )
        else:
            ticket_summary = ""
            delivery = _duplicate_delivery(amendment, ticket)
        delivery = dataclasses.replace(
            delivery,
            alert_context=alert_context,
            ticket_summary=ticket_summary,
            stored_ticket_severity=decision.ticket_severity,
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


async def _attempt_lock_free_signature_correlation(
    body: "NotifyRequest",
    target: "GridNotificationTarget",
    alert: "AlertFacts",
    alert_context: UrgentAlertContext,
    store: Any,
    ticket_service: Any,
) -> "Optional[tuple[Optional[str], Optional[JSONResponse], Optional[Dict[str, Any]], Optional[NotificationDelivery]]]":
    """Best-effort, lock-free dedup check for the grid-correlation-lock
    timeout path only (see ``_resolve_notify_ticket_auto``).

    A lock *timeout* used to skip correlation entirely and file a blind new
    ticket -- on a busy grid, a burst of alerts queued behind the lock could
    each exceed the wait budget and each duplicate a ticket a decide() call
    earlier in the same burst had already correctly correlated. This runs
    only the deterministic, LLM-free rungs (``find_deterministic_decision``,
    shared with ``AlertCorrelator.decide()``) against a fresh, unlocked read
    of open candidates -- cheap enough to stay inline on the timeout path,
    unlike the full lock-held ``AlertCorrelator.decide()`` (candidate
    assembly + live status confirmation + LLM call).

    Returns the resolved 4-tuple (mirroring ``_resolve_notify_ticket_auto``'s
    own return shape) when a match is found -- routed through
    ``apply_amendment`` exactly like the signature-rung match inside
    ``AlertCorrelator.decide()`` does, and recorded via ``store.record_event``
    the same (best-effort) way ``AlertCorrelator._finalize`` does. Returns
    ``None`` when there's no match, or when anything here fails -- either
    way the caller falls through to ``_file_uncorrelated_ticket`` exactly as
    it did before this existed.
    """
    from orchestrator.services.ticketing.correlation_rules import DEFAULT_CORRELATION_POLICY
    from orchestrator.services.ticketing.correlator import find_deterministic_decision

    try:
        since_iso = (
            datetime.now(timezone.utc)
            - timedelta(hours=DEFAULT_CORRELATION_POLICY.open_candidate_window_hours)
        ).isoformat()
        rows = await store.open_candidates_for_grid(
            target.grid_name,
            since_iso,
            limit=DEFAULT_CORRELATION_POLICY.maximum_candidate_count,
        )
        candidates = _candidate_summaries_from_store_rows(rows)

        # Mirrors AlertCorrelator.decide()'s own three deterministic rungs
        # (correlator.py) -- decided_by="fallback_signature" (rather than
        # "signature") and the reason suffix are the only difference,
        # recording that this matched without holding the per-grid lock.
        decision = find_deterministic_decision(
            candidates,
            alert,
            decided_by="fallback_signature",
            reason_suffix=" (grid-lock timed out; matched without the lock)",
        )
        if decision is None:
            return None

        try:
            await store.record_event(
                ticket_id=decision.ticket_id,
                grid_name=target.grid_name,
                source=alert.rule_id or None,
                signature=alert.signature or None,
                dedup_key=body.dedup_key,
                decision=decision.decision,
                decided_by=decision.decided_by,
                confidence=decision.confidence,
                reason=decision.reason,
                candidate_refs=decision.candidate_refs,
                alert=alert.model_dump(),
                llm_raw=decision.llm_raw,
            )
        except Exception:
            logger.warning(
                "Notify: failed to record lock-free correlation event for grid {}",
                target.grid_name,
                exc_info=True,
            )

        return await _finalize_correlation_decision(
            body, target, alert, alert_context, store, ticket_service, decision
        )
    except Exception:
        logger.warning(
            "Notify: lock-free correlation attempt raised for grid {} -- filing plain ticket",
            target.grid_name,
            exc_info=True,
        )
        return None


async def _resolve_notify_ticket_auto(
    body: "NotifyRequest",
    target: "GridNotificationTarget",
    backend_override: str,
    alert_context: UrgentAlertContext,
) -> "tuple[Optional[str], Optional[JSONResponse], Optional[Dict[str, Any]], Optional[NotificationDelivery]]":
    """``ticket_id in ("", "auto")``: smart alert correlation (see
    docs/superpowers/plans/2026-07-27-smart-alert-correlation-notify.md).

    Fails open at every step -- ``ALERT_CORRELATION_ENABLED`` off, a grid-lock
    timeout, or the correlator/executor raising all fall back to filing a
    plain new ticket via ``_create_notify_ticket``. An alert is never
    dropped; correlation only ever adds grouping on top, never a new way to
    fail.
    """
    from orchestrator.services.supabase_client import get_supabase_client
    from orchestrator.services.ticketing.alert_facts import enrich_alert_facts
    from orchestrator.services.ticketing.correlation_store import CorrelationStore
    from shared.config import flag_registry as fr

    if body.alert is not None:
        base_alert = body.alert
    else:
        first_line = next(
            (line.strip() for line in body.text.splitlines() if line.strip()), ""
        )
        base_alert = AlertFacts(subject=first_line, details=body.text)
    alert = enrich_alert_facts(base_alert, grid_name=target.grid_name)

    if not fr.get("ALERT_CORRELATION_ENABLED"):
        store = CorrelationStore(get_client=_raw_supabase_client)
        return await _file_uncorrelated_ticket(
            body, target, backend_override, alert, alert_context, store, "flag_off"
        )

    from orchestrator.services.ticketing.correlation_render import apply_amendment
    from orchestrator.services.ticketing.correlation_rules import (
        DEFAULT_CORRELATION_POLICY,
    )
    from orchestrator.services.ticketing.correlator import AlertCorrelator
    from orchestrator.services.ticketing.service import TicketService

    timeout_seconds = DEFAULT_CORRELATION_POLICY.grid_lock_timeout_seconds
    store = CorrelationStore(get_client=_raw_supabase_client)

    async with _acquire_grid_correlation_lock(target.grid_name, timeout_seconds) as acquired:
        if not acquired:
            logger.warning(
                "Notify: grid-correlation lock timeout for {!r} -- attempting lock-free "
                "deterministic correlation before filing a plain ticket",
                target.grid_name,
            )
            ticket_service = TicketService(get_supabase_client=get_supabase_client)
            lock_free_result = await _attempt_lock_free_signature_correlation(
                body, target, alert, alert_context, store, ticket_service
            )
            if lock_free_result is not None:
                return lock_free_result
            return await _file_uncorrelated_ticket(
                body, target, backend_override, alert, alert_context, store, "fallback"
            )

        ticket_service = TicketService(get_supabase_client=get_supabase_client)
        correlator = AlertCorrelator(store=store, ticket_service=ticket_service)

        if fr.get("ALERT_LLM_JUDGMENT_ENABLED"):
            return await _resolve_notify_ticket_llm_judgment(
                body, target, backend_override, alert_context, alert, store, ticket_service, correlator
            )

        try:
            decision = await correlator.decide(
                target.grid_name,
                alert,
                dedup_key=body.dedup_key,
                backend_override=backend_override,
                get_live_facts=alert_context.llm_facts,
            )
        except Exception:
            logger.exception(
                "Notify: correlator.decide() raised for grid {!r} -- filing plain ticket",
                target.grid_name,
            )
            decision = None

        if decision is None:
            return await _file_uncorrelated_ticket(
                body, target, backend_override, alert, alert_context, store, "fallback"
            )


        try:
            from orchestrator.services.ticketing.correlator import _is_urgent_severity_increase

            if decision.decided_by == "replay" and decision.ticket_ref:
                if not _is_urgent_severity_increase(alert.severity, decision.ticket_severity):
                    # This dedup_key was already decided, already applied to
                    # the ticket, and already posted. Re-running the amend
                    # would double the comment and the Telegram message.
                    logger.info(
                        "Notify: replayed dedup_key for {!r} -- suppressing duplicate delivery",
                        decision.ticket_ref,
                    )
                    return (
                        decision.ticket_ref,
                        None,
                        {
                            "decision": decision.decision,
                            "correlated_with": decision.ticket_ref,
                            "confidence": decision.confidence,
                            "decided_by": decision.decided_by,
                        },
                        NotificationDelivery(
                            suppress=True,
                            alert_context=alert_context,
                            stored_ticket_severity=decision.ticket_severity,
                        ),
                    )
                # Urgent severity increase on a replay -- decision.ticket_ref
                # already names a real, existing ticket (whether the original
                # decision was "new" or "amend"), so this must escalate that
                # ticket, never file a second one. Coercing decision to
                # "amend" here routes it into the ordinary amend-execution
                # path below instead of the "new"-ticket branch, which would
                # otherwise fire because decision.decision on a replay is
                # still whatever the ORIGINAL (pre-replay) decision type was.
                decision = dataclasses.replace(
                    decision, decision="amend", needs_root_cause_ticket=False
                )

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
                        body,
                        target,
                        backend_override,
                        summary=root_summary,
                        description=_seed_description(
                            alert, root_description, decision.root_cause_kind
                        ),
                        alert_context=alert_context,
                    )
                    if error is not None:
                        return None, None, {"ticket_error": error}, _ticket_failure_delivery(alert_context)
                    await _record_new_correlation(
                        store, alert, result, decision.root_cause_kind, root_summary, root_description
                    )
                    if body.dedup_key and result.ticket_id:
                        await store.record_event_ticket_id(body.dedup_key, result.ticket_id)
                    await apply_amendment(
                        store=store,
                        ticket_service=ticket_service,
                        ticket_ref=result.ref,
                        ticket_id=result.ticket_id,
                        alert=alert,
                        decision=dataclasses.replace(
                            decision, ticket_ref=result.ref, ticket_id=result.ticket_id
                        ),
                        raw_text=body.text,
                        grid_name=target.grid_name,
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
                        _new_ticket_delivery(
                            _notification_ticket_from_result(result),
                            alert_context,
                            root_summary,
                        ),
                    )

                summary = _notify_ticket_subject(body)
                result, error = await _create_notify_ticket(
                    body,
                    target,
                    backend_override,
                    summary=summary,
                    description=_seed_description(alert, body.text, decision.root_cause_kind),
                    alert_context=alert_context,
                )
                if error is not None:
                    return None, None, {"ticket_error": error}, _ticket_failure_delivery(alert_context)
                await _record_new_correlation(
                    store, alert, result, decision.root_cause_kind, summary, body.text
                )
                if body.dedup_key and result.ticket_id:
                    await store.record_event_ticket_id(body.dedup_key, result.ticket_id)
                return (
                    result.ref,
                    None,
                    {
                        "decision": "new",
                        "correlated_with": None,
                        "confidence": decision.confidence,
                        "decided_by": decision.decided_by,
                    },
                    _new_ticket_delivery(_notification_ticket_from_result(result), alert_context),
                )

            # amend (onto an existing ticket) or duplicate.
            return await _finalize_correlation_decision(
                body, target, alert, alert_context, store, ticket_service, decision
            )
        except Exception:
            logger.exception(
                "Notify: correlation execution raised for grid {!r} -- filing plain ticket",
                target.grid_name,
            )
            return await _file_uncorrelated_ticket(
                body, target, backend_override, alert, alert_context, store, "fallback"
            )


async def _resolve_notify_ticket_llm_judgment(
    body: "NotifyRequest", target: "GridNotificationTarget", backend_override: str,
    alert_context: UrgentAlertContext, alert: AlertFacts, store: Any,
    ticket_service: Any, correlator: Any,
) -> "tuple[Optional[str], Optional[JSONResponse], Optional[Dict[str, Any]], Optional[NotificationDelivery]]":
    """LLM-first auto path: gather bounded evidence, judge once, then fail open."""
    from orchestrator.services.ticketing.alert_delivery_policy import decide_alert_delivery
    from orchestrator.services.ticketing.alert_judgment_context import AlertJudgmentContextAssembler
    from orchestrator.services.ticketing.correlator import (
        collect_deterministic_findings,
        to_legacy_correlation_decision,
    )
    from orchestrator.services.ticketing.notify_alert_delivery_repository import (
        NotifyAlertDeliveryRepository,
    )
    from shared.config import flag_registry as fr

    try:
        candidates = await correlator._assemble_candidates(target.grid_name, backend_override=backend_override)
    except Exception:
        logger.warning("Notify: judgment candidate assembly failed", exc_info=True)
        candidates = []
    since = (datetime.now(timezone.utc) - timedelta(hours=168)).isoformat()
    history = NotifyAlertDeliveryRepository(get_client=_raw_supabase_client)

    async def findings_provider(): return collect_deterministic_findings(candidates, alert)
    async def tickets_provider(): return [candidate.model_dump() for candidate in candidates]
    async def telemetry_provider(): return await alert_context.telemetry()
    async def prior_provider(): return await history.recent_for_grid(target.grid_name, since, limit=20)
    async def om_provider(): return await history.recent_om_messages(chat_id=target.chat_id, topic_id=target.topic_id, since=since, limit=50)

    context = await AlertJudgmentContextAssembler(
        deterministic_findings_provider=findings_provider, open_tickets_provider=tickets_provider,
        telemetry_provider=telemetry_provider, prior_alerts_provider=prior_provider,
        om_messages_provider=om_provider,
    ).assemble(grid_name=target.grid_name, chat_id=target.chat_id, topic_id=target.topic_id, alert=alert)
    judgment = await correlator.judge(target.grid_name, alert, context)
    decision = to_legacy_correlation_decision(judgment, candidates)
    send_decision = decide_alert_delivery(
        judgment, context, latest_prior_alert=context.prior_alerts[0] if context.prior_alerts else None,
        enforcement_enabled=bool(fr.get("ALERT_LLM_SUPPRESSION_ENFORCED")),
    )
    if decision.decision == "new" or not decision.ticket_ref or not decision.ticket_id:
        ref, response, extra, delivery = await _file_uncorrelated_ticket(body, target, backend_override, alert, alert_context, store, "llm_judgment")
    else:
        ref, response, extra, delivery = await _finalize_correlation_decision(body, target, alert, alert_context, store, ticket_service, decision)
    extra = extra or {}
    extra.update({"judgment_valid": judgment.valid, "send_decision": "send" if send_decision.send else "suppress", "send_force_reasons": send_decision.forced_by})
    if delivery is not None:
        status = (
            judgment.judgment.grid_impact.current_assessed_status.value
            if judgment.valid and judgment.judgment is not None
            else context.telemetry.site_status.value
        )
        if "all_phase_zero_reminder" in send_decision.forced_by:
            status = "off"
        delivery = dataclasses.replace(
            delivery, suppress=not send_decision.send, site_status=status
        )
    return ref, response, extra, delivery


async def _resolve_notify_ticket_full(
    body: "NotifyRequest",
    target: "GridNotificationTarget",
    alert_context: Optional[UrgentAlertContext] = None,
) -> "tuple[Optional[str], Optional[JSONResponse], Optional[Dict[str, Any]], Optional[NotificationDelivery]]":
    """Resolve ``body.ticket_id`` into a ticket ref per the /notify ticketing contract.

    Returns ``(ticket_ref, None, extra, delivery)`` on success -- ``ticket_ref``
    is ``None`` when ``body.ticket_id`` was omitted (pure passthrough,
    unchanged behavior) -- or ``(None, response, None, None)`` when the
    request must fail fast with ``response`` before any delivery is
    scheduled. ``extra`` (decision/correlated_with/confidence/decided_by) and
    ``delivery`` (how ``_deliver_notification`` should post/suppress/reply)
    are only ever populated for the ticket-filing paths (blank or ``"auto"``)
    -- the populated-``ticket_id`` comment/close path returns ``None`` for
    both, so neither the response body nor the Telegram send behavior changes
    for existing callers of that path.

    Runs synchronously in the handler (not the background delivery task) so
    ticket failures reach the caller in the HTTP response, same rationale as
    the existing synchronous grid resolution.

    Notify-originated tickets use NOTIFY_TICKETS_BACKEND (default 'internal'),
    independent of TICKET_BACKEND_OVERRIDE (which only governs customer
    escalations) -- so Grafana/n8n/VRM alerts never land in the Jira OPS
    project unless an operator explicitly opts them into 'auto'.

    A blank ``ticket_id`` ("") is routed through the same correlation pipeline
    as ``"auto"``. They used to diverge -- blank meant "always file a new
    ticket, no dedup" -- but that made ALERT_CORRELATION_ENABLED a trap: a
    caller that quietly stopped sending "auto" (or never sent it) got zero
    correlation and zero signal that anything had changed, since blank still
    filed tickets successfully, just one per alert forever. Correlation's own
    kill switch (``ALERT_CORRELATION_ENABLED``, checked inside
    ``_resolve_notify_ticket_auto``) is what actually turns grouping off; a
    request-level flag shouldn't be a second, silent way to bypass it.
    """
    if alert_context is None:
        alert_context = _build_notify_alert_context(body, target)

    if body.ticket_id is None:
        return None, None, None, None

    from shared.config import flag_registry as fr

    backend_override = fr.get("NOTIFY_TICKETS_BACKEND") or "internal"
    normalized = body.ticket_id.strip().lower()

    if body.ticket_id == "" or normalized == "auto":
        return await _resolve_notify_ticket_auto(body, target, backend_override, alert_context)

    # Populated ticket_id: comment on (and optionally close) an existing ticket.
    from orchestrator.services.supabase_client import get_supabase_client
    from orchestrator.services.ticketing.service import TicketService

    ticket_service = TicketService(get_supabase_client=get_supabase_client)
    ticket_ref = body.ticket_id
    try:
        status = await ticket_service.get_status(ticket_ref)
    except Exception as exc:
        logger.exception("Notify: ticket lookup failed for {!r}", ticket_ref)
        return (
            None,
            None,
            {"ticket_error": f"Ticket lookup failed: {exc}"},
            _ticket_failure_delivery(alert_context),
        )
    if status is None:
        logger.warning("Notify: unresolvable ticket_id={!r} (source={})", ticket_ref, body.source)
        return None, JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Unknown or unresolvable ticket_id: {ticket_ref!r}"},
        ), None, None
    ticket_error: Optional[str] = None
    try:
        commented = await ticket_service.add_comment(ticket_ref, body.text, public=False)
    except Exception as exc:
        logger.exception("Notify: ticket comment failed for {!r}", ticket_ref)
        commented = False
        ticket_error = f"Ticket update failed: {exc}"
    if not commented:
        logger.warning(
            "Notify: add_comment reported failure for ticket_ref={!r} (source={})",
            ticket_ref,
            body.source,
        )
    if body.close:
        try:
            await ticket_service.transition_to_done(ticket_ref)
        except Exception as exc:
            logger.exception("Notify: ticket close failed for {!r}", ticket_ref)
            ticket_error = ticket_error or f"Ticket close failed: {exc}"
    ticket = NotificationTicket(
        ref=ticket_ref,
        backend=await ticket_service.get_backend_name(ticket_ref),
    )
    return ticket_ref, None, ({"ticket_error": ticket_error} if ticket_error else None), NotificationDelivery(
        ticket=ticket,
        alert_context=alert_context,
        ticket_summary=status.summary,
    )


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
        logger.warning("Notify: secret mismatch from source={}", body.source)
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
            "Notify: grid resolution failed for {!r} (source={})", body.grid_name, body.source
        )
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "Grid resolution temporarily unavailable"},
        )
    if target is None:
        logger.warning("Notify: unresolvable grid_name={!r} (source={})", body.grid_name, body.source)
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
    alert_context = _build_notify_alert_context(body, target)
    ticket_ref, error_response, extra, delivery = await _resolve_notify_ticket_full(
        body, target, alert_context
    )
    if error_response is not None:
        return error_response

    # Skill alert trigger (Phase 5 of
    # docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 6):
    # deliberately AFTER the correlation decision above, never before --
    # firing earlier would re-run triggered skills on duplicate re-fires of
    # the same alert, exactly the noise ALERT_CORRELATION_ENABLED exists to
    # avoid. Backgrounded like the notification delivery itself below, so a
    # skill run's latency never delays this endpoint's response.
    from orchestrator.experts.skill_schedule_dispatch import dispatch_skill_alert_trigger

    background_tasks.add_task(
        dispatch_skill_alert_trigger, target.grid_name, target.chat_id, target.topic_id
    )

    # Return fast; the send + logging happen in the background (mirrors the
    # Telegram-webhook pattern — responses go out via the Bot API, not this body).
    background_tasks.add_task(_deliver_notification, body, target, ticket_ref, delivery)
    response_content: Dict[str, Any] = {"ok": True}
    if ticket_ref:
        response_content["ticket_ref"] = ticket_ref
    if extra:
        response_content.update(extra)
    from orchestrator.services.ticketing.correlation_store import failures_last_hour

    if failures_last_hour() > 0:
        # Visible to the caller (n8n), not just /health -- a degraded store
        # ran silently for ~12 hours in the 2026-08-10 incident with no
        # signal anywhere outside the logs.
        response_content["correlation_degraded"] = True
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


def _get_verification_criteria() -> str:
    """
    Get verification criteria from the same prompt used for response verification.

    Sourced from the shared prompt library (verification.criteria): DB
    override, then an attached Google Doc, then the bundled default. This is
    the same resolution every other verification consumer uses, from one
    cache (shared.prompts.gdoc.GDocStore) -- no local cache of our own.
    """
    from shared.prompts import PROMPTS

    return PROMPTS.text("verification.criteria")


@app.post("/api/v1/verify/broadcast", response_model=BroadcastVerifyResponse)
async def verify_broadcast(request: Request, body: BroadcastVerifyRequest):
    """
    Verify a broadcast message before sending.

    Uses the same ResponseVerificationService as response verification,
    with the same verification.criteria prompt, same model, and same LLM path.

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
