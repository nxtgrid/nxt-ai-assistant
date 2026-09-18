"""Detects the ``/notify`` pipeline (n8n's polling job -> chat_orchestrator's
``/notify`` webhook) going silent fleet-wide, and files a ticket + Telegram
alert so it does not take a customer complaint to notice.

Exists because n8n once stopped calling ``/notify`` for *any* grid for 5
days straight, and nothing in this repo could have caught it --
``correlator.judge()`` and everything downstream of it only ever runs once
an alert actually arrives. This is the missing upstream check: is anything
arriving at all.

Deliberately grid-less. This is a statement about the pipeline itself, not
about any one site, so it carries none of ``TicketCreateRequest``'s
grid-specific fields and dedups by searching Jira for a fixed, reused label
(``NOTIFY_WATCHDOG_LABEL``) rather than the per-alert correlation machinery,
which assumes a grid.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

NOTIFY_WATCHDOG_LABEL = "notify-pipeline-watchdog"
DEFAULT_THRESHOLD_HOURS = 24.0


def watchdog_enabled() -> bool:
    """Default on, matching every other scheduled-batch gate in this
    deployment (see ``episodic_scheduler.py``'s ``_episodic_scheduling_enabled``
    / ``JIRA_SWEEP_ENABLED``) -- a deployment that has never heard of this
    flag still gets the check. This is the deliberate off switch for a
    deployment that does not wire up ``/notify`` at all; ``silence_detected``
    also treats a table that has *never* had a single row as "not
    applicable" rather than silence, so an unconfigured deployment that
    forgets to flip this is still unlikely to false-fire -- but the explicit
    flag is the documented way to opt out.
    """
    return os.getenv("NOTIFY_WATCHDOG_ENABLED", "").strip().lower() != "false"


def watchdog_threshold_hours() -> float:
    try:
        return float(os.getenv("NOTIFY_WATCHDOG_HOURS", str(DEFAULT_THRESHOLD_HOURS)))
    except ValueError:
        return DEFAULT_THRESHOLD_HOURS


def _parse_sent_at(value: Optional[str]) -> Optional[datetime]:
    """Mirrors ``downtime_alert_policy.py``'s/``alert_delivery_policy.py``'s
    own ``_parse_sent_at`` -- each small ticketing module here keeps its own
    rather than sharing one, and this follows that precedent."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def silence_detected(
    last_delivery_at: Optional[datetime], now: datetime, threshold_hours: float
) -> bool:
    """True when the pipeline has gone quiet long enough to flag.

    ``None`` (the table has never had a row at all, or its ``sent_at``
    could not be parsed) is deliberately NOT silence -- ambiguous by the
    same convention ``distill_anchor_type`` / ``eligible_anchor_names`` use
    elsewhere: a fresh deployment and one that has genuinely never wired up
    ``/notify`` both look like an empty table, and only a table that *used
    to* have rows and stopped is this watchdog's business.
    """
    if last_delivery_at is None:
        return False
    if last_delivery_at.tzinfo is None:
        last_delivery_at = last_delivery_at.replace(tzinfo=timezone.utc)
    return (now - last_delivery_at) > timedelta(hours=threshold_hours)


