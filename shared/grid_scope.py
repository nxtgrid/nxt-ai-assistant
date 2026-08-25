"""Which grid a conversation is about, for RequestScope.

RequestScope.grid gates every ``site:``-scoped context module and picks the
episodic module's anchor (grid beats organization). Nothing ever populated it:
prepare_context.py calls _fetch_jit_context without a grid, so scope.grid was
always None, every site-scoped module silently never matched, and episodic
memory could only ever resolve against an organization. The Context admin page
has said "not currently wired up" next to grid scope for exactly this reason.

Two signals, most specific first:

1. **The conversation's own channel.** A grid carries
   ``internal_telegram_group_chat_id`` / ``internal_telegram_group_thread_id``.
   A message in that group is about that grid -- true for staff too, which
   matters because staff can see every grid and so have no unambiguous
   fallback.
2. **An unambiguous permission set.** A caller who can see exactly one grid is
   asking about that grid. A caller who can see three is not, and a caller who
   can see forty (staff) certainly is not.

Anything else stays None -- the pre-existing behaviour. This never guesses:
picking an arbitrary grid would attach one site's material to another site's
conversation, which is worse than attaching none.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

CHANNEL_MAP_TTL = 300

_CACHE: Dict[str, Tuple[float, Any]] = {}


def _cached(key: str):
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _store(key: str, ttl: float, value: Any) -> Any:
    _CACHE[key] = (time.time() + ttl, value)
    return value


def invalidate() -> None:
    """Drop the channel map. For tests, and for an operator-triggered refresh."""
    _CACHE.clear()


def _norm(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def build_channel_map(grids: List[Dict[str, Any]]) -> Dict[Tuple[str, str], str]:
    """(chat_id, thread_id) -> grid name, plus a (chat_id, "") fallback.

    Both keys are recorded because a grid's group may or may not be a forum:
    a non-forum group has no thread id, and a forum group's General topic
    reports none either. The exact-topic key is looked up first so a forum
    grid still wins its own topic.
    """
    mapping: Dict[Tuple[str, str], str] = {}
    for grid in grids:
        name = _norm(grid.get("name"))
        chat_id = _norm(grid.get("internal_telegram_group_chat_id"))
        if not name or not chat_id:
            continue
        thread_id = _norm(grid.get("internal_telegram_group_thread_id"))
        if thread_id:
            mapping[(chat_id, thread_id)] = name
        # Never overwrite a chat-wide entry claimed by a grid with no thread:
        # that grid owns the whole group, and a forum sibling must not steal it.
        mapping.setdefault((chat_id, ""), name)
    return mapping


def grid_from_channel(
    channel_map: Dict[Tuple[str, str], str],
    chat_id: Optional[str],
    topic_id: Optional[str],
) -> Optional[str]:
    """Resolve a conversation's channel to a grid name, exact topic first."""
    chat = _norm(chat_id)
    if not chat:
        return None
    topic = _norm(topic_id)
    if topic:
        hit = channel_map.get((chat, topic))
        if hit:
            return hit
    return channel_map.get((chat, ""))


async def _channel_map() -> Dict[Tuple[str, str], str]:
    hit = _cached("channel_map")
    if hit is not None:
        return hit
    try:
        from shared.entity_eligibility import get_eligible_entities

        grids = await get_eligible_entities("grid")
    except Exception:
        LOGGER.opt(exception=True).warning("Grid channel map lookup failed")
        return {}
    # Not cached on failure: an empty map from an outage would otherwise
    # suppress grid scope for the next five minutes.
    if not grids:
        return {}
    return _store("channel_map", CHANNEL_MAP_TTL, build_channel_map(grids))


async def _sole_visible_grid(
    organization_ids: List[str], is_staff: bool
) -> Optional[str]:
    """The caller's grid, when they can see exactly one."""
    if is_staff:
        # Staff see every grid; "exactly one" would only ever be true for a
        # deployment with a single grid, where the channel signal already
        # covers it. Skip the query entirely.
        return None
    if not organization_ids:
        return None
    try:
        from shared.auth import get_auth_service

        names = await get_auth_service().get_grid_names_for_organization(
            organization_id=organization_ids[0]
        )
    except Exception:
        LOGGER.opt(exception=True).warning("Grid name lookup failed while resolving scope")
        return None
    return names[0] if names and len(names) == 1 else None


async def resolve_scope_grid(
    chat_id: Optional[str] = None,
    topic_id: Optional[str] = None,
    organization_ids: Optional[List[str]] = None,
    is_staff: bool = False,
) -> Optional[str]:
    """The grid this conversation is about, or None. Never raises."""
    try:
        from_channel = grid_from_channel(await _channel_map(), chat_id, topic_id)
        if from_channel:
            return from_channel
        return await _sole_visible_grid(list(organization_ids or []), is_staff)
    except Exception:
        LOGGER.opt(exception=True).warning("Grid scope resolution failed; continuing unscoped")
        return None


__all__ = [
    "build_channel_map",
    "grid_from_channel",
    "invalidate",
    "resolve_scope_grid",
]
