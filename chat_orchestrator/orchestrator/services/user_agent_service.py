"""Service for user-created persistent monitoring agents.

Supports a subscriber model: multiple users can subscribe to the same
agent. When a condition triggers, all subscribers are notified. When the
last subscriber leaves, the agent terminates.
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from orchestrator.services.supabase_client import get_supabase_client
from shared.auth.auth_service import STAFF_ORG_ID as _STAFF_ORG_ID

LOGGER = logging.getLogger(__name__)

# Words that indicate agent/schedule creation intent — strip from check_prompt
_CREATION_PATTERN = re.compile(
    r"\b(alert me|let me know|notify me|watch for|tell me when|"
    r"create|make|set up|start|launch|build|establish|configure)"
    r"(\s+(an?|the)\s+)?"
    r"(\s*(agent|monitor|watcher|alert|schedule|reminder))?",
    re.IGNORECASE,
)

# Default wake schedule: every hour during working hours (8am-6pm WAT, Mon-Fri)
DEFAULT_WAKE_SCHEDULE = "0 8-18 * * 1-5"

# Maximum agents per user (prevents resource exhaustion)
MAX_AGENTS_PER_USER = 10

# Minimum wake interval in minutes (prevents * * * * * cron abuse)
MIN_WAKE_INTERVAL_MINUTES = 15

# Maximum subscribers per agent (prevents unbounded JSONB growth)
MAX_SUBSCRIBERS_PER_AGENT = 25


def _paraphrase_to_check(original_request: str, llm_paraphrase: str) -> str:
    """Ensure the stored prompt is a question, not a creation instruction."""
    prompt = llm_paraphrase.strip()
    prompt = _CREATION_PATTERN.sub("", prompt).strip()
    prompt = re.sub(r"^\s*[,.:;-]+\s*", "", prompt)
    prompt = re.sub(r"\s+", " ", prompt).strip()
    if prompt and prompt[0].islower():
        prompt = prompt[0].upper() + prompt[1:]
    if not prompt.endswith("?"):
        prompt = prompt.rstrip(".!") + "?"
    if len(prompt) < 10:
        prompt = f"Check: {original_request}?"
    return prompt


def _make_subscriber(
    chat_id: str,
    topic_id: Optional[str],
    email: str,
    events: Optional[List[str]] = None,
    auto_remove: bool = False,
) -> Dict[str, Any]:
    """Build a subscriber dict."""
    return {
        "chat_id": chat_id,
        "topic_id": topic_id,
        "email": email,
        "events": events or ["all"],
        "auto_remove": auto_remove,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }


class UserAgentService:
    """CRUD operations for user-created persistent agents with subscriber model."""

    def _get_client(self):
        return get_supabase_client()._get_client()

    async def create_agent(
        self,
        instance_name: str,
        check_prompt: str,
        response_prompt: str,
        user_id: str,
        user_email: str,
        organization_id: int,
        chat_id: str,
        agent_type: str = "condition_monitor",
        topic_id: Optional[str] = None,
        wake_schedule: str = DEFAULT_WAKE_SCHEDULE,
        auto_complete: bool = False,
        model_tier: str = "standard",
        anchor_entity_type: str = "user_monitor",
        anchor_entity_id: Optional[str] = None,
        anchor_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a new agent or subscribe to an existing equivalent one.

        If an active agent exists with the same anchor_entity_id and expert_id,
        the user is added as a subscriber instead of creating a duplicate.
        """
        try:
            client = self._get_client()

            # Rate limit: max agents per user (count where user is creator)
            existing_count = (
                client.table("persistent_agent_instances")
                .select("id", count="exact")
                .eq("created_by_user_id", user_id)
                .eq("expert_id", "user_agent")
                .neq("status", "terminated")
                .execute()
            )
            if existing_count.count and existing_count.count >= MAX_AGENTS_PER_USER:
                return {
                    "success": False,
                    "error": f"You have {existing_count.count} active agents "
                    f"(max {MAX_AGENTS_PER_USER}). Cancel some before creating new ones.",
                }

            # Validate cron schedule minimum interval
            if wake_schedule:
                try:
                    from croniter import croniter  # type: ignore[import-untyped]

                    cron = croniter(wake_schedule)
                    next1 = cron.get_next(float)
                    next2 = cron.get_next(float)
                    interval_min = (next2 - next1) / 60
                    if interval_min < MIN_WAKE_INTERVAL_MINUTES:
                        return {
                            "success": False,
                            "error": f"Wake schedule too frequent ({interval_min:.0f}min interval). "
                            f"Minimum is every {MIN_WAKE_INTERVAL_MINUTES} minutes.",
                        }
                except (ValueError, KeyError) as e:
                    return {"success": False, "error": f"Invalid cron expression: {e}"}

            if not anchor_entity_id:
                stable_hash = hashlib.md5(check_prompt.encode()).hexdigest()[:8]
                anchor_entity_id = f"user_{user_id}_{stable_hash}"

            # Map agent_type to expert_id and validate
            effective_expert_id = "user_agent" if agent_type == "condition_monitor" else agent_type

            # Validate non-default agent types exist in the Google Doc
            if effective_expert_id != "user_agent":
                try:
                    from orchestrator.services.expert_instructions_provider import (
                        ExpertInstructionsProvider,
                    )

                    provider = ExpertInstructionsProvider()
                    config = await provider.get_expert_config(effective_expert_id)
                    if not config:
                        return {
                            "success": False,
                            "error": f"Unknown agent type '{agent_type}'. "
                            f"Check the expert instructions document for available types.",
                        }
                    if not config.is_user_startable:
                        return {
                            "success": False,
                            "error": f"Agent type '{agent_type}' is not user-startable. "
                            f"It is a {config.expert_type} expert.",
                        }
                except Exception as e:
                    LOGGER.warning(f"Could not validate agent type '{agent_type}': {e}")

            # Check for existing equivalent agent to subscribe to (org-scoped)
            existing_agent = await self._find_subscribable_agent(
                anchor_entity_id,
                anchor_entity_type,
                organization_id,
                expert_id=effective_expert_id,
            )
            if existing_agent:
                return await self._subscribe_to_agent(
                    existing_agent,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    email=user_email,
                    auto_remove=auto_complete,
                    wake_schedule=wake_schedule,
                )

            # No existing agent — create new one
            subscriber = _make_subscriber(
                chat_id=chat_id,
                topic_id=topic_id,
                email=user_email,
                auto_remove=auto_complete,
            )

            thread_id = f"{effective_expert_id}:{anchor_entity_id}"
            row = {
                "expert_id": effective_expert_id,
                "instance_name": instance_name,
                "anchor_entity_type": anchor_entity_type,
                "anchor_entity_id": anchor_entity_id,
                "anchor_metadata": anchor_metadata or {},
                "thread_id": thread_id,
                "status": "active",
                "organization_id": organization_id,
                "wake_schedule": wake_schedule,
                "created_by": user_email,
                "created_by_user_id": user_id,
                "check_prompt": check_prompt,
                "response_prompt": response_prompt,
                "notify_chat_id": chat_id,
                "notify_topic_id": topic_id,
                "auto_complete": False,  # Managed per-subscriber now
                "subscribers": [subscriber],
                "metadata": {
                    "model_tier": model_tier if model_tier in ("standard", "pro") else "standard",
                },
                "user_context": {
                    "user_email": user_email,
                    "organization_id": organization_id,
                    "is_staff": organization_id == _STAFF_ORG_ID,
                    "chat_id": chat_id,
                    "topic_id": topic_id,
                },
            }

            result = client.table("persistent_agent_instances").insert(row).execute()
            data = (result.data or [{}])[0]

            LOGGER.info(
                f"Created user agent '{instance_name}' for user {user_id}: "
                f"check={check_prompt[:60]}, response={response_prompt[:60]}"
            )
            return {
                "success": True,
                "instance_id": data.get("id"),
                "instance_name": instance_name,
                "thread_id": thread_id,
                "check_prompt": check_prompt,
                "response_prompt": response_prompt,
                "wake_schedule": wake_schedule,
                "subscribed": False,
            }

        except Exception as e:
            LOGGER.exception(f"Failed to create user agent: {e}")
            return {"success": False, "error": str(e)}

    async def _find_subscribable_agent(
        self,
        anchor_entity_id: str,
        anchor_entity_type: str,
        organization_id: int,
        expert_id: str = "user_agent",
    ) -> Optional[Dict[str, Any]]:
        """Find an active agent with the same anchor (org-scoped) to subscribe to."""
        try:
            client = self._get_client()
            result = (
                client.table("persistent_agent_instances")
                .select("id, instance_name, check_prompt, wake_schedule, subscribers")
                .eq("expert_id", expert_id)
                .eq("anchor_entity_id", anchor_entity_id)
                .eq("anchor_entity_type", anchor_entity_type)
                .eq("organization_id", organization_id)
                .in_("status", ["active", "executing"])
                .limit(1)
                .execute()
            )
            return dict(result.data[0]) if result.data else None
        except Exception as e:
            LOGGER.debug(f"Error finding subscribable agent: {e}")
            return None

    async def _subscribe_to_agent(
        self,
        agent: Dict[str, Any],
        chat_id: str,
        topic_id: Optional[str],
        email: str,
        auto_remove: bool = False,
        wake_schedule: str = DEFAULT_WAKE_SCHEDULE,
    ) -> Dict[str, Any]:
        """Add a subscriber to an existing agent."""
        try:
            client = self._get_client()
            subscribers = agent.get("subscribers") or []

            # Cap subscriber count
            if len(subscribers) >= MAX_SUBSCRIBERS_PER_AGENT:
                return {
                    "success": False,
                    "error": f"Agent has {len(subscribers)} subscribers "
                    f"(max {MAX_SUBSCRIBERS_PER_AGENT}).",
                }

            # Check if already subscribed (same email + chat_id)
            for sub in subscribers:
                if sub.get("email") == email and sub.get("chat_id") == chat_id:
                    return {
                        "success": True,
                        "instance_id": agent["id"],
                        "instance_name": agent["instance_name"],
                        "check_prompt": agent.get("check_prompt", ""),
                        "subscribed": True,
                        "message": f"You are already subscribed to '{agent['instance_name']}'.",
                    }

            # Add new subscriber
            new_sub = _make_subscriber(
                chat_id=chat_id,
                topic_id=topic_id,
                email=email,
                auto_remove=auto_remove,
            )
            subscribers.append(new_sub)

            # Upgrade wake schedule if new subscriber wants faster checks
            current_schedule = agent.get("wake_schedule", DEFAULT_WAKE_SCHEDULE)
            faster_schedule = self._pick_faster_schedule(current_schedule, wake_schedule)

            client.table("persistent_agent_instances").update(
                {"subscribers": subscribers, "wake_schedule": faster_schedule}
            ).eq("id", agent["id"]).execute()

            LOGGER.info(
                f"User {email} subscribed to agent '{agent['instance_name']}' "
                f"({len(subscribers)} subscribers)"
            )
            return {
                "success": True,
                "instance_id": agent["id"],
                "instance_name": agent["instance_name"],
                "check_prompt": agent.get("check_prompt", ""),
                "subscribed": True,
                "subscriber_count": len(subscribers),
                "message": f"Subscribed to existing agent '{agent['instance_name']}' "
                f"({len(subscribers)} subscribers).",
                "wake_schedule": faster_schedule,
            }

        except Exception as e:
            LOGGER.exception(f"Failed to subscribe to agent: {e}")
            return {"success": False, "error": str(e)}

    def _pick_faster_schedule(self, schedule_a: str, schedule_b: str) -> str:
        """Return the faster of two cron schedules.

        Compares by measuring the gap between two consecutive firings
        (not next vs prev, which is unreliable for non-uniform crons).
        """
        try:
            from croniter import croniter  # type: ignore[import-untyped]

            cron_a = croniter(schedule_a)
            a1 = cron_a.get_next(float)
            a2 = cron_a.get_next(float)
            interval_a = a2 - a1

            cron_b = croniter(schedule_b)
            b1 = cron_b.get_next(float)
            b2 = cron_b.get_next(float)
            interval_b = b2 - b1

            return schedule_a if interval_a <= interval_b else schedule_b
        except Exception:
            return schedule_a  # Keep current on error

    async def list_agents(
        self,
        user_email: str = "",
        user_id: str = "",
        chat_id: str = "",
        include_terminated: bool = False,
    ) -> List[Dict[str, Any]]:
        """List user agents visible in the current context.

        In group chats: show all agents notifying this group (any subscriber).
        In DMs: show agents where this user is a subscriber or creator.
        """
        try:
            client = self._get_client()
            query = (
                client.table("persistent_agent_instances")
                .select(
                    "id, instance_name, check_prompt, response_prompt, status, "
                    "wake_schedule, wake_count, last_woke_at, created_at, "
                    "auto_complete, metadata, created_by, subscribers"
                )
                .eq("expert_id", "user_agent")
            )
            # Group chats: show agents with any subscriber in this group
            # DMs: show agents where this user is creator or subscriber
            if chat_id and chat_id.startswith("-"):
                query = query.eq("notify_chat_id", chat_id)
            elif user_email:
                query = query.eq("created_by", user_email)
            elif user_id:
                query = query.eq("created_by_user_id", user_id)
            if not include_terminated:
                query = query.neq("status", "terminated")

            result = query.order("created_at", desc=True).execute()
            agents = result.data or []

            # Enrich with subscriber count
            for a in agents:
                subs = a.get("subscribers") or []
                a["subscriber_count"] = len(subs)

            return agents

        except Exception as e:
            LOGGER.exception(f"Failed to list user agents: {e}")
            return []

    async def cancel_agent(
        self,
        instance_id: str,
        user_email: str = "",
        user_id: str = "",
        chat_id: str = "",
        organization_id: int = 0,
    ) -> Dict[str, Any]:
        """Unsubscribe from an agent. Terminates if last subscriber.

        In group chats: unsubscribe the group chat_id.
        In DMs: unsubscribe the user by email.
        Force-terminate only happens when the last subscriber leaves.
        Organization-scoped: can only cancel agents in your own org.
        """
        try:
            client = self._get_client()

            # Fetch agent with subscribers (org-scoped for security)
            query = (
                client.table("persistent_agent_instances")
                .select(
                    "id, instance_name, status, subscribers, created_by, "
                    "notify_chat_id, organization_id"
                )
                .eq("id", instance_id)
                .eq("expert_id", "user_agent")
            )
            if organization_id:
                query = query.eq("organization_id", organization_id)
            result = query.execute()
            if not result.data:
                return {"success": False, "error": "Agent not found or not in your organization"}

            agent = result.data[0]
            if agent["status"] == "terminated":
                return {"success": False, "error": "Agent is already terminated"}

            subscribers = agent.get("subscribers") or []
            original_count = len(subscribers)

            # Determine what to remove
            if chat_id and chat_id.startswith("-"):
                # Group: remove subscribers with this chat_id
                subscribers = [s for s in subscribers if s.get("chat_id") != chat_id]
            elif user_email:
                # DM: remove subscribers with this email
                subscribers = [s for s in subscribers if s.get("email") != user_email]
            else:
                return {"success": False, "error": "Cannot identify subscriber to remove"}

            removed = original_count - len(subscribers)
            if removed == 0:
                return {"success": False, "error": "You are not subscribed to this agent"}

            if not subscribers:
                # Last subscriber — terminate the agent
                client.table("persistent_agent_instances").update(
                    {"status": "terminated", "subscribers": []}
                ).eq("id", instance_id).execute()

                LOGGER.info(
                    f"Agent '{agent['instance_name']}' terminated "
                    f"(last subscriber left: {user_email or chat_id})"
                )
                return {
                    "success": True,
                    "instance_name": agent["instance_name"],
                    "message": f"Agent '{agent['instance_name']}' terminated (you were the last subscriber).",
                    "terminated": True,
                }
            else:
                # Update subscribers list
                client.table("persistent_agent_instances").update({"subscribers": subscribers}).eq(
                    "id", instance_id
                ).execute()

                LOGGER.info(
                    f"Unsubscribed {user_email or chat_id} from "
                    f"'{agent['instance_name']}' ({len(subscribers)} remaining)"
                )
                return {
                    "success": True,
                    "instance_name": agent["instance_name"],
                    "message": f"Unsubscribed from '{agent['instance_name']}' "
                    f"({len(subscribers)} subscriber(s) remaining).",
                    "terminated": False,
                }

        except Exception as e:
            LOGGER.exception(f"Failed to cancel/unsubscribe agent: {e}")
            return {"success": False, "error": str(e)}

    async def get_agent(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """Get a single agent instance by ID."""
        try:
            client = self._get_client()
            result = (
                client.table("persistent_agent_instances")
                .select("*")
                .eq("id", instance_id)
                .execute()
            )
            rows = result.data or []
            return dict(rows[0]) if rows else None
        except Exception as e:
            LOGGER.exception(f"Failed to get agent: {e}")
            return None