async def run_notify_watchdog(
    *,
    delivery_repo: Any,
    ticket_service: Any,
    send_telegram: Callable[[str, str], Awaitable[Any]],
    escalation_chat_id: str,
    now: Optional[datetime] = None,
    threshold_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """One watchdog pass. Returns a result dict for logging/testing.

    Never raises -- a broken watchdog must not take the rest of the daily
    scheduler down with it, matching every other job registered alongside
    it in ``app.py``.

    ``delivery_repo`` needs ``most_recent_delivery_at() -> str | None``
    (``NotifyAlertDeliveryRepository``). ``ticket_service`` needs
    ``resolve_backend()`` and ``create_ticket_with_internal_fallback(req)``
    (``TicketService``). ``send_telegram(chat_id, text)`` is injected rather
    than imported directly so tests don't need a real bot token.
    """
    if not watchdog_enabled():
        return {"enabled": False}

    now = now if now is not None else datetime.now(timezone.utc)
    threshold_hours = (
        threshold_hours if threshold_hours is not None else watchdog_threshold_hours()
    )

    try:
        last_at_raw = await delivery_repo.most_recent_delivery_at()
    except Exception:
        LOGGER.opt(exception=True).warning("Notify watchdog: delivery lookup failed")
        return {"enabled": True, "error": "delivery_lookup_failed"}

    last_at = _parse_sent_at(last_at_raw)
    if not silence_detected(last_at, now, threshold_hours):
        return {"enabled": True, "silent": False, "last_delivery_at": last_at}

    hours_silent = (now - last_at).total_seconds() / 3600.0

    existing_ticket: Optional[str] = None
    try:
        backend = await ticket_service.resolve_backend()
        finder = getattr(backend, "find_open_ticket_by_label", None)
        if finder is not None:
            existing_ticket = await finder(NOTIFY_WATCHDOG_LABEL)
    except Exception:
        LOGGER.opt(exception=True).warning(
            "Notify watchdog: dedup check failed -- filing anyway rather than risk staying silent"
        )

    if existing_ticket:
        LOGGER.info(
            "Notify watchdog: pipeline silent {:.1f}h, already tracked as {}",
            hours_silent,
            existing_ticket,
        )
        return {
            "enabled": True,
            "silent": True,
            "hours_silent": hours_silent,
            "already_tracked": existing_ticket,
        }

    summary = f"No /notify alerts received on any grid in over {threshold_hours:.0f}h"
    description = (
        "The /notify pipeline (n8n's polling job -> chat_orchestrator's /notify webhook) "
        f"has not delivered a single alert for any grid since "
        f"{last_at.isoformat()} ({hours_silent:.1f}h ago).\n\n"
        "This usually means the upstream n8n polling job has stopped or lost VRM access, "
        "not that the whole fleet has gone quiet at once -- check n8n's execution history "
        "first.\n\n"
        "Filed automatically by the notify-pipeline watchdog. Resolving this ticket clears "
        "the check; a fresh one files if the silence recurs later."
    )

    ticket_ref: Optional[str] = None
    try:
        from orchestrator.services.ticketing.backend import TicketCreateRequest

        outcome = await ticket_service.create_ticket_with_internal_fallback(
            TicketCreateRequest(
                summary=summary,
                description=description,
                labels=[NOTIFY_WATCHDOG_LABEL],
            )
        )
        if outcome.result is not None:
            ticket_ref = outcome.result.ref
        else:
            LOGGER.warning("Notify watchdog: ticket creation returned no result: {}", outcome.error)
    except Exception:
        LOGGER.opt(exception=True).warning("Notify watchdog: ticket creation failed")

    if escalation_chat_id:
        ticket_line = f"\nTicket: {ticket_ref}" if ticket_ref else ""
        try:
            await send_telegram(
                escalation_chat_id,
                (
                    f"⚠️ *No /notify alerts received for any grid in over "
                    f"{threshold_hours:.0f}h.*\nLast delivery: {last_at.isoformat()} "
                    f"({hours_silent:.1f}h ago).\nLikely the upstream n8n trigger has "
                    f"stopped -- check its execution history.{ticket_line}"
                ),
            )
        except Exception:
            LOGGER.opt(exception=True).warning("Notify watchdog: Telegram alert failed")

    LOGGER.warning(
        "Notify watchdog: pipeline silent {:.1f}h (threshold {:.0f}h) -- filed {}",
        hours_silent,
        threshold_hours,
        ticket_ref or "no ticket (see log above)",
    )
    return {
        "enabled": True,
        "silent": True,
        "hours_silent": hours_silent,
        "filed_ticket": ticket_ref,
    }
