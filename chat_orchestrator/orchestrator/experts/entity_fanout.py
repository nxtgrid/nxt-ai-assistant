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

from typing import Any, Dict

# Re-exported, not defined here: the enumeration moved to `shared` so
# anansi_app -- whose image has no `orchestrator` package, and which is
# where scheduled batch work runs -- can use the same one. Keeping the names
# importable from this path is what makes that a move rather than a fork.
from shared.entity_eligibility import (
    SUPPORTED_ANCHOR_ENTITY_TYPES,
    get_eligible_entities,
)
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


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
