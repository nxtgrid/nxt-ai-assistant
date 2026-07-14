"""
Customer Support Escalation Service

This service handles escalation of customer support questions to an internal
Telegram group when the bot cannot answer or when customers request escalation.

Features:
- Posts escalation messages to internal support Telegram group
- Includes organization hashtag for filtering (e.g., #yourorg)
- Formats customer question summary
- Includes customer context (chat_id, user info)
- Stores escalation metadata for response routing
- Handles support replies and forwards them to customers
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, cast
from zoneinfo import ZoneInfo

import aiohttp

from shared.utils.logging import get_logger
from shared.utils.telegram_buttons import build_escalation_track_keyboard
from shared.utils.telegram_markdown import convert_github_to_telegram_markdown
from shared.utils.telegram_markdown import escape_markdown as _escape_telegram_markdown
from shared.utils.telegram_send import is_markdown_parse_error as _is_markdown_parse_error

LOGGER = get_logger(__name__)

_DEFAULT_TZ = ZoneInfo(os.getenv("DEFAULT_TIMEZONE", "UTC"))

# ---------------------------------------------------------------------------
# Module-level shared resources
# ---------------------------------------------------------------------------

# Shared aiohttp session for all Jira API calls (avoids per-request TCP setup).
# Created lazily on first use; replaced when closed.
_jira_session: Optional[aiohttp.ClientSession] = None


def _get_jira_session() -> aiohttp.ClientSession:
    global _jira_session
    if _jira_session is None or _jira_session.closed:
        _jira_session = aiohttp.ClientSession()
    return _jira_session


# TTL cache for Jira organization list (changes rarely — max one fetch per 30 min).
_jira_orgs_cache: List[Dict[str, Any]] = []
_jira_orgs_cache_time: float = 0.0
_JIRA_ORGS_TTL: float = 1800.0  # 30 minutes


def _is_after_hours() -> bool:
    """True if current time (configurable timezone) is on a weekend or after AFTER_HOURS_START_HOUR."""
    tz_name = os.getenv("AFTER_HOURS_TIMEZONE", os.getenv("DEFAULT_TIMEZONE", "UTC"))
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = _DEFAULT_TZ
    now = datetime.now(tz)
    start_hour = int(os.getenv("AFTER_HOURS_START_HOUR", "19"))
    return now.weekday() >= 5 or now.hour >= start_hour


def _adf_to_text(adf: Any, _depth: int = 0, _max_depth: int = 50) -> str:
    """Extract plain text from an Atlassian Document Format node (recursive, depth-limited)."""
    if _depth > _max_depth or not adf or not isinstance(adf, dict):
        return ""
    if adf.get("type") == "text":
        return str(adf.get("text", ""))
    return "".join(_adf_to_text(child, _depth + 1, _max_depth) for child in adf.get("content", []))


class EscalationService:
    """Service for escalating customer support issues to internal Telegram group."""

    def __init__(
        self,
        escalation_chat_id: Optional[str] = None,
        bot_token: Optional[str] = None,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
    ):
        """
        Initialize escalation service.

        Args:
            escalation_chat_id: Telegram chat ID for internal support group
            bot_token: Telegram bot token for sending messages
            supabase_url: Supabase URL for database persistence
            supabase_key: Supabase service key for database persistence
        """
        self._escalation_chat_id = escalation_chat_id or os.getenv(
            "ESCALATION_TELEGRAM_CHAT_ID", ""
        )
        self._bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")

        # Supabase configuration for database persistence (with legacy fallback)
        self._supabase_url = (
            supabase_url or os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
        )
        self._supabase_key = (
            supabase_key or os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
        )
        self._supabase_client = None

        # Jira configuration — used by track_as_ticket (ticket creation from Telegram callback)
        self._jira_base_url = os.getenv("JIRA_BASE_URL", "")
        self._jira_email = os.getenv("JIRA_USERNAME", "")
        self._jira_api_token = os.getenv("JIRA_API_TOKEN", "")
        self._jira_project_key = os.getenv("JIRA_PROJECT_KEY", "OPS")
        self._jira_issue_type = os.getenv("JIRA_ISSUE_TYPE", "Task")

    def is_enabled(self) -> bool:
        """Check if escalation service is properly configured."""
        # Always use Telegram for escalations (Jira integration available for future use)
        return bool(self._escalation_chat_id and self._bot_token)

    def _get_supabase_client(self):
        """Get or create Supabase client for database persistence."""
        if self._supabase_client is None and self._supabase_url and self._supabase_key:
            from orchestrator.services.supabase_client import SupabaseClient

            self._supabase_client = SupabaseClient(
                url=self._supabase_url,
                key=self._supabase_key,
            )
        return self._supabase_client

    async def escalate_to_support(
        self,
        question_summary: str,
        session_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        organization_short_name: Optional[str] = None,
        customer_chat_id: Optional[str] = None,
        customer_topic_id: Optional[str] = None,
        customer_username: Optional[str] = None,
        customer_email: Optional[str] = None,
        conversation_context: Optional[str] = None,
        grid_name: Optional[str] = None,
        reason: Optional[str] = None,
        action_type: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Escalate a customer support question to internal support group or Jira.

        In DEBUG mode: Posts to Telegram
        In PRODUCTION mode: Creates Jira ticket

        Args:
            question_summary: Brief summary of customer's question
            session_id: Session identifier for tracking escalation state
            organization_short_name: Organization short name for hashtag (e.g., "yourorg")
            customer_chat_id: Customer's Telegram chat ID
            customer_topic_id: Customer's topic/thread ID (if in a forum group)
            customer_username: Customer's username or name
            customer_email: Customer's email
            conversation_context: Recent conversation context (optional)
            grid_name: Grid name for Jira custom field (optional)
            reason: Categorized escalation reason (optional). Valid values:
                - user_requested: User explicitly asked for human help
                - could_not_answer: Bot couldn't answer the question
                - out_of_scope: Request outside bot capabilities
                - staff_action_required: Needs action bot can't perform
                - inappropriate_language: Offensive content from user
                - negative_feedback: User expressed dissatisfaction
                - verification_failed: LLM judge rejected response twice
                - safety_escalation: Bot claimed escalation without tool call
                - other: Doesn't fit other categories
            action_type: Specific action needed when reason=staff_action_required:
                - meter_unassignment: Customer wants meter removed
                - wallet_credit: Manual wallet credit needed
                - hps_power_limit: HPS power limit review
                - meter_replacement: Physical meter swap
                - commissioning_retry: Manual commissioning retry
                - other_action: Other staff action

        Returns:
            Dict with success status and message
        """
        if not self.is_enabled():
            return {
                "success": False,
                "error": "Escalation service not configured",
            }

        # Always use Telegram for escalations (Jira integration available for future use)
        return await self._escalate_to_telegram(
            question_summary=question_summary,
            session_id=session_id,
            organization_id=organization_id,
            organization_short_name=organization_short_name,
            customer_chat_id=customer_chat_id,
            customer_topic_id=customer_topic_id,
            customer_username=customer_username,
            customer_email=customer_email,
            conversation_context=conversation_context,
            reason=reason,
            action_type=action_type,
            thread_id=thread_id,
        )

    async def _get_or_create_escalation_topic(
        self,
        organization_id: int,
        org_name: str,
    ) -> Optional[int]:
        """Return the Telegram forum topic_id for this org, creating it if needed.

        Returns the message_thread_id on success, None if creation fails or
        bot lacks can_manage_topics admin right (callers fall back to General).
        """
        from shared.utils.telegram_send import create_forum_topic

        supabase_client = self._get_supabase_client()
        if not supabase_client:
            return None

        topic_id = await supabase_client.get_org_escalation_topic(organization_id)
        if topic_id:
            return int(topic_id)

        topic_name = f"{org_name} Escalations" if org_name else f"Org {organization_id} Escalations"
        topic_id = await create_forum_topic(
            bot_token=self._bot_token,
            chat_id=self._escalation_chat_id,
            name=topic_name,
        )
        if topic_id is None:
            LOGGER.warning(
                "Could not create forum topic for org=%s — falling back to General", org_name
            )
            return None

        await supabase_client.save_org_escalation_topic(
            organization_id=organization_id,
            topic_id=topic_id,
        )
        LOGGER.info("Created escalation forum topic for org=%s: topic_id=%s", org_name, topic_id)
        return int(topic_id)

    async def _escalate_to_telegram(
        self,
        question_summary: str,
        session_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        organization_short_name: Optional[str] = None,
        customer_chat_id: Optional[str] = None,
        customer_topic_id: Optional[str] = None,
        customer_username: Optional[str] = None,
        customer_email: Optional[str] = None,
        conversation_context: Optional[str] = None,
        reason: Optional[str] = None,
        action_type: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Escalate to Telegram (debug mode)."""

        try:
            # Check if session already has an active escalation
            if session_id:
                existing = await self.get_escalation_info(session_id)
                if existing and existing.get("is_active"):
                    existing_msg_id = existing.get("escalation_message_id")
                    LOGGER.info(
                        f"Session {session_id} already has active escalation "
                        f"(msg_id={existing_msg_id}), sending follow-up"
                    )

                    # Send follow-up as a NEW escalation message (not just a reply)
                    # so it gets its own mapping and won't be lost if the original
                    # escalation is closed before this one is handled.
                    followup_parts = ["🆘 *Follow-Up Escalation*\n"]
                    escaped_summary = _escape_telegram_markdown(question_summary)
                    followup_parts.append(f"\n*Summary:*\n{escaped_summary}\n")
                    if reason:
                        followup_parts.append(f"\n*Reason:* {reason}")
                    if action_type:
                        followup_parts.append(f"\n*Action:* {action_type}")
                    if customer_username:
                        escaped_name = _escape_telegram_markdown(customer_username)
                        followup_parts.append(f"\n*Customer:* {escaped_name}")
                    followup_parts.append(
                        "\n\n_Reply to this message to respond to the customer. "
                        "The bot will forward your response._"
                    )

                    followup_text = "".join(followup_parts)
                    followup_msg_id = None

                    # Pre-generate mapping UUID for the callback button
                    followup_mapping_id = str(uuid.uuid4())
                    followup_keyboard = build_escalation_track_keyboard(followup_mapping_id)

                    followup_topic_id = existing.get("escalation_topic_id")
                    if followup_topic_id is None and organization_id:
                        followup_topic_id = await self._get_or_create_escalation_topic(
                            organization_id=organization_id,
                            org_name=organization_short_name or "",
                        )

                    if existing_msg_id:
                        # Send as reply to existing thread for context
                        reply_result = await self._send_telegram_reply(
                            chat_id=self._escalation_chat_id,
                            reply_to_message_id=existing_msg_id,
                            text=followup_text,
                            reply_markup=followup_keyboard,
                            topic_id=followup_topic_id,
                        )
                        if reply_result.get("ok"):
                            followup_msg_id = reply_result.get("result", {}).get("message_id")
                            LOGGER.info(
                                f"Sent follow-up escalation for session {session_id} "
                                f"as reply to msg_id={existing_msg_id}, "
                                f"new msg_id={followup_msg_id}"
                            )
                        else:
                            LOGGER.error(f"Failed to send follow-up escalation: {reply_result}")
                    else:
                        # Original escalation message ID is missing — send as a new top-level
                        # message so it's not silently dropped. This covers cases where the
                        # session was marked is_escalated=True but the message ID was never
                        # saved (e.g. DB write failed after Telegram send on the first escalation).
                        LOGGER.warning(
                            f"Session {session_id} is_active=True but escalation_message_id "
                            f"is None — sending follow-up as new standalone message"
                        )
                        standalone_result = await self._send_telegram_message(
                            chat_id=self._escalation_chat_id,
                            text=followup_text,
                            reply_markup=followup_keyboard,
                            topic_id=followup_topic_id,
                        )
                        if standalone_result.get("ok"):
                            followup_msg_id = standalone_result.get("result", {}).get("message_id")
                            LOGGER.info(
                                f"Sent follow-up escalation for session {session_id} "
                                f"as standalone message, msg_id={followup_msg_id}"
                            )
                        else:
                            LOGGER.error(
                                f"Failed to send standalone follow-up escalation: {standalone_result}"
                            )

                    # If the parent escalation already has a Jira ticket, add a follow-up
                    # comment to it (only when the Telegram send succeeded and the ticket
                    # is still open) so the sweep does not create a duplicate ticket.
                    existing_jira_key: Optional[str] = existing.get("jira_ticket_key")
                    if existing_jira_key:
                        try:
                            ticket_fields = await self._fetch_jira_issue_fields(existing_jira_key)
                            if ticket_fields and ticket_fields.get("is_done"):
                                LOGGER.info(
                                    "Parent Jira ticket %s is Done — will not pre-link follow-up "
                                    "for session %s; sweep will create a fresh ticket",
                                    existing_jira_key,
                                    session_id,
                                )
                                existing_jira_key = None
                        except Exception:
                            pass  # fail-open: keep existing_jira_key if status check fails

                    # Create a new escalation mapping for this follow-up so closing
                    # the original doesn't lose this request. Pre-link to the existing
                    # Jira ticket (if any) so the sweep does not create a second ticket.
                    supabase_client = self._get_supabase_client()
                    if supabase_client and followup_msg_id and customer_chat_id:
                        if existing_jira_key:
                            comment_body = f"Follow-up from customer:\n\n{question_summary}"
                            commented = await self._add_jira_comment(
                                existing_jira_key, comment_body
                            )
                            if commented:
                                LOGGER.info(
                                    "Added follow-up comment to existing Jira ticket %s for session %s",
                                    existing_jira_key,
                                    session_id,
                                )
                            else:
                                LOGGER.warning(
                                    "Failed to add follow-up comment to Jira ticket %s for session %s",
                                    existing_jira_key,
                                    session_id,
                                )
                        await supabase_client.save_escalation_mapping(
                            escalation_message_id=followup_msg_id,
                            customer_chat_id=customer_chat_id,
                            session_id=session_id,
                            customer_topic_id=customer_topic_id,
                            org_hashtag=(
                                f"#{organization_short_name}" if organization_short_name else None
                            ),
                            customer_email=customer_email,
                            customer_username=customer_username,
                            reason=reason,
                            action_type=action_type,
                            mapping_id=followup_mapping_id,
                            organization_id=organization_id,
                            escalation_topic_id=followup_topic_id,
                            question_text=question_summary,
                            thread_id=thread_id,
                            jira_ticket_key=existing_jira_key,
                        )

                    return {
                        "success": True,
                        "message": "Your request has been forwarded to the support team. "
                        "They will respond shortly.",
                        "escalation_message_id": followup_msg_id or existing_msg_id,
                        "is_escalated": True,
                        "was_duplicate": True,
                    }

            # Build escalation message using Telegram Markdown v1 format
            # Note: Telegram uses *bold* not **bold**
            message_parts = ["🆘 *Customer Support Escalation*\n"]

            # Add organization hashtag if available
            clean_tag = None
            if organization_short_name:
                # Clean up short name for hashtag (remove spaces, special chars)
                clean_tag = "".join(c for c in organization_short_name if c.isalnum())
                message_parts.append(f"*Organization:* #{clean_tag}\n")

            # Escape user-provided content to prevent markdown parsing errors
            escaped_summary = _escape_telegram_markdown(question_summary)
            message_parts.append(f"\n*Question:*\n{escaped_summary}\n")

            # Add customer context
            message_parts.append("\n*Customer Info:*")
            if customer_username:
                escaped_username = _escape_telegram_markdown(customer_username)
                message_parts.append(f"\n• Name: {escaped_username}")
            if customer_chat_id:
                message_parts.append(f"\n• Chat ID: `{customer_chat_id}`")
            if customer_topic_id:
                message_parts.append(f"\n• Topic ID: `{customer_topic_id}`")

            # Add conversation context if provided
            if conversation_context:
                escaped_context = _escape_telegram_markdown(conversation_context)
                message_parts.append(f"\n\n*Recent Context:*\n{escaped_context}")

            message_parts.append(
                "\n\n_Reply to this message to respond to the customer. "
                "The bot will forward your response._"
            )

            message_text = "".join(message_parts)

            # Pre-generate mapping UUID for the callback button
            mapping_id = str(uuid.uuid4())
            # After-hours: auto-Jira will be created in background; hide Track button.
            after_hours = _is_after_hours()
            track_keyboard = build_escalation_track_keyboard(
                mapping_id, include_track=not after_hours
            )

            # Resolve org's forum topic (lazy get-or-create)
            escalation_topic_id: Optional[int] = None
            if organization_id:
                escalation_topic_id = await self._get_or_create_escalation_topic(
                    organization_id=organization_id,
                    org_name=organization_short_name or "",
                )

            # Send to escalation group (org topic if resolved, else General)
            LOGGER.info(
                f"Sending escalation to Telegram chat {self._escalation_chat_id} "
                f"topic={escalation_topic_id} "
                f"for {customer_email or customer_username or 'unknown user'}"
            )
            result = await self._send_telegram_message(
                chat_id=self._escalation_chat_id,
                text=message_text,
                reply_markup=track_keyboard,
                topic_id=escalation_topic_id,
            )

            # Handle stale topic: topic was deleted externally in Telegram
            if (
                not result.get("ok")
                and "message thread not found" in (result.get("description") or "").lower()
                and escalation_topic_id is not None
                and organization_id is not None
            ):
                LOGGER.warning(
                    "Escalation topic deleted for org_id=%s topic_id=%s — "
                    "clearing cached id and retrying to General",
                    organization_id,
                    escalation_topic_id,
                )
                supabase_client = self._get_supabase_client()
                if supabase_client:
                    await supabase_client.clear_org_escalation_topic(organization_id)
                result = await self._send_telegram_message(
                    chat_id=self._escalation_chat_id,
                    text=message_text,
                    reply_markup=track_keyboard,
                )

            LOGGER.info(f"Telegram API response: ok={result.get('ok')}, result={result}")

            if result.get("ok"):
                escalation_message_id = result.get("result", {}).get("message_id")

                # Store mapping for response routing in database
                if escalation_message_id and customer_chat_id and session_id:
                    supabase_client = self._get_supabase_client()
                    if supabase_client:
                        await supabase_client.save_escalation_mapping(
                            escalation_message_id=escalation_message_id,
                            customer_chat_id=customer_chat_id,
                            session_id=session_id,
                            customer_topic_id=customer_topic_id,
                            org_hashtag=f"#{clean_tag}" if clean_tag else None,
                            customer_email=customer_email,
                            customer_username=customer_username,
                            reason=reason,
                            action_type=action_type,
                            mapping_id=mapping_id,
                            organization_id=organization_id,
                            escalation_topic_id=escalation_topic_id,
                            question_text=question_summary,
                            thread_id=thread_id,
                        )
                        LOGGER.info(
                            f"Saved escalation to database: msg_id={escalation_message_id} → "
                            f"chat_id={customer_chat_id}, session={session_id}, "
                            f"reason={reason}, action_type={action_type}"
                        )
                    else:
                        LOGGER.warning(
                            "No Supabase client available - escalation not persisted to database"
                        )

                # After-hours: create Jira ticket and update the message before returning.
                if after_hours and escalation_message_id:
                    await self._auto_create_jira_and_edit_message(
                        mapping_id=mapping_id,
                        escalation_message_id=escalation_message_id,
                        escalation_topic_id=escalation_topic_id,
                        question_summary=question_summary,
                        conversation_context=conversation_context,
                        customer_chat_id=customer_chat_id,
                        customer_topic_id=customer_topic_id,
                        organization_short_name=organization_short_name,
                    )

                LOGGER.info(
                    f"Escalated question to support group for org={organization_short_name}, "
                    f"customer={customer_email or customer_username}"
                )
                return {
                    "success": True,
                    "message": "Your question has been escalated to our support team. "
                    "They will respond shortly.",
                    "escalation_message_id": escalation_message_id,
                    "is_escalated": True,
                }
            else:
                LOGGER.error(f"Failed to send escalation message: {result}")
                return {
                    "success": False,
                    "error": f"Failed to send escalation: {result.get('description', 'Unknown error')}",
                }

        except Exception as e:
            LOGGER.exception(f"Error escalating to support: {e}")
            return {
                "success": False,
                "error": f"Escalation failed: {str(e)}",
            }

    # JIRA Grid field (customfield_10057) option IDs — required select field.
    # Fallback used when grid cannot be resolved from escalation context.
    JIRA_GRID_FALLBACK_OPTION_ID = "10315"  # "Software"

    def _jira_auth_headers(self) -> Dict[str, str]:
        """Return Basic-auth + JSON headers for Jira API calls (cached per instance)."""
        if not hasattr(self, "_cached_jira_auth_header"):
            auth_b64 = base64.b64encode(
                f"{self._jira_email}:{self._jira_api_token}".encode("ascii")
            ).decode("ascii")
            self._cached_jira_auth_header = f"Basic {auth_b64}"
        return {
            "Authorization": self._cached_jira_auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _resolve_jira_grid_option(
        self,
        grid_name: Optional[str],
        headers: Dict[str, str],
    ) -> Dict[str, str]:
        """Resolve a grid name to a JIRA customfield_10057 option.

        Fetches allowed values from JIRA create metadata, fuzzy-matches the
        grid name, and returns ``{"id": "<option_id>"}``.  Falls back to the
        ``Software`` option when no match is found or when *grid_name* is None.
        """
        fallback = {"id": self.JIRA_GRID_FALLBACK_OPTION_ID}
        if not grid_name:
            return fallback

        try:
            meta_url = (
                f"{self._jira_base_url}/rest/api/3/issue/createmeta"
                f"/{self._jira_project_key}/issuetypes"
            )
            session = _get_jira_session()
            # Find Task issue type ID
            async with session.get(
                meta_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    LOGGER.warning(f"Could not fetch issue types: {resp.status}")
                    return fallback
                type_data = await resp.json()

            task_type_id = None
            for it in type_data.get("issueTypes", type_data.get("values", [])):
                if it.get("name") == "Task":
                    task_type_id = it.get("id")
                    break
            if not task_type_id:
                return fallback

            # Fetch field metadata for Task type
            fields_url = (
                f"{self._jira_base_url}/rest/api/3/issue/createmeta"
                f"/{self._jira_project_key}/issuetypes/{task_type_id}"
            )
            async with session.get(
                fields_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp2:
                if resp2.status != 200:
                    return fallback
                fields_data = await resp2.json()

            # Find customfield_10057 and match
            for field in fields_data.get("fields", fields_data.get("values", [])):
                fid = field.get("fieldId", field.get("key", ""))
                if fid == "customfield_10057":
                    allowed = field.get("allowedValues", [])
                    # Exact match first (case-insensitive)
                    for opt in allowed:
                        if opt["value"].lower() == grid_name.lower():
                            LOGGER.info(f"Grid '{grid_name}' matched JIRA option id={opt['id']}")
                            return {"id": opt["id"]}
                    # Fuzzy match
                    try:
                        from shared.utils.grid_matcher import find_best_grid_match

                        option_names = [o["value"] for o in allowed]
                        matched, was_fuzzy, score = find_best_grid_match(
                            grid_name, option_names, threshold=80
                        )
                        if matched:
                            for opt in allowed:
                                if opt["value"] == matched:
                                    LOGGER.info(
                                        f"Grid '{grid_name}' fuzzy matched to "
                                        f"'{matched}' (score={score}%) -> id={opt['id']}"
                                    )
                                    return {"id": opt["id"]}
                    except ImportError:
                        pass
                    LOGGER.warning(f"No JIRA grid option matched for '{grid_name}', using fallback")
                    return fallback
        except Exception as e:
            LOGGER.warning(f"Error resolving JIRA grid option: {e}")
        return fallback

    async def _resolve_jira_account_id(
        self,
        email: str,
        headers: Dict[str, str],
    ) -> Optional[str]:
        """Resolve a JIRA account ID from an email address."""
        try:
            url = f"{self._jira_base_url}/rest/api/3/user/search"
            async with _get_jira_session().get(
                url,
                params={"query": email},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                users = await resp.json()
                for user in users:
                    if user.get("emailAddress", "").lower() == email.lower():
                        return str(user.get("accountId"))
        except Exception as e:
            LOGGER.debug(f"Could not resolve JIRA account for {email}: {e}")
        return None

    async def _fetch_jira_organizations(self) -> List[Dict[str, Any]]:
        """GET all JSM organizations, handling pagination (max 50 per page, 20 pages max).

        Results are cached for 30 minutes to avoid hammering the Jira API on every escalation.
        """
        global _jira_orgs_cache, _jira_orgs_cache_time
        if time.monotonic() - _jira_orgs_cache_time < _JIRA_ORGS_TTL:
            return _jira_orgs_cache

        orgs: List[Dict[str, Any]] = []
        url: Optional[str] = f"{self._jira_base_url}/rest/servicedeskapi/organization"
        headers = self._jira_auth_headers()
        session = _get_jira_session()
        page = 0
        max_pages = 20
        while url and page < max_pages:
            try:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        LOGGER.warning(
                            "Jira org fetch returned HTTP %d — stopping pagination", resp.status
                        )
                        break
                    data = await resp.json()
            except Exception as e:
                LOGGER.warning("Error fetching Jira organizations (page %d): %s", page, e)
                break
            orgs.extend(data.get("values", []))
            next_url = (
                data.get("_links", {}).get("next") if not data.get("isLastPage", True) else None
            )
            # Validate next URL is on the same Jira host to prevent SSRF
            if next_url and self._jira_base_url and next_url.startswith(self._jira_base_url):
                url = next_url
            else:
                url = None
            page += 1

        _jira_orgs_cache = orgs
        _jira_orgs_cache_time = time.monotonic()
        return orgs

    async def _resolve_jira_org_id(self, org_name: str) -> Optional[str]:
        """Fuzzy-match org_name against Jira's organisation list.

        Returns the Jira org ID as a string, or None if no match.
        """
        from shared.utils.grid_matcher import find_best_grid_match

        try:
            orgs = await self._fetch_jira_organizations()
            name_to_id = {o["name"]: str(o["id"]) for o in orgs}
            matched_name, _, _score = find_best_grid_match(org_name, list(name_to_id.keys()))
            return name_to_id[matched_name] if matched_name else None
        except Exception as e:
            LOGGER.warning("Could not resolve Jira org for '%s': %s", org_name, e)
            return None

    async def _create_jira_ticket(
        self,
        summary: str,
        description: str,
        grid_name: Optional[str] = None,
        assignee_email: Optional[str] = None,
        organization_short_name: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Create a Jira ticket.

        Args:
            summary: Ticket summary/title
            description: Ticket description
            grid_name: Grid name to match against JIRA options (optional)
            assignee_email: Email to auto-assign the ticket to (optional)
            organization_short_name: Org name for JSM Organizations field (optional)

        Returns:
            Dict with success status and ticket key
        """
        try:
            headers = self._jira_auth_headers()
            url = f"{self._jira_base_url}/rest/api/3/issue"

            # Resolve grid option (required field in OPS project)
            grid_option = await self._resolve_jira_grid_option(grid_name, headers)

            # Resolve assignee account ID
            assignee_account_id = None
            if assignee_email:
                assignee_account_id = await self._resolve_jira_account_id(assignee_email, headers)
                if assignee_account_id:
                    LOGGER.info(f"Will assign ticket to {assignee_email}")
                else:
                    LOGGER.debug(f"Could not resolve JIRA account for {assignee_email}")

            # Build ticket payload
            payload: Dict[str, Any] = {
                "fields": {
                    "project": {"key": self._jira_project_key},
                    "summary": summary,
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": description}],
                            }
                        ],
                    },
                    "issuetype": {"name": self._jira_issue_type},
                    "customfield_10057": grid_option,
                }
            }

            if assignee_account_id:
                payload["fields"]["assignee"] = {"accountId": assignee_account_id}

            if labels:
                payload["fields"]["labels"] = labels

            # Tag the JSM Organizations field (fuzzy-match our org to Jira's org list)
            org_field_id = os.getenv("JIRA_ORGANIZATION_FIELD_ID")
            if organization_short_name and org_field_id:
                jira_org_id = await self._resolve_jira_org_id(organization_short_name)
                if jira_org_id:
                    payload["fields"][org_field_id] = [int(jira_org_id)]
                    LOGGER.info(
                        "Tagged Jira org field %s=%s for org '%s'",
                        org_field_id,
                        jira_org_id,
                        organization_short_name,
                    )

            LOGGER.info(f"JIRA ticket grid option: {grid_option}")

            jira_sess = _get_jira_session()
            async with jira_sess.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response_text = await response.text()

                if response.status in (200, 201):
                    result: Dict[str, Any] = await response.json()
                    jira_key = result.get("key")
                    LOGGER.info(f"Successfully created Jira ticket: {jira_key}")
                    return {
                        "success": True,
                        "key": jira_key,
                        "id": result.get("id"),
                    }

                # Fallback: if issue type is invalid, retry with "Task"
                if (
                    response.status == 400
                    and "issuetype" in response_text
                    and payload["fields"]["issuetype"]["name"] != "Task"
                ):
                    LOGGER.warning(
                        f"Issue type '{payload['fields']['issuetype']['name']}' "
                        f"rejected, retrying with 'Task'"
                    )
                    payload["fields"]["issuetype"]["name"] = "Task"
                    async with jira_sess.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as retry_resp:
                        retry_text = await retry_resp.text()
                        if retry_resp.status in (200, 201):
                            retry_result: Dict[str, Any] = await retry_resp.json()
                            jira_key = retry_result.get("key")
                            LOGGER.info(f"Created Jira ticket with fallback type: {jira_key}")
                            return {
                                "success": True,
                                "key": jira_key,
                                "id": retry_result.get("id"),
                            }
                        LOGGER.error(
                            f"Fallback also failed: status={retry_resp.status}, "
                            f"response={retry_text}"
                        )

                LOGGER.error(
                    f"Failed to create Jira ticket: status={response.status}, "
                    f"response={response_text}"
                )
                return {
                    "success": False,
                    "error": f"Jira API returned {response.status}: {response_text}",
                }

        except Exception as e:
            LOGGER.exception(f"Error creating Jira ticket: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    async def _add_jira_comment(self, issue_key: str, body: str) -> bool:
        """Post a plain-text comment to an existing Jira issue. Returns True on success."""
        if not self._jira_base_url:
            return False
        try:
            headers = self._jira_auth_headers()
            url = f"{self._jira_base_url}/rest/api/3/issue/{issue_key}/comment"
            payload = {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}],
                }
            }
            jira_sess = _get_jira_session()
            async with jira_sess.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status in (200, 201):
                    return True
                LOGGER.warning(
                    "Failed to add Jira comment to %s: status=%s", issue_key, resp.status
                )
                return False
        except Exception as e:
            LOGGER.warning("Error adding Jira comment to %s: %s", issue_key, e)
            return False

    async def _transition_jira_to_done(self, issue_key: str) -> None:
        """Transition a Jira issue to Done from whatever status it currently has.

        Fetches available transitions for the issue and picks the first one
        whose target status is "Done" (statusCategory key "done"). This mirrors
        how handle_jira_issue_updated detects closures from Jira — no hardcoded
        transition IDs required, works regardless of current workflow state.

        Non-blocking — failures are logged but never raised.
        """
        transitions_url = f"{self._jira_base_url}/rest/api/3/issue/{issue_key}/transitions"
        try:
            session = _get_jira_session()
            headers = self._jira_auth_headers()

            # 1. Fetch available transitions for this issue's current state
            async with session.get(
                transitions_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    LOGGER.warning(
                        "Could not fetch transitions for %s: HTTP %s — %s",
                        issue_key,
                        resp.status,
                        body,
                    )
                    return
                data = await resp.json()

            # 2. Find the transition that leads to "Done" status category
            transition_id = None
            for t in data.get("transitions", []):
                to_status = t.get("to", {})
                category_key = to_status.get("statusCategory", {}).get("key", "")
                status_name = to_status.get("name", "")
                if category_key == "done" or status_name in ("Done", "Closed"):
                    transition_id = t["id"]
                    break

            if not transition_id:
                LOGGER.warning(
                    "No 'Done' transition available for %s — already closed or workflow mismatch",
                    issue_key,
                )
                return

            # 3. Execute the transition
            async with session.post(
                transitions_url,
                json={"transition": {"id": transition_id}},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    LOGGER.warning(
                        "Jira transition failed for %s: HTTP %s — %s",
                        issue_key,
                        resp.status,
                        body,
                    )
                else:
                    LOGGER.info(
                        "Transitioned Jira %s to Done (transition %s)", issue_key, transition_id
                    )

        except Exception:
            LOGGER.warning("Error transitioning Jira %s to Done", issue_key, exc_info=True)

    async def _fetch_jira_issue_fields(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Fetch summary and status category for a Jira issue.

        Returns {"summary": str, "is_done": bool} or None on error/not-found.
        """
        if not self._jira_base_url:
            return None
        url = f"{self._jira_base_url}/rest/api/3/issue/{issue_key}?fields=summary,status"
        try:
            session = _get_jira_session()
            async with session.get(
                url,
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    LOGGER.debug("Jira fetch %s returned HTTP %s", issue_key, resp.status)
                    return None
                data = await resp.json()
            fields = data.get("fields", {})
            status_category = fields.get("status", {}).get("statusCategory", {}).get("key", "")
            return {
                "summary": fields.get("summary", ""),
                "is_done": status_category == "done",
            }
        except Exception:
            LOGGER.debug("Error fetching Jira issue fields for %s", issue_key, exc_info=True)
            return None

    async def _search_jira_for_escalation(self, mapping_id: str) -> Optional[str]:
        """Search Jira for an existing ticket filed for this escalation mapping.

        Tickets are tagged with label "escalation-{mapping_id[:8]}" at creation.
        Returns the issue key if found, None otherwise.
        """
        if not self._jira_base_url or not self._jira_project_key:
            return None
        label = f"escalation-{mapping_id[:8]}"
        jql = f'project = "{self._jira_project_key}" AND labels = "{label}" ORDER BY created DESC'
        url = f"{self._jira_base_url}/rest/api/3/issue/search"
        try:
            session = _get_jira_session()
            async with session.get(
                url,
                params={"jql": jql, "fields": "summary,status", "maxResults": "1"},
                headers=self._jira_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            issues = data.get("issues", [])
            return str(issues[0]["key"]) if issues else None
        except Exception:
            LOGGER.debug("Error searching Jira for escalation %s", mapping_id, exc_info=True)
            return None

    async def notify_customer_resolved(
        self,
        customer_chat_id: str,
        customer_topic_id: Optional[str] = None,
    ) -> None:
        """Send a resolution notification to the customer."""
        await self._send_telegram_message(
            chat_id=customer_chat_id,
            topic_id=int(customer_topic_id) if customer_topic_id else None,
            text=(
                "\u2705 Your support request has been resolved. "
                "If you need further assistance, please feel free to reach out again!"
            ),
        )

    async def _send_telegram_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "Markdown",
        topic_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Send a message to Telegram using Bot API.

        Args:
            chat_id: Telegram chat ID
            text: Message text
            parse_mode: Message formatting mode (Markdown, HTML)
            topic_id: Optional topic/thread ID (int) for forum groups
            reply_markup: Optional inline keyboard markup

        Returns:
            Telegram API response dict
        """
        from shared.utils.telegram_send import _get_session

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"

        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }

        # Add topic ID for forum groups
        if topic_id is not None:
            payload["message_thread_id"] = topic_id

        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            session = _get_session()
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                result: Dict[str, Any] = await response.json()

            # Retry as plain text if the message was rejected for malformed Markdown
            # (e.g. a lone underscore in a support reply like "CLEAR_TAMPER"). Without
            # this, the reply is silently dropped instead of reaching the customer.
            if _is_markdown_parse_error(result) and "parse_mode" in payload:
                LOGGER.warning(
                    "Telegram rejected message as malformed Markdown (%s); retrying as plain text",
                    result.get("description"),
                )
                retry_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                async with session.post(
                    url,
                    json=retry_payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as retry_response:
                    result = await retry_response.json()

            return result

        except Exception as e:
            LOGGER.exception(f"Error sending Telegram message: {e}")
            return {
                "ok": False,
                "description": str(e),
            }

    async def handle_support_reply(
        self,
        reply_to_message_id: int,
        reply_text: str,
        from_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Handle a support team member's reply to an escalation and forward to customer.

        Args:
            reply_to_message_id: The message_id being replied to (escalation message)
            reply_text: The support team's response text
            from_username: Username of support team member who replied

        Returns:
            Dict with success status and message
        """
        # Look up customer chat from database
        supabase_client = self._get_supabase_client()
        mapping = None

        if supabase_client:
            mapping = await supabase_client.get_escalation_mapping(reply_to_message_id)

        if not mapping:
            LOGGER.warning(
                f"No escalation mapping found for message_id={reply_to_message_id}. "
                f"Reply cannot be forwarded."
            )
            return {
                "success": False,
                "error": "Could not find original escalation. Reply not forwarded.",
            }

        # Warn support if this escalation has already been closed
        if not mapping.get("is_active", True):
            LOGGER.info(f"Support replied to closed escalation message_id={reply_to_message_id}")
            await self._send_telegram_reply(
                chat_id=self._escalation_chat_id,
                reply_to_message_id=reply_to_message_id,
                text=(
                    "⚠️ This escalation is already closed. "
                    "Your reply was not forwarded to the customer.\n\n"
                    'To reopen, reply "Reopen" to this escalation message.'
                ),
                topic_id=mapping.get("escalation_topic_id"),
            )
            return {
                "success": False,
                "error": "Escalation already closed. Reply not forwarded.",
            }

        customer_chat_id = mapping["customer_chat_id"]
        customer_topic_id = mapping.get("customer_topic_id")
        customer_email = mapping.get("customer_email")

        try:
            # Format support response for customer (no staff name - keep anonymous).
            # Run the staff-typed reply through the Telegram-markdown converter so
            # **bold** becomes *bold* and mid-word underscores (e.g. "CLEAR_TAMPER")
            # are escaped — otherwise Telegram rejects the send as malformed Markdown.
            # The plain-text fallback in _send_telegram_message covers anything missed.
            response_text = convert_github_to_telegram_markdown(
                f"💬 **Response from Support Team**\n\n{reply_text}"
            )

            # Send to customer's chat
            result = await self._send_telegram_message(
                chat_id=customer_chat_id,
                text=response_text,
                topic_id=int(customer_topic_id) if customer_topic_id else None,
            )

            if result.get("ok"):
                LOGGER.info(
                    f"Forwarded support reply to customer chat_id={customer_chat_id}, "
                    f"topic_id={customer_topic_id}, email={customer_email}"
                )

                # Save forwarded message to chat history
                session_id = mapping.get("session_id")
                if supabase_client and session_id:
                    try:
                        from orchestrator.models.schemas import ConversationMessage

                        session = await supabase_client.get_session(session_id)
                        if session:
                            message = ConversationMessage(
                                role="model",
                                content=response_text,
                            )
                            await supabase_client.save_messages(
                                session_uuid=session.id,
                                messages=[message],
                                from_chat_id=customer_chat_id,
                            )
                            LOGGER.info(
                                f"Saved support reply to chat history for session {session_id}"
                            )
                        else:
                            LOGGER.warning(
                                f"Could not find session {session_id} to save support reply"
                            )
                    except Exception as save_error:
                        LOGGER.error(f"Failed to save support reply to chat history: {save_error}")

                return {
                    "success": True,
                    "message": "Response forwarded to customer",
                }
            else:
                LOGGER.error(f"Failed to forward reply to customer: {result}")
                return {
                    "success": False,
                    "error": f"Failed to forward: {result.get('description', 'Unknown error')}",
                }

        except Exception as e:
            LOGGER.exception(f"Error forwarding support reply: {e}")
            return {
                "success": False,
                "error": f"Failed to forward reply: {str(e)}",
            }

    @staticmethod
    def extract_hashtag_from_text(text: str) -> Optional[str]:
        """
        Extract organization hashtag from escalation message text.

        Args:
            text: Message text containing hashtag

        Returns:
            Hashtag (e.g., "#yourorg") or None if not found
        """
        # Look for pattern: **Organization:** #hashtag
        match = re.search(r"\*\*Organization:\*\*\s+(#\w+)", text)
        if match:
            return match.group(1)
        return None

    async def is_session_escalated(self, session_id: str) -> bool:
        """
        Check if a session has an active escalation.

        Args:
            session_id: Session identifier

        Returns:
            True if session has active escalation, False otherwise
        """
        supabase_client = self._get_supabase_client()
        if not supabase_client:
            return False

        info = await supabase_client.get_session_escalation_info(session_id)
        return info is not None and info.get("is_escalated", False)

    async def get_escalation_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get escalation info for a session.

        Args:
            session_id: Session identifier

        Returns:
            Escalation info dict or None if not escalated
        """
        supabase_client = self._get_supabase_client()
        if not supabase_client:
            return None

        result: Optional[Dict[str, Any]] = await supabase_client.get_escalation_by_session(
            session_id
        )
        return result

    async def forward_customer_message(
        self,
        session_id: str,
        customer_message: str,
        customer_username: Optional[str] = None,
        media_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Forward a customer's follow-up message to the escalation group.

        Supports text, photos (single + albums), video, voice, and audio.
        Media is forwarded using Telegram file_ids (no download/re-upload needed).

        Args:
            session_id: Session identifier
            customer_message: Customer's message text
            customer_username: Customer's username (optional)
            media_metadata: Metadata dict with file_ids (photo_file_id, photo_file_ids,
                          video_file_id, voice_file_id, audio_file_id)

        Returns:
            Dict with success status and message
        """
        # Get escalation info from database
        escalation_info = await self.get_escalation_info(session_id)

        if not escalation_info or not escalation_info.get("is_active"):
            return {
                "success": False,
                "error": "No active escalation for this session",
            }

        escalation_message_id = escalation_info.get("escalation_message_id")
        escalation_topic_id: Optional[int] = escalation_info.get("escalation_topic_id")

        if not escalation_message_id:
            return {
                "success": False,
                "error": "Escalation message ID not found",
            }

        # Fall back to org topic lookup for mappings saved before escalation_topic_id was stored
        if escalation_topic_id is None:
            org_id = escalation_info.get("organization_id")
            if org_id:
                org_short = (escalation_info.get("org_hashtag") or "").lstrip("#")
                escalation_topic_id = await self._get_or_create_escalation_topic(
                    organization_id=int(org_id),
                    org_name=org_short,
                )

        try:
            # Build caption/header for the forwarded message
            caption_parts = ["💬 **Customer Follow-up**\n"]
            if customer_username:
                caption_parts.append(f"**From:** {customer_username}\n")
            if customer_message:
                caption_parts.append(f"\n{customer_message}")
            caption_text = "".join(caption_parts)

            # Determine if there's media to forward
            media_metadata = media_metadata or {}
            photo_file_ids = media_metadata.get("photo_file_ids") or []
            if not photo_file_ids and media_metadata.get("photo_file_id"):
                photo_file_ids = [media_metadata["photo_file_id"]]

            video_file_id = media_metadata.get("video_file_id")
            voice_file_id = media_metadata.get("voice_file_id")
            audio_file_id = media_metadata.get("audio_file_id")

            has_media = photo_file_ids or video_file_id or voice_file_id or audio_file_id

            if not has_media:
                # Text-only: send as before
                result = await self._send_telegram_reply(
                    chat_id=self._escalation_chat_id,
                    reply_to_message_id=escalation_message_id,
                    text=caption_text,
                    topic_id=escalation_topic_id,
                )
            elif len(photo_file_ids) > 1:
                # Album: send first photo with caption, rest without
                result = await self._forward_telegram_media(
                    chat_id=self._escalation_chat_id,
                    reply_to_message_id=escalation_message_id,
                    media_type="photo",
                    file_id=photo_file_ids[0],
                    caption=caption_text,
                    topic_id=escalation_topic_id,
                )
                # Forward remaining album photos (best effort)
                for fid in photo_file_ids[1:]:
                    await self._forward_telegram_media(
                        chat_id=self._escalation_chat_id,
                        reply_to_message_id=escalation_message_id,
                        media_type="photo",
                        file_id=fid,
                        topic_id=escalation_topic_id,
                    )
            elif photo_file_ids:
                # Single photo with caption
                result = await self._forward_telegram_media(
                    chat_id=self._escalation_chat_id,
                    reply_to_message_id=escalation_message_id,
                    media_type="photo",
                    file_id=photo_file_ids[0],
                    caption=caption_text,
                    topic_id=escalation_topic_id,
                )
            elif video_file_id:
                result = await self._forward_telegram_media(
                    chat_id=self._escalation_chat_id,
                    reply_to_message_id=escalation_message_id,
                    media_type="video",
                    file_id=video_file_id,
                    caption=caption_text,
                    topic_id=escalation_topic_id,
                )
            elif voice_file_id:
                # Voice messages don't support captions — send text separately
                result = await self._forward_telegram_media(
                    chat_id=self._escalation_chat_id,
                    reply_to_message_id=escalation_message_id,
                    media_type="voice",
                    file_id=voice_file_id,
                    topic_id=escalation_topic_id,
                )
                if result.get("ok") and customer_message:
                    await self._send_telegram_reply(
                        chat_id=self._escalation_chat_id,
                        reply_to_message_id=escalation_message_id,
                        text=caption_text,
                        topic_id=escalation_topic_id,
                    )
            elif audio_file_id:
                result = await self._forward_telegram_media(
                    chat_id=self._escalation_chat_id,
                    reply_to_message_id=escalation_message_id,
                    media_type="audio",
                    file_id=audio_file_id,
                    caption=caption_text,
                    topic_id=escalation_topic_id,
                )

            if result.get("ok"):
                media_desc = ""
                if photo_file_ids:
                    media_desc = f" ({len(photo_file_ids)} photo(s))"
                elif video_file_id:
                    media_desc = " (video)"
                elif voice_file_id:
                    media_desc = " (voice)"
                elif audio_file_id:
                    media_desc = " (audio)"
                LOGGER.info(
                    f"Forwarded customer follow-up{media_desc} for session {session_id} "
                    f"to escalation message {escalation_message_id}"
                )
                return {
                    "success": True,
                    "message": "Your message has been forwarded to the support team.",
                }
            else:
                LOGGER.error(f"Failed to forward customer message: {result}")
                return {
                    "success": False,
                    "error": f"Failed to forward: {result.get('description', 'Unknown error')}",
                }

        except Exception as e:
            LOGGER.exception(f"Error forwarding customer message: {e}")
            return {
                "success": False,
                "error": f"Failed to forward message: {str(e)}",
            }

    async def _forward_telegram_media(
        self,
        chat_id: str,
        reply_to_message_id: int,
        media_type: str,
        file_id: str,
        caption: Optional[str] = None,
        topic_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Forward a Telegram media file to a chat using its file_id.

        Uses Telegram's native file_id forwarding (no download/re-upload).

        Args:
            chat_id: Target Telegram chat ID
            reply_to_message_id: Message ID to reply to
            media_type: One of "photo", "video", "voice", "audio", "document"
            file_id: Telegram file_id of the media
            caption: Optional caption text

        Returns:
            Telegram API response dict
        """
        api_methods = {
            "photo": "sendPhoto",
            "video": "sendVideo",
            "voice": "sendVoice",
            "audio": "sendAudio",
            "document": "sendDocument",
        }
        file_param_names = {
            "photo": "photo",
            "video": "video",
            "voice": "voice",
            "audio": "audio",
            "document": "document",
        }

        method = api_methods.get(media_type, "sendDocument")
        param_name = file_param_names.get(media_type, "document")
        url = f"https://api.telegram.org/bot{self._bot_token}/{method}"

        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            param_name: file_id,
            "reply_to_message_id": reply_to_message_id,
        }
        if topic_id is not None:
            payload["message_thread_id"] = topic_id
        if caption:
            payload["caption"] = caption[:1024]  # Telegram caption limit
            payload["parse_mode"] = "Markdown"

        try:
            from shared.utils.telegram_send import _get_session as _get_tg_session

            tg_session = _get_tg_session()
            async with tg_session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                result: Dict[str, Any] = await response.json()
                if not result.get("ok"):
                    LOGGER.error(
                        f"Telegram {method} failed: {result.get('description', 'Unknown')}"
                    )
                return result

        except Exception as e:
            LOGGER.exception(f"Error forwarding Telegram media ({media_type}): {e}")
            return {
                "ok": False,
                "description": str(e),
            }

    async def _send_telegram_reply(
        self,
        chat_id: str,
        reply_to_message_id: int,
        text: str,
        parse_mode: str = "Markdown",
        reply_markup: Optional[Dict[str, Any]] = None,
        topic_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Send a reply to a specific message in Telegram.

        Args:
            chat_id: Telegram chat ID
            reply_to_message_id: Message ID to reply to
            text: Message text
            parse_mode: Message formatting mode (Markdown, HTML)
            reply_markup: Optional inline keyboard markup

        Returns:
            Telegram API response dict
        """
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"

        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_to_message_id": reply_to_message_id,
        }

        if topic_id is not None:
            payload["message_thread_id"] = topic_id
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            from shared.utils.telegram_send import _get_session as _get_tg_session

            tg_session = _get_tg_session()
            async with tg_session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                result: Dict[str, Any] = await response.json()

            # Retry as plain text if rejected for malformed Markdown (see
            # _send_telegram_message for the rationale). reply_markup and
            # message_thread_id are preserved — only parse_mode is dropped.
            if _is_markdown_parse_error(result) and "parse_mode" in payload:
                LOGGER.warning(
                    "Telegram rejected reply as malformed Markdown (%s); retrying as plain text",
                    result.get("description"),
                )
                retry_payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                async with tg_session.post(
                    url,
                    json=retry_payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as retry_response:
                    result = await retry_response.json()

            return result

        except Exception as e:
            LOGGER.exception(f"Error sending Telegram reply: {e}")
            return {
                "ok": False,
                "description": str(e),
            }

    async def track_as_ticket(
        self,
        escalation_mapping: Dict[str, Any],
        assignee_email: Optional[str] = None,
        auto_filed: bool = False,
    ) -> Dict[str, Any]:
        """Create JIRA ticket from escalation, notify customer, store ticket key.

        The caller is responsible for:
        - Atomically claiming the escalation (is_active = false)
        - Editing the escalation message in Telegram
        - Re-activating if this method fails

        Args:
            escalation_mapping: Claimed escalation row from DB
            assignee_email: Email of the person who clicked the button (for JIRA assignment)
        """
        session_id = escalation_mapping["session_id"]
        customer_chat_id = escalation_mapping["customer_chat_id"]
        customer_topic_id = escalation_mapping.get("customer_topic_id")
        mapping_id = escalation_mapping["id"]
        # Derive org short name from hashtag stored at escalation time (e.g. "#yourorg" → "yourorg")
        _org_hashtag = escalation_mapping.get("org_hashtag") or ""
        _org_short_name: Optional[str] = _org_hashtag.lstrip("#") or None

        try:
            # The stored question text is the most reliable source — it's exactly what the
            # customer wrote when escalating, captured at creation time regardless of session age.
            stored_question = escalation_mapping.get("question_text") or ""

            # 1. Fetch recent chat history
            # escalation_mappings.session_id is the string session ID (e.g. "telegram_abc123").
            # chat_messages stores the UUID primary key (chat_sessions.id) — look it up first.
            supabase_client = self._get_supabase_client()
            raw_messages: list = []
            if supabase_client:
                try:
                    session_obj = await supabase_client.get_session(session_id)
                    if session_obj and session_obj.id:
                        raw_messages = (
                            await supabase_client.get_messages(
                                session_uuid=session_obj.id, max_age_hours=168, max_messages=50
                            )
                            or []
                        )
                except Exception as e:
                    LOGGER.warning("Could not fetch messages for Jira ticket: %s", e)
            # Normalise to plain dicts — get_messages returns ConversationMessage Pydantic
            # objects (no .get()), so access attributes and convert.
            messages: List[Dict[str, Any]] = [
                m
                if isinstance(m, dict)
                else {"role": getattr(m, "role", ""), "content": getattr(m, "content", "") or ""}
                for m in raw_messages
            ]

            # 2. Build JIRA title: prefer stored question, fall back to first chat message
            first_user_msg = next(
                (m["content"] for m in messages if m.get("role") == "user" and m.get("content")),
                "",
            )
            best_summary = stored_question or first_user_msg or "Customer support escalation"
            summary = best_summary[:120].strip()
            if len(best_summary) > 120:
                summary += "..."

            # 3. Build JIRA description from chat history (with Telegram link to original message)
            escalation_msg_id = escalation_mapping.get("escalation_message_id")
            telegram_link = _build_telegram_msg_link(self._escalation_chat_id, escalation_msg_id)
            description = _build_ticket_description(
                messages, escalation_mapping, stored_question, telegram_link=telegram_link
            )

            # 4. Resolve grid name from customer chat/topic for the JIRA grid field
            grid_name = None
            try:
                if customer_chat_id and customer_topic_id:
                    from shared.auth import get_auth_service

                    auth_pool = await get_auth_service()._get_db_pool()
                    async with auth_pool.acquire() as auth_conn:
                        grid_row = await auth_conn.fetchrow(
                            """
                            SELECT name FROM grids
                            WHERE internal_telegram_group_chat_id::text = $1
                              AND internal_telegram_group_thread_id::text = $2
                              AND deleted_at IS NULL
                            LIMIT 1
                            """,
                            str(customer_chat_id),
                            str(customer_topic_id),
                        )
                        if grid_row:
                            grid_name = grid_row["name"]
            except Exception as e:
                LOGGER.debug(f"Could not resolve grid for JIRA ticket: {e}")

            # Dedup guard: if a previous attempt created a Jira ticket but failed to
            # store the key in DB, find it by label and reuse it instead of filing again.
            existing_key = await self._search_jira_for_escalation(mapping_id)
            if existing_key:
                LOGGER.info(
                    "Dedup: found existing Jira ticket %s for mapping %s — skipping creation",
                    existing_key,
                    mapping_id,
                )
                if supabase_client:
                    try:
                        _client = supabase_client._get_client()
                        _client.table("escalation_mappings").update(
                            {"jira_ticket_key": existing_key}
                        ).eq("id", mapping_id).execute()
                    except Exception as e:
                        LOGGER.warning(
                            "Dedup: failed to store recovered key %s: %s", existing_key, e
                        )
                return {"success": True, "jira_ticket_key": existing_key}

            ticket_result = await self._create_jira_ticket(
                summary=summary,
                description=description,
                grid_name=grid_name,
                assignee_email=assignee_email,
                organization_short_name=_org_short_name,
                labels=[f"escalation-{mapping_id[:8]}"],
            )

            if not ticket_result.get("success"):
                return {"success": False, "error": ticket_result.get("error", "JIRA API error")}

            jira_key = ticket_result["key"]
            issue_number = jira_key.split("-")[-1]

            # 6+7+8. Run independent post-ticket operations concurrently.
            async def _store_jira_key():
                # Retry up to 3 times — a transient DB error here causes the sweep to
                # reactivate and re-file a duplicate ticket on the next run.
                if supabase_client:
                    _client = supabase_client._get_client()
                    for _attempt in range(3):
                        try:
                            _client.table("escalation_mappings").update(
                                {"jira_ticket_key": jira_key}
                            ).eq("id", mapping_id).execute()
                            return
                        except Exception:
                            if _attempt == 2:
                                raise
                            await asyncio.sleep(0.5 * (_attempt + 1))

            async def _release_session():
                # Release session back to bot only if no other blocking escalations
                # remain. Non-blocking ones (safety_escalation) never set is_escalated
                # on the session, so they shouldn't prevent release.
                sc = self._get_supabase_client()
                if sc:
                    remaining = await sc.count_active_blocking_escalations(session_id)
                    if remaining == 0:
                        await sc.update_session_escalation_status(
                            session_id=session_id, is_escalated=False
                        )

            async def _notify_customer():
                try:
                    if auto_filed:
                        text = (
                            f"Your support request has been assigned a tracking reference: "
                            f"{issue_number}. Our operations team will follow up with you shortly."
                        )
                    else:
                        text = (
                            f"Your issue is being tracked (ref: {issue_number}). "
                            "The team is working on it. You'll hear back when it's resolved."
                        )
                    await self._send_telegram_message(
                        chat_id=customer_chat_id,
                        text=text,
                        topic_id=int(customer_topic_id) if customer_topic_id else None,
                    )
                except Exception as e:
                    LOGGER.warning(f"Failed to notify customer about ticket {jira_key}: {e}")

            results = await asyncio.gather(
                _store_jira_key(),
                _release_session(),
                _notify_customer(),
                return_exceptions=True,
            )
            store_result = results[0]
            if isinstance(store_result, Exception):
                LOGGER.warning(
                    "Failed to store JIRA key for mapping %s: %s", mapping_id, store_result
                )
                return {"success": False, "error": f"DB write failed: {store_result}"}

            return {"success": True, "jira_ticket_key": jira_key}

        except Exception as e:
            LOGGER.exception(f"Error in track_as_ticket: {e}")
            return {"success": False, "error": str(e)}

    async def run_escalation_jira_sweep(
        self,
        min_age_hours: int = 1,
        max_age_hours: int = 24,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Auto-file Jira tickets for stale escalations that staff never manually tracked.

        Runs daily at 9am WAT (scheduled via APScheduler in app.py).  For each
        eligible escalation (active, no ticket, 1-24h old, not safety_escalation):

        1. Atomically claim via claim_escalation_for_tracking()
        2. Post-claim check: if jira_ticket_key already set (after-hours race), reactivate
        3. Call track_as_ticket() with auto_filed=True
        4. On success: edit Telegram escalation message to show ticket ref
        5. On failure: call reactivate_escalation() so staff Track button still works

        After the batch, alert ESCALATION_TELEGRAM_CHAT_ID if any escalations are
        older than max_age_hours with no ticket (window-missed orphans).
        """
        supabase_client = self._get_supabase_client()
        if not supabase_client:
            LOGGER.error("Escalation sweep: no Supabase client, aborting")
            return {"eligible": 0, "filed": 0, "skipped": 0, "failed": 0}

        eligible = await supabase_client.get_stale_unfiled_escalations(
            min_age_hours=min_age_hours,
            max_age_hours=max_age_hours,
            limit=limit,
        )

        if len(eligible) == limit:
            LOGGER.warning(
                "Escalation sweep: query capped at %d rows — backlog may be building", limit
            )

        filed = skipped = failed = 0

        for idx, mapping in enumerate(eligible):
            mapping_id = mapping["id"]

            # 1. Atomic claim — prevents race with staff clicking Track button
            claimed_mapping = await supabase_client.claim_escalation_for_tracking(mapping_id)
            if not claimed_mapping:
                skipped += 1
                continue

            # 2. Post-claim check — guard against concurrent after-hours auto-create
            # (_auto_create_jira_and_edit_message does not use claim; it writes the key
            # directly.  If it ran concurrently, the fetched row will now have the key.)
            if claimed_mapping.get("jira_ticket_key"):
                await supabase_client.reactivate_escalation(mapping_id)
                skipped += 1
                continue

            try:
                result = await self.track_as_ticket(
                    escalation_mapping=claimed_mapping,
                    assignee_email=None,
                    auto_filed=True,
                )

                if result.get("success"):
                    jira_key = result["jira_ticket_key"]
                    escalation_message_id = claimed_mapping.get("escalation_message_id")
                    escalation_topic_id = claimed_mapping.get("escalation_topic_id")
                    jira_url = f"{self._jira_base_url}/browse/{jira_key}"
                    escaped_key = _escape_telegram_markdown(jira_key)

                    if escalation_message_id:
                        # Edit original message: remove Track button, show ticket ref
                        edit_text = (
                            f"🆘 *Customer Support Escalation*\n\n"
                            f"🎫 *Auto-tracked:* [{escaped_key}]({jira_url})"
                        )
                        close_keyboard = build_escalation_track_keyboard(
                            mapping_id, include_track=False
                        )
                        await self._edit_telegram_message(
                            chat_id=self._escalation_chat_id,
                            message_id=escalation_message_id,
                            text=edit_text,
                            reply_markup=close_keyboard,
                        )
                        # Also send a threaded reply so staff get a notification
                        # (edits are silent — a reply surfaces in the thread)
                        escalation_dt = claimed_mapping.get("created_at", "")
                        escalation_date = escalation_dt[:10] if escalation_dt else "previous date"
                        await self._send_telegram_reply(
                            chat_id=self._escalation_chat_id,
                            reply_to_message_id=escalation_message_id,
                            text=(
                                f"⏰ *Auto-filed by daily sweep* (escalated {escalation_date})\n"
                                f"🎫 [{escaped_key}]({jira_url})"
                            ),
                            topic_id=escalation_topic_id,
                        )
                    filed += 1
                    LOGGER.info("Sweep filed %s for escalation %s", jira_key, mapping_id)
                else:
                    failed += 1
                    LOGGER.warning(
                        "Sweep track_as_ticket failed for %s: %s",
                        mapping_id,
                        result.get("error"),
                    )
                    await supabase_client.reactivate_escalation(mapping_id)

            except Exception:
                failed += 1
                LOGGER.exception("Sweep error for escalation %s", mapping_id)
                await supabase_client.reactivate_escalation(mapping_id)

            # Brief delay between calls to respect Jira rate limits (skip after last item)
            if idx < len(eligible) - 1:
                await asyncio.sleep(1)

        # Alert staff about escalations that aged out of the sweep window.
        # Always check — the batch cap guards against processing too many, but old
        # stragglers exist even when the batch is small.
        old_escalations = await supabase_client.get_old_unfiled_escalations(
            max_age_hours=max_age_hours
        )
        if old_escalations and self._escalation_chat_id:
            old_count = len(old_escalations)
            lines = [
                f"⚠️ *{old_count} escalation{'s' if old_count > 1 else ''} "
                f"older than {max_age_hours}h with no Jira ticket:*"
            ]
            chat_id_str = str(self._escalation_chat_id)
            channel_id = chat_id_str[4:] if chat_id_str.startswith("-100") else None
            for esc in old_escalations:
                username = esc.get("customer_username")
                email = esc.get("customer_email") or ""
                masked_email = (email[:2] + "***@" + email.split("@", 1)[1]) if "@" in email else ""
                label = _escape_telegram_markdown(username or masked_email or f"id:{esc['id']}")
                org_raw = esc.get("org_hashtag") or ""
                org_part = f" ({_escape_telegram_markdown(org_raw)})" if org_raw else ""
                msg_id = esc.get("escalation_message_id")
                if msg_id and isinstance(msg_id, int) and channel_id:
                    link = f"https://t.me/c/{channel_id}/{msg_id}"
                    lines.append(f"• {label}{org_part} — [View]({link})")
                else:
                    lines.append(f"• {label}{org_part}")
            if old_count == 20:
                lines.append("_(showing first 20 — check Supabase for full list)_")
            await self._send_telegram_message(
                chat_id=self._escalation_chat_id,
                text="\n".join(lines),
            )

        # Reconcile tracked escalations whose Jira ticket was closed outside the webhook
        # path, then notify each customer group of their remaining open issues in one message.
        reconciled = 0
        notified_groups = 0
        tracked = await supabase_client.get_active_tracked_escalations()
        if tracked:
            open_tracked: List[tuple] = []
            # Fetch all Jira ticket statuses concurrently (cap at 10 parallel to avoid rate limits).
            _sem = asyncio.Semaphore(10)

            async def _fetch_with_sem(key: str):
                async with _sem:
                    return await self._fetch_jira_issue_fields(key)

            keys_to_fetch = [esc.get("jira_ticket_key") or "" for esc in tracked]
            field_results = await asyncio.gather(
                *[_fetch_with_sem(k) for k in keys_to_fetch],
                return_exceptions=True,
            )

            for esc, fields_or_exc in zip(tracked, field_results):
                key = esc.get("jira_ticket_key") or ""
                if not key:
                    continue
                fields: Optional[Dict[str, Any]] = (
                    None
                    if isinstance(fields_or_exc, Exception)
                    else cast(Optional[Dict[str, Any]], fields_or_exc)
                )
                if fields and fields["is_done"]:
                    # Jira webhook was missed — close the mapping silently
                    LOGGER.info("Reconciling Jira-closed ticket %s (mapping %s)", key, esc["id"])
                    try:
                        client = supabase_client._get_client()
                        client.table("escalation_mappings").update(
                            {
                                "is_active": False,
                                "resolved_at": datetime.now(timezone.utc).isoformat(),
                            }
                        ).eq("id", esc["id"]).eq("is_active", True).execute()
                        reconciled += 1
                    except Exception:
                        LOGGER.warning("Could not reconcile mapping %s", esc["id"], exc_info=True)
                else:
                    open_tracked.append((esc, fields))

            # Group open tracked escalations by (customer_chat_id, customer_topic_id),
            # deduplicating by Jira ticket key so follow-up mappings pre-linked to the
            # same ticket don't trigger duplicate sweep notifications.
            groups: Dict[str, Dict] = {}
            for esc, fields in open_tracked:
                chat_id = esc.get("customer_chat_id") or ""
                topic_id = str(esc.get("customer_topic_id") or "")
                if not chat_id:
                    continue
                group_key = f"{chat_id}|{topic_id}"
                if group_key not in groups:
                    groups[group_key] = {
                        "chat_id": chat_id,
                        "topic_id": topic_id,
                        "issues": {},  # keyed by jira_ticket_key for dedup
                    }
                ticket_key = esc["jira_ticket_key"]
                if ticket_key not in groups[group_key]["issues"]:
                    groups[group_key]["issues"][ticket_key] = {
                        "key": ticket_key,
                        "summary": (fields or {}).get("summary", "") if fields else "",
                    }

            for group_info in groups.values():
                chat_id = group_info["chat_id"]
                topic_id = group_info["topic_id"]
                issues = group_info["issues"]
                sent_any = False
                for issue in issues.values():
                    escaped_key = _escape_telegram_markdown(issue["key"])
                    desc = _escape_telegram_markdown(_customer_facing_desc(issue["summary"]))
                    text = (
                        f"Your issue *{escaped_key}* about {desc} is in progress "
                        f"and will be attended to in working hours by our team."
                    )
                    try:
                        await self._send_telegram_message(
                            chat_id=chat_id,
                            text=text,
                            topic_id=int(topic_id) if topic_id else None,
                        )
                        sent_any = True
                    except Exception:
                        LOGGER.warning(
                            "Failed to send pending issue %s to chat_id=%s",
                            issue["key"],
                            chat_id,
                            exc_info=True,
                        )
                if sent_any:
                    notified_groups += 1

        summary = {
            "eligible": len(eligible),
            "filed": filed,
            "skipped": skipped,
            "failed": failed,
            "reconciled": reconciled,
            "notified_groups": notified_groups,
        }
        LOGGER.info("Escalation sweep complete: %s", summary)
        return summary

    async def recover_orphaned_claims(self) -> None:
        """Reactivate escalation mappings claimed but never completed (SIGTERM orphans).

        Runs once at startup after a brief delay.  Safe to repeat — reactivating an
        already-active row is a no-op because reactivate_escalation sets is_active=True
        unconditionally, and active rows are not returned by get_orphaned_claimed_escalations
        (which filters is_active=False).
        """
        supabase_client = self._get_supabase_client()
        if not supabase_client:
            return
        orphaned = await supabase_client.get_orphaned_claimed_escalations()
        if len(orphaned) == 50:  # matches the default limit in get_orphaned_claimed_escalations
            LOGGER.warning("Orphan recovery: hit row cap, may have more orphans")
        for row in orphaned:
            LOGGER.warning(
                "Startup recovery: orphaned escalation claim %s — reactivating", row["id"]
            )
            await supabase_client.reactivate_escalation(row["id"])
        if orphaned:
            LOGGER.info("Startup recovery: reactivated %d orphaned escalation(s)", len(orphaned))

    async def _edit_telegram_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Edit an existing Telegram message text and/or keyboard."""
        url = f"https://api.telegram.org/bot{self._bot_token}/editMessageText"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            from shared.utils.telegram_send import _get_session as _get_tg_session

            tg_session = _get_tg_session()
            async with tg_session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                result: Dict[str, Any] = await resp.json()
                return result
        except Exception as e:
            LOGGER.warning("editMessageText failed for msg %s: %s", message_id, e)
            return {"ok": False, "description": str(e)}

    async def _auto_create_jira_and_edit_message(
        self,
        mapping_id: str,
        escalation_message_id: int,
        escalation_topic_id: Optional[int],
        question_summary: str,
        conversation_context: Optional[str],
        customer_chat_id: Optional[str],
        customer_topic_id: Optional[str],
        organization_short_name: Optional[str],
    ) -> None:
        """Background task: create Jira ticket after-hours, then edit the escalation message."""
        try:
            supabase_client = self._get_supabase_client()
            raw_messages: list = []
            if supabase_client and customer_chat_id:
                try:
                    session_row = await supabase_client.get_session_by_chat_id(
                        source="telegram",
                        chat_id=customer_chat_id,
                        topic_id=customer_topic_id,
                    )
                    if session_row and session_row.id:
                        raw_messages = (
                            await supabase_client.get_messages(
                                session_uuid=session_row.id, max_age_hours=168, max_messages=50
                            )
                            or []
                        )
                except Exception as e:
                    LOGGER.debug("Could not fetch messages for after-hours Jira: %s", e)

            # Normalise ConversationMessage Pydantic objects → plain dicts
            messages: List[Dict[str, Any]] = [
                m
                if isinstance(m, dict)
                else {"role": getattr(m, "role", ""), "content": getattr(m, "content", "") or ""}
                for m in raw_messages
            ]

            # Prefer question_summary (captured at escalation time) over chat history
            first_user_msg = next(
                (m["content"] for m in messages if m.get("role") == "user" and m.get("content")),
                "",
            )
            best_summary = question_summary or first_user_msg or "Customer support escalation"
            summary = best_summary[:120].strip()
            if len(best_summary) > 120:
                summary += "..."

            dummy_mapping: Dict[str, Any] = {
                "id": mapping_id,
                "org_hashtag": f"#{organization_short_name}" if organization_short_name else None,
            }
            telegram_link = _build_telegram_msg_link(
                self._escalation_chat_id, escalation_message_id
            )
            description = _build_ticket_description(
                messages, dummy_mapping, question_summary, telegram_link=telegram_link
            )

            ticket_result = await self._create_jira_ticket(
                summary=summary,
                description=description,
                organization_short_name=organization_short_name,
                labels=[f"escalation-{mapping_id[:8]}"],
            )

            if ticket_result.get("success") and ticket_result.get("key"):
                jira_key = ticket_result["key"]
                LOGGER.info(
                    "After-hours auto-created Jira ticket %s for mapping %s", jira_key, mapping_id
                )

                # Store Jira key in DB
                if supabase_client:
                    try:
                        client = supabase_client._get_client()
                        client.table("escalation_mappings").update(
                            {"jira_ticket_key": jira_key}
                        ).eq("id", mapping_id).execute()
                    except Exception as e:
                        LOGGER.warning("Failed to store after-hours Jira key: %s", e)

                # Edit the Telegram escalation message to prepend the ticket ref while
                # preserving the original question text so staff retain context.
                jira_url = f"{self._jira_base_url}/browse/{jira_key}"
                escaped_jira_key = _escape_telegram_markdown(jira_key)
                edit_suffix = f"\n\n🎫 *Auto-tracked:* [{escaped_jira_key}]({jira_url})"
                if question_summary:
                    escaped_summary = _escape_telegram_markdown(question_summary)
                    base_text = (
                        f"🆘 *Customer Support Escalation*\n\n*Question:*\n{escaped_summary}"
                    )
                else:
                    base_text = "🆘 *Customer Support Escalation*"
                # Build close-only keyboard (Track already absent, now Jira exists)
                close_keyboard = build_escalation_track_keyboard(mapping_id, include_track=False)
                await self._edit_telegram_message(
                    chat_id=self._escalation_chat_id,
                    message_id=escalation_message_id,
                    text=f"{base_text}{edit_suffix}",
                    reply_markup=close_keyboard,
                )
            else:
                # Jira failed — restore Track button so staff can create manually
                LOGGER.warning(
                    "After-hours Jira creation failed for mapping %s: %s — restoring Track button",
                    mapping_id,
                    ticket_result.get("error"),
                )
                restore_keyboard = build_escalation_track_keyboard(mapping_id, include_track=True)
                if question_summary:
                    escaped_summary = _escape_telegram_markdown(question_summary)
                    fail_text = f"🆘 *Customer Support Escalation*\n\n*Question:*\n{escaped_summary}\n\n⚠️ _Auto-Jira failed — please track manually._"
                else:
                    fail_text = "🆘 *Customer Support Escalation*\n\n⚠️ _Auto-Jira failed — please track manually._"
                await self._edit_telegram_message(
                    chat_id=self._escalation_chat_id,
                    message_id=escalation_message_id,
                    text=fail_text,
                    reply_markup=restore_keyboard,
                )
        except Exception:
            LOGGER.exception("After-hours auto-Jira task failed for mapping %s", mapping_id)

    def _single_matching_org(
        self, jira_orgs: List[Dict[str, Any]], escalation_org_name: Optional[str]
    ) -> bool:
        """True only when the ticket has exactly 1 Jira org AND it fuzzy-matches escalation_org_name."""
        if len(jira_orgs) != 1 or not escalation_org_name:
            return False
        from shared.utils.grid_matcher import find_best_grid_match

        jira_org_name = jira_orgs[0].get("name", "")
        matched, _, _ = find_best_grid_match(jira_org_name, [escalation_org_name])
        return matched is not None

    async def _resolve_escalation_context_for_jira_key(
        self, issue_key: str, issue_fields: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Shared lookup for Jira webhook handlers.

        Returns a dict with mapping, escalation_org_id, escalation_topic_id,
        jira_orgs, and escalation_org_name — or None if no active mapping found.
        """
        supabase_client = self._get_supabase_client()
        if not supabase_client:
            LOGGER.error("No Supabase client — cannot route Jira event for %s", issue_key)
            return None

        mapping = await supabase_client.get_escalation_mapping_by_jira_key(issue_key)
        if not mapping:
            LOGGER.debug("No active escalation mapping for Jira ticket %s", issue_key)
            return None

        escalation_org_id: Optional[int] = mapping.get("organization_id")

        escalation_topic_id: Optional[int] = None
        if escalation_org_id:
            try:
                escalation_topic_id = await supabase_client.get_org_escalation_topic(
                    escalation_org_id
                )
            except Exception as e:
                LOGGER.warning(
                    "Could not fetch escalation topic for org %s: %s", escalation_org_id, e
                )

        org_field_id = os.getenv("JIRA_ORGANIZATION_FIELD_ID", "")
        jira_orgs: List[Dict[str, Any]] = (
            issue_fields.get(org_field_id) or [] if org_field_id else []
        )

        escalation_org_name: Optional[str] = None
        if escalation_org_id:
            try:
                from shared.auth import get_auth_service

                escalation_org_name = await get_auth_service().get_organization_short_name(
                    str(escalation_org_id)
                )
            except Exception as e:
                LOGGER.warning("Could not fetch org name for %s: %s", escalation_org_id, e)

        return {
            "mapping": mapping,
            "escalation_org_id": escalation_org_id,
            "escalation_topic_id": escalation_topic_id,
            "jira_orgs": jira_orgs,
            "escalation_org_name": escalation_org_name,
        }

    async def handle_jira_comment(self, payload: Dict[str, Any]) -> None:
        """Route a Jira comment_created webhook event to the escalation group and optionally the customer."""
        comment = payload.get("comment", {})
        issue_key = payload.get("issue", {}).get("key", "")

        # Guard: ignore bot's own comments (prevents infinite loop)
        author_email = comment.get("author", {}).get("emailAddress", "")
        if author_email and author_email.lower() == self._jira_email.lower():
            LOGGER.debug("Ignoring Jira comment authored by bot on %s", issue_key)
            return

        # Only route public ("Reply to customer") comments
        is_public = comment.get("jsdPublic", False)
        if not is_public:
            LOGGER.debug("Ignoring internal Jira comment on %s", issue_key)
            return

        # Extract comment text from ADF before the DB lookup (cheap operation)
        comment_text = _adf_to_text(comment.get("body", {}))
        if not comment_text.strip():
            LOGGER.debug("Jira comment on %s has no extractable text — skipping", issue_key)
            return

        issue_fields = payload.get("issue", {}).get("fields", {})
        ctx = await self._resolve_escalation_context_for_jira_key(issue_key, issue_fields)
        if ctx is None:
            return

        mapping = ctx["mapping"]
        escalation_message_id: int = mapping["escalation_message_id"]
        escalation_topic_id = ctx["escalation_topic_id"]
        jira_orgs = ctx["jira_orgs"]
        escalation_org_name = ctx["escalation_org_name"]

        author_name = comment.get("author", {}).get("displayName", "Support")

        # Post to escalation group
        LOGGER.info(
            "Routing Jira comment on %s to escalation group (topic=%s)",
            issue_key,
            escalation_topic_id,
        )
        escaped_text = _escape_telegram_markdown(comment_text)
        escaped_author = _escape_telegram_markdown(author_name)
        await self._send_telegram_message(
            chat_id=self._escalation_chat_id,
            text=f"💬 *{escaped_author}* (via Jira):\n\n{escaped_text}",
            topic_id=escalation_topic_id,
            reply_markup=None,
        )

        # Conditionally forward to customer
        if self._single_matching_org(jira_orgs, escalation_org_name):
            LOGGER.info(
                "Forwarding Jira comment on %s to customer (single matching org)", issue_key
            )
            await self.handle_support_reply(
                reply_to_message_id=escalation_message_id,
                reply_text=comment_text,
                from_username=author_name,
            )
        else:
            LOGGER.info(
                "Jira comment on %s not forwarded to customer: %d jira_orgs, escalation_org=%s",
                issue_key,
                len(jira_orgs),
                ctx["escalation_org_id"],
            )

    async def handle_jira_issue_updated(self, payload: Dict[str, Any]) -> None:
        """Handle a jira:issue_updated webhook — notify on ticket closure."""
        # Only act on transitions to Done/Closed
        closed = any(
            item.get("field") == "status" and item.get("toString", "") in ("Done", "Closed")
            for item in payload.get("changelog", {}).get("items", [])
        )
        if not closed:
            return

        issue_key = payload.get("issue", {}).get("key", "")
        issue_fields = payload.get("issue", {}).get("fields", {})
        ctx = await self._resolve_escalation_context_for_jira_key(issue_key, issue_fields)
        if ctx is None:
            return

        mapping = ctx["mapping"]
        escalation_topic_id = ctx["escalation_topic_id"]
        jira_orgs = ctx["jira_orgs"]
        escalation_org_name = ctx["escalation_org_name"]

        # Notify escalation group
        await self._send_telegram_message(
            chat_id=self._escalation_chat_id,
            text=f"✅ Jira ticket *{issue_key}* has been closed.",
            topic_id=escalation_topic_id,
            reply_markup=None,
        )

        notify_customer = self._single_matching_org(jira_orgs, escalation_org_name)
        await self.close_escalation_by_mapping(mapping=mapping, notify_customer=notify_customer)

    async def close_escalation_by_mapping(
        self, mapping: Dict[str, Any], notify_customer: bool = False
    ) -> None:
        """Close an escalation by its mapping row (used by Jira webhook closure handler).

        Uses an atomic UPDATE with an is_active=True guard to prevent duplicate customer
        notifications when Jira retries the webhook and two calls race concurrently.
        """
        session_id = mapping.get("session_id")
        mapping_id = mapping.get("id")
        if not session_id:
            LOGGER.warning("Cannot close escalation: no session_id in mapping %s", mapping_id)
            return

        # Atomically claim the close. If another handler already closed this mapping,
        # the UPDATE affects 0 rows and we skip the customer notification.
        supabase_client = self._get_supabase_client()
        if supabase_client and mapping_id:
            try:
                client = supabase_client._get_client()
                result = (
                    client.table("escalation_mappings")
                    .update(
                        {"is_active": False, "resolved_at": datetime.now(timezone.utc).isoformat()}
                    )
                    .eq("id", mapping_id)
                    .eq("is_active", True)  # only update if currently active
                    .execute()
                )
                if not result.data:
                    LOGGER.info(
                        "Mapping %s already closed by concurrent handler — skipping", mapping_id
                    )
                    return
            except Exception as e:
                LOGGER.warning(
                    "Could not atomically close mapping %s: %s — proceeding", mapping_id, e
                )

        if notify_customer:
            customer_chat_id = mapping.get("customer_chat_id", "")
            customer_topic_id = mapping.get("customer_topic_id")
            if customer_chat_id:
                await self.notify_customer_resolved(
                    customer_chat_id=customer_chat_id,
                    customer_topic_id=customer_topic_id,
                )

        await self.close_escalation(session_id)

    async def close_escalation(self, session_id: str) -> Dict[str, Any]:
        """
        Close all active escalations for a session.

        Deactivates ALL escalation mappings for the session and clears
        the session's is_escalated flag, releasing it back to the bot.
        Support only needs to reply "Closed" once to clear everything.

        Args:
            session_id: Session identifier

        Returns:
            Dict with success status
        """
        supabase_client = self._get_supabase_client()
        if not supabase_client:
            LOGGER.error("No Supabase client available - cannot close escalation")
            return {
                "success": False,
                "error": "Database not available",
            }

        success = await supabase_client.close_escalation(session_id)

        if success:
            LOGGER.info(f"Closed escalation for session {session_id}")
            return {
                "success": True,
                "message": "Escalation closed",
            }
        else:
            LOGGER.warning(f"Failed to close escalation for session {session_id}")
            return {
                "success": False,
                "error": "Failed to close escalation",
            }

    async def reopen_escalation(
        self, session_id: str, escalation_message_id: int
    ) -> Dict[str, Any]:
        """
        Reopen a previously closed escalation.

        Re-activates the mapping and sets the session back to escalated
        so user messages route to the escalation group again.

        Args:
            session_id: Session identifier
            escalation_message_id: The escalation message to reactivate

        Returns:
            Dict with success status
        """
        supabase_client = self._get_supabase_client()
        if not supabase_client:
            LOGGER.error("No Supabase client available - cannot reopen escalation")
            return {
                "success": False,
                "error": "Database not available",
            }

        success = await supabase_client.reopen_escalation(session_id, escalation_message_id)

        if success:
            LOGGER.info(f"Reopened escalation for session {session_id}")
            return {"success": True, "message": "Escalation reopened"}
        else:
            LOGGER.warning(f"Failed to reopen escalation for session {session_id}")
            return {"success": False, "error": "Failed to reopen escalation"}

    async def escalate_verification_failure(
        self,
        original_message: str,
        failed_response: str,
        verification_feedback: str,
        session_id: str,
        customer_chat_id: Optional[str] = None,
        customer_topic_id: Optional[str] = None,
        customer_username: Optional[str] = None,
        organization_short_name: Optional[str] = None,
        organization_id: Optional[int] = None,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Escalate a response that failed verification twice.

        This is called when the LLM-as-judge verification fails on both the original
        response and the regenerated response. The support team needs to review and
        respond manually.

        Args:
            original_message: The customer's original message
            failed_response: The response that failed verification
            verification_feedback: Why the response failed verification
            session_id: Session identifier for tracking
            customer_chat_id: Customer's Telegram chat ID
            customer_topic_id: Customer's topic/thread ID (if in a forum group)
            customer_username: Customer's username or name
            organization_short_name: Organization short name for hashtag

        Returns:
            Dict with success status and message
        """
        if not self.is_enabled():
            return {
                "success": False,
                "error": "Escalation service not configured",
            }

        try:
            # Build escalation message for verification failure
            message_parts = ["⚠️ *Response Verification Failed*\n"]
            message_parts.append(
                "_The AI response failed quality verification twice and requires human review._\n"
            )

            # Add organization hashtag if available
            if organization_short_name:
                clean_tag = "".join(c for c in organization_short_name if c.isalnum())
                message_parts.append(f"\n*Organization:* #{clean_tag}\n")

            # Add customer question
            escaped_question = _escape_telegram_markdown(original_message[:500])
            message_parts.append(f"\n*Customer Question:*\n{escaped_question}\n")

            # Add verification feedback
            escaped_feedback = _escape_telegram_markdown(verification_feedback[:300])
            message_parts.append(f"\n*Verification Failure Reason:*\n{escaped_feedback}\n")

            # Add failed response (truncated)
            escaped_response = _escape_telegram_markdown(failed_response[:300])
            message_parts.append(f"\n*Failed Response (truncated):*\n{escaped_response}\n")

            # Add customer context
            message_parts.append("\n*Customer Info:*")
            if customer_username:
                escaped_username = _escape_telegram_markdown(customer_username)
                message_parts.append(f"\n• Name: {escaped_username}")
            if customer_chat_id:
                message_parts.append(f"\n• Chat ID: `{customer_chat_id}`")

            message_parts.append(
                "\n\n_Reply to this message to respond to the customer. "
                "The bot will forward your response._"
            )

            message_text = "".join(message_parts)

            # Resolve org's forum topic (lazy get-or-create)
            escalation_topic_id: Optional[int] = None
            if organization_id:
                escalation_topic_id = await self._get_or_create_escalation_topic(
                    organization_id=organization_id,
                    org_name=organization_short_name or "",
                )

            # Send to escalation group
            LOGGER.info(
                f"Sending verification failure escalation to Telegram for session {session_id}"
            )
            result = await self._send_telegram_message(
                chat_id=self._escalation_chat_id,
                text=message_text,
                topic_id=escalation_topic_id,
            )

            if result.get("ok"):
                escalation_message_id = result.get("result", {}).get("message_id")

                # Store mapping for response routing
                if escalation_message_id and customer_chat_id:
                    supabase_client = self._get_supabase_client()
                    if supabase_client:
                        clean_tag = None
                        if organization_short_name:
                            clean_tag = "".join(c for c in organization_short_name if c.isalnum())
                        await supabase_client.save_escalation_mapping(
                            escalation_message_id=escalation_message_id,
                            customer_chat_id=customer_chat_id,
                            session_id=session_id,
                            customer_topic_id=customer_topic_id,
                            org_hashtag=f"#{clean_tag}" if clean_tag else None,
                            customer_username=customer_username,
                            reason="verification_failed",
                            organization_id=organization_id,
                            escalation_topic_id=escalation_topic_id,
                            question_text=original_message[:2000] if original_message else None,
                            thread_id=thread_id,
                        )
                        LOGGER.info(
                            f"Saved verification failure escalation to database: "
                            f"msg_id={escalation_message_id} → session={session_id}, "
                            f"reason=verification_failed"
                        )

                LOGGER.info(f"Verification failure escalated for session {session_id}")
                return {
                    "success": True,
                    "message": "Verification failure escalated to support team",
                    "escalation_message_id": escalation_message_id,
                    "is_escalated": True,
                }
            else:
                LOGGER.error(f"Failed to send verification failure escalation: {result}")
                return {
                    "success": False,
                    "error": f"Failed to escalate: {result.get('description', 'Unknown error')}",
                }

        except Exception as e:
            LOGGER.exception(f"Error escalating verification failure: {e}")
            return {
                "success": False,
                "error": f"Escalation failed: {str(e)}",
            }


def _customer_facing_desc(summary: str, max_words: int = 7) -> str:
    """Return a customer-facing short description, stripping AI 'User ...' phrasing."""
    # Handles both "User requested ..." and "User is following up on ..."
    text = re.sub(r"(?i)^user(?:\s+is\s+following\s+up\s+on)?\s+", "", summary).strip()
    words = text.split()[:max_words]
    # Fall back to truncating the original summary if stripping left nothing
    return " ".join(words) or " ".join(summary.split()[:max_words]) or "support issue"


def _build_telegram_msg_link(
    escalation_chat_id: Optional[str], message_id: Optional[int]
) -> Optional[str]:
    """Build a t.me deep-link to a specific message in the escalation support group."""
    if not escalation_chat_id or not message_id:
        return None
    try:
        chat_str = str(escalation_chat_id)
        # t.me/c/ deep-links only work for supergroups (IDs starting with -100).
        # Legacy group IDs and positive IDs cannot be linked via this format.
        if not chat_str.startswith("-100"):
            return None
        group_id = chat_str[4:]
        if not group_id.isdigit():
            return None
        return f"https://t.me/c/{group_id}/{message_id}"
    except Exception:
        LOGGER.warning("Failed to build Telegram message link for chat_id=%s", escalation_chat_id)
        return None


def _build_ticket_description(
    messages: List[Dict[str, Any]],
    escalation_mapping: Dict[str, Any],
    question_text: Optional[str] = None,
    telegram_link: Optional[str] = None,
) -> str:
    """Build a JIRA ticket description from chat history."""
    parts: List[str] = []

    # Header with escalation context
    org = escalation_mapping.get("org_hashtag", "")
    customer = escalation_mapping.get("customer_username", "Unknown")
    reason = escalation_mapping.get("reason", "")
    if org:
        parts.append(f"Organization: {org}")
    parts.append(f"Customer: {customer}")
    if reason:
        parts.append(f"Escalation reason: {reason}")
    if telegram_link:
        parts.append(f"Telegram: {telegram_link}")

    # Escalation question — the exact message that triggered the escalation
    stored = question_text or escalation_mapping.get("question_text") or ""
    if stored:
        parts.append("")
        parts.append("--- Escalation Message ---")
        parts.append(stored[:1000])

    parts.append("")

    # Chat history (most recent messages, truncated)
    parts.append("--- Chat History ---")
    for msg in messages[-20:]:
        # Support both plain dicts and Pydantic ConversationMessage objects
        if isinstance(msg, dict):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
        else:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", "") or ""
        if not content:
            continue
        label = "Customer" if role == "user" else "Bot" if role == "model" else role
        if len(content) > 300:
            content = content[:300] + "..."
        parts.append(f"{label}: {content}")

    description = "\n".join(parts)
    return description[:3000]


__all__ = ["EscalationService"]
