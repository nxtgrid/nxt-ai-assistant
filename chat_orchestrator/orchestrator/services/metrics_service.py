"""
Weekly Metrics and Analytics Service

Posts weekly statistics to the escalations Telegram group every Monday:
- Users: Unique users of the bot during the week
- Messages: Total user messages sent during the week
- Escalations: Number of issues escalated by the bot to human support
- Likes: Count of thumbs up feedback given
- Dislikes: Count of thumbs down feedback given
"""

from __future__ import annotations

import os
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from orchestrator.services.escalation_service import EscalationService
from orchestrator.services.supabase_client import SupabaseClient
from shared.auth.auth_service import AuthService, get_auth_service
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class MetricsService:
    """Service for collecting and posting weekly metrics."""

    def __init__(
        self,
        supabase_client: Optional[SupabaseClient] = None,
        escalation_service: Optional[EscalationService] = None,
        auth_service: Optional[AuthService] = None,
    ):
        """
        Initialize metrics service.

        Args:
            supabase_client: Supabase client for database queries
            escalation_service: Escalation service for posting to Telegram
            auth_service: Auth service for user/org name lookups
        """
        # Initialize Supabase client
        if supabase_client:
            self._supabase = supabase_client
        else:
            supabase_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
            supabase_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
            if supabase_url and supabase_key:
                self._supabase = SupabaseClient(url=supabase_url, key=supabase_key)
            else:
                self._supabase = None
                LOGGER.warning("Supabase credentials not configured for metrics service")

        # Initialize escalation service
        if escalation_service:
            self._escalation = escalation_service
        else:
            self._escalation = EscalationService()

        # Use singleton auth service for name lookups
        if auth_service:
            self._auth_service = auth_service
        else:
            try:
                self._auth_service = get_auth_service()
            except Exception as e:
                LOGGER.warning(f"Auth service not available for metrics: {e}")
                self._auth_service = None

    def is_enabled(self) -> bool:
        """Check if metrics service is properly configured."""
        metrics_enabled = os.getenv("METRICS_ENABLED", "true").lower() == "true"
        return metrics_enabled and self._supabase is not None and self._escalation.is_enabled()

    async def _get_excluded_chat_ids(self) -> set:
        """
        Get set of chat IDs to exclude from metrics:
        - Escalation group chat ID
        - Organization developer group chat IDs

        Returns:
            Set of chat_ids to exclude
        """
        excluded = set()

        # Add escalation group chat ID
        escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
        if escalation_chat_id:
            excluded.add(escalation_chat_id)

        LOGGER.debug(f"Excluding {len(excluded)} chat IDs from metrics: {excluded}")
        return excluded

    async def collect_metrics_for_date(
        self, target_date: datetime, end_date: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Collect per-user metrics for a specific date or date range.

        Args:
            target_date: The start date to collect metrics for (timezone-aware)
            end_date: Optional end date for range. If None, collects just target_date.

        Returns:
            Dict with structure:
            {
                "start_date": target_date,
                "end_date": end_date or target_date,
                "per_user": {
                    "chat_id": {
                        "user_name": "Name or chat_id",
                        "messages": 3,
                        "escalations": 1,
                        "likes": 0,
                        "dislikes": 2
                    }
                }
            }
        """
        if not self._supabase:
            raise ValueError("Supabase client not configured")

        # Calculate start and end of the date range (in UTC)
        start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        if end_date:
            # End at the end of end_date
            end_of_range = end_date.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                days=1
            )
        else:
            # Single day: end at the next day
            end_of_range = start_of_day + timedelta(days=1)

        LOGGER.info(
            f"Collecting per-user metrics from {start_of_day.date()} to {end_of_range.date()} "
            f"(from {start_of_day.isoformat()} to {end_of_range.isoformat()})"
        )

        client = self._supabase._get_client()

        # Get excluded chat IDs (escalation group, org groups)
        excluded_chat_ids = await self._get_excluded_chat_ids()

        # Initialize per-user metrics dictionary
        user_metrics: Dict[str, Dict[str, Any]] = {}

        # Track which chat_ids are groups (for icon display)
        # Groups start with -100 or 100 (Telegram supergroup/group IDs)
        group_chat_ids: set = set()

        # 1. Get all messages with from_chat_id and session_id for the date
        try:
            messages_response = (
                client.table("chat_messages")
                .select("from_chat_id, session_id")
                .eq("role", "user")
                .gte("created_at", start_of_day.isoformat())
                .lt("created_at", end_of_range.isoformat())
                .execute()
            )

            # Group by chat_id and count unique sessions (issues) per user
            for msg in messages_response.data or []:
                chat_id = msg.get("from_chat_id") or "unknown"
                session_id = msg.get("session_id")

                # Skip excluded chat IDs
                if chat_id in excluded_chat_ids:
                    continue

                if chat_id not in user_metrics:
                    user_metrics[chat_id] = {
                        "user_name": chat_id,  # Will be replaced with lookup
                        "messages": 0,  # Count total messages
                        "escalations": 0,
                        "likes": 0,
                        "dislikes": 0,
                        "response_times": [],  # List of response times in seconds
                    }
                    # Track if this is a group chat (for icon display)
                    if chat_id.startswith("-100") or chat_id.startswith("100"):
                        group_chat_ids.add(chat_id)

                user_metrics[chat_id]["messages"] += 1

            LOGGER.info(
                f"Found {len(user_metrics)} unique users with {len(messages_response.data or [])} messages"
            )
        except Exception as e:
            LOGGER.error(f"Error collecting user messages: {e}")

        # 2. Get escalations per user (if table exists)
        try:
            # Get all escalations with customer_chat_id directly
            escalations_response = (
                client.table("escalation_mappings")
                .select("customer_chat_id")
                .gte("created_at", start_of_day.isoformat())
                .lt("created_at", end_of_range.isoformat())
                .execute()
            )

            # Count escalations per user
            for row in escalations_response.data or []:
                chat_id = row.get("customer_chat_id") or "unknown"

                # Skip excluded chat IDs
                if chat_id in excluded_chat_ids:
                    continue

                # Initialize user if not exists (for cases where user only has escalations)
                if chat_id not in user_metrics:
                    user_metrics[chat_id] = {
                        "user_name": chat_id,  # Will be replaced with lookup
                        "messages": 0,  # Count total messages
                        "escalations": 0,
                        "likes": 0,
                        "dislikes": 0,
                        "response_times": [],
                    }

                user_metrics[chat_id]["escalations"] += 1

            escalation_count = len(escalations_response.data or [])
            LOGGER.info(f"Escalation query returned {escalation_count} records")
            if escalation_count > 0:
                LOGGER.info(f"Escalation data: {escalations_response.data}")

        except Exception as e:
            if "Could not find the table" in str(e):
                LOGGER.debug("escalation_mappings table not found, skipping escalations")
            else:
                LOGGER.error(f"Error counting escalations per user: {e}")

        # 3. Get likes and dislikes per user from chat_messages metadata
        # NOTE: Feedback is given AFTER the message is created, so we query messages
        # from a wider time window and filter feedback by the timestamp field.
        try:
            # Query model messages that have feedback (from last 30 days to catch old messages)
            # We'll filter by feedback timestamp, not message created_at
            feedback_window_start = start_of_day - timedelta(days=30)
            feedback_response = (
                client.table("chat_messages")
                .select("session_id, metadata, created_at")
                .eq("role", "model")
                .not_.is_("metadata->feedback", "null")
                .gte("created_at", feedback_window_start.isoformat())
                .execute()
            )

            LOGGER.info(
                f"Feedback query returned {len(feedback_response.data or [])} messages with feedback"
            )

            # Get session_ids that have feedback given on the target date
            feedback_sessions_with_date: set = set()
            messages_with_valid_feedback: list = []

            for row in feedback_response.data or []:
                metadata = row.get("metadata") or {}
                feedback = metadata.get("feedback")
                if not feedback:
                    continue

                # Handle both array (new format) and single object (legacy)
                feedback_list = feedback if isinstance(feedback, list) else [feedback]

                # Check if any feedback entry is from the target date
                has_feedback_on_target_date = False
                for fb in feedback_list:
                    if not isinstance(fb, dict):
                        continue
                    fb_timestamp_str = fb.get("timestamp")
                    if fb_timestamp_str:
                        try:
                            # Parse feedback timestamp and check if it's on the target date
                            fb_timestamp = datetime.fromisoformat(
                                fb_timestamp_str.replace("Z", "+00:00")
                            )
                            if start_of_day <= fb_timestamp < end_of_range:
                                has_feedback_on_target_date = True
                                break
                        except (ValueError, TypeError):
                            # If timestamp parsing fails, skip this feedback entry
                            continue
                    else:
                        # Legacy feedback without timestamp - fall back to message date check
                        # (this maintains backwards compatibility)
                        msg_created = row.get("created_at")
                        if msg_created:
                            try:
                                msg_time = datetime.fromisoformat(
                                    msg_created.replace("Z", "+00:00")
                                )
                                if start_of_day <= msg_time < end_of_range:
                                    has_feedback_on_target_date = True
                                    break
                            except (ValueError, TypeError):
                                continue

                if has_feedback_on_target_date:
                    feedback_sessions_with_date.add(row.get("session_id"))
                    messages_with_valid_feedback.append(row)

            feedback_sessions = list(feedback_sessions_with_date)

            if feedback_sessions:
                # Query user messages for those sessions to find the chat_id
                feedback_user_messages = (
                    client.table("chat_messages")
                    .select("from_chat_id, session_id")
                    .in_("session_id", feedback_sessions)
                    .eq("role", "user")
                    .execute()
                )

                # Map session_id -> chat_id
                session_to_chat = {}
                for msg in feedback_user_messages.data or []:
                    session_id = msg.get("session_id")
                    chat_id = msg.get("from_chat_id") or "unknown"
                    if session_id:
                        session_to_chat[session_id] = chat_id

                # Count likes/dislikes per user (only feedback from target date)
                for row in messages_with_valid_feedback:
                    session_id = row.get("session_id")
                    metadata = row.get("metadata") or {}
                    feedback = metadata.get("feedback")

                    if not feedback:
                        continue

                    # Handle both array (new) and single object (legacy) formats
                    feedback_list = feedback if isinstance(feedback, list) else [feedback]

                    chat_id = session_to_chat.get(session_id, "unknown")

                    # Skip excluded chat IDs
                    if chat_id in excluded_chat_ids:
                        continue

                    # Initialize user if not exists (for cases where user only has feedback)
                    if chat_id not in user_metrics:
                        user_metrics[chat_id] = {
                            "user_name": chat_id,  # Will be replaced with lookup
                            "messages": 0,  # Count total messages
                            "escalations": 0,
                            "likes": 0,
                            "dislikes": 0,
                            "response_times": [],
                        }
                        # Track if this is a group chat
                        if chat_id.startswith("-100") or chat_id.startswith("100"):
                            group_chat_ids.add(chat_id)

                    # Count each feedback entry from the target date only
                    for fb in feedback_list:
                        if not isinstance(fb, dict):
                            continue

                        # Check if this specific feedback is from target date
                        fb_timestamp_str = fb.get("timestamp")
                        is_from_target_date = False
                        if fb_timestamp_str:
                            try:
                                fb_timestamp = datetime.fromisoformat(
                                    fb_timestamp_str.replace("Z", "+00:00")
                                )
                                is_from_target_date = start_of_day <= fb_timestamp < end_of_range
                            except (ValueError, TypeError):
                                continue
                        else:
                            # Legacy feedback without timestamp - include if message is from target date
                            is_from_target_date = True  # Already filtered above

                        if not is_from_target_date:
                            continue

                        feedback_type = fb.get("type")
                        if feedback_type == "thumbs_up":
                            user_metrics[chat_id]["likes"] += 1
                        elif feedback_type == "thumbs_down":
                            user_metrics[chat_id]["dislikes"] += 1

            # Log feedback totals for debugging
            total_likes = sum(u.get("likes", 0) for u in user_metrics.values())
            total_dislikes = sum(u.get("dislikes", 0) for u in user_metrics.values())
            LOGGER.info(
                f"Feedback counting complete: {len(messages_with_valid_feedback)} messages "
                f"with feedback on target date, {total_likes} likes, {total_dislikes} dislikes"
            )

        except Exception as e:
            LOGGER.error(f"Error counting feedback per user: {e}")

        # 4. Calculate response times per user
        try:
            # Query all messages (user and model) with timestamps for the date
            response_time_query = (
                client.table("chat_messages")
                .select("session_id, from_chat_id, role, created_at")
                .in_("role", ["user", "model"])
                .gte("created_at", start_of_day.isoformat())
                .lt("created_at", end_of_range.isoformat())
                .order("created_at")
                .execute()
            )

            # Group messages by session
            session_messages: Dict[str, List[Dict[str, Any]]] = {}
            for msg in response_time_query.data or []:
                session_id = msg.get("session_id")
                if session_id:
                    if session_id not in session_messages:
                        session_messages[session_id] = []
                    session_messages[session_id].append(msg)

            # Calculate response times for each session
            for session_id, messages in session_messages.items():
                # Find the chat_id for this session (from user messages)
                session_chat_id = None
                for msg in messages:
                    if msg.get("role") == "user" and msg.get("from_chat_id"):
                        session_chat_id = msg.get("from_chat_id")
                        break

                if not session_chat_id or session_chat_id in excluded_chat_ids:
                    continue

                # Pair user messages with following model responses
                for i, msg in enumerate(messages):
                    if msg.get("role") == "user":
                        # Find next model message
                        for j in range(i + 1, len(messages)):
                            if messages[j].get("role") == "model":
                                # Calculate response time
                                user_time = datetime.fromisoformat(
                                    msg["created_at"].replace("Z", "+00:00")
                                )
                                model_time = datetime.fromisoformat(
                                    messages[j]["created_at"].replace("Z", "+00:00")
                                )
                                response_time = (model_time - user_time).total_seconds()

                                # Only count reasonable response times (0-10 min)
                                if 0 < response_time < 600:
                                    if session_chat_id in user_metrics:
                                        user_metrics[session_chat_id]["response_times"].append(
                                            response_time
                                        )
                                break

            # Calculate median for each user
            for chat_id, data in user_metrics.items():
                times = data.get("response_times", [])
                if times:
                    data["median_response_time"] = statistics.median(times)
                else:
                    data["median_response_time"] = None
                # Remove the raw list to keep result clean
                del data["response_times"]

            LOGGER.info(
                f"Calculated response times for {len([u for u in user_metrics.values() if u.get('median_response_time')])} users"
            )

        except Exception as e:
            LOGGER.error(f"Error calculating response times: {e}")
            # Ensure all users have the field even on error
            for data in user_metrics.values():
                if "response_times" in data:
                    del data["response_times"]
                if "median_response_time" not in data:
                    data["median_response_time"] = None

        # 5. Get scheduled messages delivered per user
        try:
            # Query user_schedule_logs with status='success' for the date
            # Join with user_schedules to get chat_id
            schedule_logs_response = (
                client.table("user_schedule_logs")
                .select("schedule_id, status, executed_at")
                .eq("status", "success")
                .gte("executed_at", start_of_day.isoformat())
                .lt("executed_at", end_of_range.isoformat())
                .execute()
            )

            if schedule_logs_response.data:
                # Get unique schedule_ids
                schedule_ids = list(
                    set(
                        log.get("schedule_id")
                        for log in schedule_logs_response.data
                        if log.get("schedule_id")
                    )
                )

                if schedule_ids:
                    # Look up chat_id for each schedule
                    schedules_response = (
                        client.table("user_schedules")
                        .select("id, chat_id")
                        .in_("id", schedule_ids)
                        .execute()
                    )

                    # Map schedule_id -> chat_id
                    schedule_to_chat = {
                        s["id"]: s["chat_id"]
                        for s in (schedules_response.data or [])
                        if s.get("id") and s.get("chat_id")
                    }

                    # Count delivered schedules per chat_id
                    for log in schedule_logs_response.data:
                        schedule_id = log.get("schedule_id")
                        chat_id = schedule_to_chat.get(schedule_id)

                        if not chat_id or chat_id in excluded_chat_ids:
                            continue

                        # Initialize user if not exists
                        if chat_id not in user_metrics:
                            user_metrics[chat_id] = {
                                "user_name": chat_id,
                                "messages": 0,
                                "escalations": 0,
                                "likes": 0,
                                "dislikes": 0,
                                "scheduled": 0,
                                "response_times": [],
                            }
                            if chat_id.startswith("-100") or chat_id.startswith("100"):
                                group_chat_ids.add(chat_id)

                        # Ensure scheduled field exists for existing users
                        if "scheduled" not in user_metrics[chat_id]:
                            user_metrics[chat_id]["scheduled"] = 0

                        user_metrics[chat_id]["scheduled"] += 1

                    delivered_count = len(schedule_logs_response.data)
                    LOGGER.info(f"Found {delivered_count} delivered scheduled messages")

        except Exception as e:
            if "Could not find" in str(e) or "relation" in str(e).lower():
                LOGGER.debug("user_schedule_logs table not found, skipping scheduled metrics")
            else:
                LOGGER.error(f"Error counting scheduled messages: {e}")

        # Ensure all users have the scheduled field
        for chat_id in user_metrics:
            if "scheduled" not in user_metrics[chat_id]:
                user_metrics[chat_id]["scheduled"] = 0

        # 6. Get total token usage (input + output) across all conversations
        total_input_tokens = 0
        total_output_tokens = 0
        try:
            token_response = (
                client.table("chat_messages")
                .select("metadata")
                .eq("role", "model")
                .not_.is_("metadata->input_tokens", "null")
                .gte("created_at", start_of_day.isoformat())
                .lt("created_at", end_of_range.isoformat())
                .execute()
            )

            for row in token_response.data or []:
                metadata = row.get("metadata") or {}
                total_input_tokens += metadata.get("input_tokens", 0)
                total_output_tokens += metadata.get("output_tokens", 0)

            LOGGER.info(
                f"Token usage: {total_input_tokens:,} input, {total_output_tokens:,} output"
            )
        except Exception as e:
            LOGGER.error(f"Error collecting token usage: {e}")

        # 7. Look up user names for all chat_ids using batch lookup (fail-safe)
        all_chat_ids = list(user_metrics.keys())
        name_map: Dict[str, str] = {}
        if self._auth_service and all_chat_ids:
            try:
                name_map = await self._auth_service.batch_lookup_display_names(all_chat_ids)  # type: ignore[attr-defined]
                LOGGER.info(
                    f"Batch lookup resolved {len(name_map)} names from {len(all_chat_ids)} IDs"
                )
            except Exception as e:
                LOGGER.warning(f"Batch name lookup failed, falling back to IDs: {e}")

        # Log name lookup results for debugging
        LOGGER.info(f"Name lookup: {len(all_chat_ids)} IDs -> {len(name_map)} names resolved")
        for cid in all_chat_ids:
            if cid not in name_map:
                LOGGER.warning(f"No name found for chat_id: {cid}")

        for chat_id in user_metrics.keys():
            # Use looked-up name or fall back to chat_id
            user_metrics[chat_id]["user_name"] = name_map.get(chat_id, chat_id)

        result = {
            "start_date": target_date,
            "end_date": end_date or target_date,
            "per_user": user_metrics,
            "group_chat_ids": group_chat_ids,  # Set of chat_ids that are groups (for icon display)
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
        }

        date_range_str = (
            f"{target_date.date()} to {end_date.date()}" if end_date else f"{target_date.date()}"
        )
        LOGGER.info(f"Collected per-user metrics for {date_range_str}: {len(user_metrics)} users")
        return result

    def format_metrics_table(self, metrics: Dict[str, Any], date: datetime) -> str:
        """
        Format per-user metrics in a mobile-friendly format.

        Args:
            metrics: Dict with per-user metrics from collect_metrics_for_date
            date: The date these metrics are for

        Returns:
            Formatted metrics as string optimized for mobile display
        """
        per_user = metrics.get("per_user", {})
        group_chat_ids = metrics.get("group_chat_ids", set())

        # Initialize totals
        total_messages = 0
        total_escalations = 0
        total_likes = 0
        total_dislikes = 0
        total_scheduled = 0

        # Collect response times for overall median
        all_response_times: List[float] = []

        # Add metric descriptions at the top
        lines = [
            "_Messages: User messages sent_",
            "_Escalations: Requests sent to support team_",
            "_📅: Scheduled messages delivered_",
            "_👍 👎: Feedback reactions_",
            "_⏱️: Median response time_",
            "_🔤: Gemini token usage (input / output)_",
            "",  # Empty line for spacing
        ]

        # Build lines for each user (sorted by user name)
        sorted_users = sorted(per_user.items(), key=lambda x: x[1]["user_name"].lower())

        for chat_id, user_data in sorted_users:
            # Format: 👤/👥 Name: 5 messages, 2 escalations, 1 👍, 3 👎
            # Use group icon (👥) for group chats, person icon (👤) for individuals
            icon = "👥" if chat_id in group_chat_ids else "👤"
            user_line = f"{icon} {user_data['user_name']}: {user_data['messages']} messages"

            # Add optional metrics only if non-zero
            extras = []
            if user_data["escalations"] > 0:
                extras.append(f"{user_data['escalations']} escalations")
            if user_data.get("scheduled", 0) > 0:
                extras.append(f"{user_data['scheduled']} 📅")
            if user_data["likes"] > 0:
                extras.append(f"{user_data['likes']} 👍")
            if user_data["dislikes"] > 0:
                extras.append(f"{user_data['dislikes']} 👎")

            # Add response time if available
            median_rt = user_data.get("median_response_time")
            if median_rt is not None:
                extras.append(f"⏱️ {median_rt:.1f}s")
                all_response_times.append(median_rt)

            if extras:
                user_line += ", " + ", ".join(extras)

            lines.append(user_line)

            # Accumulate totals
            total_messages += user_data["messages"]
            total_escalations += user_data["escalations"]
            total_scheduled += user_data.get("scheduled", 0)
            total_likes += user_data["likes"]
            total_dislikes += user_data["dislikes"]

        # Add separator if there are users
        if lines:
            lines.append("─" * 30)

        # Always add total line (even if zero activity)
        total_line = (
            f"📊 TOTAL: {total_messages} messages, "
            f"{total_escalations} escalations, "
            f"{total_scheduled} 📅, "
            f"{total_likes} 👍, {total_dislikes} 👎"
        )
        # Add overall median response time if available
        if all_response_times:
            overall_median = statistics.median(all_response_times)
            total_line += f", ⏱️ {overall_median:.1f}s"
        lines.append(total_line)

        # Add token usage summary if available
        total_input_tokens = metrics.get("total_input_tokens", 0)
        total_output_tokens = metrics.get("total_output_tokens", 0)
        if total_input_tokens or total_output_tokens:
            lines.append(f"🔤 Tokens: {total_input_tokens:,} in / {total_output_tokens:,} out")

        return "\n".join(lines)

    async def post_metrics_to_telegram(
        self, metrics: Dict[str, Any], date: datetime, is_weekly: bool = False
    ) -> Dict[str, Any]:
        """
        Post metrics table to Telegram escalation group.

        Args:
            metrics: Dict with per-user metrics from collect_metrics_for_date
            date: The primary date for display (start_date for ranges)
            is_weekly: If True, format as weekly report with date range

        Returns:
            Dict with success status
        """
        if not self._escalation.is_enabled():
            return {"success": False, "error": "Escalation service not enabled"}

        try:
            # Format table
            table_str = self.format_metrics_table(metrics, date)

            # Build message with appropriate title
            if is_weekly:
                start_date = metrics.get("start_date", date)
                end_date = metrics.get("end_date", date)
                date_range = f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
                message = f"📊 *Weekly Statistics - {date_range}*\n\n{table_str}"
            else:
                message = f"📊 *Daily Statistics - {date.strftime('%Y-%m-%d')}*\n\n{table_str}"

            # Send to escalation group
            result = await self._escalation._send_telegram_message(
                chat_id=self._escalation._escalation_chat_id,
                text=message,
            )

            period = "weekly" if is_weekly else "daily"
            if result.get("ok"):
                LOGGER.info(f"Posted {period} metrics for {date.date()} to Telegram")
                return {"success": True}
            else:
                LOGGER.error(f"Failed to post metrics: {result}")
                return {"success": False, "error": result.get("description", "Unknown error")}

        except Exception as e:
            LOGGER.exception(f"Error posting metrics to Telegram: {e}")
            return {"success": False, "error": str(e)}

    async def send_daily_metrics(self) -> Dict[str, Any]:
        """
        Collect and send metrics for the previous day.

        This is the main function called by the scheduler at 9 AM daily.

        Returns:
            Dict with success status
        """
        if not self.is_enabled():
            LOGGER.warning("Metrics service is not enabled or not configured")
            return {"success": False, "error": "Metrics service not enabled"}

        try:
            # Get yesterday's date in UTC
            now = datetime.now(timezone.utc)
            yesterday = now - timedelta(days=1)

            # Collect metrics
            metrics = await self.collect_metrics_for_date(yesterday)

            # Post to Telegram
            result = await self.post_metrics_to_telegram(metrics, yesterday)

            return result

        except Exception as e:
            LOGGER.exception(f"Error sending daily metrics: {e}")
            return {"success": False, "error": str(e)}

    async def send_weekly_metrics(self) -> Dict[str, Any]:
        """
        Collect and send metrics for the previous week.

        This is the main function called by the scheduler every Monday.
        Collects data from Monday through Sunday of the previous week.

        Returns:
            Dict with success status
        """
        if not self.is_enabled():
            LOGGER.warning("Metrics service is not enabled or not configured")
            return {"success": False, "error": "Metrics service not enabled"}

        try:
            # Get the previous week's Monday through Sunday
            now = datetime.now(timezone.utc)
            # today is Monday, so go back 7 days to get last Monday
            last_monday = now - timedelta(days=7)
            last_sunday = now - timedelta(days=1)

            LOGGER.info(
                f"Collecting weekly metrics from {last_monday.date()} to {last_sunday.date()}"
            )

            # Collect metrics for the full week
            metrics = await self.collect_metrics_for_date(last_monday, end_date=last_sunday)

            # Post to Telegram with weekly formatting
            result = await self.post_metrics_to_telegram(metrics, last_monday, is_weekly=True)

            return result

        except Exception as e:
            LOGGER.exception(f"Error sending weekly metrics: {e}")
            return {"success": False, "error": str(e)}

    async def send_metrics_for_date(self, target_date: datetime) -> Dict[str, Any]:
        """
        Collect and send metrics for a specific date (for testing).

        Args:
            target_date: The date to collect metrics for

        Returns:
            Dict with success status
        """
        if not self.is_enabled():
            LOGGER.warning("Metrics service is not enabled or not configured")
            return {"success": False, "error": "Metrics service not enabled"}

        try:
            # Ensure timezone-aware
            if target_date.tzinfo is None:
                target_date = target_date.replace(tzinfo=timezone.utc)

            # Collect metrics
            metrics = await self.collect_metrics_for_date(target_date)

            # Post to Telegram
            result = await self.post_metrics_to_telegram(metrics, target_date)

            return result

        except Exception as e:
            LOGGER.exception(f"Error sending metrics for {target_date}: {e}")
            return {"success": False, "error": str(e)}


__all__ = ["MetricsService"]
