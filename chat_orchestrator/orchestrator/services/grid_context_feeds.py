"""Bounded O&M-message and episodic-distillation context for a grid.

Shared by the notify/alert-judgment flow (a single-shot LLM call with no tool
access -- see ``orchestrator.services.ticketing.correlator.judge``) and
interactive staff chat, which otherwise only reaches O&M chat on demand via
the ``customer_get_grid_chat_chronology`` MCP tool, and only when the model
thinks to call it. Both flows want the same bounded, always-on baseline
instead of relying on a tool call or a single-shot model having no way to
ask at all.

O&M messages are raw internal Telegram chat from a grid's O&M topic --
staff-only, matching ``customer_get_grid_chat_chronology``'s own
``visible_to_customer: False``. Never surface ``fetch_om_messages_for_grid``'s
result to a customer-facing conversation.

Episodic distillation is a summarized distillation, already access-gated for
non-staff callers by ``resolve_episodic_anchor`` (grid-scoped to an
organization-permitted grid, or the caller's own organization) -- safe for
customer.system too.
"""

from __future__ import annotations

from typing import Optional, Sequence

from shared.auth import get_auth_service
from shared.prompts.providers import ResolutionContext
from shared.prompts.providers_episodic import fetch_episodic_summary
from shared.prompts.types import RequestScope
from shared.utils.logging import get_logger

from .ticketing.notify_alert_delivery_repository import NotifyAlertDeliveryRepository, OMChatMessage

LOGGER = get_logger(__name__)

OM_MESSAGE_LIMIT = 50
# Per-message truncation happens in NotifyAlertDeliveryRepository itself
# (_OM_MESSAGE_CONTENT_CHARS) -- not duplicated here to avoid a circular
# import (that module is what this one wraps).
EPISODIC_SUMMARY_CHARS = 1000


def _raw_client() -> Optional[object]:
    """Same raw-postgrest-client accessor as app.py's ``_raw_supabase_client``
    and ``TicketService._raw_client`` -- one shared connection, not a new one
    per feed."""
    from orchestrator.services.supabase_client import get_supabase_client

    wrapper = get_supabase_client()
    return wrapper._get_client() if wrapper else None


async def fetch_om_messages_for_grid(
    grid_name: str, *, since: str, limit: int = OM_MESSAGE_LIMIT
) -> list[OMChatMessage]:
    """Recent O&M Telegram messages for a grid, resolved by name.

    Staff-only content -- callers must not surface this to a customer-facing
    conversation. Returns ``[]`` (never raises) when the grid has no
    confidently-matched O&M topic or the fetch fails, same fail-open
    contract as ``NotifyAlertDeliveryRepository.recent_om_messages``.
    """
    try:
        target = await get_auth_service().resolve_grid_notification_target(grid_name)
    except Exception:
        LOGGER.opt(exception=True).warning(f"Grid O&M target resolution failed for {grid_name!r}")
        return []
    if target is None:
        return []
    repo = NotifyAlertDeliveryRepository(get_client=_raw_client)
    return await repo.recent_om_messages(
        chat_id=target.chat_id, topic_id=target.topic_id, since=since, limit=limit
    )


async def fetch_episodic_summary_for_scope(
    *,
    grid: Optional[str],
    organization_id: Optional[str] = None,
    organization_ids: Sequence[str] = (),
    is_staff: bool = False,
) -> Optional[str]:
    """The grid's (or organization's) distilled prior history, capped to
    ``EPISODIC_SUMMARY_CHARS``. Safe for customer scope -- access is gated
    inside ``resolve_episodic_anchor`` the same way a pinned knowledge module
    would be."""
    ctx = ResolutionContext(
        scope=RequestScope(grid=grid, organization_id=organization_id),
        organization_ids=tuple(organization_ids),
        is_staff=is_staff,
    )
    summary = await fetch_episodic_summary(ctx)
    if not summary:
        return None
    return summary[:EPISODIC_SUMMARY_CHARS]


__all__ = [
    "EPISODIC_SUMMARY_CHARS",
    "OM_MESSAGE_LIMIT",
    "fetch_episodic_summary_for_scope",
    "fetch_om_messages_for_grid",
]
