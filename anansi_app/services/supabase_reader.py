"""
Read-only Supabase service for querying chat history.

Provides access to chat messages, sessions, and user information
without any write capabilities.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import streamlit as st
from supabase import Client, create_client  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)


def _merge_undifferentiated_group_topics(context_list: list[dict]) -> list[dict]:
    """Merge group entries that can't be distinguished by topic name.

    When a Telegram forum group has multiple topics but none of their
    display names contain a topic suffix (no " / TopicName"), they all
    show as e.g. "MySite" or "MySite · MySite". Collapse these
    into a single sidebar entry with aggregated token counts.

    Entries WITH distinct topic names (e.g., "O&M / SiteAlpha" vs
    "O&M / SiteBeta") are kept separate — they represent genuinely
    different topics with known names.
    """
    from collections import defaultdict

    # Group all entries by telegram_chat_id
    groups: dict[str, list[int]] = defaultdict(list)  # chat_id → [indices]
    for i, ctx in enumerate(context_list):
        chat_id = ctx.get("telegram_chat_id", "")
        if ctx.get("is_group") and chat_id:
            groups[chat_id].append(i)

    # Identify which indices to merge (groups with multiple entries, none
    # having a topic name in the display)
    indices_to_merge: set[int] = set()
    merge_targets: dict[str, int] = {}  # chat_id → index of the entry to keep

    for chat_id, idx_list in groups.items():
        if len(idx_list) <= 1:
            continue

        # Check if ALL entries for this group lack a topic name
        has_topic_names = any(" / " in context_list[i].get("display_name", "") for i in idx_list)
        if has_topic_names:
            # Some have topic names — keep them separate (they're distinguishable)
            continue

        # All entries are indistinguishable — merge into the first one
        keep_idx = idx_list[0]
        merge_targets[chat_id] = keep_idx
        for merge_idx in idx_list[1:]:
            indices_to_merge.add(merge_idx)

    if not indices_to_merge:
        return context_list

    # Build merged list
    result = []
    for i, ctx in enumerate(context_list):
        if i in indices_to_merge:
            # Find the target and aggregate into it
            chat_id = ctx.get("telegram_chat_id", "")
            target_idx = merge_targets[chat_id]
            target = context_list[target_idx]
            target["message_count"] += ctx["message_count"]
            target["input_tokens"] += ctx["input_tokens"]
            target["output_tokens"] += ctx["output_tokens"]
            if ctx["last_message"] > target["last_message"]:
                target["last_message"] = ctx["last_message"]
        else:
            result.append(ctx)

    return result


class SupabaseReader:
    """Read-only access to chat history database."""

    def __init__(self):
        """Initialize Supabase clients for chat and auth databases."""
        # Main database (chat messages) - with legacy fallback
        self.supabase_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL")
        self.supabase_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

        if self.supabase_url and self.supabase_key:
            self.client: Client = create_client(self.supabase_url, self.supabase_key)
        else:
            self.client = None

    def is_configured(self) -> bool:
        """Check if database connections are configured."""
        return self.client is not None

    @st.cache_data(ttl=300, show_spinner=False)  # Cache for 5 minutes
    def get_chat_contexts(_self, user_email: str, days_back: int = 30) -> List[Dict[str, Any]]:
        """
        Get all unique chat contexts (groups and individual users) with recent activity.

        Args:
            user_email: Email of authenticated user (for cache isolation)
            days_back: Number of days to look back for activity

        Returns:
            List of chat contexts with metadata

        Note:
            user_email is included in cache key to prevent data leakage between users.
            Each user gets their own cached results to protect PII.
        """
        if not _self.client:
            logger.warning("Supabase client not configured")
            return []

        try:
            # Calculate date threshold
            since = datetime.utcnow() - timedelta(days=days_back)

            # Query for unique chat contexts
            # Join with chat_sessions to get session_id and telegram_chat_id
            # Include metadata for token tracking
            # Explicit limit to avoid Supabase PostgREST default (~1000 rows),
            # which silently drops messages from less-active users.
            response = (
                _self.client.table("chat_messages")
                .select(
                    "id, role, content, function_call, from_chat_id, group_id, created_at, metadata, "
                    "chat_sessions!inner(session_id, telegram_chat_id, telegram_topic_id)"
                )
                .gte("created_at", since.isoformat())
                .order("created_at", desc=True)
                .limit(10000)
                .execute()
            )

            logger.debug("Found %d messages in last %d days", len(response.data), days_back)

            # Group by chat context
            contexts = {}
            telegram_ids = set()  # Collect all telegram IDs for batch lookup (users and groups)

            for row in response.data:
                # Get session_id from the joined chat_sessions table
                session_data = row.get("chat_sessions")
                if not session_data:
                    continue

                session_id = session_data.get("session_id", "")
                chat_id = session_id if session_id else "Unknown"

                group_id = row.get("group_id")

                # Use telegram_chat_id column (preserved after session_id hashing)
                telegram_chat_id = session_data.get("telegram_chat_id", "")
                # Fallback to parsing session_id for legacy data
                if not telegram_chat_id and session_id.startswith("telegram_"):
                    telegram_chat_id = session_id.replace("telegram_", "")
                # Groups have telegram IDs starting with "-100" or "100"
                is_group = (
                    telegram_chat_id.startswith("-100") or telegram_chat_id.startswith("100")
                    if telegram_chat_id
                    else False
                )

                # Create unique key for this context.
                # For forum-style groups: separate by topic_id so each grid
                # topic gets its own sidebar entry. For non-forum groups or
                # groups without topic_id: aggregate all sessions together.
                telegram_topic_id = session_data.get("telegram_topic_id")
                if is_group and telegram_chat_id:
                    base_group_id = telegram_chat_id.split("_")[0]
                    if telegram_topic_id:
                        # Forum group with topic → separate entry per topic
                        context_key = f"group_{base_group_id}_topic_{telegram_topic_id}"
                    else:
                        # Non-forum group or no topic → aggregate
                        context_key = f"group_{base_group_id}"
                else:
                    context_key = f"{session_id}_{group_id or 'direct'}"

                if context_key not in contexts:
                    contexts[context_key] = {
                        "chat_id": chat_id,
                        "group_id": None if is_group else group_id,
                        "is_group": is_group,
                        "last_message": row["created_at"],
                        "message_count": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "display_name": None,
                        "telegram_chat_id": telegram_chat_id,
                        "telegram_topic_id": telegram_topic_id,
                    }

                    # Collect telegram_id for batch lookup
                    if telegram_chat_id:
                        telegram_ids.add(telegram_chat_id)

                # Only count visible conversation messages (user + bot text responses)
                # Must match _is_internal_message() in conversation_view.py
                role = row.get("role", "")
                if role == "tool":
                    pass  # Tool results are internal, don't count
                elif role == "user":
                    metadata = row.get("metadata") or {}
                    if isinstance(metadata, dict) and metadata.get("message_type") in (
                        "command_result",
                        "scheduled",
                    ):
                        pass  # System instruction / prompt template, don't count
                    else:
                        contexts[context_key]["message_count"] += 1
                elif role == "model":
                    # Skip tool calls with no text content (internal to LLM)
                    if row.get("function_call") and not row.get("content"):
                        pass
                    else:
                        contexts[context_key]["message_count"] += 1

                # Aggregate token usage from metadata
                metadata = row.get("metadata")
                if metadata and isinstance(metadata, dict):
                    contexts[context_key]["input_tokens"] += metadata.get("input_tokens", 0)
                    contexts[context_key]["output_tokens"] += metadata.get("output_tokens", 0)

            logger.debug("Created %d unique chat contexts", len(contexts))

            # Convert to list and sort by last message
            context_list = sorted(contexts.values(), key=lambda x: x["last_message"], reverse=True)

            # Batch lookup user names
            import asyncio

            try:
                user_name_map = asyncio.run(_self._batch_lookup_user_names(list(telegram_ids)))
                logger.debug("Batch lookup returned %d user names", len(user_name_map))
            except Exception as e:
                logger.error("Batch lookup failed: %s", e)
                import traceback

                traceback.print_exc()
                user_name_map = {}

            # Enrich with user names using cached map
            for context in context_list:
                context["display_name"] = _self._get_display_name_with_cache(
                    context["chat_id"],
                    context["group_id"],
                    user_name_map,
                    context.get("telegram_chat_id"),
                )

            # Enrich with escalation status, org metadata, and session titles
            escalated_sessions = set()
            session_org_names: dict = {}  # session_id → org_short_name
            session_titles: dict = {}  # session_id → title
            try:
                session_ids = [ctx["chat_id"] for ctx in context_list if ctx.get("chat_id")]
                if session_ids:
                    meta_resp = (
                        _self.client.table("chat_sessions")
                        .select("session_id, title, is_escalated, metadata")
                        .in_("session_id", session_ids)
                        .execute()
                    )
                    for r in meta_resp.data or []:
                        if r.get("is_escalated"):
                            escalated_sessions.add(r["session_id"])
                        if r.get("title"):
                            session_titles[r["session_id"]] = r["title"]
                        meta = r.get("metadata") or {}
                        org_name = meta.get("organization_short_name")
                        if org_name:
                            session_org_names[r["session_id"]] = org_name
            except Exception as e:
                logger.warning("Could not fetch session metadata: %s", e)

            # Build logbook (chat_id, topic_id) → grid_name mapping from Auth DB
            logbook_topic_to_grid: dict[tuple[str, str], str] = {}
            try:
                import asyncio

                from shared.auth import get_auth_service

                logbook_topic_to_grid = asyncio.run(
                    get_auth_service().get_all_logbook_grid_mapping()
                )
            except Exception as e:
                logger.warning("Could not fetch logbook grid mapping: %s", e)

            for context in context_list:
                context["is_escalated"] = context["chat_id"] in escalated_sessions

                # Resolve group display names
                is_shared_group = False
                if context.get("is_group"):
                    # First: check logbook mapping (chat_id + topic_id → grid name)
                    tg_chat_id = context.get("telegram_chat_id", "")
                    tg_topic_id = context.get("telegram_topic_id", "")
                    logbook_grid = (
                        logbook_topic_to_grid.get((tg_chat_id, str(tg_topic_id)))
                        if tg_topic_id
                        else None
                    )

                    if logbook_grid:
                        context["display_name"] = f"Logbook / {logbook_grid}"
                        is_shared_group = True
                    else:
                        # Fall back to session title for unresolved groups
                        title = session_titles.get(context["chat_id"], "")
                        if title and not title.startswith("Chat telegram_"):
                            if "O&M" in title and " / " in title:
                                context["display_name"] = "O&M / " + title.split(" / ")[-1]
                                is_shared_group = True
                            elif " / " in title:
                                parts = title.split(" / ")
                                group_short = (
                                    parts[0].split(" - ")[-1].strip()
                                    if " - " in parts[0]
                                    else parts[0]
                                )
                                context["display_name"] = f"{group_short} / {parts[-1]}"
                            else:
                                context["display_name"] = title

                # Annotate display name with org for DMs and non-shared groups
                if not is_shared_group:
                    org_name = session_org_names.get(context["chat_id"])
                    staff_org_name = os.getenv("STAFF_ORG_NAME", "Staff")
                    if org_name and org_name != staff_org_name:
                        context["display_name"] = f"{context['display_name']} · {org_name}"

            # Merge group entries that can't be distinguished by topic name.
            # When topic names aren't stored, multiple topics in the same group
            # all show as "MySite" — merge them into one sidebar entry.
            context_list = _merge_undifferentiated_group_topics(context_list)

            # Filter out escalation group if configured
            escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID")
            if escalation_chat_id:
                context_list = [
                    ctx for ctx in context_list if ctx.get("telegram_chat_id") != escalation_chat_id
                ]

            return context_list

        except Exception as e:
            logger.error("Error fetching chat contexts: %s", e)
            import traceback

            traceback.print_exc()
            return []

    def get_conversation_messages(
        self,
        chat_id: str,
        group_id: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        telegram_topic_id: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """
        Get all messages for a specific chat context.

        Args:
            chat_id: Hashed session_id (e.g. telegram_abc123...)
            group_id: Telegram group ID (None for direct chats)
            telegram_chat_id: Real Telegram chat ID — for groups, fetches
                messages across ALL sessions sharing this chat ID.
            date_from: Start date filter (optional)
            date_to: End date filter (optional)
            limit: Maximum number of messages to return

        Returns:
            List of messages ordered by timestamp
        """
        if not self.client:
            return []

        try:
            # For groups: look up sessions by telegram_chat_id.
            # If topic_id is provided, filter to that specific topic only.
            if telegram_chat_id:
                query = (
                    self.client.table("chat_sessions")
                    .select("id")
                    .eq("telegram_chat_id", telegram_chat_id)
                )
                if telegram_topic_id:
                    query = query.eq("telegram_topic_id", str(telegram_topic_id))
                session_response = query.execute()
            else:
                session_response = (
                    self.client.table("chat_sessions")
                    .select("id")
                    .eq("session_id", chat_id)
                    .limit(1)
                    .execute()
                )

            if not session_response.data:
                logger.debug("No session found for chat_id: %s", chat_id)
                return []

            session_uuids = [row["id"] for row in session_response.data]

            # Now query messages using the UUID(s)
            query = self.client.table("chat_messages").select(
                "id, session_id, role, content, function_call, tool_result, "
                "metadata, created_at, message_index, from_chat_id, group_id, "
                "thread_id, telegram_message_id"
            )

            # Filter by session_id UUID(s)
            if len(session_uuids) == 1:
                query = query.eq("session_id", session_uuids[0])
            else:
                query = query.in_("session_id", session_uuids)

            # Date filters
            if date_from:
                query = query.gte("created_at", date_from.isoformat())
            if date_to:
                query = query.lte("created_at", date_to.isoformat())

            # Execute query - order by newest first, then reverse to show chronologically
            response = query.order("created_at", desc=True).limit(limit).execute()

            messages: List[Dict[str, Any]] = response.data
            # Reverse to show oldest->newest in the UI
            return list(reversed(messages))

        except Exception as e:
            logger.error("Error fetching conversation messages: %s", e)
            return []

    def delete_bot_message(
        self,
        message_id: str,
        chat_id: str,
        telegram_message_id: int | None = None,
    ) -> Dict[str, Any]:
        """Delete a bot message from Telegram and soft-delete from DB.

        Args:
            message_id: UUID of the chat_messages row
            chat_id: Telegram chat ID where the message was sent
            telegram_message_id: Telegram message ID to delete (None = DB-only)

        Returns:
            Dict with success status and optional error
        """
        import os

        import requests

        # 1. Delete from Telegram (skip if no telegram_message_id)
        if telegram_message_id:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            if not bot_token:
                return {"success": False, "error": "TELEGRAM_BOT_TOKEN not configured"}

            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{bot_token}/deleteMessage",
                    json={"chat_id": chat_id, "message_id": telegram_message_id},
                    timeout=10,
                )
                tg_result = resp.json()
                if not tg_result.get("ok"):
                    # Message may already be deleted or too old (>48h)
                    tg_error = tg_result.get("description", "Unknown Telegram error")
                    logger.warning("Telegram deleteMessage failed: %s", tg_error)
            except Exception as e:
                logger.warning("Telegram deleteMessage request failed: %s", e)
                # Continue to soft-delete from DB even if Telegram fails

        # 2. Soft-delete from DB: clear content, merge deleted flag into existing metadata
        #    (preserves agent_instance_id and other fields needed for reply routing)
        try:
            existing = (
                self.client.table("chat_messages")
                .select("metadata")
                .eq("id", message_id)
                .single()
                .execute()
            )
            current_metadata = (existing.data or {}).get("metadata") or {}
            merged_metadata = {
                **current_metadata,
                "deleted": True,
                "deleted_at": datetime.utcnow().isoformat(),
            }
            self.client.table("chat_messages").update(
                {
                    "content": "[Message deleted]",
                    "metadata": merged_metadata,
                }
            ).eq("id", message_id).execute()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": f"DB update failed: {e}"}

    def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get session metadata.

        Args:
            session_id: Session UUID or session identifier

        Returns:
            Session information or None
        """
        if not self.client:
            return None

        try:
            response = (
                self.client.table("chat_sessions")
                .select("*")
                .eq("session_id", session_id)
                .limit(1)
                .execute()
            )

            if response.data:
                session_info: Dict[str, Any] = response.data[0]
                return session_info
            return None

        except Exception as e:
            logger.error("Error fetching session info: %s", e)
            return None

    def search_messages(
        self, search_term: str, chat_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Search for messages containing a specific term.

        Args:
            search_term: Text to search for
            chat_id: Optional filter by specific chat
            limit: Maximum results

        Returns:
            List of matching messages
        """
        if not self.client:
            return []

        try:
            query = (
                self.client.table("chat_messages")
                .select("id, session_id, role, content, created_at, from_chat_id, group_id")
                .ilike("content", f"%{search_term}%")
            )

            if chat_id:
                query = query.eq("from_chat_id", chat_id)

            response = query.order("created_at", desc=True).limit(limit).execute()

            search_results: List[Dict[str, Any]] = response.data
            return search_results

        except Exception as e:
            logger.error("Error searching messages: %s", e)
            return []

    @st.cache_data(ttl=300, show_spinner=False)  # Cache for 5 minutes
    def search_conversations_by_content(
        _self, search_term: str, days_back: int, user_email: str
    ) -> List[Dict[str, Any]]:
        """
        Search for conversations that contain messages matching the search term.

        Args:
            search_term: Text to search for in message content
            days_back: Number of days to look back
            user_email: Email of authenticated user (for cache isolation)

        Returns:
            List of chat contexts that have matching messages
        """
        if not _self.client:
            return []

        try:
            # Calculate date threshold
            since = datetime.utcnow() - timedelta(days=days_back)

            # Search for messages containing the search term within date range
            response = (
                _self.client.table("chat_messages")
                .select(
                    "id, from_chat_id, group_id, created_at, chat_sessions!inner(session_id, telegram_chat_id, telegram_topic_id)"
                )
                .ilike("content", f"%{search_term}%")
                .gte("created_at", since.isoformat())
                .order("created_at", desc=True)
                .execute()
            )

            # Group by chat context
            contexts = {}
            telegram_ids = set()

            for row in response.data:
                session_data = row.get("chat_sessions")
                if not session_data:
                    continue

                session_id = session_data.get("session_id", "")
                chat_id = session_id if session_id else "Unknown"
                group_id = row.get("group_id")

                # Use telegram_chat_id column (preserved after session_id hashing)
                telegram_chat_id = session_data.get("telegram_chat_id", "")
                # Fallback to parsing session_id for legacy data
                if not telegram_chat_id and session_id.startswith("telegram_"):
                    telegram_chat_id = session_id.replace("telegram_", "")
                # Groups have telegram IDs starting with "-100" or "100"
                is_group = (
                    telegram_chat_id.startswith("-100") or telegram_chat_id.startswith("100")
                    if telegram_chat_id
                    else False
                )

                # For forum groups: separate by topic_id
                telegram_topic_id = session_data.get("telegram_topic_id")
                if is_group and telegram_chat_id:
                    base_group_id = telegram_chat_id.split("_")[0]
                    if telegram_topic_id:
                        context_key = f"group_{base_group_id}_topic_{telegram_topic_id}"
                    else:
                        context_key = f"group_{base_group_id}"
                else:
                    context_key = f"{session_id}_{group_id or 'direct'}"

                if context_key not in contexts:
                    contexts[context_key] = {
                        "chat_id": chat_id,
                        "group_id": None if is_group else group_id,
                        "is_group": is_group,
                        "telegram_topic_id": telegram_topic_id,
                        "last_message": row["created_at"],
                        "message_count": 0,
                        "display_name": None,
                        "telegram_chat_id": telegram_chat_id,  # Store for name lookup
                    }

                    if telegram_chat_id:
                        telegram_ids.add(telegram_chat_id)

                contexts[context_key]["message_count"] += 1

            # Convert to list and sort
            context_list = sorted(contexts.values(), key=lambda x: x["last_message"], reverse=True)

            # Batch lookup user names
            import asyncio

            try:
                user_name_map = asyncio.run(_self._batch_lookup_user_names(list(telegram_ids)))
                logger.debug("Batch lookup returned %d user names", len(user_name_map))
            except Exception as e:
                logger.error("Batch lookup failed: %s", e)
                import traceback

                traceback.print_exc()
                user_name_map = {}

            # Enrich with display names
            for context in context_list:
                context["display_name"] = _self._get_display_name_with_cache(
                    context["chat_id"],
                    context["group_id"],
                    user_name_map,
                    context.get("telegram_chat_id"),
                )

            # Filter out escalation group if configured
            escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID")
            if escalation_chat_id:
                context_list = [
                    ctx for ctx in context_list if ctx.get("telegram_chat_id") != escalation_chat_id
                ]

            return context_list

        except Exception as e:
            logger.error("Error searching conversations by content: %s", e)
            import traceback

            traceback.print_exc()
            return []

    def get_daily_stats(self, date: datetime) -> Dict[str, int]:
        """
        Get statistics for a specific date.

        Args:
            date: Date to get stats for

        Returns:
            Dict with user count, message count, session count
        """
        if not self.client:
            return {"users": 0, "messages": 0, "sessions": 0}

        try:
            start_of_day = date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = start_of_day + timedelta(days=1)

            # Get messages for the day
            response = (
                self.client.table("chat_messages")
                .select("session_id")
                .eq("role", "user")
                .gte("created_at", start_of_day.isoformat())
                .lt("created_at", end_of_day.isoformat())
                .execute()
            )

            data = response.data
            # Extract unique chat IDs from session_id (format: telegram_XXXXXXX)
            unique_users = len(
                set(
                    row.get("session_id", "").replace("telegram_", "")
                    for row in data
                    if row.get("session_id", "").startswith("telegram_")
                )
            )
            unique_sessions = len(set(row["session_id"] for row in data if row.get("session_id")))

            return {
                "users": unique_users,
                "messages": len(data),
                "sessions": unique_sessions,
            }

        except Exception as e:
            logger.error("Error fetching daily stats: %s", e)
            return {"users": 0, "messages": 0, "sessions": 0}

    @st.cache_data(ttl=600, show_spinner=False)  # Cache for 10 minutes
    def get_period_stats(
        _self, user_email: str, start_date: datetime, end_date: datetime
    ) -> Dict[str, Any]:
        """
        Get statistics for a date range.

        Args:
            user_email: Email of authenticated user (for cache isolation)
            start_date: Start of period
            end_date: End of period

        Returns:
            Dict with user count, message count, session count, token counts, median response time

        Note:
            user_email is included in cache key to prevent data leakage between users.
            Each user gets their own cached statistics to protect PII.
        """
        if not _self.client:
            return {
                "users": 0,
                "messages": 0,
                "sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "median_response_time": None,
            }

        try:
            # Get user messages for the period (for unique user/session counts)
            user_response = (
                _self.client.table("chat_messages")
                .select("session_id")
                .eq("role", "user")
                .gte("created_at", start_date.isoformat())
                .lt("created_at", end_date.isoformat())
                .execute()
            )

            user_data = user_response.data
            # Extract unique chat IDs from session_id (format: telegram_XXXXXXX)
            unique_users = len(
                set(
                    row.get("session_id", "").replace("telegram_", "")
                    for row in user_data
                    if row.get("session_id", "").startswith("telegram_")
                )
            )
            unique_sessions = len(
                set(row["session_id"] for row in user_data if row.get("session_id"))
            )

            # Count conversation messages only (user messages + bot text responses)
            # Excludes tool calls, tool results, and internal LLM prompt templates
            user_msg_response = (
                _self.client.table("chat_messages")
                .select("id", count="exact")
                .eq("role", "user")
                .gte("created_at", start_date.isoformat())
                .lt("created_at", end_date.isoformat())
                .execute()
            )
            model_msg_response = (
                _self.client.table("chat_messages")
                .select("id", count="exact")
                .eq("role", "model")
                .not_.is_("content", "null")
                .gte("created_at", start_date.isoformat())
                .lt("created_at", end_date.isoformat())
                .execute()
            )
            total_messages = (user_msg_response.count or 0) + (model_msg_response.count or 0)

            # Get model messages with metadata for token counts
            token_response = (
                _self.client.table("chat_messages")
                .select("metadata")
                .eq("role", "model")
                .gte("created_at", start_date.isoformat())
                .lt("created_at", end_date.isoformat())
                .not_.is_("metadata", "null")
                .execute()
            )

            # Sum up tokens from metadata (separate input and output)
            input_tokens = 0
            output_tokens = 0
            for row in token_response.data:
                metadata = row.get("metadata", {})
                if metadata and isinstance(metadata, dict):
                    input_tokens += metadata.get("input_tokens", 0)
                    output_tokens += metadata.get("output_tokens", 0)

            # Calculate median response time (time from user message to model response)
            median_response_time = _self._calculate_median_response_time(start_date, end_date)

            return {
                "users": unique_users,
                "messages": total_messages,
                "sessions": unique_sessions,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "median_response_time": median_response_time,
            }

        except Exception as e:
            logger.error("Error fetching period stats: %s", e)
            return {
                "users": 0,
                "messages": 0,
                "sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "median_response_time": None,
            }

    def _calculate_median_response_time(
        self, start_date: datetime, end_date: datetime
    ) -> Optional[float]:
        """
        Calculate median response time (seconds) from user message to model response.

        Args:
            start_date: Start of period
            end_date: End of period

        Returns:
            Median response time in seconds, or None if no data
        """
        if not self.client:
            return None

        try:
            # Get all messages (user and model) with session_id and timestamp
            # Order by session_id and created_at to pair consecutive messages
            response = (
                self.client.table("chat_messages")
                .select("session_id, role, created_at")
                .in_("role", ["user", "model"])
                .gte("created_at", start_date.isoformat())
                .lt("created_at", end_date.isoformat())
                .order("session_id")
                .order("created_at")
                .execute()
            )

            if not response.data:
                return None

            # Group messages by session_id
            from collections import defaultdict

            sessions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for msg in response.data:
                sessions[msg["session_id"]].append(msg)

            # Calculate response times for each user->model pair within a session
            response_times = []
            for session_messages in sessions.values():
                for i in range(len(session_messages) - 1):
                    current = session_messages[i]
                    next_msg = session_messages[i + 1]

                    # Only consider user->model transitions
                    if current["role"] == "user" and next_msg["role"] == "model":
                        user_time = datetime.fromisoformat(
                            current["created_at"].replace("Z", "+00:00")
                        )
                        model_time = datetime.fromisoformat(
                            next_msg["created_at"].replace("Z", "+00:00")
                        )
                        delta = (model_time - user_time).total_seconds()

                        # Only include reasonable response times (0-300 seconds)
                        # Filter out outliers from session resumptions or errors
                        if 0 < delta < 300:
                            response_times.append(delta)

            if not response_times:
                return None

            # Calculate median
            import statistics

            return statistics.median(response_times)

        except Exception as e:
            logger.error("Error calculating median response time: %s", e)
            return None

    def get_chat_context_by_id(
        self, chat_id: str, group_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get a single chat context by chat_id and group_id.

        Args:
            chat_id: Session ID
            group_id: Optional group ID

        Returns:
            Chat context dict or None if not found
        """
        if not self.client:
            return None

        try:
            # First, get the session UUID and telegram_chat_id from the session_id text
            session_response = (
                self.client.table("chat_sessions")
                .select("id, telegram_chat_id")
                .eq("session_id", chat_id)
                .limit(1)
                .execute()
            )

            if not session_response.data:
                return None

            session_uuid = session_response.data[0]["id"]
            # Use telegram_chat_id column (preserved after session_id hashing)
            telegram_chat_id = session_response.data[0].get("telegram_chat_id", "")
            # Fallback to parsing session_id for legacy data
            if not telegram_chat_id and chat_id.startswith("telegram_"):
                telegram_chat_id = chat_id.replace("telegram_", "")

            # Query for messages from this chat using UUID
            query = (
                self.client.table("chat_messages")
                .select("session_id, from_chat_id, group_id, created_at")
                .eq("session_id", session_uuid)
            )

            if group_id:
                query = query.eq("group_id", group_id)
            else:
                query = query.is_("group_id", "null")

            response = query.order("created_at", desc=True).limit(100).execute()

            if not response.data:
                return None

            # Build context from messages
            messages = response.data

            # Batch lookup user name using telegram_chat_id
            user_name_map = {}
            if telegram_chat_id:
                import asyncio

                try:
                    user_name_map = asyncio.run(self._batch_lookup_user_names([telegram_chat_id]))
                except Exception:
                    user_name_map = {}

            # Determine if group from telegram_chat_id
            is_group = (
                telegram_chat_id.startswith("-100") or telegram_chat_id.startswith("100")
                if telegram_chat_id
                else bool(group_id)
            )

            context = {
                "chat_id": chat_id,
                "group_id": group_id,
                "is_group": is_group,
                "last_message": messages[0]["created_at"],
                "message_count": len(messages),
                "display_name": self._get_display_name_with_cache(
                    chat_id, group_id, user_name_map, telegram_chat_id
                ),
                "telegram_chat_id": telegram_chat_id,
            }

            return context

        except Exception as e:
            logger.error("Error fetching chat context by ID: %s", e)
            return None

    async def _batch_lookup_user_names(self, telegram_ids: List[str]) -> Dict[str, str]:
        """Batch lookup display names using a fresh Auth DB connection.

        Creates its own asyncpg connection (not AuthService pool) because
        anansi-app calls this via asyncio.run() which creates a new event loop
        incompatible with AuthService's pool.
        """
        if not telegram_ids:
            return {}

        import ssl

        import asyncpg

        conn = None
        try:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "6543")),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                ssl=ssl_context,
                command_timeout=10,
                statement_cache_size=0,
                server_settings={"statement_timeout": "10000"},
            )

            name_map: Dict[str, str] = {}

            # Separate user IDs from group IDs
            user_ids = [
                tid for tid in telegram_ids if not (tid.startswith("-100") or tid.startswith("100"))
            ]
            group_ids = [
                tid for tid in telegram_ids if tid.startswith("-100") or tid.startswith("100")
            ]

            # Special system groups
            escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
            for gid in group_ids[:]:
                base_id = gid.split("_")[0]
                if base_id == escalation_chat_id:
                    name_map[gid] = "Internal escalation group"
                    group_ids.remove(gid)

            # Lookup users
            if user_ids:
                user_rows = await conn.fetch(
                    """
                    SELECT telegram_id, full_name, email
                    FROM accounts
                    WHERE telegram_id = ANY($1::text[])
                    AND deleted_at IS NULL
                    """,
                    user_ids,
                )
                for row in user_rows:
                    tid = row["telegram_id"]
                    name = row["full_name"] or (row["email"].split("@")[0] if row["email"] else "")
                    if tid and name:
                        name_map[tid] = name

            # Lookup groups (orgs, O&M, Logbook)
            if group_ids:
                base_ids = [gid.split("_")[0] for gid in group_ids]
                base_to_original = {gid.split("_")[0]: gid for gid in group_ids}

                # Organizations (dev groups)
                org_rows = await conn.fetch(
                    """
                    SELECT developer_group_telegram_chat_id, name
                    FROM organizations
                    WHERE developer_group_telegram_chat_id = ANY($1::text[])
                    AND deleted_at IS NULL
                    """,
                    base_ids,
                )
                for row in org_rows:
                    chat_id = row["developer_group_telegram_chat_id"]
                    if chat_id and row["name"]:
                        name_map[base_to_original.get(chat_id, chat_id)] = row["name"]

                # O&M groups
                unresolved = [b for b in base_ids if base_to_original.get(b, b) not in name_map]
                if unresolved:
                    grid_rows = await conn.fetch(
                        """
                        SELECT DISTINCT internal_telegram_group_chat_id::text
                        FROM grids
                        WHERE internal_telegram_group_chat_id::text = ANY($1::text[])
                        AND deleted_at IS NULL
                        """,
                        unresolved,
                    )
                    for row in grid_rows:
                        cid = str(row["internal_telegram_group_chat_id"])
                        name_map[base_to_original.get(cid, cid)] = "O&M Group"

                # Logbook groups
                still_unresolved = [
                    b for b in base_ids if base_to_original.get(b, b) not in name_map
                ]
                if still_unresolved:
                    logbook_rows = await conn.fetch(
                        """
                        SELECT DISTINCT telegram_config->>'internal_logbook_chat_id' AS logbook_chat_id
                        FROM grids
                        WHERE telegram_config->>'internal_logbook_chat_id' = ANY($1::text[])
                        AND deleted_at IS NULL
                        """,
                        still_unresolved,
                    )
                    for row in logbook_rows:
                        cid = str(row["logbook_chat_id"])
                        name_map[base_to_original.get(cid, cid)] = "Logbook"

            return name_map

        except Exception as e:
            logger.error("Error batch looking up user names: %s", e)
            import traceback

            traceback.print_exc()
            return {}
        finally:
            if conn:
                try:
                    await conn.close()
                except Exception:
                    pass

    def _get_display_name_with_cache(
        self,
        chat_id: str,
        group_id: Optional[str],
        user_name_map: Dict[str, str],
        telegram_chat_id: Optional[str] = None,
    ) -> str:
        """
        Get display name for a chat context using cached user name map.

        Args:
            chat_id: Session ID (format: telegram_XXXXX, hashed, or UUID)
            group_id: Telegram group ID (if applicable)
            user_name_map: Pre-loaded map of telegram_id -> name (includes both users and groups)
            telegram_chat_id: Original telegram chat ID (preserved after session_id hashing)

        Returns:
            Formatted display name
        """
        # Use telegram_chat_id if provided (for hashed session IDs)
        telegram_id = telegram_chat_id
        # Fallback to parsing session_id for legacy data
        if not telegram_id and chat_id.startswith("telegram_"):
            telegram_id = chat_id.replace("telegram_", "")

        if telegram_id:
            # Check if it's a group (starts with "-100" or "100")
            if telegram_id.startswith("-100") or telegram_id.startswith("100"):
                # Look up organization name in cached map
                if telegram_id in user_name_map:
                    return user_name_map[telegram_id]
                # Fallback to showing telegram ID
                clean_id = telegram_id.lstrip("-")  # Remove minus sign
                base_id = clean_id.split("_")[0]  # Remove suffix if present
                return f"Group {base_id[-8:]}"
            else:
                # Regular user - look up in cached map
                if telegram_id in user_name_map:
                    return user_name_map[telegram_id]
                return f"User {telegram_id}"

        # Fallback to shortened session ID
        return f"Session {chat_id[:8]}..."

    def _get_display_name(self, chat_id: str, group_id: Optional[str]) -> str:
        """
        Get display name for a chat context.

        Args:
            chat_id: Telegram chat ID
            group_id: Telegram group ID (if applicable)

        Returns:
            Formatted display name
        """
        if group_id:
            return f"Group {group_id[-8:]}"  # Show last 8 chars of group ID

        # chat_id is actually session_id (UUID), try to look up from chat_sessions
        try:
            if self.client:
                session_response = (
                    self.client.table("chat_sessions")
                    .select("session_id, title, metadata")
                    .eq("session_id", chat_id)
                    .limit(1)
                    .execute()
                )

                if session_response.data and len(session_response.data) > 0:
                    session = session_response.data[0]
                    # Extract telegram ID from session_id field (format: telegram_XXXXX)
                    session_id_value = session.get("session_id", "")
                    if session_id_value.startswith("telegram_"):
                        telegram_id = session_id_value.replace("telegram_", "")

                        # Try to look up user name from accounts
                        # Note: auth_client removed, using direct DB connection instead
                        if False:  # Disabled - use _batch_lookup_user_names instead
                            try:
                                response = (
                                    None.select("name, email")  # type: ignore
                                    .eq("telegram_id", telegram_id)
                                    .is_("deleted_at", None)
                                    .limit(1)
                                    .execute()
                                )

                                if response.data and len(response.data) > 0:
                                    user = response.data[0]
                                    name = user.get("name") or user.get("email", "").split("@")[0]
                                    return f"{name}"

                            except Exception as e:
                                logger.error("Error looking up user name: %s", e)

                        return f"User {telegram_id}"
        except Exception as e:
            logger.error("Error looking up session: %s", e)

        # Fallback to shortened session ID
        return f"Session {chat_id[:8]}..."

    def get_ingested_documents(
        self, limit: int = 100, offset: int = 0, doc_type: str = None, procedure_id: str = None
    ) -> tuple[List[Dict[str, Any]], int]:
        """
        Get list of ingested RAG documents.

        Args:
            limit: Maximum documents per page
            offset: Offset for pagination
            doc_type: Filter by document type (metadata->>doc_type)
            procedure_id: Filter by procedure ID (metadata->procedure_ids array contains value)

        Returns:
            Tuple of (list of documents, total count)
        """
        if not self.client:
            return [], 0

        try:
            # If filtering by procedure_id, find matching document IDs from chunks first
            procedure_doc_ids = None
            if procedure_id:
                chunk_resp = (
                    self.client.table("chunks")
                    .select("document_id")
                    .contains("chunk_metadata", json.dumps({"procedure_ids": [procedure_id]}))
                    .limit(5000)
                    .execute()
                )
                procedure_doc_ids = list(
                    {row["document_id"] for row in chunk_resp.data if row.get("document_id")}
                )
                if not procedure_doc_ids:
                    return [], 0

            # Get total count (with filters)
            count_query = self.client.table("documents").select("id", count="exact")
            if doc_type:
                count_query = count_query.eq("metadata->>doc_type", doc_type)
            if procedure_doc_ids is not None:
                count_query = count_query.in_("id", procedure_doc_ids)
            count_response = count_query.execute()
            total_count = count_response.count or 0

            # Get documents with pagination (with filters)
            data_query = (
                self.client.table("documents")
                .select("id, title, source_type, source_id, metadata, ingested_at, updated_at")
                .order("ingested_at", desc=True)
            )
            if doc_type:
                data_query = data_query.eq("metadata->>doc_type", doc_type)
            if procedure_doc_ids is not None:
                data_query = data_query.in_("id", procedure_doc_ids)
            response = data_query.range(offset, offset + limit - 1).execute()

            documents = []
            for row in response.data:
                metadata = row.get("metadata", {}) or {}
                source_id = row.get("source_id", "")
                source_type = row.get("source_type", "")

                # Construct source URL if not in metadata
                source_url = metadata.get("source_url")
                if not source_url and source_id:
                    if source_type == "gdrive":
                        # Determine URL type based on doc_type
                        meta_doc_type = metadata.get("doc_type", "")
                        if meta_doc_type in ["google_doc"]:
                            source_url = f"https://docs.google.com/document/d/{source_id}/edit"
                        else:
                            source_url = f"https://drive.google.com/file/d/{source_id}/view"

                documents.append(
                    {
                        "id": row["id"],
                        "title": row.get("title", "Untitled"),
                        "source_type": source_type,
                        "source_id": source_id,
                        "source_url": source_url,
                        "doc_type": metadata.get("doc_type", "unknown"),
                        "audience": metadata.get("audience", "staff"),
                        "allowed_role_ids": metadata.get("allowed_role_ids", []),
                        "ingested_at": row.get("ingested_at"),
                        "updated_at": row.get("updated_at"),
                    }
                )

            return documents, total_count

        except Exception as e:
            logger.error("Error fetching ingested documents: %s", e)
            import traceback

            traceback.print_exc()
            return [], 0

    def get_distinct_doc_types(self) -> List[str]:
        """Get distinct document types from metadata."""
        if not self.client:
            return []

        try:
            response = (
                self.client.table("documents")
                .select("metadata->>doc_type")
                .not_.is_("metadata->>doc_type", "null")
                .limit(5000)
                .execute()
            )
            types = sorted({row["doc_type"] for row in response.data if row.get("doc_type")})
            return types
        except Exception as e:
            logger.error("Error fetching distinct doc types: %s", e)
            return []

    def get_distinct_procedures(self) -> List[str]:
        """Get distinct procedure IDs from chunk metadata."""
        if not self.client:
            return []

        try:
            response = (
                self.client.table("chunks")
                .select("chunk_metadata")
                .not_.is_("chunk_metadata->>procedure_ids", "null")
                .limit(5000)
                .execute()
            )
            proc_ids = set()
            for row in response.data:
                meta = row.get("chunk_metadata") or {}
                for pid in meta.get("procedure_ids", []):
                    if pid:
                        proc_ids.add(pid)
            return sorted(proc_ids)
        except Exception as e:
            logger.error("Error fetching distinct procedures: %s", e)
            return []

    def get_document_chunks(
        self, document_id: str, procedure_id: str = None
    ) -> List[Dict[str, Any]]:
        """Get chunks for a document, optionally filtered by procedure_id."""
        if not self.client:
            return []

        try:
            query = (
                self.client.table("chunks")
                .select("id, chunk_index, content, chunk_metadata")
                .eq("document_id", document_id)
                .order("chunk_index")
            )
            if procedure_id:
                query = query.contains(
                    "chunk_metadata", json.dumps({"procedure_ids": [procedure_id]})
                )
            response = query.limit(200).execute()
            return response.data or []
        except Exception as e:
            logger.error("Error fetching document chunks: %s", e)
            return []

    def delete_chunk(self, chunk_id: str) -> bool:
        """Delete a single chunk by ID."""
        if not self.client:
            return False

        try:
            self.client.table("chunks").delete().eq("id", chunk_id).execute()
            return True
        except Exception as e:
            logger.error("Error deleting chunk %s: %s", chunk_id, e)
            return False

    def get_document_chunks_count(self, document_id: str) -> int:
        """Get count of chunks for a document."""
        if not self.client:
            return 0

        try:
            response = (
                self.client.table("chunks")
                .select("id", count="exact")
                .eq("document_id", document_id)
                .execute()
            )
            return response.count or 0
        except Exception:
            return 0

    def get_document_preview(self, document_id: str, max_length: int = 200) -> str:
        """Get content preview from first chunk of a document.

        Args:
            document_id: Document UUID
            max_length: Maximum characters to return

        Returns:
            Preview string, truncated with "..." if longer than max_length
        """
        if not self.client:
            return ""

        try:
            response = (
                self.client.table("chunks")
                .select("content")
                .eq("document_id", document_id)
                .order("chunk_index")
                .limit(1)
                .execute()
            )

            if not response.data:
                return ""

            content: str = response.data[0].get("content", "") or ""
            if len(content) > max_length:
                return content[:max_length].strip() + "..."
            return content.strip()
        except Exception:
            return ""

    def get_document_entities(
        self, document_id: str, limit: int = 5
    ) -> tuple[List[Dict[str, Any]], int]:
        """Get entities for a document.

        Args:
            document_id: Document UUID
            limit: Maximum entities to return

        Returns:
            Tuple of (list of entities, total count)
        """
        if not self.client:
            return [], 0

        try:
            # Get total count
            count_response = (
                self.client.table("entities")
                .select("id", count="exact")
                .eq("document_id", document_id)
                .execute()
            )
            total_count = count_response.count or 0

            # Get limited entities
            response = (
                self.client.table("entities")
                .select("name, type")
                .eq("document_id", document_id)
                .limit(limit)
                .execute()
            )

            entities = response.data if response.data else []
            return entities, total_count
        except Exception:
            return [], 0

    def delete_document(self, document_id: str) -> bool:
        """Delete a document and its related data.

        Deletion order: entities → chunks → document (document last for retryability).
        If any step fails, the document record remains so the user can retry.
        """
        if not self.client:
            logger.error("Supabase client not initialized")
            return False

        try:
            # 1. Delete entities first (no cascade dependency)
            try:
                logger.info("Deleting entities for document %s", document_id)
                entities_result = (
                    self.client.table("entities").delete().eq("document_id", document_id).execute()
                )
                logger.info(
                    "Entities deleted: %d", len(entities_result.data) if entities_result.data else 0
                )
            except Exception as e:
                # Entities table may not exist or have different schema - continue anyway
                logger.info("Could not delete entities (table may not exist): %s", e)

            # 2. Delete chunks (has foreign key to document)
            logger.info("Deleting chunks for document %s", document_id)
            chunks_result = (
                self.client.table("chunks").delete().eq("document_id", document_id).execute()
            )
            logger.info("Chunks deleted: %d", len(chunks_result.data) if chunks_result.data else 0)

            # 3. Delete document last (so failed deletions can be retried)
            logger.info("Deleting document %s", document_id)
            doc_result = self.client.table("documents").delete().eq("id", document_id).execute()
            logger.info("Document deleted: %d", len(doc_result.data) if doc_result.data else 0)

            return True
        except Exception:
            logger.exception("Error deleting document %s", document_id)
            return False

    def update_document_access(
        self, document_id: str, audience: str, allowed_role_ids: List[int]
    ) -> bool:
        """Update access level for a document and all its chunks.

        Updates the document's metadata.audience and each chunk's
        chunk_metadata.allowed_role_ids.

        Args:
            document_id: Document UUID
            audience: "all" or "staff"
            allowed_role_ids: List of role IDs (e.g., [1, 2, 3])

        Returns:
            True if update succeeded
        """
        if not self.client:
            return False

        try:
            # 1. Get current document metadata
            doc_response = (
                self.client.table("documents").select("metadata").eq("id", document_id).execute()
            )
            if not doc_response.data:
                return False

            # 2. Update document metadata
            metadata = doc_response.data[0].get("metadata", {}) or {}
            metadata["audience"] = audience
            metadata["allowed_role_ids"] = allowed_role_ids

            self.client.table("documents").update({"metadata": metadata}).eq(
                "id", document_id
            ).execute()

            # 3. Update all chunks' chunk_metadata.allowed_role_ids
            chunks_response = (
                self.client.table("chunks")
                .select("id, chunk_metadata")
                .eq("document_id", document_id)
                .execute()
            )

            for chunk in chunks_response.data or []:
                chunk_meta = chunk.get("chunk_metadata", {}) or {}
                chunk_meta["allowed_role_ids"] = allowed_role_ids
                chunk_meta["allowed_org_ids"] = []  # Reset org filter when access changes

                self.client.table("chunks").update({"chunk_metadata": chunk_meta}).eq(
                    "id", chunk["id"]
                ).execute()

            return True

        except Exception as e:
            logger.error("Error updating document access: %s", e)
            import traceback

            traceback.print_exc()
            return False

    def update_document_title(self, document_id: str, new_title: str) -> bool:
        """Update a document's title.

        Args:
            document_id: Document UUID
            new_title: New title string

        Returns:
            True if update succeeded
        """
        if not self.client:
            return False

        try:
            self.client.table("documents").update({"title": new_title}).eq(
                "id", document_id
            ).execute()
            return True
        except Exception as e:
            logger.error("Error updating document title: %s", e)
            return False

    @st.cache_data(ttl=60, show_spinner=False)
    def get_all_user_schedules(_self) -> List[Dict[str, Any]]:
        """Fetch all user schedules from Supabase, ordered by next_run_at."""
        if not _self.client:
            return []
        try:
            result = (
                _self.client.table("user_schedules")
                .select(
                    "id, chat_id, topic_id, created_by_email, organization_id, "
                    "command, schedule_type, cron_expression, timezone, "
                    "next_run_at, is_active, status, friendly_name"
                )
                .order("next_run_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error("Error fetching user schedules: %s", e)
            return []


__all__ = ["SupabaseReader"]
