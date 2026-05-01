#!/usr/bin/env python3
"""
Broadcast Scheduler Background Worker

Polls for scheduled messages and executes them at the scheduled time.
Supports two message types:
- 'broadcast': Admin broadcast messages to multiple groups
- 'user_command': User-scheduled commands (e.g., /tickets, /grid)

Runs as a standalone script, typically invoked by a cron job or systemd timer.

Usage:
    python broadcast_scheduler.py          # Single run (check and process pending)
    python broadcast_scheduler.py --daemon # Continuous run (poll every 60s)
"""

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.broadcast_service import BroadcastService
from services.scheduling_service import SchedulingService

# Chat orchestrator URL for executing commands
CHAT_ORCHESTRATOR_URL = os.getenv(
    "CHAT_ORCHESTRATOR_URL",
    "http://localhost:8000",  # Default for local dev; in production use internal URL
)

# Telegram bot token for sending results
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# API key for authenticating with chat orchestrator
API_KEY = os.getenv("API_KEY", "")


def process_pending_broadcasts(processor_id: str, verbose: bool = True) -> int:
    """
    Process all pending scheduled broadcasts that are due.

    Args:
        processor_id: Unique identifier for this processor instance
        verbose: Print progress messages

    Returns:
        Number of broadcasts processed
    """
    scheduling_service = SchedulingService()
    broadcast_service = BroadcastService()

    if not scheduling_service.is_configured():
        if verbose:
            print(
                "ERROR: Scheduling service not configured. Check CHAT_DB_URL/CHAT_DB_SERVICE_KEY (or legacy SUPABASE_URL/KEY)."
            )
        return 0

    if not broadcast_service.is_configured():
        if verbose:
            print("ERROR: Broadcast service not configured. Check TELEGRAM_BOT_TOKEN.")
        return 0

    # Claim pending broadcast messages atomically
    # NOTE: The RPC claims ALL message types, so we must release non-broadcasts
    pending = scheduling_service.claim_pending_messages(
        processor_id=processor_id,
        batch_size=10,
    )

    if not pending:
        if verbose:
            print(f"[{datetime.now(timezone.utc).isoformat()}] No pending broadcasts to process")
        return 0

    # Separate broadcasts from other message types
    broadcasts = [msg for msg in pending if msg.get("message_type") == "broadcast"]
    non_broadcasts = [msg for msg in pending if msg.get("message_type") != "broadcast"]

    # Release non-broadcast messages back to pending so other processors can claim them
    if non_broadcasts:
        try:
            from supabase import create_client

            supabase_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
            supabase_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
            if supabase_url and supabase_key:
                supabase = create_client(supabase_url, supabase_key)
                non_broadcast_ids = [msg["id"] for msg in non_broadcasts]
                supabase.table("scheduled_messages").update(
                    {
                        "status": "pending",
                        "processed_by": None,
                        "processed_at": None,
                    }
                ).in_("id", non_broadcast_ids).execute()
                if verbose:
                    print(
                        f"  Released {len(non_broadcasts)} non-broadcast message(s) back to pending"
                    )
        except Exception as e:
            if verbose:
                print(f"  WARNING: Failed to release non-broadcasts: {e}")

    if verbose:
        print(f"[{datetime.now(timezone.utc).isoformat()}] Claimed {len(broadcasts)} broadcast(s)")

    if not broadcasts:
        return 0

    processed = 0

    for scheduled_msg in broadcasts:
        schedule_id = scheduled_msg["id"]
        payload = scheduled_msg.get("payload", {})

        if verbose:
            print(f"  Processing broadcast {schedule_id}...")

        try:
            # Extract broadcast_id from payload
            broadcast_id = payload.get("broadcast_id")

            if not broadcast_id:
                if verbose:
                    print("    SKIP: Missing broadcast_id in payload")
                scheduling_service.mark_failed(
                    schedule_id, "Missing broadcast_id", should_retry=False
                )
                continue

            # Execute the existing scheduled broadcast (updates original record)
            result = broadcast_service.execute_scheduled_broadcast(broadcast_id)

            # Mark as completed with result
            scheduling_service.mark_completed(
                schedule_id,
                result={
                    "broadcast_id": result.broadcast_id,
                    "total": result.total,
                    "successful": result.successful,
                    "failed": result.failed,
                    "errors": result.errors[:5],  # Limit stored errors
                },
            )

            if verbose:
                print(
                    f"    SUCCESS: Sent to {result.successful}/{result.total} groups "
                    f"(broadcast_id: {result.broadcast_id})"
                )

            processed += 1

        except Exception as e:
            if verbose:
                print(f"    ERROR: {e}")
            scheduling_service.mark_failed(schedule_id, str(e), should_retry=True)

    return processed


