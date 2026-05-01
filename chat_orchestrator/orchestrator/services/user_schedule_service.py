"""
User Schedule Service

Manages user-created schedules for deferred or recurring command execution.
Schedules are scoped to chat_id/topic_id and execute with the creator's permissions.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from orchestrator.models.schemas import UserContext
from orchestrator.utils.cron_parser import (
    DEFAULT_TIMEZONE,
    calculate_next_run,
    format_schedule_display,
    generate_friendly_name,
    parse_time_expression,
)
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Maximum schedules per chat (rate limiting)
MAX_SCHEDULES_PER_CHAT = 20

# Minimum interval for recurring schedules (1 hour)
MIN_RECURRING_INTERVAL_HOURS = 1


class UserScheduleService:
    """
    Service for managing user-created command schedules.

    Handles creating, listing, cancelling, and executing scheduled commands.
    Integrates with the scheduled_messages table for execution queueing.
    """

    def __init__(self) -> None:
        """Initialize schedule service with Supabase client."""
        from supabase import create_client  # type: ignore[attr-defined]

        supabase_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
        supabase_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
        self._supabase: Optional[Any] = None
        if supabase_url and supabase_key:
            self._supabase = create_client(supabase_url, supabase_key)

    def is_configured(self) -> bool:
        """Check if service is properly configured."""
        return self._supabase is not None

    async def create_schedule(
        self,
        chat_id: str,
        topic_id: Optional[str],
        command: str,
        time_expression: str,
        user_context: UserContext,
        timezone_str: str = os.getenv("DEFAULT_TIMEZONE", "UTC"),
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Create a new scheduled command.

        Args:
            chat_id: Telegram chat where results will be posted
            topic_id: Optional forum topic ID
            command: Command to execute (e.g., "/tickets" or "/grid ExampleGrid")
            time_expression: Natural language time (e.g., "daily at 9am")
            user_context: Creator's context for permission preservation
            timezone_str: User's timezone (default: configured by DEFAULT_TIMEZONE env var)

        Returns:
            Tuple of (success, message, schedule_data)
        """
        if not self._supabase:
            return False, "Service not configured", None

        try:
            # Check rate limit
            count = await self._count_active_schedules(chat_id)
            if count >= MAX_SCHEDULES_PER_CHAT:
                return False, f"Maximum {MAX_SCHEDULES_PER_CHAT} schedules per chat", None

            # Parse time expression
            cron_expression, next_run_at, schedule_type = parse_time_expression(
                time_expression, timezone_str
            )

            # Validate minimum interval for recurring
            if schedule_type in ("recurring", "biweekly") and cron_expression:
                interval = self._estimate_cron_interval_hours(cron_expression)
                if interval < MIN_RECURRING_INTERVAL_HOURS:
                    return (
                        False,
                        f"Minimum recurring interval is {MIN_RECURRING_INTERVAL_HOURS} hour",
                        None,
                    )

            # Generate friendly name
            friendly_name = generate_friendly_name(command, schedule_type, time_expression)

            # Serialize user context for re-execution
            user_context_json = {
                "user_id": user_context.user_id,
                "user_email": user_context.user_email,
                "username": user_context.username,
                "source": user_context.source,
                "roles": user_context.roles,
                "organization_ids": user_context.organization_ids,
                "grid_ids": user_context.grid_ids,
                "meter_ids": user_context.meter_ids,
                "is_admin": user_context.is_admin,
                "is_staff": user_context.is_staff,
            }

            # Insert schedule
            schedule_id = str(uuid4())
            schedule_data = {
                "id": schedule_id,
                "chat_id": chat_id,
                "topic_id": topic_id,
                "created_by_user_id": user_context.user_id,
                "created_by_email": user_context.user_email,
                "organization_id": (
                    int(user_context.organization_ids[0]) if user_context.organization_ids else None
                ),
                "command": command,
                "schedule_type": schedule_type,
                "cron_expression": cron_expression,
                "timezone": timezone_str,
                "next_run_at": next_run_at.isoformat(),
                "is_active": True,
                "status": "active",
                "friendly_name": friendly_name,
                "user_context": user_context_json,
            }

            result = self._supabase.table("user_schedules").insert(schedule_data).execute()

            if not result.data:
                return False, "Failed to create schedule", None

            # Queue first execution in scheduled_messages
            await self._queue_execution(
                schedule_id=schedule_id,
                chat_id=chat_id,
                topic_id=topic_id,
                command=command,
                next_run_at=next_run_at,
                user_context_json=user_context_json,
            )

            # Format display for confirmation
            display = format_schedule_display(
                schedule_type, cron_expression, next_run_at, timezone_str
            )

            LOGGER.info(
                f"Created schedule {schedule_id}: {command} ({schedule_type}) "
                f"for chat {chat_id}, next run: {next_run_at.isoformat()}"
            )

            return (
                True,
                f"Schedule created: {display}",
                {
                    "id": schedule_id,
                    "friendly_name": friendly_name,
                    "schedule_type": schedule_type,
                    "next_run_at": next_run_at.isoformat(),
                    "display": display,
                },
            )

        except ValueError as e:
            # Time parsing error
            return False, str(e), None
        except Exception as e:
            LOGGER.error(f"Error creating schedule: {e}")
            return False, f"Error: {str(e)}", None

    async def list_schedules(
        self,
        chat_id: str,
        topic_id: Optional[str] = None,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List schedules for a chat.

        Args:
            chat_id: Telegram chat ID
            topic_id: Optional forum topic ID
            include_inactive: Whether to include paused/cancelled schedules

        Returns:
            List of schedule records with formatted display
        """
        if not self._supabase:
            return []

        try:
            query = self._supabase.table("user_schedules").select("*").eq("chat_id", chat_id)

            # Only filter by topic_id if provided
            if topic_id:
                query = query.eq("topic_id", topic_id)

            if not include_inactive:
                query = query.eq("is_active", True).eq("status", "active")

            result = query.order("created_at", desc=True).execute()
            schedules = list(result.data) if result.data else []

            # Add formatted display to each schedule
            for schedule in schedules:
                schedule["display"] = format_schedule_display(
                    schedule.get("schedule_type", "once"),
                    schedule.get("cron_expression"),
                    (
                        datetime.fromisoformat(schedule["next_run_at"].replace("Z", "+00:00"))
                        if schedule.get("next_run_at")
                        else datetime.now(timezone.utc)
                    ),
                    schedule.get("timezone", DEFAULT_TIMEZONE),
                )

            return schedules

        except Exception as e:
            LOGGER.error(f"Error listing schedules: {e}")
            return []

    async def cancel_schedule(
        self,
        schedule_id: str,
        chat_id: str,
    ) -> Tuple[bool, str]:
        """
        Cancel a schedule.

        Args:
            schedule_id: Schedule UUID
            chat_id: Chat ID for security validation

        Returns:
            Tuple of (success, message)
        """
        if not self._supabase:
            return False, "Service not configured"

        try:
            # Verify ownership via chat_id
            existing = (
                self._supabase.table("user_schedules")
                .select("id, chat_id, status, friendly_name")
                .eq("id", schedule_id)
                .single()
                .execute()
            )

            if not existing.data:
                return False, "Schedule not found"

            if existing.data.get("chat_id") != chat_id:
                return False, "Schedule not found in this chat"

            if existing.data.get("status") == "cancelled":
                return False, "Schedule already cancelled"

            # Cancel the schedule
            self._supabase.table("user_schedules").update(
                {
                    "status": "cancelled",
                    "is_active": False,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", schedule_id).execute()

            # Delete pending scheduled_messages for this schedule
            self._supabase.table("scheduled_messages").delete().eq("status", "pending").eq(
                "payload->>schedule_id", schedule_id
            ).execute()

            name = existing.data.get("friendly_name", schedule_id[:8])
            LOGGER.info(f"Cancelled schedule {schedule_id} for chat {chat_id}")
            return True, f"Cancelled: {name}"

        except Exception as e:
            LOGGER.error(f"Error cancelling schedule: {e}")
            return False, f"Error: {str(e)}"

    async def pause_schedule(
        self,
        schedule_id: str,
        chat_id: str,
    ) -> Tuple[bool, str]:
        """Pause a recurring schedule."""
        if not self._supabase:
            return False, "Service not configured"

        try:
            # Verify ownership
            existing = (
                self._supabase.table("user_schedules")
                .select("id, chat_id, status, schedule_type, friendly_name")
                .eq("id", schedule_id)
                .single()
                .execute()
            )

            if not existing.data:
                return False, "Schedule not found"

            if existing.data.get("chat_id") != chat_id:
                return False, "Schedule not found in this chat"

            if existing.data.get("status") != "active":
                return False, "Schedule is not active"

            # Pause the schedule
            self._supabase.table("user_schedules").update(
                {
                    "status": "paused",
                    "is_active": False,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", schedule_id).execute()

            name = existing.data.get("friendly_name", schedule_id[:8])
            return True, f"Paused: {name}"

        except Exception as e:
            LOGGER.error(f"Error pausing schedule: {e}")
            return False, f"Error: {str(e)}"

    async def resume_schedule(
        self,
        schedule_id: str,
        chat_id: str,
    ) -> Tuple[bool, str]:
        """Resume a paused schedule."""
        if not self._supabase:
            return False, "Service not configured"

        try:
            existing = (
                self._supabase.table("user_schedules")
                .select("*")
                .eq("id", schedule_id)
                .single()
                .execute()
            )

            if not existing.data:
                return False, "Schedule not found"

            if existing.data.get("chat_id") != chat_id:
                return False, "Schedule not found in this chat"

            if existing.data.get("status") != "paused":
                return False, "Schedule is not paused"

            # Calculate new next_run_at
            cron_expr = existing.data.get("cron_expression")
            schedule_type = existing.data.get("schedule_type", "recurring")
            if cron_expr:
                if schedule_type == "biweekly":
                    next_weekly = calculate_next_run(cron_expr)
                    next_run_at = calculate_next_run(cron_expr, after=next_weekly)
                else:
                    next_run_at = calculate_next_run(cron_expr)
            else:
                # One-time schedule - use original time if in future, else error
                original = existing.data.get("next_run_at")
                if original:
                    next_run_at = datetime.fromisoformat(original.replace("Z", "+00:00"))
                    if next_run_at <= datetime.now(timezone.utc):
                        return False, "One-time schedule has already passed"
                else:
                    return False, "Cannot resume schedule without next run time"

            # Resume the schedule
            self._supabase.table("user_schedules").update(
                {
                    "status": "active",
                    "is_active": True,
                    "next_run_at": next_run_at.isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", schedule_id).execute()

            # Queue next execution
            await self._queue_execution(
                schedule_id=schedule_id,
                chat_id=existing.data["chat_id"],
                topic_id=existing.data.get("topic_id"),
                command=existing.data["command"],
                next_run_at=next_run_at,
                user_context_json=existing.data.get("user_context", {}),
            )

            name = existing.data.get("friendly_name", schedule_id[:8])
            return True, f"Resumed: {name}"

        except Exception as e:
            LOGGER.error(f"Error resuming schedule: {e}")
            return False, f"Error: {str(e)}"

    async def log_execution(
        self,
        schedule_id: str,
        status: str,
        result_message: Optional[str] = None,
        error_message: Optional[str] = None,
        telegram_message_id: Optional[str] = None,
        execution_time_ms: Optional[int] = None,
        verification_passed: Optional[bool] = None,
        verification_feedback: Optional[str] = None,
    ) -> bool:
        """
        Log an execution result.

        Args:
            schedule_id: Parent schedule UUID
            status: 'success', 'failed', 'skipped', 'verification_failed'
            result_message: Response sent to chat
            error_message: Error if failed
            telegram_message_id: Message ID of sent response
            execution_time_ms: How long execution took
            verification_passed: LLM-as-judge result
            verification_feedback: Feedback if verification failed

        Returns:
            True if logged successfully
        """
        if not self._supabase:
            return False

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

            self._supabase.table("user_schedule_logs").insert(log_data).execute()

            # Update parent schedule
            update_data: Dict[str, Any] = {
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            # Increment run count on success
            if status == "success":
                # Get current count
                schedule = (
                    self._supabase.table("user_schedules")
                    .select("run_count")
                    .eq("id", schedule_id)
                    .single()
                    .execute()
                )
                current_count = schedule.data.get("run_count", 0) if schedule.data else 0
                update_data["run_count"] = current_count + 1

            self._supabase.table("user_schedules").update(update_data).eq(
                "id", schedule_id
            ).execute()

            return True

        except Exception as e:
            LOGGER.error(f"Error logging execution: {e}")
            return False

    async def get_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        """Get a schedule by ID."""
        if not self._supabase:
            return None

        try:
            result = (
                self._supabase.table("user_schedules")
                .select("*")
                .eq("id", schedule_id)
                .single()
                .execute()
            )
            return dict(result.data) if result.data else None
        except Exception as e:
            LOGGER.error(f"Error getting schedule: {e}")
            return None

    async def update_next_run(self, schedule_id: str) -> Optional[datetime]:
        """
        Calculate and update next_run_at for a recurring schedule.

        Args:
            schedule_id: Schedule UUID

        Returns:
            The new next_run_at datetime, or None if not recurring
        """
        if not self._supabase:
            return None

        try:
            schedule = await self.get_schedule(schedule_id)
            if not schedule:
                return None

            if schedule.get("schedule_type") not in ("recurring", "biweekly"):
                # Mark one-time schedule as completed
                self._supabase.table("user_schedules").update(
                    {
                        "status": "completed",
                        "is_active": False,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                ).eq("id", schedule_id).execute()
                return None

            cron_expr = schedule.get("cron_expression")
            if not cron_expr:
                return None

            # Biweekly: skip one cron occurrence so next run is 2 weeks out
            if schedule.get("schedule_type") == "biweekly":
                next_weekly = calculate_next_run(cron_expr)
                next_run: datetime = calculate_next_run(cron_expr, after=next_weekly)
            else:
                next_run = calculate_next_run(cron_expr)

            self._supabase.table("user_schedules").update(
                {
                    "next_run_at": next_run.isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", schedule_id).execute()

            # Queue next execution
            await self._queue_execution(
                schedule_id=schedule_id,
                chat_id=schedule["chat_id"],
                topic_id=schedule.get("topic_id"),
                command=schedule["command"],
                next_run_at=next_run,
                user_context_json=schedule.get("user_context", {}),
            )

            return next_run

        except Exception as e:
            LOGGER.error(f"Error updating next run: {e}")
            return None

    async def _queue_execution(
        self,
        schedule_id: str,
        chat_id: str,
        topic_id: Optional[str],
        command: str,
        next_run_at: datetime,
        user_context_json: Dict[str, Any],
    ) -> Optional[str]:
        """Queue an execution in the scheduled_messages table."""
        if not self._supabase:
            return None

        try:
            payload = {
                "schedule_id": schedule_id,
                "chat_id": chat_id,
                "topic_id": topic_id,
                "command": command,
                "user_context": user_context_json,
            }

            result = (
                self._supabase.table("scheduled_messages")
                .insert(
                    {
                        "message_type": "user_command",
                        "payload": payload,
                        "scheduled_for": next_run_at.isoformat(),
                        "created_by": user_context_json.get("user_email", ""),
                        "status": "pending",
                    }
                )
                .execute()
            )

            if result.data:
                return str(result.data[0]["id"])
            return None

        except Exception as e:
            LOGGER.error(f"Error queueing execution: {e}")
            return None

    async def _count_active_schedules(self, chat_id: str) -> int:
        """Count active schedules for rate limiting."""
        if not self._supabase:
            return 0

        try:
            result = (
                self._supabase.table("user_schedules")
                .select("id", count="exact")
                .eq("chat_id", chat_id)
                .eq("is_active", True)
                .execute()
            )
            return result.count if result.count else 0
        except Exception as e:
            LOGGER.error(f"Error counting schedules: {e}")
            return 0

    def _estimate_cron_interval_hours(self, cron_expression: str) -> float:
        """Estimate the interval in hours for a cron expression."""
        parts = cron_expression.split()
        if len(parts) < 5:
            return 24  # Default to daily if invalid

        minute, hour, day, month, weekday = parts[:5]

        # Hourly check (*/N in hour field)
        if "/" in hour:
            interval = int(hour.split("/")[1])
            return interval

        # Every hour
        if hour == "*":
            return 1

        # Daily (specific hour, every day)
        if day == "*" and month == "*" and weekday == "*":
            return 24

        # Weekly (specific weekday)
        if weekday != "*":
            return 24 * 7

        return 24  # Default to daily


__all__ = ["UserScheduleService"]
