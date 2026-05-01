# mypy: disable-error-code="no-any-return"
"""Service for managing pending user decisions.

Stores decisions in database (future: Valkey) so they persist across HTTP
requests without relying on LangGraph checkpointing.

Pattern from: orchestrator/services/work_packet_service.py
- Lazy client initialization
- Comprehensive CRUD operations
- Error handling with logging

Decision Types:
- "duplicate": User asked for work that already exists (similar completed packet)
- "resume": User asked for work that has a failed/blocked packet

Usage:
    from orchestrator.services.pending_decision_service import PendingDecisionService

    service = PendingDecisionService()

    # Create a pending decision
    decision = await service.create_decision(
        session_id="telegram_abc123",
        decision_type="duplicate",
        context={
            "similar_work_packet": {...},
            "matched_expert_id": "lpp_expert",
            ...
        },
        prompt="I found similar work. Would you like to...",
    )

    # Check for pending decision when user responds
    pending = await service.get_pending_decision("telegram_abc123")
    if pending:
        # Parse user response and resolve
        await service.resolve_decision(pending["id"], "run_new")
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Decision type constants
DECISION_TYPE_DUPLICATE = "duplicate"
DECISION_TYPE_RESUME = "resume"
DECISION_TYPE_CANCEL = "cancel"

# Resolution constants for duplicate decisions
RESOLUTION_VIEW_EXISTING = "view_existing"
RESOLUTION_RUN_NEW = "run_new"

# Resolution constants for resume decisions
RESOLUTION_RESUME = "resume"
RESOLUTION_START_FRESH = "start_fresh"
RESOLUTION_ABANDON = "abandon"

# Shared resolution constant (used by both duplicate and resume)
RESOLUTION_CANCEL = "cancel"

# Auto-expire stale decisions
RESOLUTION_EXPIRED = "expired"


class PendingDecisionService:
    """CRUD operations for pending user decisions.

    Follows work_packet_service.py pattern with lazy initialization.

    This service manages multi-turn decision flows where the system presents
    options to the user and waits for their response. Unlike LangGraph
    checkpointing, this uses database storage which:
    - Survives HTTP request boundaries reliably
    - Provides an audit trail
    - Maps cleanly to Valkey keys for future migration
    """

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
    ):
        """Initialize the service with Supabase credentials.

        Args:
            supabase_url: Supabase project URL. Falls back to env vars.
            supabase_key: Supabase service key. Falls back to env vars.
        """
        self._supabase_url = (
            supabase_url or os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
        )
        self._supabase_key = (
            supabase_key or os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
        )
        self._client = None

    def _get_client(self):
        """Lazy initialize Supabase client."""
        if self._client is None and self._supabase_url and self._supabase_key:
            from supabase import create_client

            self._client = create_client(self._supabase_url, self._supabase_key)
        return self._client

    @property
    def client(self):
        """Get the Supabase client."""
        return self._get_client()

    # =========================================================================
    # CREATE Operations
    # =========================================================================

    async def create_decision(
        self,
        session_id: str,
        decision_type: str,
        context: Dict[str, Any],
        prompt: str,
        ttl_hours: int = 24,
    ) -> Dict[str, Any]:
        """Create a pending decision for this session.

        If there's already a pending decision for this session, it will be
        marked as expired before creating the new one.

        Args:
            session_id: Session identifier (e.g., "telegram_abc123")
            decision_type: Type of decision ("duplicate" or "resume")
            context: All data needed to handle the user's response.
                     For duplicate: similar_work_packet, matched_expert_id, etc.
                     For resume: resumable_packet, expert_command, etc.
            prompt: The message shown to the user (for logging/debugging)
            ttl_hours: Hours until decision expires (default 24)

        Returns:
            Created decision dictionary from database

        Raises:
            ValueError: If decision_type is not recognized
        """
        if decision_type not in (DECISION_TYPE_DUPLICATE, DECISION_TYPE_RESUME):
            raise ValueError(f"Unknown decision_type: {decision_type}")

        # Expire any existing pending decisions for this session
        await self._expire_pending_for_session(session_id)

        # Calculate expiration
        expires_at = (datetime.utcnow() + timedelta(hours=ttl_hours)).isoformat()

        data = {
            "session_id": session_id,
            "decision_type": decision_type,
            "context": context,
            "prompt": prompt,
            "expires_at": expires_at,
        }

        result = self.client.table("pending_decisions").insert(data).execute()
        decision = result.data[0]

        LOGGER.info(
            f"Created pending decision: {decision['id']} "
            f"(type={decision_type}, session={session_id})"
        )
        return decision

    # =========================================================================
    # READ Operations
    # =========================================================================

    async def get_pending_decision(
        self,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get any pending decision for this session.

        Returns the most recent unresolved, non-expired decision.

        Args:
            session_id: Session identifier

        Returns:
            Decision dictionary or None if no pending decision
        """
        now = datetime.utcnow().isoformat()

        result = (
            self.client.table("pending_decisions")
            .select("*")
            .eq("session_id", session_id)
            .is_("resolved_at", "null")
            .gt("expires_at", now)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if result.data:
            decision = result.data[0]
            LOGGER.debug(
                f"Found pending decision for session {session_id}: "
                f"{decision['id']} (type={decision['decision_type']})"
            )
            return decision

        return None

    async def get_decision_by_id(
        self,
        decision_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get a decision by its ID.

        Args:
            decision_id: UUID of the decision

        Returns:
            Decision dictionary or None if not found
        """
        result = self.client.table("pending_decisions").select("*").eq("id", decision_id).execute()

        return result.data[0] if result.data else None

    # =========================================================================
    # UPDATE Operations
    # =========================================================================

    async def resolve_decision(
        self,
        decision_id: str,
        resolution: str,
    ) -> Dict[str, Any]:
        """Mark a decision as resolved.

        Args:
            decision_id: UUID of the decision
            resolution: User's choice (e.g., "view_existing", "run_new",
                        "resume", "start_fresh", "abandon")

        Returns:
            Updated decision dictionary

        Raises:
            ValueError: If decision not found
        """
        decision = await self.get_decision_by_id(decision_id)
        if not decision:
            raise ValueError(f"Decision not found: {decision_id}")

        result = (
            self.client.table("pending_decisions")
            .update(
                {
                    "resolved_at": datetime.utcnow().isoformat(),
                    "resolution": resolution,
                }
            )
            .eq("id", decision_id)
            .execute()
        )

        LOGGER.info(
            f"Resolved decision {decision_id}: {resolution} "
            f"(type={decision['decision_type']}, session={decision['session_id']})"
        )
        return result.data[0]

    # =========================================================================
    # CLEANUP Operations
    # =========================================================================

    async def cleanup_expired(self) -> int:
        """Clean up expired decisions.

        Marks expired decisions as resolved with resolution='expired'.

        Returns:
            Count of decisions marked as expired
        """
        now = datetime.utcnow().isoformat()

        # Find expired, unresolved decisions
        result = (
            self.client.table("pending_decisions")
            .select("id")
            .is_("resolved_at", "null")
            .lt("expires_at", now)
            .execute()
        )

        if not result.data:
            return 0

        # Mark them as expired
        expired_ids = [d["id"] for d in result.data]
        self.client.table("pending_decisions").update(
            {
                "resolved_at": now,
                "resolution": RESOLUTION_EXPIRED,
            }
        ).in_("id", expired_ids).execute()

        LOGGER.info(f"Cleaned up {len(expired_ids)} expired pending decisions")
        return len(expired_ids)

    async def _expire_pending_for_session(self, session_id: str) -> int:
        """Expire any pending decisions for a session.

        Called before creating a new decision to ensure only one pending
        decision per session.

        Args:
            session_id: Session identifier

        Returns:
            Count of decisions expired
        """
        # Find pending decisions for this session
        result = (
            self.client.table("pending_decisions")
            .select("id")
            .eq("session_id", session_id)
            .is_("resolved_at", "null")
            .execute()
        )

        if not result.data:
            return 0

        # Mark them as expired
        expired_ids = [d["id"] for d in result.data]
        now = datetime.utcnow().isoformat()

        self.client.table("pending_decisions").update(
            {
                "resolved_at": now,
                "resolution": RESOLUTION_EXPIRED,
            }
        ).in_("id", expired_ids).execute()

        LOGGER.debug(f"Expired {len(expired_ids)} pending decisions for session {session_id}")
        return len(expired_ids)


__all__ = [
    "PendingDecisionService",
    "DECISION_TYPE_DUPLICATE",
    "DECISION_TYPE_RESUME",
    "RESOLUTION_VIEW_EXISTING",
    "RESOLUTION_RUN_NEW",
    "RESOLUTION_RESUME",
    "RESOLUTION_START_FRESH",
    "RESOLUTION_ABANDON",
    "RESOLUTION_CANCEL",
    "RESOLUTION_EXPIRED",
]