def process_pending_user_commands(processor_id: str, verbose: bool = True) -> int:
    """
    Process all pending user-scheduled commands that are due.

    Uses atomic claim via RPC to prevent race conditions and recover
    stale processing messages (stuck for >5 minutes).

    Args:
        processor_id: Unique identifier for this processor instance
        verbose: Print progress messages

    Returns:
        Number of commands processed
    """
    from supabase import Client, create_client

    supabase_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")

    if not supabase_url or not supabase_key:
        if verbose:
            print(
                "ERROR: Chat database not configured. Check CHAT_DB_URL/CHAT_DB_SERVICE_KEY (or legacy SUPABASE_URL/KEY)."
            )
        return 0

    supabase: Client = create_client(supabase_url, supabase_key)
    now = datetime.now(timezone.utc)

    # Claim pending user_command messages atomically via dedicated RPC
    # The RPC filters by message_type='user_command' and handles stale recovery
    try:
        pending_result = supabase.rpc(
            "claim_user_command_messages",
            {"p_batch_size": 10, "p_processor_id": processor_id},
        ).execute()
        pending = pending_result.data or []

        if pending and verbose:
            # Check if any were stale (status was already 'processing' before claim)
            stale_count = sum(1 for m in pending if m.get("status") == "processing")
            if stale_count:
                print(f"  (recovered {stale_count} stale message(s))", flush=True)

    except Exception as e:
        if verbose:
            print(f"ERROR claiming user commands via RPC: {e}", flush=True)
        return 0

    if not pending:
        if verbose:
            print(f"[{now.isoformat()}] No pending user commands", flush=True)
        return 0

    if verbose:
        print(f"[{now.isoformat()}] Claimed {len(pending)} user command(s)", flush=True)

    processed = 0

    for scheduled_msg in pending:
        msg_id = scheduled_msg["id"]
        payload = scheduled_msg.get("payload", {})

        schedule_id = payload.get("schedule_id")
        chat_id = payload.get("chat_id")
        topic_id = payload.get("topic_id")
        command = payload.get("command")
        user_context = payload.get("user_context", {})

        if not chat_id or not command:
            if verbose:
                print(f"  SKIP {msg_id}: Missing chat_id or command", flush=True)
            _mark_failed(supabase, msg_id, "Missing chat_id or command")
            continue

        # Check if the parent schedule is still active before executing
        if schedule_id:
            try:
                parent = (
                    supabase.table("user_schedules")
                    .select("is_active, status")
                    .eq("id", schedule_id)
                    .single()
                    .execute()
                )
                if parent.data and not parent.data.get("is_active"):
                    if verbose:
                        parent_status = parent.data.get("status", "unknown")
                        print(
                            f"  SKIP {msg_id[:8]}: Parent schedule {schedule_id[:8]} is {parent_status}",
                            flush=True,
                        )
                    _mark_cancelled(supabase, msg_id, schedule_id)
                    continue
            except Exception as e:
                if verbose:
                    print(f"  WARNING: Could not check parent schedule: {e}", flush=True)
                # Continue with execution if we can't verify — fail open

        if verbose:
            print(f"  Processing {msg_id[:8]}: {command} for chat {chat_id}", flush=True)

        start_time = time.time()

        try:
            # Execute command via chat orchestrator
            if verbose:
                print(f"  Calling orchestrator for {command}...", flush=True)
            response_text = _execute_user_command(
                command=command,
                chat_id=chat_id,
                topic_id=topic_id,
                user_context=user_context,
                verbose=verbose,
            )

            # Add schedule prefix to identify scheduled messages
            schedule_id_short = schedule_id[:8] if schedule_id else "unknown"
            prefixed_text = (
                f"☕️ Here is your scheduled message `{schedule_id_short}`: \n\n{response_text}"
            )

            # Strip [BUTTONS] tags from scheduled messages — inline buttons are
            # not actionable in scheduled context (no user present to tap them).
            try:
                from shared.utils.telegram_buttons import strip_buttons_tags

                prefixed_text = strip_buttons_tags(prefixed_text)
            except ImportError:
                pass  # shared/ not available

            # Send result to Telegram (no reply_markup for scheduled messages)
            message_id = _send_telegram_message(
                chat_id=chat_id,
                topic_id=topic_id,
                text=prefixed_text,
                verbose=verbose,
            )

            execution_time_ms = int((time.time() - start_time) * 1000)

            # Log to user_schedule_logs
            if schedule_id:
                _log_schedule_execution(
                    supabase=supabase,
                    schedule_id=schedule_id,
                    status="success",
                    result_message=response_text[:4000] if response_text else None,
                    telegram_message_id=message_id,
                    execution_time_ms=execution_time_ms,
                )

                # Update next_run_at for recurring schedules
                _update_recurring_schedule(supabase, schedule_id, verbose)

            # Mark scheduled_message as completed
            supabase.table("scheduled_messages").update(
                {
                    "status": "completed",
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                    "result": {
                        "telegram_message_id": message_id,
                        "execution_time_ms": execution_time_ms,
                    },
                }
            ).eq("id", msg_id).execute()

            if verbose:
                print(f"    SUCCESS: Sent to chat {chat_id} ({execution_time_ms}ms)", flush=True)

            processed += 1

        except Exception as e:
            if verbose:
                print(f"    ERROR: {e}", flush=True)

            # Log failure
            if schedule_id:
                _log_schedule_execution(
                    supabase=supabase,
                    schedule_id=schedule_id,
                    status="failed",
                    error_message=str(e),
                    execution_time_ms=int((time.time() - start_time) * 1000),
                )

                # CRITICAL: Still update next_run_at for recurring schedules even on failure
                # Otherwise a single failure breaks the recurring schedule forever
                _update_recurring_schedule(supabase, schedule_id, verbose)

            _mark_failed(supabase, msg_id, str(e))

    return processed


