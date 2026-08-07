"""Entity fan-out: resolving which entities (grids, organizations) a
schedule/skill/persistent-agent anchor covers, and how to describe one.

Lifted out of orchestrator/services/agent_worker.py (its
_get_eligible_entities / _build_anchor_metadata methods) by Phase 5 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 5, so that
skill scheduling (this phase) and persistent-agent reconciliation (still
agent_worker.py, until Phase 6 deletes it) share one implementation instead
of drifting into two. agent_worker.py now delegates here; see its
_get_eligible_entities/_build_anchor_metadata docstrings.

Adds the "organization" anchor type (skills can fan out per-organization,
not just per-grid) -- everything else stays unsupported, matching the
plan's explicit scope: "anything else stays unsupported."

Callers are responsible for the safety property this module deliberately
does NOT enforce itself: if get_eligible_entities returns an empty list,
that must be treated as "the Auth DB may be down," not "there are zero
entities" -- skip the tick/run rather than acting on an empty set. See
agent_worker.py's _reconcile_expert for the reference implementation of
that check, which this phase's skill dispatcher also replicates.
"""

from __future__ import annotations

from typing import Any, Dict, List

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Anchor types with an eligibility query and a metadata shape below.
# "anything else stays unsupported" (the plan's own words) -- callers
# should treat an entity_type outside this set as a hard "cannot fan out",
# not a silent empty list indistinguishable from "no eligible entities".
SUPPORTED_ANCHOR_ENTITY_TYPES = ("grid", "organization")


async def get_eligible_entities(entity_type: str) -> List[Dict[str, Any]]:
    """Get eligible entities for a given anchor_entity_type.

    Empty list means either "genuinely zero eligible entities" or "the
    Auth DB query failed" -- get_eligible_grids_for_agents/
    get_eligible_organizations_for_agents both degrade to [] on error
    rather than raising, so this function can't distinguish the two either.
    That ambiguity is exactly why callers must not treat an empty result as
    license to act on an empty set -- see this module's docstring.
    """
    if entity_type not in SUPPORTED_ANCHOR_ENTITY_TYPES:
        LOGGER.warning(f"No eligibility query registered for entity_type={entity_type}")
        return []

    # Deferred until entity_type is known to be supported -- constructing
    # AuthService opens a real DB connection (requires AUTH_DB_HOST/USER/
    # PASSWORD), which an unsupported entity_type has no reason to pay for.
    from shared.auth.auth_service import get_auth_service

    auth_service = get_auth_service()

    if entity_type == "grid":
        return await auth_service.get_eligible_grids_for_agents()
    return await auth_service.get_eligible_organizations_for_agents()


def build_anchor_metadata(entity_type: str, entity: Dict[str, Any]) -> Dict[str, Any]:
    """Build anchor_metadata dict from entity data.

    Each entity type maps its DB fields to a standard metadata shape used
    for event routing and context. The "grid" shape is byte-for-byte what
    agent_worker.py always produced, to keep persistent-agent instances
    (still reconciled through agent_worker.py's now-delegating wrapper)
    unaffected by the lift.
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

    if entity_type == "organization":
        return {
            "organization_name": entity.get("name", ""),
            "telegram_chat_id": str(entity.get("developer_group_telegram_chat_id") or ""),
            "telegram_topic_id": None,  # org chats are not forum/topic groups
            "organization_id": entity["id"],
        }

    # Fallback: store name + organization_id (matches agent_worker.py's
    # pre-lift fallback for an unrecognized entity_type).
    return {
        "name": entity.get("name", ""),
        "organization_id": entity.get("organization_id"),
    }


__all__ = [
    "SUPPORTED_ANCHOR_ENTITY_TYPES",
    "build_anchor_metadata",
    "get_eligible_entities",
]
