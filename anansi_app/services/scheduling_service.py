"""
Generic scheduling service for Anansi App.

Provides reusable scheduling functionality for broadcasts and future automated messages.
Uses the scheduled_messages table for persistent storage with atomic claim mechanism.
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase import Client, create_client


class SchedulingService:
    """Generic scheduling service for delayed message delivery."""

    def __init__(self):
        """Initialize scheduling service."""
        # Chat database credentials (with legacy fallback)
        supabase_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
        self._supabase: Optional[Client] = None
        if supabase_url and supabase_key:
            self._supabase = create_client(supabase_url, supabase_key)

    def is_configured(self) -> bool:
        """Check if service is properly configured."""
        return self._supabase is not None

    def schedule_message(
        self,
        message_type: str,
        payload: Dict[str, Any],
        scheduled_for: datetime,
        created_by: str = "",
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Schedule a message for future delivery.

        Args:
            message_type: Type of message ('broadcast', 'notification', etc.)
            payload: Message payload (flexible JSON structure)
            scheduled_for: When to send (should be UTC)
            created_by: Creator identifier

        Returns:
            Tuple of (success, message, schedule_id)
        """
        if not self._supabase:
            return False, "Service not configured", None

        try:
            # Ensure scheduled_for is in UTC
            if scheduled_for.tzinfo is None:
                scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)

            result = (
                self._supabase.table("scheduled_messages")
                .insert(
                    {
                        "message_type": message_type,
                        "payload": payload,
                        "scheduled_for": scheduled_for.isoformat(),
                        "created_by": created_by,
                        "status": "pending",
                    }
                )
                .execute()
            )

            schedule_id = result.data[0]["id"]
            return True, "Message scheduled", schedule_id

        except Exception as e:
            print(f"Error scheduling message: {e}")
            return False, f"Error: {str(e)}", None

    def cancel_scheduled(self, schedule_id: str) -> Tuple[bool, str]:
        """
        Cancel a scheduled message.

        Args:
            schedule_id: Schedule UUID

        Returns:
            Tuple of (success, message)
        """
        if not self._supabase:
            return False, "Service not configured"

        try:
            # Only cancel if still pending
            result = (
                self._supabase.table("scheduled_messages")
                .update({"status": "cancelled"})
                .eq("id", schedule_id)
                .eq("status", "pending")
                .execute()
            )

            if result.data:
                return True, "Schedule cancelled"
            else:
                return False, "Schedule not found or already processed"

        except Exception as e:
            print(f"Error cancelling schedule: {e}")
            return False, f"Error: {str(e)}"

    def get_pending_messages(
        self,
        before: Optional[datetime] = None,
        message_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get pending scheduled messages.

        Args:
            before: Only messages scheduled before this time
            message_type: Filter by message type
            limit: Maximum records

        Returns:
            List of scheduled message records
        """
        if not self._supabase:
            return []

        try:
            query = self._supabase.table("scheduled_messages").select("*").eq("status", "pending")

            if before:
                query = query.lte("scheduled_for", before.isoformat())

            if message_type:
                query = query.eq("message_type", message_type)

            result = query.order("scheduled_for", desc=False).limit(limit).execute()
            return list(result.data) if result.data else []

        except Exception as e:
            print(f"Error fetching pending messages: {e}")
            return []

    def claim_pending_messages(
        self, processor_id: str, batch_size: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Atomically claim pending messages for processing.
        Uses FOR UPDATE SKIP LOCKED to prevent race conditions.

        Args:
            processor_id: Identifier for this processor instance
            batch_size: Maximum messages to claim

        Returns:
            List of claimed message records
        """
        if not self._supabase:
            return []

        try:
            # Call RPC function for atomic claim
            result = self._supabase.rpc(
                "claim_scheduled_messages",
                {"batch_size": batch_size, "processor_id": processor_id},
            ).execute()
            return list(result.data) if result.data else []

        except Exception as e:
            print(f"Error claiming messages: {e}")

            # Fallback to non-atomic claim if RPC not available
            # This is less safe but allows operation without the function
            try:
                now = datetime.now(timezone.utc)
                pending = (
                    self._supabase.table("scheduled_messages")
                    .select("*")
                    .eq("status", "pending")
                    .lte("scheduled_for", now.isoformat())
                    .order("scheduled_for", desc=False)
                    .limit(batch_size)
                    .execute()
                )

                if not pending.data:
                    return []

                # Update status to processing
                ids = [msg["id"] for msg in pending.data]
                self._supabase.table("scheduled_messages").update(
                    {
                        "status": "processing",
                        "processed_by": processor_id,
                        "processed_at": now.isoformat(),
                    }
                ).in_("id", ids).execute()

                return list(pending.data) if pending.data else []

            except Exception as fallback_error:
                print(f"Fallback claim also failed: {fallback_error}")
                return []

    def mark_completed(
        self, schedule_id: str, result: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str]:
        """
        Mark a scheduled message as completed.

        Args:
            schedule_id: Schedule UUID
            result: Optional result data to store

        Returns:
            Tuple of (success, message)
        """
        if not self._supabase:
            return False, "Service not configured"

        try:
            update_data: Dict[str, Any] = {
                "status": "completed",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            if result:
                update_data["result"] = result

            self._supabase.table("scheduled_messages").update(update_data).eq(
                "id", schedule_id
            ).execute()
            return True, "Marked as completed"

        except Exception as e:
            print(f"Error marking completed: {e}")
            return False, f"Error: {str(e)}"

    def mark_failed(
        self,
        schedule_id: str,
        error: str,
        should_retry: bool = True,
        max_retries: int = 3,
    ) -> Tuple[bool, str]:
        """
        Mark a scheduled message as failed.

        Args:
            schedule_id: Schedule UUID
            error: Error message
            should_retry: Whether to allow retry
            max_retries: Maximum retry attempts

        Returns:
            Tuple of (success, message)
        """
        if not self._supabase:
            return False, "Service not configured"

        try:
            # Get current retry count
            current = (
                self._supabase.table("scheduled_messages")
                .select("retry_count")
                .eq("id", schedule_id)
                .single()
                .execute()
            )

            retry_count = current.data.get("retry_count", 0) + 1

            if should_retry and retry_count < max_retries:
                # Reset to pending for retry
                self._supabase.table("scheduled_messages").update(
                    {
                        "status": "pending",
                        "retry_count": retry_count,
                        "result": {"last_error": error, "retry_count": retry_count},
                    }
                ).eq("id", schedule_id).execute()
                return True, f"Queued for retry (attempt {retry_count + 1})"
            else:
                # Mark as failed permanently
                self._supabase.table("scheduled_messages").update(
                    {
                        "status": "failed",
                        "processed_at": datetime.now(timezone.utc).isoformat(),
                        "result": {"error": error, "total_attempts": retry_count},
                    }
                ).eq("id", schedule_id).execute()
                return True, "Marked as failed"

        except Exception as e:
            print(f"Error marking failed: {e}")
            return False, f"Error: {str(e)}"

    def get_schedule_history(
        self,
        message_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get scheduled message history.

        Args:
            message_type: Filter by type
            status: Filter by status
            limit: Maximum records

        Returns:
            List of scheduled message records
        """
        if not self._supabase:
            return []

        try:
            query = self._supabase.table("scheduled_messages").select("*")

            if message_type:
                query = query.eq("message_type", message_type)

            if status:
                query = query.eq("status", status)

            result = query.order("created_at", desc=True).limit(limit).execute()
            return list(result.data) if result.data else []

        except Exception as e:
            print(f"Error fetching schedule history: {e}")
            return []


__all__ = ["SchedulingService"]