def _execute_user_command(
    command: str,
    chat_id: str,
    topic_id: Optional[str],
    user_context: Dict[str, Any],
    verbose: bool = True,
) -> str:
    """
    Execute a user command via the chat orchestrator API.

    Args:
        command: The command to execute (e.g., "/tickets")
        chat_id: Telegram chat ID
        topic_id: Optional forum topic ID
        user_context: User context for permissions

    Returns:
        The response text from the orchestrator
    """
    # Use WebhookRequest format (message, user_id, source, etc.)
    # Note: is_group is derived by handler from bool(chat_id)
    # user_email must be top-level for auth to work (not inside metadata)
    #
    # SECURITY: Pass the stored organization context explicitly.
    # This ensures the scheduled command executes with the CHAT's org permissions
    # (captured at schedule creation), not the user's personal org.
    org_ids = user_context.get("organization_ids", [])
    scheduled_org_id = org_ids[0] if org_ids else None

    request_payload = {
        "message": command,
        "user_id": user_context.get("user_id", "scheduled"),
        "user_email": user_context.get("user_email", ""),
        "source": "telegram",
        "username": user_context.get("username"),
        "chat_id": chat_id,
        "topic_id": topic_id,
        "metadata": {
            "scheduled_execution": True,
            # Stored context from schedule creation (chat's org, not user's personal org)
            "scheduled_organization_id": scheduled_org_id,
            "scheduled_is_staff": user_context.get("is_staff", False),
            # Legacy fields for backwards compatibility
            "organization_ids": org_ids,
            "grid_ids": user_context.get("grid_ids", []),
            "meter_ids": user_context.get("meter_ids", []),
            "is_admin": user_context.get("is_admin", False),
            "is_staff": user_context.get("is_staff", False),
        },
    }

    headers = {}
    if API_KEY:
        headers["X-Api-Key"] = API_KEY

    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            f"{CHAT_ORCHESTRATOR_URL}/chat",
            json=request_payload,
            headers=headers,
        )
        response.raise_for_status()
        result = response.json()

    # API returns "message" field, but fallback to "final_text" for backward compatibility
    return str(result.get("message") or result.get("final_text") or "No response generated")


