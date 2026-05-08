"""Background worker for persistent agent event processing.

Processes events from the agent_events queue, waking persistent agents
by invoking their LangGraph graph with checkpointed state.

Event delivery: PG LISTEN/NOTIFY for near-instant wake-up, with a 60s
safety poll as fallback for missed notifications.

Concurrency: at most one event per agent instance at a time, enforced
by per-instance asyncio locks + FOR UPDATE SKIP LOCKED in the DB claim.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class AgentWorker:
    """Processes agent events by waking persistent LangGraph agents."""

    # Back off for 30 minutes when a quota/spending cap error is detected
    QUOTA_BACKOFF_SECONDS = 1800

    def __init__(self, supabase_url: str, supabase_key: str):
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self._processing = False  # Overlap guard for batch processing
        self._reconciling = False  # Overlap guard for reconciliation
        self._instance_locks: Dict[str, asyncio.Lock] = {}
        self._compiled_graph = None
        self._checkpointer_ctx = None
        self._checkpointer = None
        self._supabase = None
        self._listener_conn = None
        self._started = False
        self._quota_backoff_until: Optional[datetime] = None  # Circuit breaker

    async def start(self):
        """Initialize the worker: checkpointer, graph, PG listener."""
        if not self._is_enabled():
            LOGGER.info("Persistent agents disabled (PERSISTENT_AGENTS_ENABLED != true)")
            return

        try:
            await self._init_checkpointer()
            self._compile_graph()
            await self._init_supabase()
            await self._start_pg_listener()
            self._started = True
            LOGGER.info("Agent worker started successfully")
        except Exception as e:
            LOGGER.error(f"Agent worker failed to start: {e}", exc_info=True)

    async def stop(self):
        """Clean up resources."""
        if self._listener_conn:
            try:
                await self._listener_conn.close()
            except Exception:
                pass
        if self._checkpointer_ctx:
            try:
                await self._checkpointer_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self._started = False
        LOGGER.info("Agent worker stopped")

    # ── Public entry points ─────────────────────────────────────────────

    async def process_batch(self):
        """Claim and process pending events. Called by APScheduler or PG NOTIFY.

        Overlap guard ensures only one batch runs at a time.
        """
        if not self._started or not self._is_enabled():
            return

        if self._processing:
            LOGGER.debug("Agent worker already processing, skipping batch")
            return

        # Circuit breaker: skip processing if backing off from a quota error
        if self._quota_backoff_until:
            now = datetime.now(timezone.utc)
            if now < self._quota_backoff_until:
                remaining = int((self._quota_backoff_until - now).total_seconds())
                LOGGER.debug(f"Agent worker quota backoff active ({remaining}s remaining)")
                return
            LOGGER.info("Agent worker quota backoff expired, resuming")
            self._quota_backoff_until = None

        self._processing = True
        try:
            events = await self._claim_events(batch_size=5)
            if not events:
                return

            LOGGER.info(f"Agent worker claimed {len(events)} event(s)")
            for event in events:
                await self._process_single_event(event)
        except Exception as e:
            error_str = str(e).lower()
            if (
                "429" in error_str
                or "spending cap" in error_str
                or "resource_exhausted" in error_str
            ):
                self._quota_backoff_until = datetime.now(timezone.utc) + timedelta(
                    seconds=self.QUOTA_BACKOFF_SECONDS
                )
                LOGGER.warning(
                    f"Agent worker hitting quota limit — backing off for "
                    f"{self.QUOTA_BACKOFF_SECONDS}s: {e}"
                )
            else:
                LOGGER.error(f"Agent worker batch error: {e}", exc_info=True)
        finally:
            self._processing = False

    async def queue_scheduled_wakes(self):
        """Check for agents due for a scheduled wake and queue events.

        Called by APScheduler every 15 minutes.
        """
        if not self._started or not self._is_enabled():
            return

        # Skip if backing off from quota error
        if self._quota_backoff_until and datetime.now(timezone.utc) < self._quota_backoff_until:
            return

        try:
            from croniter import croniter  # type: ignore[import-untyped]

            supabase = await self._get_supabase()
            result = await asyncio.to_thread(
                lambda: supabase.table("persistent_agent_instances")
                .select("id, thread_id, instance_name, wake_schedule, last_woke_at")
                .in_("status", ["active"])
                .not_.is_("wake_schedule", "null")
                .execute()
            )

            now = datetime.now(timezone.utc)
            queued = 0

            for instance in result.data or []:
                schedule = instance.get("wake_schedule")
                if not schedule:
                    continue

                # Check if wake is due
                last_woke = instance.get("last_woke_at")
                if last_woke:
                    last_woke_dt = datetime.fromisoformat(last_woke.replace("Z", "+00:00"))
                else:
                    last_woke_dt = datetime.min.replace(tzinfo=timezone.utc)

                try:
                    cron = croniter(schedule, last_woke_dt)
                    next_wake = cron.get_next(datetime)
                    if next_wake.tzinfo is None:
                        next_wake = next_wake.replace(tzinfo=timezone.utc)
                except (ValueError, KeyError):
                    LOGGER.warning(
                        f"Invalid cron schedule for {instance['instance_name']}: {schedule}"
                    )
                    continue

                # Stagger wakes: add a per-instance offset (0-299s) based on
                # thread_id hash to avoid thundering herd when all agents
                # share the same cron schedule
                from datetime import timedelta

                stagger_seconds = hash(instance.get("thread_id", "")) % 300
                next_wake = next_wake + timedelta(seconds=stagger_seconds)

                if next_wake <= now:
                    # Queue a scheduled_wake event with deterministic source_message_id
                    # for dedup (prevents duplicate wakes on restart/clock skew)
                    dedup_key = f"scheduled:{instance['id']}:{next_wake.isoformat()}"
                    try:
                        await asyncio.to_thread(
                            lambda inst=instance, dk=dedup_key: supabase.table("agent_events")
                            .insert(
                                {
                                    "target_instance_id": inst["id"],
                                    "event_type": "scheduled_wake",
                                    "source_message_id": dk,
                                    "event_data": {
                                        "reason": "periodic",
                                        "scheduled_at": now.isoformat(),
                                    },
                                }
                            )
                            .execute()
                        )
                        queued += 1
                    except Exception as insert_err:
                        if "duplicate" in str(insert_err).lower():
                            LOGGER.debug(
                                f"Scheduled wake already queued for "
                                f"{instance['instance_name']} at {next_wake}"
                            )
                        else:
                            raise

            if queued > 0:
                LOGGER.info(f"Queued {queued} scheduled wake event(s)")

        except Exception as e:
            LOGGER.error(f"Failed to queue scheduled wakes: {e}", exc_info=True)

    async def reconcile_instances(self):
        """Auto-provision/terminate agent instances for all persistent experts.

        Discovers persistent experts from the Google Doc, fetches eligible
        entities per anchor_entity_type, and converges actual instances to
        match. Runs every 5 minutes via APScheduler. Self-healing.
        """
        if not self._started or not self._is_enabled():
            return

        if self._reconciling:
            LOGGER.debug("Reconciliation already in progress, skipping")
            return

        self._reconciling = True
        try:
            from orchestrator.services.expert_instructions_provider import (
                ExpertInstructionsProvider,
            )

            provider = ExpertInstructionsProvider()
            all_experts = await provider.get_all_experts()

            for expert_id, config in all_experts.items():
                if not config.is_persistent or not config.anchor_entity_type:
                    continue
                # user_startable experts are instantiated on-demand, not auto-provisioned
                if config.is_user_startable:
                    continue
                try:
                    await self._reconcile_expert(expert_id, config)
                except Exception:
                    LOGGER.exception(f"Reconciliation failed for {expert_id}, will retry next tick")

        except Exception:
            LOGGER.exception("Reconciliation failed to load expert configs, will retry next tick")
        finally:
            self._reconciling = False

    async def _reconcile_expert(self, expert_id: str, config):
        """Reconcile instances for one persistent expert."""
        entity_type = config.anchor_entity_type

        # Get eligible entities from Auth DB
        eligible = await self._get_eligible_entities(entity_type)

        # Safety: if 0 rows returned, skip — don't mass-terminate (Auth DB may be down)
        if not eligible:
            LOGGER.warning(
                f"Reconciliation({expert_id}): 0 eligible {entity_type}s returned, skipping"
            )
            return

        eligible_map = {str(e["id"]): e for e in eligible}

        # Get existing instances from Chat DB
        supabase = await self._get_supabase()
        result = await asyncio.to_thread(
            lambda: supabase.table("persistent_agent_instances")
            .select("*")
            .eq("expert_id", expert_id)
            .execute()
        )
        existing = result.data or []
        existing_map = {i["anchor_entity_id"]: i for i in existing}

        # P1 safety: if eligible count dropped by >50% vs active instances,
        # treat as a partial Auth DB result and skip terminations
        active_count = sum(1 for i in existing if i["status"] not in ("terminated",))
        partial_result = active_count > 0 and len(eligible) < active_count * 0.5

        if partial_result:
            LOGGER.warning(
                f"Reconciliation({expert_id}): eligible ({len(eligible)}) < 50% of "
                f"active ({active_count}), skipping terminations (possible partial Auth DB result)"
            )

        created = 0
        refreshed = 0
        reactivated = 0
        terminated = 0

        # Create missing instances (or re-activate terminated ones)
        for entity_id, entity in eligible_map.items():
            if entity_id not in existing_map:
                await self._auto_create_instance(
                    expert_id, entity_type, entity_id, entity, config.wake_schedule
                )
                created += 1
            elif existing_map[entity_id]["status"] == "error":
                # Auto-recover from transient errors (e.g. Gemini timeout, network blip)
                await self._reactivate_instance(existing_map[entity_id], entity_type, entity)
                reactivated += 1
            elif existing_map[entity_id]["status"] == "terminated":
                # Only re-activate if auto-terminated by reconciliation (not user-stopped)
                error_msg = existing_map[entity_id].get("error_message") or ""
                if "removed from eligibility" in error_msg:
                    await self._reactivate_instance(existing_map[entity_id], entity_type, entity)
                    reactivated += 1

        # Refresh anchor_metadata and wake_schedule on existing active instances
        for entity_id, instance in existing_map.items():
            if instance["status"] == "terminated":
                continue
            if entity_id in eligible_map:
                metadata = self._build_anchor_metadata(entity_type, eligible_map[entity_id])
                updated = await self._refresh_anchor_metadata(instance, metadata)
                schedule_updated = await self._refresh_wake_schedule(instance, config.wake_schedule)
                if updated or schedule_updated:
                    refreshed += 1
            elif not partial_result:
                await self._auto_terminate_instance(instance, entity_type)
                terminated += 1

        if created or terminated or reactivated:
            LOGGER.info(
                f"Reconciliation({expert_id}): "
                f"created={created}, reactivated={reactivated}, "
                f"refreshed={refreshed}, terminated={terminated}"
            )
        else:
            LOGGER.debug(
                f"Reconciliation({expert_id}): no changes "
                f"(refreshed={refreshed}, {len(existing)} instances, "
                f"{len(eligible)} eligible {entity_type}s)"
            )

    # ── Internal: reconciliation helpers ─────────────────────────────────

    # Registry: anchor_entity_type → eligibility query function
    # To add a new entity type, add an entry here and a corresponding
    # method on AuthService.

    async def _get_eligible_entities(self, entity_type: str) -> List[Dict[str, Any]]:
        """Get eligible entities for a given anchor_entity_type."""
        from shared.auth.auth_service import get_auth_service

        auth_service = get_auth_service()

        if entity_type == "grid":
            return await auth_service.get_eligible_grids_for_agents()

        LOGGER.warning(f"No eligibility query registered for entity_type={entity_type}")
        return []

    @staticmethod
    def _build_anchor_metadata(entity_type: str, entity: Dict[str, Any]) -> Dict[str, Any]:
        """Build anchor_metadata dict from entity data.

        Each entity type maps its DB fields to a standard metadata shape
        used for event routing and context.
        """
        if entity_type == "grid":
            return {
                "grid_name": entity["name"],
                "telegram_chat_id": str(entity["internal_telegram_group_chat_id"]),
                "telegram_topic_id": entity.get("internal_telegram_group_thread_id"),
                "vrm_site_id": entity.get("generation_external_site_id"),
                "organization_id": entity["organization_id"],
                "organization_name": entity.get("organization_name", ""),
            }

        # Fallback: store name + organization_id
        return {
            "name": entity.get("name", ""),
            "organization_id": entity.get("organization_id"),
        }

    async def _auto_create_instance(
        self,
        expert_id: str,
        entity_type: str,
        entity_id: str,
        entity: Dict[str, Any],
        schedule: Optional[str],
    ):
        """Create a new agent instance for an auto-discovered entity."""
        anchor_metadata = self._build_anchor_metadata(entity_type, entity)
        thread_id = f"{expert_id}:{entity_id}"
        instance_name = f"{entity.get('name', entity_id)} ({expert_id})"

        supabase = await self._get_supabase()
        try:
            await asyncio.to_thread(
                lambda: supabase.table("persistent_agent_instances")
                .insert(
                    {
                        "expert_id": expert_id,
                        "instance_name": instance_name,
                        "anchor_entity_type": entity_type,
                        "anchor_entity_id": entity_id,
                        "anchor_metadata": anchor_metadata,
                        "thread_id": thread_id,
                        "status": "active",
                        "organization_id": entity.get("organization_id") or 0,
                        "wake_schedule": schedule,
                        "created_by": "auto:reconciliation",
                    }
                )
                .execute()
            )
            LOGGER.info(
                f"Auto-provisioned {expert_id} for {entity.get('name', entity_id)} "
                f"(entity {entity_id})"
            )
        except Exception as e:
            # Unique constraint violation = benign race condition
            if "duplicate" in str(e).lower() or "unique" in str(e).lower():
                LOGGER.debug(f"Instance already exists for {expert_id}:{entity_id} (benign)")
            else:
                raise

    async def _refresh_anchor_metadata(
        self, instance: Dict[str, Any], new_metadata: Dict[str, Any]
    ) -> bool:
        """Refresh anchor_metadata on an existing instance if data changed.

        Returns True if metadata was updated.
        """
        current = instance.get("anchor_metadata") or {}
        if new_metadata == current:
            return False

        supabase = await self._get_supabase()
        await asyncio.to_thread(
            lambda: supabase.table("persistent_agent_instances")
            .update({"anchor_metadata": new_metadata})
            .eq("id", str(instance["id"]))
            .execute()
        )
        LOGGER.info(f"Refreshed anchor_metadata for {instance['instance_name']} ({instance['id']})")
        return True

    async def _refresh_wake_schedule(
        self, instance: Dict[str, Any], new_schedule: Optional[str]
    ) -> bool:
        """Refresh wake_schedule on an existing instance if it changed.

        Returns True if schedule was updated.
        """
        current = instance.get("wake_schedule")
        if new_schedule == current:
            return False

        supabase = await self._get_supabase()
        await asyncio.to_thread(
            lambda: supabase.table("persistent_agent_instances")
            .update({"wake_schedule": new_schedule})
            .eq("id", str(instance["id"]))
            .execute()
        )
        LOGGER.info(
            f"Updated wake_schedule for {instance['instance_name']} ({instance['id']}): "
            f"'{current}' -> '{new_schedule}'"
        )
        return True

    async def _reactivate_instance(
        self, instance: Dict[str, Any], entity_type: str, entity: Dict[str, Any]
    ):
        """Re-activate a terminated instance whose entity became eligible again."""
        metadata = self._build_anchor_metadata(entity_type, entity)
        supabase = await self._get_supabase()
        await asyncio.to_thread(
            lambda: supabase.table("persistent_agent_instances")
            .update(
                {
                    "status": "active",
                    "anchor_metadata": metadata,
                    "error_message": None,
                }
            )
            .eq("id", str(instance["id"]))
            .execute()
        )
        LOGGER.info(
            f"Re-activated {instance['instance_name']} ({instance['id']}): "
            f"{entity_type} became eligible again"
        )

    async def _auto_terminate_instance(self, instance: Dict[str, Any], entity_type: str = "entity"):
        """Terminate an instance whose entity is no longer eligible."""
        supabase = await self._get_supabase()
        await asyncio.to_thread(
            lambda: supabase.table("persistent_agent_instances")
            .update(
                {
                    "status": "terminated",
                    "error_message": f"{entity_type.title()} removed from eligibility",
                }
            )
            .eq("id", str(instance["id"]))
            .execute()
        )
        LOGGER.info(
            f"Auto-terminated {instance['instance_name']} ({instance['id']}): "
            f"{entity_type} no longer eligible"
        )

    # ── Internal: event processing ──────────────────────────────────────

    async def _process_single_event(self, event: Dict[str, Any]):
        """Process one event for one agent instance."""
        instance_id = str(event["target_instance_id"])
        event_id = str(event["id"])

        # Per-instance lock prevents concurrent processing
        if instance_id not in self._instance_locks:
            self._instance_locks[instance_id] = asyncio.Lock()

        lock = self._instance_locks[instance_id]
        if lock.locked():
            # Already processing this agent — release event back to pending
            LOGGER.debug(f"Agent {instance_id} busy, releasing event {event_id}")
            await self._release_event(event_id)
            return

        async with lock:
            instance = await self._load_instance(instance_id)
            if not instance:
                await self._discard_event(event_id, "instance_not_found")
                return

            if instance["status"] == "paused":
                await self._discard_event(event_id, "agent_paused")
                return

            if instance["status"] == "terminated":
                await self._discard_event(event_id, "agent_terminated")
                return

            try:
                # Mark instance as executing
                await self._update_instance_status(instance_id, "executing")

                # Gather additional pending events for this instance (batch into one wake)
                additional = await self._get_pending_events_for_instance(instance_id, event_id)

                # Wake the agent
                result = await self._wake_agent(instance, event, additional)

                # Persist results
                now_iso = datetime.now(timezone.utc).isoformat()
                update_data = {
                    "status": "active",
                    "last_woke_at": now_iso,
                    "wake_count": (instance.get("wake_count") or 0) + 1,
                    "error_message": None,
                }

                # Update metadata if the agent produced changes
                metadata_updates = result.get("metadata_updates", {})
                if metadata_updates:
                    merged = {**instance.get("metadata", {}), **metadata_updates}
                    update_data["metadata"] = merged

                if result.get("actions_taken"):
                    update_data["last_acted_at"] = now_iso

                await self._update_instance(instance_id, update_data)

                # Mark event completed with result summary
                await self._complete_event(
                    event_id,
                    {
                        "observations": result.get("observations", []),
                        "actions": result.get("actions_taken", []),
                        "assessment": (result.get("assessment") or "")[:500],
                    },
                )

                # Mark additional batched events as completed too
                for extra_event in additional:
                    await self._complete_event(
                        str(extra_event["id"]),
                        {
                            "batched_with": event_id,
                        },
                    )

            except Exception as e:
                LOGGER.error(
                    f"Agent {instance.get('instance_name', instance_id)} failed: {e}",
                    exc_info=True,
                )
                from shared.utils.error_messages import sanitize_error_for_user

                safe_error = sanitize_error_for_user(str(e))
                await self._update_instance_status(instance_id, "error", error_message=safe_error)
                await self._fail_event(event_id, safe_error)

    async def _wake_agent(
        self,
        instance: Dict[str, Any],
        primary_event: Dict[str, Any],
        additional_events: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Invoke the persistent agent's LangGraph graph."""
        config = {"configurable": {"thread_id": instance["thread_id"]}}

        # Build initial state for this wake cycle
        all_event_data = [primary_event.get("event_data", {})]
        for extra in additional_events:
            all_event_data.append(extra.get("event_data", {}))

        # Add event_type to each event's data for the LLM
        primary_data = primary_event.get("event_data", {})
        primary_data["event_type"] = primary_event.get("event_type", "unknown")
        all_event_data[0] = primary_data

        for i, extra in enumerate(additional_events):
            extra_data = extra.get("event_data", {})
            extra_data["event_type"] = extra.get("event_type", "unknown")
            all_event_data[i + 1] = extra_data

        initial_state: Dict[str, Any] = {
            "instance_id": str(instance["id"]),
            "thread_id": instance["thread_id"],
            "organization_id": instance.get("organization_id", 0),
            "entity_data": {},  # Loaded by load_context node if needed
            "metadata": instance.get("metadata", {}),
            "current_events": all_event_data,
            "recent_event_history": [],  # Loaded by load_context (not checkpointed)
            "telegram_chat_id": instance.get("anchor_metadata", {}).get("telegram_chat_id", ""),
            "telegram_topic_id": instance.get("anchor_metadata", {}).get("telegram_topic_id"),
            "system_instructions": "",  # Loaded by load_context
            "available_tools": [],  # Loaded by load_context
            "assessment": "",
            "actions_taken": [],
            "metadata_updates": {},
        }

        # Invoke graph (checkpointed — state persists across wakes)
        assert self._compiled_graph is not None, "Graph not compiled"
        result: Dict[str, Any] = await self._compiled_graph.ainvoke(initial_state, config=config)
        return result

    # ── Internal: DB operations ─────────────────────────────────────────

    async def _claim_events(self, batch_size: int = 5) -> List[Dict[str, Any]]:
        """Atomically claim pending events (1 per instance)."""
        supabase = await self._get_supabase()
        try:
            result = await asyncio.to_thread(
                lambda: supabase.rpc(
                    "claim_agent_events",
                    {"p_batch_size": batch_size, "p_processor_id": "worker"},
                ).execute()
            )
            return result.data or []
        except Exception as e:
            LOGGER.error(f"Failed to claim events: {e}")
            return []

    async def _load_instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """Load a persistent agent instance by ID."""
        supabase = await self._get_supabase()
        result = await asyncio.to_thread(
            lambda: supabase.table("persistent_agent_instances")
            .select("*")
            .eq("id", instance_id)
            .single()
            .execute()
        )
        data: Optional[Dict[str, Any]] = result.data if result.data else None
        return data

    async def _get_pending_events_for_instance(
        self, instance_id: str, exclude_event_id: str
    ) -> List[Dict[str, Any]]:
        """Claim additional pending events for an instance to batch into one wake.

        Uses an RPC to atomically mark events as 'processing' to prevent
        duplicate processing by concurrent workers or restarts.
        """
        supabase = await self._get_supabase()
        # Atomically claim: select pending events and mark as processing in one step
        # This prevents the race condition where a concurrent worker could also read them
        result = await asyncio.to_thread(
            lambda: supabase.table("agent_events")
            .select("*")
            .eq("target_instance_id", instance_id)
            .eq("status", "pending")
            .neq("id", exclude_event_id)
            .order("created_at", desc=False)
            .limit(10)
            .execute()
        )
        events = result.data or []

        # Mark claimed events as processing — only return successfully claimed ones
        # to prevent duplicate processing if a concurrent worker claimed the same event
        claimed = []
        for evt in events:
            try:
                await self._claim_additional_event(supabase, str(evt["id"]))
                claimed.append(evt)
            except Exception:
                pass  # Already claimed by another worker — skip

        return claimed

    async def _claim_additional_event(self, supabase: Any, event_id: str) -> None:
        """Mark a single additional event as processing (atomic guard)."""
        await asyncio.to_thread(
            lambda: supabase.table("agent_events")
            .update(
                {
                    "status": "processing",
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", event_id)
            .eq("status", "pending")
            .execute()
        )

    async def _get_recent_completed_events(
        self, instance_id: str, limit: int = 30
    ) -> List[Dict[str, Any]]:
        """Get recent completed events for context."""
        supabase = await self._get_supabase()
        result = await asyncio.to_thread(
            lambda: supabase.table("agent_events")
            .select("event_type, event_data, result, processed_at")
            .eq("target_instance_id", instance_id)
            .eq("status", "completed")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        events = result.data or []
        events.reverse()  # Chronological order
        return events

    async def _update_instance(self, instance_id: str, data: Dict[str, Any]):
        supabase = await self._get_supabase()
        await asyncio.to_thread(
            lambda: supabase.table("persistent_agent_instances")
            .update(data)
            .eq("id", instance_id)
            .execute()
        )

    async def _update_instance_status(
        self, instance_id: str, status: str, error_message: str = None
    ):
        data: Dict[str, Any] = {"status": status}
        if error_message is not None:
            data["error_message"] = error_message
        await self._update_instance(instance_id, data)

    async def _complete_event(self, event_id: str, result: Dict[str, Any]):
        supabase = await self._get_supabase()
        await asyncio.to_thread(
            lambda: supabase.table("agent_events")
            .update({"status": "completed", "result": result})
            .eq("id", event_id)
            .execute()
        )

    async def _fail_event(self, event_id: str, error: str):
        supabase = await self._get_supabase()
        await asyncio.to_thread(
            lambda: supabase.table("agent_events")
            .update({"status": "failed", "error": error})
            .eq("id", event_id)
            .execute()
        )

    async def _discard_event(self, event_id: str, reason: str):
        supabase = await self._get_supabase()
        await asyncio.to_thread(
            lambda: supabase.table("agent_events")
            .update({"status": "discarded", "result": {"reason": reason}})
            .eq("id", event_id)
            .execute()
        )

    async def _release_event(self, event_id: str):
        """Release a claimed event back to pending (for retry)."""
        supabase = await self._get_supabase()
        await asyncio.to_thread(
            lambda: supabase.table("agent_events")
            .update({"status": "pending", "processed_at": None})
            .eq("id", event_id)
            .execute()
        )

    # ── Internal: initialization ────────────────────────────────────────

    async def _init_checkpointer(self):
        """Initialize the LangGraph PostgreSQL checkpointer."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        db_url = os.getenv("CHAT_DB_POSTGRES_URL") or os.getenv("CHAT_DB_URL", "")
        if not db_url:
            raise ValueError("CHAT_DB_POSTGRES_URL or CHAT_DB_URL required for agent checkpointer")

        # AsyncPostgresSaver needs a psycopg connection string (direct Postgres, not REST)
        if not db_url.startswith("postgresql://") and not db_url.startswith("postgres://"):
            LOGGER.warning(
                "CHAT_DB_POSTGRES_URL may not be a direct Postgres URL — checkpointer may fail"
            )

        # Ensure sslmode is set for Supabase
        if "sslmode" not in db_url:
            separator = "&" if "?" in db_url else "?"
            db_url = f"{db_url}{separator}sslmode=require"

        self._checkpointer_ctx = AsyncPostgresSaver.from_conn_string(db_url)
        self._checkpointer = await self._checkpointer_ctx.__aenter__()
        await self._checkpointer.setup()
        LOGGER.info("Agent checkpointer initialized (PostgreSQL)")

    def _compile_graph(self):
        """Compile the persistent agent graph with checkpointer."""
        from orchestrator.graphs.persistent_agent_graph import build_persistent_agent_graph

        graph = build_persistent_agent_graph()
        self._compiled_graph = graph.compile(checkpointer=self._checkpointer)
        LOGGER.info("Persistent agent graph compiled")

    async def _init_supabase(self):
        """Initialize the Supabase client for DB operations."""
        from supabase import create_client

        self._supabase = create_client(self.supabase_url, self.supabase_key)

    async def _get_supabase(self):
        if not self._supabase:
            await self._init_supabase()
        return self._supabase

    async def _start_pg_listener(self):
        """Start PG LISTEN for near-instant event wake-up."""
        import asyncpg

        db_url = os.getenv("CHAT_DB_POSTGRES_URL") or os.getenv("CHAT_DB_URL", "")
        if not db_url:
            LOGGER.warning("No direct Postgres URL for LISTEN/NOTIFY, using poll-only mode")
            return

        try:
            self._listener_conn = await asyncpg.connect(
                db_url, ssl="require", statement_cache_size=0
            )
            await self._listener_conn.add_listener("agent_events", self._on_notify)
            LOGGER.info("PG LISTEN/NOTIFY active on 'agent_events' channel")
        except Exception as e:
            LOGGER.warning(f"PG LISTEN/NOTIFY failed ({e}), falling back to poll-only mode")
            self._listener_conn = None

    def _on_notify(self, conn, pid, channel, payload):
        """PG NOTIFY callback — schedule batch processing."""
        # Schedule on the event loop (callback runs in a different context)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.process_batch())
        except RuntimeError:
            # No running event loop — will be picked up by safety poll
            pass

    @staticmethod
    def _is_enabled() -> bool:
        return os.getenv("PERSISTENT_AGENTS_ENABLED", "false").lower() in ("true", "1", "yes")
