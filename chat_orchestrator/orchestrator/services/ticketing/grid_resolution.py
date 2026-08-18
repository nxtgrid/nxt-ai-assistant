"""Org- and text-mention-based fallback grid resolution for ticket creation.

Used by escalation_service.py's track_as_ticket() when the exact
(customer_chat_id, customer_topic_id) match against
grids.internal_telegram_group_chat_id/thread_id finds nothing -- e.g. a
customer DM, or a group chat that isn't using Telegram's forum/topics
feature, neither of which has a topic id to match on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.utils.grid_matcher import find_grid_mention
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class GridResolution:
    """Result of the org/text-mention grid fallback.

    ``candidates`` is populated only in the genuinely ambiguous case (the
    organization has 2+ grids and no confident text match was found) --
    that's what the caller uses to flag the created ticket for a human
    instead of leaving it silently blank.
    """

    grid_name: Optional[str] = None
    candidates: List[str] = field(default_factory=list)


async def resolve_grid_name(
    *,
    organization_id: Optional[int],
    messages: List[Dict[str, Any]],
) -> GridResolution:
    """Resolve a grid name for an organization whose exact chat/topic match
    (escalation_service.py's own lookup, run before this is called) failed.

    - If the org has exactly one grid, use it.
    - If the org has 2+ grids, search recent customer ("user"-role) messages
      for a mention of one of them.
    - Otherwise, return the org's grid names as ``candidates`` so the caller
      can flag the ticket -- or nothing at all if the organization or its
      grids couldn't be resolved, since there's nothing meaningful to flag.

    Every failure degrades to an empty ``GridResolution()``; this is a
    data-quality enrichment and must never raise into ticket creation.
    """
    if not organization_id:
        return GridResolution()

    try:
        from shared.auth import get_auth_service

        grid_names = await get_auth_service().get_grid_names_for_organization(
            str(organization_id)
        )
    except Exception as e:
        LOGGER.debug(f"Could not fetch grids for organization {organization_id}: {e}")
        return GridResolution()

    if not grid_names:
        return GridResolution()

    if len(grid_names) == 1:
        return GridResolution(grid_name=grid_names[0])

    text = "\n".join(
        m.get("content", "") for m in messages if m.get("role") == "user" and m.get("content")
    )
    matched = find_grid_mention(text, grid_names)
    if matched:
        return GridResolution(grid_name=matched)

    return GridResolution(candidates=grid_names)