def _send_telegram_message(
    chat_id: str,
    topic_id: Optional[str],
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    verbose: bool = True,
) -> Optional[str]:
    """Send a message to Telegram and return the message ID.

    Automatically splits messages that exceed Telegram's 4096 character limit.
    """
    if not TELEGRAM_BOT_TOKEN:
        if verbose:
            print("    WARNING: No TELEGRAM_BOT_TOKEN - cannot send message")
        return None

    # Convert GitHub-style markdown (from LLM) to Telegram markdown format
    try:
        from shared.utils.telegram_markdown import convert_github_to_telegram_markdown

        text = convert_github_to_telegram_markdown(text)
    except ImportError:
        pass  # shared/ not available — send raw text

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    MAX_TELEGRAM_LENGTH = 4096

    # Split message if too long
    if len(text) > MAX_TELEGRAM_LENGTH:
        if verbose:
            print(
                f"    Message too long ({len(text)} chars), splitting into chunks",
                flush=True,
            )
        chunks = []
        current_chunk = ""
        for line in text.split("\n"):
            if len(current_chunk) + len(line) + 1 <= MAX_TELEGRAM_LENGTH:
                current_chunk += line + "\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.rstrip())
                current_chunk = line + "\n"
        if current_chunk:
            chunks.append(current_chunk.rstrip())

        last_message_id = None
        with httpx.Client(timeout=30.0) as client:
            for chunk in chunks:
                chunk_payload: Dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "Markdown",
                }
                if topic_id:
                    chunk_payload["message_thread_id"] = int(topic_id)
                response = client.post(url, json=chunk_payload)
                response.raise_for_status()
                result = response.json()
                last_message_id = str(result.get("result", {}).get("message_id", ""))
        return last_message_id

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    if topic_id:
        payload["message_thread_id"] = int(topic_id)

    if reply_markup:
        payload["reply_markup"] = reply_markup

    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        result = response.json()

    return str(result.get("result", {}).get("message_id", ""))


