"""Service for managing persistent agent instances.

Provides CRUD operations for the persistent_agent_instances and
agent_events tables in the Chat DB.
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import Client, create_client


class AgentManagementService:
    """Manages persistent agent instances via Chat DB (Supabase)."""

    def __init__(self):
        url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL")
        key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
        self.client: Client = create_client(url, key) if url and key else None

    def is_configured(self) -> bool:
        return self.client is not None

    def list_instances(
        self,
        expert_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List persistent agent instances with optional filters."""
        query = self.client.table("persistent_agent_instances").select("*")
        if expert_id:
            query = query.eq("expert_id", expert_id)
        if status:
            query = query.eq("status", status)
        query = query.order("created_at", desc=False)
        result = query.execute()
        return result.data or []

    def get_instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """Get a single instance by ID."""
        result = (
            self.client.table("persistent_agent_instances")
            .select("*")
            .eq("id", instance_id)
            .single()
            .execute()
        )
        data: Optional[Dict[str, Any]] = result.data
        return data

    def create_instance(
        self,
        expert_id: str,
        instance_name: str,
        anchor_entity_type: str,
        anchor_entity_id: str,
        anchor_metadata: Dict[str, Any],
        organization_id: int,
        wake_schedule: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new persistent agent instance."""
        # For user_startable agents, we allow multiple instances for the same anchor
        # by ensuring the anchor_entity_id (and thus thread_id) is unique.
        # The UI implementation for site visits will pass a unique ID.
        thread_id = f"{expert_id}:{anchor_entity_id}"

        data = {
            "expert_id": expert_id,
            "instance_name": instance_name,
            "anchor_entity_type": anchor_entity_type,
            "anchor_entity_id": anchor_entity_id,
            "anchor_metadata": anchor_metadata,
            "thread_id": thread_id,
            "organization_id": organization_id,
            "status": "active",
            "metadata": {},
        }
        if wake_schedule:
            data["wake_schedule"] = wake_schedule
        if created_by:
            data["created_by"] = created_by
        result = self.client.table("persistent_agent_instances").insert(data).execute()
        return result.data[0] if result.data else {}

    def update_status(
        self,
        instance_id: str,
        new_status: str,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update the status of a single instance."""
        data: Dict[str, Any] = {"status": new_status}
        if error_message is not None:
            data["error_message"] = error_message
        if new_status == "active":
            data["error_message"] = None
        result = (
            self.client.table("persistent_agent_instances")
            .update(data)
            .eq("id", instance_id)
            .execute()
        )
        return result.data[0] if result.data else {}

    def update_status_by_expert(self, expert_id: str, new_status: str) -> int:
        """Bulk update status for all instances of an expert type.

        Returns the number of updated instances.
        """
        # Only allow pausing active or resuming paused
        if new_status == "paused":
            filter_status = "active"
        elif new_status == "active":
            filter_status = "paused"
        else:
            return 0

        result = (
            self.client.table("persistent_agent_instances")
            .update({"status": new_status})
            .eq("expert_id", expert_id)
            .eq("status", filter_status)
            .execute()
        )
        return len(result.data) if result.data else 0

    def restart_instance(self, instance_id: str) -> Dict[str, Any]:
        """Restart an instance: reset metadata, wake_count, set to active."""
        data = {
            "status": "active",
            "metadata": {},
            "wake_count": 0,
            "error_message": None,
            "last_woke_at": None,
            "last_acted_at": None,
        }
        result = (
            self.client.table("persistent_agent_instances")
            .update(data)
            .eq("id", instance_id)
            .execute()
        )
        return result.data[0] if result.data else {}

    def terminate_instance(self, instance_id: str) -> Dict[str, Any]:
        """Terminate an instance (soft delete)."""
        return self.update_status(instance_id, "terminated")

    def queue_manual_wake(self, instance_id: str) -> Dict[str, Any]:
        """Insert a manual_wake event so the agent wakes on the next worker tick."""
        now = datetime.now(timezone.utc).isoformat()
        result = (
            self.client.table("agent_events")
            .insert(
                {
                    "target_instance_id": instance_id,
                    "event_type": "scheduled_wake",
                    "source_message_id": f"manual:{instance_id}:{now}",
                    "event_data": {"reason": "manual_wake", "triggered_at": now},
                }
            )
            .execute()
        )
        return result.data[0] if result.data else {}

    def get_recent_events(self, instance_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent events for an instance.

        Fetches lightweight columns only — result JSONB can be very large
        and cause PostgREST 500 errors on serialization.
        """
        result = (
            self.client.table("agent_events")
            .select("id, event_type, event_data, status, error, created_at, processed_at")
            .eq("target_instance_id", instance_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []

    def get_distinct_expert_ids(self) -> List[str]:
        """Get distinct expert_id values from instances."""
        result = self.client.table("persistent_agent_instances").select("expert_id").execute()
        return list({r["expert_id"] for r in (result.data or [])})

    @staticmethod
    def build_anchor_metadata(entity_type: str, entity: Dict[str, Any]) -> Dict[str, Any]:
        """Build anchor_metadata dict from entity data.

        Delegates to AgentWorker._build_anchor_metadata to avoid duplication.
        """
        from orchestrator.services.agent_worker import AgentWorker

        result: Dict[str, Any] = AgentWorker._build_anchor_metadata(entity_type, entity)
        return result

    def get_eligible_grids(self) -> List[Dict[str, Any]]:
        """Get grids eligible for auto-provisioned agents.

        Queries the Auth DB (asyncpg) via the shared AuthService.
        Returns empty list if Auth DB is unavailable.
        """
        import asyncio

        from shared.auth.auth_service import get_auth_service

        try:
            auth = get_auth_service()
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(auth.get_eligible_grids_for_agents())
            finally:
                loop.close()
        except Exception as e:
            print(f"[AgentManagement] get_eligible_grids failed: {e}")
            return []

    def get_eligible_grid_count(self) -> int:
        """Get count of grids eligible for auto-provisioned agents."""
        return len(self.get_eligible_grids())

    def get_all_sites(self) -> List[Dict[str, Any]]:
        """Get all sites from pd_site_submissions."""
        import asyncio
        import ssl

        import asyncpg

        async def _fetch():
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
            )
            try:
                rows = await conn.fetch(
                    """SELECT id, site_name, created_at
                       FROM pd_site_submissions
                       WHERE site_name IS NOT NULL AND deleted_at IS NULL
                       ORDER BY created_at DESC"""
                )
                return [dict(r) for r in rows]
            finally:
                await conn.close()

        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_fetch())
            finally:
                loop.close()
        except Exception as e:
            print(f"[AgentManagement] get_all_sites failed: {e}")
            return []

    @staticmethod
    def get_persistent_expert_configs() -> List[Dict[str, Any]]:
        """Get persistent expert definitions from the Google Doc.

        Returns list of dicts with expert_id, anchor_entity_type, wake_schedule, is_user_startable.
        """
        import asyncio

        try:
            from orchestrator.services.expert_instructions_provider import (
                ExpertInstructionsProvider,
            )

            provider = ExpertInstructionsProvider()
            loop = asyncio.new_event_loop()
            try:
                all_experts = loop.run_until_complete(provider.get_all_experts())
            finally:
                loop.close()

            return [
                {
                    "expert_id": eid,
                    "anchor_entity_type": cfg.anchor_entity_type,
                    "wake_schedule": cfg.wake_schedule,
                    "is_user_startable": cfg.is_user_startable,
                }
                for eid, cfg in all_experts.items()
                if cfg.is_persistent and cfg.anchor_entity_type
            ]
        except Exception as e:
            print(f"[AgentManagement] get_persistent_expert_configs failed: {e}")
            return []
