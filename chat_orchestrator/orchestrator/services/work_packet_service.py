# mypy: disable-error-code="no-any-return"
"""Service for managing agent work packets.

Pattern from: orchestrator/services/supabase_client.py
- Lazy client initialization
- Comprehensive CRUD operations
- Error handling with logging

Usage:
    from orchestrator.services.work_packet_service import WorkPacketService

    service = WorkPacketService()

    # Create a new packet
    packet = await service.create_packet(
        packet_type="grid_analysis",
        packet_title="Grid Analysis: ExampleGrid January 2026",
        packet_goal="Analyze grid performance and identify issues",
        assigned_expert="grid_analyst",
        packet_inputs={"grid": {"grid_name": "ExampleGrid"}, "time_range": {...}},
    )

    # Get packet by ID
    packet = await service.get_packet("grid_analysis_20260120_abc123")

    # Update state
    packet = await service.update_state(packet_id, {"metrics_fetched": True})

    # Complete a step
    packet = await service.complete_step(packet_id, "fetch_metrics", next_step="analyze")
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from orchestrator.models.work_packets import (
    PACKET_TYPE_SCHEMAS,
    PacketStatus,
    get_initial_state,
    validate_packet_data,
)
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class WorkPacketService:
    """CRUD operations for agent work packets.

    Follows supabase_client.py pattern with lazy initialization.
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

    async def create_packet(
        self,
        packet_type: str,
        packet_title: str,
        packet_goal: str,
        assigned_expert: str,
        packet_inputs: Dict[str, Any],
        organization_id: Optional[int] = None,
        requested_by_email: Optional[str] = None,
        session_id: Optional[str] = None,
        additional_session_ids: Optional[List[str]] = None,
        external_system: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new work packet.

        Args:
            packet_type: Type of work (grid_analysis, kpi_report, etc.)
            packet_title: Human-readable title
            packet_goal: What the expert is trying to achieve
            assigned_expert: Expert handling this packet
            packet_inputs: Input data for the packet
            organization_id: Organization context
            requested_by_email: User who requested the work
            session_id: Session where packet was created
            external_system: External system for output (google_docs, jira)
            external_id: ID in external system

        Returns:
            Created packet dictionary from database

        Raises:
            ValueError: If packet_type has registered schema and inputs don't match
        """
        # Validate inputs against schema if registered
        # Skip validation if inputs contain "raw_request" - this indicates unparsed
        # user input that will be structured by the first workflow step (LLM parsing)
        if packet_type in PACKET_TYPE_SCHEMAS and "raw_request" not in packet_inputs:
            validate_packet_data(packet_type, "inputs", packet_inputs)

        # Generate human-readable packet_id
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        packet_id = f"{packet_type}_{timestamp}_{uuid4().hex[:6]}"

        # Initialize state from schema (if registered)
        initial_state = get_initial_state(packet_type)

        data = {
            "packet_id": packet_id,
            "packet_type": packet_type,
            "packet_title": packet_title,
            "packet_goal": packet_goal,
            "assigned_expert": assigned_expert,
            "packet_status": PacketStatus.PENDING.value,
            "packet_inputs": packet_inputs,
            "packet_state": initial_state,
            "packet_outputs": {},
            "organization_id": organization_id,
            "requested_by_email": requested_by_email,
            "requested_in_session": session_id,
            "sessions_involved": list(
                dict.fromkeys(([session_id] if session_id else []) + (additional_session_ids or []))
            ),
            "external_system": external_system,
            "external_id": external_id,
        }

        result = self.client.table("agent_work_packets").insert(data).execute()
        packet = result.data[0]

        await self._log_event(
            packet["id"],
            "created",
            None,
            f"Packet created: {packet_title}",
            session_id=session_id,
            triggered_by="user",
        )

        LOGGER.info(f"Created work packet: {packet_id}")
        return packet

    # =========================================================================
    # READ Operations
    # =========================================================================

    async def get_packet(self, packet_id: str) -> Optional[Dict[str, Any]]:
        """Get packet by packet_id or UUID.

        Args:
            packet_id: Either human-readable packet_id or UUID

        Returns:
            Packet dictionary or None if not found
        """
        # Try packet_id first (human-readable)
        result = (
            self.client.table("agent_work_packets").select("*").eq("packet_id", packet_id).execute()
        )

        if result.data:
            return result.data[0]

        # Try UUID
        result = self.client.table("agent_work_packets").select("*").eq("id", packet_id).execute()

        return result.data[0] if result.data else None

    async def get_active_packets_for_session(
        self,
        session_id: str,
        auto_fail_stale: bool = True,
        stale_threshold_minutes: int = 15,
        awaiting_input_timeout_minutes: int = int(
            os.getenv("AWAITING_INPUT_TIMEOUT_MINUTES", "180")
        ),
        pending_timeout_minutes: int = 5,
    ) -> List[Dict[str, Any]]:
        """Get all active packets that involve this session.

        Automatically times out stale packets based on their status:
        - pending: 5 minutes (startup should be quick)
        - in_progress: 15 minutes (workflow execution)
        - awaiting_input: 180 minutes (user response)

        Args:
            session_id: Session identifier
            auto_fail_stale: If True, automatically mark stale packets as failed
            stale_threshold_minutes: Minutes of inactivity before in_progress packet is stale
            awaiting_input_timeout_minutes: Minutes before awaiting_input packet times out
            pending_timeout_minutes: Minutes before pending packet times out

        Returns:
            List of active packet dictionaries (excludes auto-failed stale packets)
        """
        result = (
            self.client.table("agent_work_packets")
            .select("*")
            .contains("sessions_involved", [session_id])
            .in_("packet_status", ["pending", "in_progress", "awaiting_input"])
            .order("updated_at", desc=True)
            .execute()
        )

        if not result.data:
            return []

        # Check for stale packets and optionally auto-fail them
        active_packets = []
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)

        for packet in result.data:
            updated_at_str = packet.get("updated_at")
            if not updated_at_str:
                active_packets.append(packet)
                continue

            # Parse updated_at timestamp
            try:
                updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                age_minutes = (now - updated_at).total_seconds() / 60
                packet_status = packet.get("packet_status")

                # Determine if packet is stale based on status and appropriate timeout
                is_stale = False
                timeout_used = 0
                if packet_status == "pending" and age_minutes > pending_timeout_minutes:
                    is_stale = True
                    timeout_used = pending_timeout_minutes
                elif packet_status == "in_progress" and age_minutes > stale_threshold_minutes:
                    is_stale = True
                    timeout_used = stale_threshold_minutes
                elif (
                    packet_status == "awaiting_input"
                    and age_minutes > awaiting_input_timeout_minutes
                ):
                    is_stale = True
                    timeout_used = awaiting_input_timeout_minutes

                if is_stale:
                    if auto_fail_stale:
                        # Auto-fail stale packet
                        reason = (
                            f"Packet timed out after {int(age_minutes)} minutes "
                            f"(status: {packet_status}, timeout: {timeout_used} min)"
                        )
                        LOGGER.warning(
                            f"Auto-failing stale packet {packet['packet_id']} - {reason}"
                        )
                        try:
                            await self.fail_packet(
                                packet["packet_id"],
                                reason,
                                session_id,
                                error_state={
                                    "auto_failed": True,
                                    "stale_minutes": int(age_minutes),
                                    "timeout_type": packet_status,
                                },
                            )
                        except Exception as e:
                            LOGGER.error(f"Failed to auto-fail stale packet: {e}")
                        # Don't include in active packets
                        continue
                    else:
                        # Mark as stale but still include
                        packet["_is_stale"] = True
                        packet["_stale_minutes"] = int(age_minutes)

                active_packets.append(packet)

            except (ValueError, TypeError) as e:
                LOGGER.warning(
                    f"Could not parse updated_at for packet {packet.get('packet_id')}: {e}"
                )
                active_packets.append(packet)

        return active_packets

    async def get_active_packets_for_user(
        self,
        email: str,
        organization_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get all active packets for a user.

        Args:
            email: User's email address
            organization_id: Optional org filter

        Returns:
            List of active packet dictionaries
        """
        query = (
            self.client.table("agent_work_packets")
            .select("*")
            .eq("requested_by_email", email)
            .in_("packet_status", ["pending", "in_progress", "awaiting_input"])
        )

        if organization_id:
            query = query.eq("organization_id", organization_id)

        result = query.order("updated_at", desc=True).execute()
        return result.data

    async def get_packets_by_expert(
        self,
        expert_id: str,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get packets assigned to a specific expert.

        Args:
            expert_id: Expert identifier
            status: Optional status filter
            limit: Maximum packets to return

        Returns:
            List of packet dictionaries
        """
        query = self.client.table("agent_work_packets").select("*").eq("assigned_expert", expert_id)

        if status:
            query = query.eq("packet_status", status)

        result = query.order("updated_at", desc=True).limit(limit).execute()
        return result.data

    async def get_packet_logs(
        self,
        packet_id: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get execution logs for a packet.

        Args:
            packet_id: Packet's packet_id or UUID

        Returns:
            List of log entries
        """
        # Get packet UUID first
        packet = await self.get_packet(packet_id)
        if not packet:
            return []

        result = (
            self.client.table("agent_work_packet_logs")
            .select("*")
            .eq("packet_id", packet["id"])
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data

    async def get_resumable_packets_for_session(
        self,
        session_id: str,
        max_age_hours: int = 24,
    ) -> List[Dict[str, Any]]:
        """Get failed/blocked packets that can be resumed.

        These are packets that stopped due to an error but could be
        retried after the underlying issue is fixed (e.g., API timeout,
        permission issue, missing data).

        Args:
            session_id: Session identifier
            max_age_hours: Only return packets updated within this time

        Returns:
            List of resumable packet dictionaries, most recent first
        """
        from datetime import timedelta

        cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()

        # "failed" covers both genuine step failures and packets interrupted by
        # deployment (interrupt_packet uses fail_packet with auto_resumable=True).
        # "blocked" covers packets waiting on an external dependency.
        result = (
            self.client.table("agent_work_packets")
            .select("*")
            .contains("sessions_involved", [session_id])
            .in_("packet_status", ["failed", "blocked"])
            .gte("updated_at", cutoff)
            .order("updated_at", desc=True)
            .execute()
        )

        return result.data

    async def find_similar_completed(
        self,
        packet_type: str,
        key_entity: str,
        since_days: int = 14,
        organization_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Find recently completed packets matching type and key entity.

        Used for deduplication - detecting if similar work was already done.

        Args:
            packet_type: Type of packet (grid_analysis, kpi_report, etc.)
            key_entity: Key identifier to match (grid name, ticket ID, etc.)
            since_days: Only look back this many days
            organization_id: Optional org filter

        Returns:
            List of matching completed packets, most recent first
        """
        from datetime import timedelta

        since_date = (datetime.utcnow() - timedelta(days=since_days)).isoformat()

        query = (
            self.client.table("agent_work_packets")
            .select("*")
            .eq("packet_type", packet_type)
            .eq("packet_status", "completed")
            .gte("completed_at", since_date)
            .order("completed_at", desc=True)
        )

        if organization_id:
            query = query.eq("organization_id", organization_id)

        result = query.execute()

        # Filter by key entity in packet_goal or packet_inputs
        matches = []
        key_lower = key_entity.lower()
        for packet in result.data:
            goal = (packet.get("packet_goal") or "").lower()
            inputs_str = str(packet.get("packet_inputs") or {}).lower()

            if key_lower in goal or key_lower in inputs_str:
                matches.append(packet)

        return matches

    async def find_packets_by_entity(
        self,
        packet_type: str,
        key_entity: str,
        organization_id: Optional[int] = None,
        since_days: int = 90,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Find packets of ANY status matching type and key entity.

        Broader than `find_similar_completed` (which only searches
        `packet_status == "completed"`): this looks across every status --
        pending, in_progress, awaiting_input, failed, blocked, cancelled, and
        completed -- so a caller can tell whether a packet for this entity
        already exists at all, and if so, where it currently sits in its
        lifecycle. Used by `expert_meta_tools.find_packet` so staff can ask
        "is there already an LPP for Foo?" and get a real answer regardless
        of whether that packet is still running, paused for input, or done.

        Uses the same key_entity substring-match convention as
        `find_similar_completed` (checked against `packet_goal` and
        `packet_inputs`, case-insensitive) for consistency with the rest of
        this file's duplicate-detection logic.

        Args:
            packet_type: Type of packet (light_preliminary_package, etc.)
            key_entity: Key identifier to match (grid/site name, ticket ID, etc.)
            organization_id: Optional org filter
            since_days: Only look back this many days (by updated_at)
            limit: Maximum packets to inspect (most recently updated first)

        Returns:
            List of matching packets across all statuses, most recently
            updated first.
        """
        from datetime import timedelta

        since_date = (datetime.utcnow() - timedelta(days=since_days)).isoformat()

        query = (
            self.client.table("agent_work_packets")
            .select("*")
            .eq("packet_type", packet_type)
            .gte("updated_at", since_date)
            .order("updated_at", desc=True)
            .limit(limit)
        )

        if organization_id:
            query = query.eq("organization_id", organization_id)

        result = query.execute()

        key_lower = key_entity.lower()
        matches = []
        for packet in result.data:
            goal = (packet.get("packet_goal") or "").lower()
            inputs_str = str(packet.get("packet_inputs") or {}).lower()

            if key_lower in goal or key_lower in inputs_str:
                matches.append(packet)

        return matches

    # =========================================================================
    # UPDATE Operations
    # =========================================================================

    async def update_state(
        self,
        packet_id: str,
        state_updates: Dict[str, Any],
        session_id: Optional[str] = None,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """Update packet state (merge with existing), using optimistic concurrency.

        Plain read-then-merge-then-write is a lost-update race: if two
        callers (e.g. a running full workflow AND a `run_single_step` call)
        update the same packet concurrently, the second writer's unconditional
        `.update()` can silently clobber the first writer's changes. Instead,
        this conditions the UPDATE on both `id` and the `state_version` value
        read moments earlier (same conditional-update pattern as
        `claim_signing()`). If a concurrent writer commits in between, the
        conditional update matches zero rows; we re-fetch the packet, re-merge
        `state_updates` on top of the FRESH `packet_state` (never the stale
        copy from before), and retry -- up to `max_retries` attempts total.

        Args:
            packet_id: Packet's packet_id or UUID
            state_updates: Dictionary of state updates to merge
            session_id: Session making the update
            max_retries: Maximum conditional-update attempts before giving up
                due to sustained concurrent-writer contention (default 3)

        Returns:
            Updated packet dictionary

        Raises:
            ValueError: If packet not found
            RuntimeError: If max_retries conditional-update attempts all lose
                the race to a concurrent writer
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        for attempt in range(max_retries):
            # Merge state on top of whatever this iteration's packet snapshot
            # is (the original read on attempt 0, a fresh re-fetch on later
            # attempts) -- never the stale snapshot from a prior attempt.
            current_state = packet.get("packet_state") or {}
            new_state = {**current_state, **state_updates}

            # Validate if schema exists
            if packet["packet_type"] in PACKET_TYPE_SCHEMAS:
                validate_packet_data(packet["packet_type"], "state", new_state)

            # Update sessions_involved if new session
            sessions = packet.get("sessions_involved", []) or []
            if session_id and session_id not in sessions:
                sessions = sessions + [session_id]

            current_version = packet.get("state_version", 0) or 0

            result = (
                self.client.table("agent_work_packets")
                .update(
                    {
                        "packet_state": new_state,
                        "sessions_involved": sessions,
                        "state_version": current_version + 1,
                    }
                )
                .eq("id", packet["id"])
                .eq("state_version", current_version)
                .execute()
            )

            if result.data:
                # Log message always describes the CALLER's originally-requested
                # state_updates keys (the method parameter, never mutated by the
                # retry loop) -- not any intermediate merge artifact.
                await self._log_event(
                    packet["id"],
                    "state_update",
                    None,
                    f"State updated: {list(state_updates.keys())}",
                    session_id=session_id,
                    input_data=state_updates,
                )
                return result.data[0]

            # Lost the race to a concurrent writer: state_version no longer
            # matched by the time our conditional update ran.
            LOGGER.warning(
                f"update_state: conditional update lost race for packet {packet_id} "
                f"(attempt {attempt + 1}/{max_retries})"
            )
            if attempt == max_retries - 1:
                break  # out of retries -- skip the pointless final re-fetch

            # Re-fetch and re-merge on the FRESH packet_state before retrying.
            packet = await self.get_packet(packet_id)
            if not packet:
                raise ValueError(f"Packet not found during retry: {packet_id}")

        raise RuntimeError(
            f"update_state: exceeded {max_retries} retries for packet {packet_id} "
            "due to concurrent writes"
        )

    async def claim_signing(self, packet_id: str) -> bool:
        """Atomically transition signing_status from "pending"/"failed" → "signing".

        Also reclaims a packet stuck in "signing" with no signed_at and updated_at
        older than 10 minutes (process-death recovery).

        Returns True if the claim succeeded (this caller owns the signing slot).
        Returns False if the status was already "signing" (active) or "signed".
        Uses a conditional Supabase update so two concurrent requests cannot both succeed.
        """
        from datetime import datetime, timezone

        packet = await self.get_packet(packet_id)
        if not packet:
            return False

        current_state = packet.get("packet_state") or {}
        status = current_state.get("signing_status")

        if status in ("pending", "failed"):
            status_filter = '("pending","failed")'
        elif status == "signing" and not current_state.get("signing_signed_at"):
            # Recover a packet stranded by a previous process death.
            # Only reclaim if the last update was >10 minutes ago.
            updated_at_str = packet.get("updated_at", "")
            try:
                updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                age_minutes = (datetime.now(timezone.utc) - updated_at).total_seconds() / 60
            except (ValueError, TypeError):
                age_minutes = 0
            if age_minutes < 10:
                return False  # Still being processed — don't clobber it
            status_filter = '("signing")'
        else:
            return False

        new_state = {**current_state, "signing_status": "signing"}

        result = (
            self.client.table("agent_work_packets")
            .update({"packet_state": new_state})
            .eq("id", packet["id"])
            .filter("packet_state->>signing_status", "in", status_filter)
            .execute()
        )

        return bool(result.data)

    async def complete_step(
        self,
        packet_id: str,
        step_name: str,
        next_step: Optional[str] = None,
        state_updates: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mark a step as completed and optionally move to next.

        Args:
            packet_id: Packet's packet_id or UUID
            step_name: Name of completed step
            next_step: Optional name of next step
            state_updates: Optional state updates to apply
            session_id: Session completing the step

        Returns:
            Updated packet dictionary
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        steps_completed = packet.get("steps_completed", []) or []
        if step_name not in steps_completed:
            steps_completed = steps_completed + [step_name]

        update_data: Dict[str, Any] = {
            "steps_completed": steps_completed,
            "current_step": next_step,
        }

        if state_updates:
            current_state = packet["packet_state"] or {}
            update_data["packet_state"] = {**current_state, **state_updates}

        sessions = packet.get("sessions_involved", []) or []
        if session_id and session_id not in sessions:
            update_data["sessions_involved"] = sessions + [session_id]

        result = (
            self.client.table("agent_work_packets")
            .update(update_data)
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "step_complete",
            step_name,
            f"Completed step: {step_name}",
            session_id=session_id,
        )

        return result.data[0]

    async def start_packet(
        self,
        packet_id: str,
        first_step: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start working on a packet.

        Args:
            packet_id: Packet's packet_id or UUID
            first_step: Name of first workflow step
            session_id: Session starting the work

        Returns:
            Updated packet dictionary
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        sessions = packet.get("sessions_involved", []) or []
        if session_id and session_id not in sessions:
            sessions = sessions + [session_id]

        result = (
            self.client.table("agent_work_packets")
            .update(
                {
                    "packet_status": PacketStatus.IN_PROGRESS.value,
                    "current_step": first_step,
                    "started_at": datetime.utcnow().isoformat(),
                    "sessions_involved": sessions,
                }
            )
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "started",
            first_step,
            "Started packet execution",
            session_id=session_id,
        )

        return result.data[0]

    async def set_awaiting_input(
        self,
        packet_id: str,
        prompt: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mark packet as awaiting user input.

        Args:
            packet_id: Packet's packet_id or UUID
            prompt: Question/prompt to ask user
            session_id: Current session

        Returns:
            Updated packet dictionary
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        current_state = packet["packet_state"] or {}
        new_state = {
            **current_state,
            "awaiting_user_input": True,
            "user_prompt": prompt,
        }

        result = (
            self.client.table("agent_work_packets")
            .update(
                {
                    "packet_status": PacketStatus.AWAITING_INPUT.value,
                    "packet_state": new_state,
                }
            )
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "user_input",
            packet.get("current_step"),
            f"Awaiting user input: {prompt[:100]}",
            session_id=session_id,
        )

        return result.data[0]

    async def resume_from_input(
        self,
        packet_id: str,
        user_input: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resume packet after receiving user input.

        Args:
            packet_id: Packet's packet_id or UUID
            user_input: User's response
            session_id: Current session

        Returns:
            Updated packet dictionary
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        current_state = packet["packet_state"] or {}
        new_state = {
            **current_state,
            "awaiting_user_input": False,
            "user_prompt": None,
            "last_user_input": user_input,
        }

        sessions = packet.get("sessions_involved", []) or []
        if session_id and session_id not in sessions:
            sessions = sessions + [session_id]

        result = (
            self.client.table("agent_work_packets")
            .update(
                {
                    "packet_status": PacketStatus.IN_PROGRESS.value,
                    "packet_state": new_state,
                    "sessions_involved": sessions,
                }
            )
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "user_input",
            packet.get("current_step"),
            f"Received user input: {user_input[:100]}",
            session_id=session_id,
            input_data={"user_input": user_input},
        )

        return result.data[0]

    async def complete_packet(
        self,
        packet_id: str,
        outputs: Dict[str, Any],
        external_url: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Complete a packet with final outputs.

        Args:
            packet_id: Packet's packet_id or UUID
            outputs: Final output data
            external_url: URL to external document (if created)
            session_id: Session completing the packet

        Returns:
            Updated packet dictionary
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        # Validate outputs if schema exists, but skip for simple summary outputs
        # (full validation happens when outputs are fully structured with all required fields)
        is_simple_summary = "summary" in outputs and "external_doc" not in outputs
        if packet["packet_type"] in PACKET_TYPE_SCHEMAS and not is_simple_summary:
            validate_packet_data(packet["packet_type"], "outputs", outputs)

        update_data: Dict[str, Any] = {
            "packet_status": PacketStatus.COMPLETED.value,
            "packet_outputs": outputs,
            "completed_at": datetime.utcnow().isoformat(),
        }

        if external_url:
            update_data["external_url"] = external_url

        result = (
            self.client.table("agent_work_packets")
            .update(update_data)
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "completed",
            None,
            "Packet completed successfully",
            output_data={"outputs_keys": list(outputs.keys())},
            session_id=session_id,
        )

        LOGGER.info(f"Completed work packet: {packet['packet_id']}")
        return result.data[0]

    async def fail_packet(
        self,
        packet_id: str,
        error_message: str,
        session_id: Optional[str] = None,
        error_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Mark packet as failed.

        Args:
            packet_id: Packet's packet_id or UUID
            error_message: Error description
            session_id: Session where failure occurred
            error_state: Optional error context to store for later resumption.
                         Should include keys like 'last_error', 'error_step', 'error_time'

        Returns:
            Updated packet dictionary
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        update_data: Dict[str, Any] = {
            "packet_status": PacketStatus.FAILED.value,
        }

        # Store error context in packet_state for later resumption.
        # auto_resumable and related recovery-control keys are blocked here —
        # they must only be set via interrupt_packet to prevent injection.
        _RECOVERY_ONLY_KEYS = frozenset(
            {"auto_resumable", "auto_retry_count", "recovery_pending", "recovery_at"}
        )
        if error_state:
            current_state = packet.get("packet_state") or {}
            safe_error_state = {
                k: v for k, v in error_state.items() if k not in _RECOVERY_ONLY_KEYS
            }
            new_state = {**current_state, **safe_error_state}
            update_data["packet_state"] = new_state

        result = (
            self.client.table("agent_work_packets")
            .update(update_data)
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "failed",
            packet.get("current_step"),
            error_message,
            error_data={"error": error_message, **(error_state or {})},
            session_id=session_id,
        )

        LOGGER.error(f"Failed work packet: {packet['packet_id']} - {error_message}")
        return result.data[0]

    async def interrupt_packet(
        self,
        packet_id: Optional[str],
        interrupted_step: Optional[str],
        session_id: Optional[str] = None,
    ) -> None:
        """Mark a packet as interrupted by process shutdown (SIGTERM/SIGKILL).

        Uses fail_packet() with auto_resumable=True in packet_state so that:
        - The packet surfaces in get_resumable_packets_for_session() (failed status)
        - The startup recovery scan can find and auto-resume it
        - The ask_resume_failed routing skips the user prompt for auto-resumable packets

        This is idempotent — safe to call multiple times.
        """
        if not packet_id:
            return
        try:
            packet = await self.get_packet(packet_id)
            if not packet:
                return
            current_state = packet.get("packet_state") or {}
            new_state = {
                **current_state,
                "auto_resumable": True,
                "interrupted_step": interrupted_step,
                "interrupted_reason": "sigterm",
                "interrupted_at": datetime.now(timezone.utc).isoformat(),
            }
            self.client.table("agent_work_packets").update(
                {
                    "packet_status": PacketStatus.FAILED.value,
                    "packet_state": new_state,
                }
            ).eq("id", packet["id"]).execute()
            await self._log_event(
                packet["id"],
                "interrupted",
                interrupted_step,
                f"Process shutdown (SIGTERM) — interrupted at step '{interrupted_step}'",
                session_id=session_id,
            )
        except Exception:
            LOGGER.warning(
                "interrupt_packet: best-effort during shutdown, swallowing exception",
                exc_info=True,
            )

    async def retry_packet(
        self,
        packet_id: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reset a failed/blocked packet to in_progress for retry.

        Clears error state but keeps progress (steps_completed).
        Used when a human has fixed the underlying issue.
        Also accepts auto_resumable packets (interrupted by deployment).

        Args:
            packet_id: Packet's packet_id or UUID
            session_id: Session initiating the retry

        Returns:
            Updated packet dictionary ready for resumption

        Raises:
            ValueError: If packet not found or not in retryable state
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        if packet["packet_status"] not in ["failed", "blocked"]:
            raise ValueError(
                f"Cannot retry packet in status: {packet['packet_status']}. "
                "Only failed or blocked packets can be retried."
            )

        # Clear error state, keep progress
        current_state = packet.get("packet_state") or {}
        new_state = {
            k: v
            for k, v in current_state.items()
            if k not in ["last_error", "error_step", "error_time", "awaiting_user_input"]
        }
        new_state["retry_count"] = current_state.get("retry_count", 0) + 1
        new_state["last_retry_at"] = datetime.utcnow().isoformat()

        # Update sessions_involved
        sessions = packet.get("sessions_involved", []) or []
        if session_id and session_id not in sessions:
            sessions = sessions + [session_id]

        result = (
            self.client.table("agent_work_packets")
            .update(
                {
                    "packet_status": PacketStatus.IN_PROGRESS.value,
                    "packet_state": new_state,
                    "sessions_involved": sessions,
                }
            )
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "retried",
            packet.get("current_step"),
            f"Packet retried (attempt #{new_state['retry_count']})",
            session_id=session_id,
            triggered_by="user",
        )

        LOGGER.info(
            f"Retrying work packet: {packet['packet_id']} (attempt #{new_state['retry_count']})"
        )
        return result.data[0]

    async def block_packet(
        self,
        packet_id: str,
        reason: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mark packet as blocked (waiting for external resolution).

        Use this when the packet can't proceed but isn't failed -
        e.g., waiting for human approval, external system maintenance.

        Args:
            packet_id: Packet's packet_id or UUID
            reason: Why the packet is blocked
            session_id: Current session

        Returns:
            Updated packet dictionary
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        current_state = packet.get("packet_state") or {}
        new_state = {
            **current_state,
            "blocked_reason": reason,
            "blocked_at": datetime.utcnow().isoformat(),
        }

        result = (
            self.client.table("agent_work_packets")
            .update(
                {
                    "packet_status": PacketStatus.BLOCKED.value,
                    "packet_state": new_state,
                }
            )
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "blocked",
            packet.get("current_step"),
            f"Packet blocked: {reason}",
            session_id=session_id,
        )

        LOGGER.warning(f"Blocked work packet: {packet['packet_id']} - {reason}")
        return result.data[0]

    async def cancel_packet(
        self,
        packet_id: str,
        reason: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel a packet.

        Args:
            packet_id: Packet's packet_id or UUID
            reason: Cancellation reason
            session_id: Session cancelling the packet

        Returns:
            Updated packet dictionary
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        result = (
            self.client.table("agent_work_packets")
            .update(
                {
                    "packet_status": PacketStatus.CANCELLED.value,
                }
            )
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "cancelled",
            packet.get("current_step"),
            f"Packet cancelled: {reason}",
            session_id=session_id,
        )

        LOGGER.info(f"Cancelled work packet: {packet['packet_id']} - {reason}")
        return result.data[0]

    async def cancel_active_packets_of_type(
        self,
        session_id: str,
        packet_type: str,
        reason: str = "Superseded by new workflow request",
    ) -> int:
        """Cancel all active packets of a specific type for a session.

        When a user starts a new workflow of the same type, we auto-cancel
        any existing active workflows to prevent confusion and stuck states.

        Args:
            session_id: Session identifier
            packet_type: Packet type to cancel (e.g., "grids_technical_review")
            reason: Cancellation reason

        Returns:
            Number of packets cancelled
        """
        # Get all active packets of this type for the session
        result = (
            self.client.table("agent_work_packets")
            .select("id,packet_id")
            .contains("sessions_involved", [session_id])
            .eq("packet_type", packet_type)
            .in_("packet_status", ["pending", "in_progress", "awaiting_input", "blocked"])
            .execute()
        )

        if not result.data:
            return 0

        cancelled_count = 0
        for packet in result.data:
            try:
                self.client.table("agent_work_packets").update(
                    {"packet_status": PacketStatus.CANCELLED.value}
                ).eq("id", packet["id"]).execute()

                await self._log_event(
                    packet["id"],
                    "cancelled",
                    None,
                    reason,
                    session_id=session_id,
                )
                cancelled_count += 1
                LOGGER.info(
                    f"Auto-cancelled packet {packet['packet_id']} ({packet_type}): {reason}"
                )
            except Exception as e:
                LOGGER.warning(f"Failed to cancel packet {packet['packet_id']}: {e}")

        return cancelled_count

    async def cancel_stale_packets_for_entity(
        self,
        packet_type: str,
        key_entity: str,
        organization_id: int,
        exclude_packet_id: Optional[str] = None,
        reason: str = "Superseded by start-fresh request",
        session_id: Optional[str] = None,
    ) -> int:
        """Cancel all active/failed packets matching type + entity + org.

        Used by "start fresh" to clean up ALL prior attempts for the same
        site/entity, not just the single packet that prompted the decision.

        Args:
            packet_type: Packet type (e.g. "light_preliminary_package")
            key_entity: Entity name to match in packet_inputs (e.g. site name)
            organization_id: Organization that owns the packets
            exclude_packet_id: Skip this packet (already cancelled by caller)
            reason: Cancellation reason for logs
            session_id: Session performing the cancellation

        Returns:
            Number of packets cancelled
        """
        result = (
            self.client.table("agent_work_packets")
            .select("id,packet_id,packet_inputs")
            .eq("packet_type", packet_type)
            .eq("organization_id", organization_id)
            .in_(
                "packet_status",
                ["pending", "in_progress", "awaiting_input", "blocked", "failed"],
            )
            .execute()
        )

        if not result.data:
            return 0

        key_lower = key_entity.lower()
        cancelled_count = 0
        for packet in result.data:
            pid = packet["packet_id"]
            if pid == exclude_packet_id:
                continue
            # Match entity in packet_inputs
            inputs = packet.get("packet_inputs") or {}
            entity = (inputs.get("key_entity") or inputs.get("site_name") or "").lower()
            if key_lower not in entity and entity not in key_lower:
                continue
            try:
                self.client.table("agent_work_packets").update(
                    {"packet_status": PacketStatus.CANCELLED.value}
                ).eq("id", packet["id"]).execute()
                await self._log_event(
                    packet["id"], "cancelled", None, reason, session_id=session_id
                )
                cancelled_count += 1
                LOGGER.info(f"Cancelled stale packet {pid} ({packet_type}): {reason}")
            except Exception as e:
                LOGGER.warning(f"Failed to cancel stale packet {pid}: {e}")

        return cancelled_count

    async def reset_failed_packet(
        self,
        packet_id: str,
        session_id: Optional[str] = None,
        rerun_previous_step: bool = False,
    ) -> Dict[str, Any]:
        """Reset a failed/blocked packet to in_progress for retry.

        Clears error state and resets status so the workflow can continue
        from where it left off.

        Args:
            packet_id: Packet's packet_id or UUID
            session_id: Session retrying the packet
            rerun_previous_step: If True, also remove the last completed step
                                 so the previous step is re-run. Useful when
                                 the failure might have been caused by bad
                                 output from the previous step.

        Returns:
            Updated packet dictionary
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        # Get current state and clear error info and recovery flags
        current_state = packet.get("packet_state") or {}
        current_state.pop("last_error", None)
        current_state.pop("error_step", None)
        current_state.pop("error_time", None)
        current_state.pop("blocked_reason", None)
        # Clear auto-resume recovery flags so a successful resume resets the budget
        current_state.pop("auto_resumable", None)
        current_state.pop("auto_retry_count", None)
        current_state.pop("recovery_pending", None)
        current_state.pop("recovery_at", None)
        current_state.pop("interrupted_step", None)
        current_state.pop("interrupted_reason", None)
        current_state.pop("interrupted_at", None)
        current_state.pop("interrupted_too_many_times", None)

        # Optionally back up one step to re-run the previous step
        steps_completed = packet.get("steps_completed") or []
        if rerun_previous_step and steps_completed:
            removed_step = steps_completed.pop()
            LOGGER.info(
                f"Removed last completed step '{removed_step}' from packet {packet_id} "
                f"to re-run previous step"
            )

        # Track sessions involved
        sessions = packet.get("sessions_involved") or []
        if session_id and session_id not in sessions:
            sessions = sessions + [session_id]

        result = (
            self.client.table("agent_work_packets")
            .update(
                {
                    "packet_status": PacketStatus.IN_PROGRESS.value,
                    "packet_state": current_state,
                    "steps_completed": steps_completed,
                    "sessions_involved": sessions,
                }
            )
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "retry",
            packet.get("current_step"),
            f"Packet reset for retry by user (rerun_previous={rerun_previous_step})",
            session_id=session_id,
            triggered_by="user",
        )

        LOGGER.info(f"Reset failed packet for retry: {packet['packet_id']}")
        return result.data[0]

    async def mark_step_incomplete(
        self,
        packet_id: str,
        step_name: str,
        clear_state_keys: Optional[List[str]] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Remove step_name from steps_completed and optionally pop state keys,
        so run_single_step(force=True) can re-execute it from scratch.

        Generalizes reset_failed_packet's single-step-pop logic (which only ever
        removes the LAST completed step and only ever clears a fixed set of
        error/recovery keys) to an arbitrary named step and an arbitrary list of
        state keys to clear -- typically the step's own StepContract.guard_keys,
        so its idempotency guard doesn't immediately no-op the re-run.

        Args:
            packet_id: Packet's packet_id or UUID
            step_name: Name of the step to mark incomplete
            clear_state_keys: Optional packet_state keys to pop (e.g. the
                step's StepContract.guard_keys). Defaults to clearing nothing.
            session_id: Session making the change

        Returns:
            Updated packet dictionary

        Raises:
            ValueError: if packet not found.
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            raise ValueError(f"Packet not found: {packet_id}")

        steps_completed = packet.get("steps_completed") or []
        if step_name in steps_completed:
            steps_completed.remove(step_name)

        current_state = packet.get("packet_state") or {}
        for key in clear_state_keys or []:
            current_state.pop(key, None)

        sessions = packet.get("sessions_involved") or []
        if session_id and session_id not in sessions:
            sessions = sessions + [session_id]

        result = (
            self.client.table("agent_work_packets")
            .update(
                {
                    "steps_completed": steps_completed,
                    "packet_state": current_state,
                    "sessions_involved": sessions,
                }
            )
            .eq("id", packet["id"])
            .execute()
        )

        await self._log_event(
            packet["id"],
            "step_reset",
            step_name,
            f"Step '{step_name}' marked incomplete for re-run (cleared keys: {clear_state_keys or []})",
            session_id=session_id,
            triggered_by="user",
        )

        return result.data[0]

    # =========================================================================
    # LOGGING
    # =========================================================================

    async def _log_event(
        self,
        packet_uuid: str,
        log_type: str,
        step_name: Optional[str],
        message: str,
        input_data: Optional[Dict] = None,
        output_data: Optional[Dict] = None,
        error_data: Optional[Dict] = None,
        session_id: Optional[str] = None,
        triggered_by: str = "expert",
        duration_ms: Optional[int] = None,
    ):
        """Log an event to packet_logs.

        Args:
            packet_uuid: UUID of the packet (database ID)
            log_type: Type of log event
            step_name: Current step name (if applicable)
            message: Log message
            input_data: Optional input data snapshot
            output_data: Optional output data snapshot
            error_data: Optional error data
            session_id: Session context
            triggered_by: Who triggered the event (user, scheduler, expert)
            duration_ms: Duration of operation in milliseconds
        """
        try:
            self.client.table("agent_work_packet_logs").insert(
                {
                    "packet_id": packet_uuid,
                    "log_type": log_type,
                    "step_name": step_name,
                    "message": message,
                    "input_data": input_data,
                    "output_data": output_data,
                    "error_data": error_data,
                    "session_id": session_id,
                    "triggered_by": triggered_by,
                    "duration_ms": duration_ms,
                }
            ).execute()
        except Exception as e:
            LOGGER.warning(f"Failed to log packet event: {e}")

    async def log_tool_call(
        self,
        packet_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        duration_ms: Optional[int] = None,
        session_id: Optional[str] = None,
    ):
        """Log a tool call made during packet execution.

        Args:
            packet_id: Packet's packet_id or UUID
            tool_name: Name of MCP tool called
            arguments: Arguments passed to tool
            result: Tool result (if successful)
            error: Error message (if failed)
            duration_ms: Execution time
            session_id: Session context
        """
        packet = await self.get_packet(packet_id)
        if not packet:
            return

        await self._log_event(
            packet["id"],
            "tool_call",
            packet.get("current_step"),
            f"Tool call: {tool_name}",
            input_data={"tool": tool_name, "arguments": arguments},
            output_data={"result_summary": str(result)[:500]} if result else None,
            error_data={"error": error} if error else None,
            session_id=session_id,
            triggered_by="expert",
            duration_ms=duration_ms,
        )


__all__ = ["WorkPacketService"]