def _log_schedule_execution(
    supabase,
    schedule_id: str,
    status: str,
    result_message: Optional[str] = None,
    error_message: Optional[str] = None,
    telegram_message_id: Optional[str] = None,
    execution_time_ms: Optional[int] = None,
    verification_passed: Optional[bool] = None,
    verification_feedback: Optional[str] = None,
):
    """Log execution to user_schedule_logs."""
    try:
        log_data = {
            "schedule_id": schedule_id,
            "status": status,
            "result_message": result_message,
            "error_message": error_message,
            "telegram_message_id": telegram_message_id,
            "execution_time_ms": execution_time_ms,
            "verification_passed": verification_passed,
            "verification_feedback": verification_feedback,
        }
        supabase.table("user_schedule_logs").insert(log_data).execute()

        # Update parent schedule
        update_data: Dict[str, Any] = {
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        if status == "success":
            # Increment run count
            schedule = (
                supabase.table("user_schedules")
                .select("run_count")
                .eq("id", schedule_id)
                .single()
                .execute()
            )
            current_count = int(schedule.data.get("run_count", 0)) if schedule.data else 0
            update_data["run_count"] = current_count + 1

        supabase.table("user_schedules").update(update_data).eq("id", schedule_id).execute()

    except Exception as e:
        print(f"    WARNING: Failed to log execution: {e}")


def _update_recurring_schedule(supabase, schedule_id: str, verbose: bool = True):
    """Calculate and queue next execution for recurring schedules."""
    try:
        schedule = (
            supabase.table("user_schedules").select("*").eq("id", schedule_id).single().execute()
        )

        if not schedule.data:
            return

        schedule_data = schedule.data

        if schedule_data.get("schedule_type") != "recurring":
            # Mark one-time schedule as completed
            supabase.table("user_schedules").update(
                {
                    "status": "completed",
                    "is_active": False,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", schedule_id).execute()

            if verbose:
                print(f"    One-time schedule {schedule_id[:8]} completed")
            return

        cron_expr = schedule_data.get("cron_expression")
        if not cron_expr:
            return

        # Calculate next run
        try:
            import pytz  # type: ignore[import-untyped]
            from croniter import croniter  # type: ignore[import-untyped]

            now = datetime.now(pytz.UTC)
            cron = croniter(cron_expr, now)
            next_run = cron.get_next(datetime)
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=pytz.UTC)
        except ImportError:
            # Fallback: schedule 24 hours later
            import pytz  # type: ignore[import-untyped]

            next_run = datetime.now(pytz.UTC) + timedelta(hours=24)

        # Update schedule
        supabase.table("user_schedules").update(
            {
                "next_run_at": next_run.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", schedule_id).execute()

        # Queue next execution
        payload = {
            "schedule_id": schedule_id,
            "chat_id": schedule_data["chat_id"],
            "topic_id": schedule_data.get("topic_id"),
            "command": schedule_data["command"],
            "user_context": schedule_data.get("user_context", {}),
        }

        supabase.table("scheduled_messages").insert(
            {
                "message_type": "user_command",
                "payload": payload,
                "scheduled_for": next_run.isoformat(),
                "created_by": schedule_data.get("created_by_email", ""),
                "status": "pending",
            }
        ).execute()

        if verbose:
            print(f"    Recurring: next run at {next_run.isoformat()}")

    except Exception as e:
        print(f"    WARNING: Failed to update recurring schedule: {e}")


def _mark_failed(supabase, msg_id: str, error: str):
    """Mark a scheduled message as failed."""
    try:
        supabase.table("scheduled_messages").update(
            {
                "status": "failed",
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "result": {"error": error},
            }
        ).eq("id", msg_id).execute()
    except Exception:
        pass


def _mark_cancelled(supabase, msg_id: str, schedule_id: str):
    """Mark a scheduled message as cancelled (parent schedule was cancelled)."""
    try:
        supabase.table("scheduled_messages").update(
            {
                "status": "cancelled",
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "result": {"reason": f"Parent schedule {schedule_id[:8]} is no longer active"},
            }
        ).eq("id", msg_id).execute()
    except Exception:
        pass


def run_daemon(poll_interval: int = 60, verbose: bool = True):
    """
    Run scheduler in daemon mode, polling continuously.

    Args:
        poll_interval: Seconds between polls
        verbose: Print progress messages
    """
    processor_id = f"scheduler-{uuid.uuid4().hex[:8]}"

    if verbose:
        print(f"Starting scheduler daemon (processor_id: {processor_id})")
        print(f"Poll interval: {poll_interval}s")
        print("Processing: broadcasts and user commands")
        print("Press Ctrl+C to stop\n")

    try:
        while True:
            try:
                # Process broadcasts
                process_pending_broadcasts(processor_id, verbose)

                # Process user commands
                process_pending_user_commands(processor_id, verbose)

            except Exception as e:
                if verbose:
                    print(f"ERROR during processing: {e}")

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        if verbose:
            print("\nScheduler stopped")


def main():
    parser = argparse.ArgumentParser(description="Broadcast scheduler background worker")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously (poll every 60s)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Poll interval in seconds (daemon mode only)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages",
    )

    args = parser.parse_args()

    processor_id = f"scheduler-{uuid.uuid4().hex[:8]}"

    if args.daemon:
        run_daemon(poll_interval=args.interval, verbose=not args.quiet)
    else:
        # Single run
        processed = process_pending_broadcasts(processor_id, verbose=not args.quiet)
        sys.exit(0 if processed >= 0 else 1)


if __name__ == "__main__":
    main()
