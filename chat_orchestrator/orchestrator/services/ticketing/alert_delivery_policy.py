"""Pure, fail-open policy for LLM-governed alert delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from shared.grid_status import SiteStatus

from .alert_judgment import AlertJudgment, AlertJudgmentResult, TicketAction
from .alert_judgment_context import AlertJudgmentContext, AlertTelemetry, ContextStatus
from .notify_alert_delivery_repository import PriorAlertMessage

OUTAGE_REMINDER_INTERVAL = timedelta(hours=8)
FAIL_OPEN_REMINDER_INTERVAL = timedelta(hours=8)

# Forces that carry evidence rather than doubt. These are never capped: the grid
# changing state and the all-phase-zero reminder are positive findings, and the
# last already has its own clock. An unparseable prior timestamp is listed here
# too -- if we cannot read the history we cannot dedupe against it, so it must
# not quiet anything.
#
# ``llm_requested_delivery`` used to sit here unconditionally; it is now
# conditional -- see ``_evidence_forces``.
_EVIDENCE_FORCES = frozenset(
    {
        "material_status_change",
        "all_phase_zero_reminder",
        "prior_alert_timestamp_invalid",
    }
)
_REQUIRED_SOURCES = (
    "deterministic_findings",
    "open_tickets",
    "telemetry",
    "prior_alerts",
    "om_messages",
)


@dataclass(frozen=True)
class DeliveryDecision:
    send: bool
    reason: str
    forced_by: list[str]


def all_phases_zero_for_override(telemetry: AlertTelemetry) -> bool:
    return bool(
        telemetry.generation_management == "managed"
        and telemetry.fresh
        and telemetry.l1_voltage_v is not None
        and telemetry.l2_voltage_v is not None
        and telemetry.l3_voltage_v is not None
        and all(
            value == 0.0
            for value in (
                telemetry.l1_voltage_v,
                telemetry.l2_voltage_v,
                telemetry.l3_voltage_v,
            )
        )
    )


def _status_unknown_is_explained(telemetry: AlertTelemetry) -> bool:
    """Whether UNKNOWN status is already accounted for rather than a doubt.

    Two reasons qualify, and both are facts rather than gaps:

    - ``unmanaged`` -- there is no plant of ours to read, permanently. The
      design calls this out by name as a "valid non-failure state".
    - ``stale`` -- the plant's gateway has stopped reporting. The downtime
      floor now turns that into one comms-down alert a day
      (``downtime_alert_policy.assess_downtime``), so forcing here as well only
      adds a message per device on top of it.

    Everything else -- a failed fetch, an unestablished management state, no
    VRM site on file -- is a genuine gap in our knowledge and still fails open.
    """
    return (
        telemetry.generation_management == "unmanaged"
        or telemetry.unavailable_reason in {"unmanaged", "stale"}
    )


def _parse_sent_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _already_said_recently(
    context: AlertJudgmentContext, ticket_ref: str, now: datetime
) -> bool:
    """Whether this exact ticket already reached the topic inside the window."""
    for prior in context.prior_alerts:
        if (prior.ticket_ref or "") != ticket_ref:
            continue
        sent_at = _parse_sent_at(prior.sent_at)
        if sent_at is not None and now - sent_at <= FAIL_OPEN_REMINDER_INTERVAL:
            return True
    return False


def _evidence_forces(judgment: "AlertJudgment | None") -> frozenset[str]:
    """Which forces outrank the cap for *this* judgment.

    ``llm_requested_delivery`` is the conditional one, and that is the whole
    point. The correlation prompt tells the model to ask for delivery whenever
    the site status is unknown -- and for a plant whose generation we do not
    manage, unknown is not a transient doubt but a permanent fact about the
    site, the very case ``_status_unknown_is_explained`` already stops the
    policy itself forcing on. Treating the model's request as evidence
    regardless handed that same unknown a standing exemption from the cap, so
    an unmanaged site's re-firing alert reached its topic on every scheduled
    run for as long as its ticket stayed open: the 2026-08-28 flood, restored
    through the one door the cap left open.

    ``record_occurrence`` is the model saying, in its own vocabulary, that
    nothing about the ticket changed. A send request resting on that is a doubt
    like any other, and is capped like one -- quieter, never silent, since the
    cap only ever applies when this exact ticket already reached the topic
    inside ``FAIL_OPEN_REMINDER_INTERVAL``. Any other action means the ticket
    itself is about to change, which the topic genuinely has not heard.
    """
    if judgment is not None and judgment.ticket.action is not TicketAction.RECORD_OCCURRENCE:
        return _EVIDENCE_FORCES | {"llm_requested_delivery"}
    return _EVIDENCE_FORCES


def _fail_open_is_capped(
    judgment: "AlertJudgment | None",
    context: AlertJudgmentContext,
    force_reasons: list[str],
    now: datetime,
    correlated_ticket_ref: str = "",
) -> bool:
    """Whether this forced send would only be repeating a doubt we just voiced.

    Fail-open owes the topic *a* message, not one per alert. Seven MPPT
    warnings arriving on one ticket inside a minute are seven reasons to doubt,
    not seven things to say -- and the ticket records every one of them either
    way; only Telegram is quieted.

    Capping is refused wherever it could cause silence rather than brevity: any
    evidence-driven force, an unreadable delivery history, or an alert with no
    existing ticket to have already spoken about.

    ``correlated_ticket_ref`` is that last clause's escape hatch, and the whole
    reason a device storm used to speak once per device. The model names a
    target ticket only when it decides UPDATE/DUPLICATE; a judgment that failed
    outright, or one asserting CREATE_NEW, leaves ``target_ticket_ref`` empty,
    the cap declines, and every alert in the burst posts in full. The caller
    now resolves the alert's ticket deterministically (signature match) before
    asking, so the burst's second and later alerts arrive here carrying the
    same ref the first one already spoke about -- which is the fact the cap
    needed all along and could not see.
    """
    if any(reason in _evidence_forces(judgment) for reason in force_reasons):
        return False
    history = context.availability.get("prior_alerts")
    if history is None or history.status in {ContextStatus.FAILED, ContextStatus.TIMED_OUT}:
        return False
    ticket_ref = (judgment.ticket.target_ticket_ref or "") if judgment is not None else ""
    ticket_ref = ticket_ref or (correlated_ticket_ref or "")
    if not ticket_ref:
        return False
    return _already_said_recently(context, ticket_ref, now)


def _prior_is_older_than_interval(prior: PriorAlertMessage, now: datetime) -> bool | None:
    try:
        sent_at = datetime.fromisoformat(prior.sent_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return now - sent_at > OUTAGE_REMINDER_INTERVAL


def decide_alert_delivery(
    judgment_result: AlertJudgmentResult,
    context: AlertJudgmentContext,
    *,
    latest_prior_alert: PriorAlertMessage | None = None,
    enforcement_enabled: bool,
    now: datetime | None = None,
    correlated_ticket_ref: str = "",
) -> DeliveryDecision:
    """Suppress only the explicitly healthy LLM verdict; every doubt sends.

    ``correlated_ticket_ref`` is the ticket this alert actually landed on once
    the caller has resolved it, which is not always the one the model named --
    see ``_fail_open_is_capped``. Optional so an existing caller keeps working;
    omitting it only forgoes the cap.
    """
    if not enforcement_enabled:
        return DeliveryDecision(send=True, reason="shadow_mode", forced_by=["shadow_mode"])

    moment = now or datetime.now(timezone.utc)
    force_reasons: list[str] = []
    if not judgment_result.valid or judgment_result.judgment is None:
        force_reasons.append(
            "llm_failed" if judgment_result.error_code in {"timed_out", "llm_failed"} else "llm_invalid"
        )
    for name in _REQUIRED_SOURCES:
        result = context.availability.get(name)
        if result is None or result.status in {ContextStatus.FAILED, ContextStatus.TIMED_OUT}:
            force_reasons.append(f"context_failed:{name}")

    judgment = judgment_result.judgment
    if judgment is not None:
        impact = judgment.grid_impact
        known = {SiteStatus.ON, SiteStatus.ISOLATED, SiteStatus.OFF}
        # An unmanaged plant reports UNKNOWN because there is nothing of ours to
        # read, not because a read failed -- and nothing will ever make it
        # knowable. Treating that as doubt forces every alert on the grid
        # forever -- two unmanaged sites re-fired one meter alert hourly on this
        # rung. The design says so directly: "no candidates, no history, and an
        # unmanaged plant are valid non-failure states and do not by themselves
        # force a message" -- 2026-08-21-llm-first-alert-correlation-design.md.
        # Every other rung below still applies to an unmanaged grid.
        if not _status_unknown_is_explained(context.telemetry) and (
            impact.prior_known_status not in known
            or impact.current_assessed_status not in known
        ):
            force_reasons.append("status_unknown")
        if impact.material_status_change:
            force_reasons.append("material_status_change")
        if judgment.notification.send_telegram:
            force_reasons.append("llm_requested_delivery")

    if all_phases_zero_for_override(context.telemetry):
        if latest_prior_alert is None:
            force_reasons.append("all_phase_zero_reminder")
        else:
            old = _prior_is_older_than_interval(latest_prior_alert, moment)
            if old is None:
                force_reasons.append("prior_alert_timestamp_invalid")
            elif old:
                force_reasons.append("all_phase_zero_reminder")

    if force_reasons:
        if _fail_open_is_capped(
            judgment, context, force_reasons, moment, correlated_ticket_ref
        ):
            # forced_by is kept: the audit should still show what would have
            # sent this, so a capped storm is greppable rather than invisible.
            return DeliveryDecision(send=False, reason="fail_open_capped", forced_by=force_reasons)
        return DeliveryDecision(send=True, reason="fail_open", forced_by=force_reasons)
    return DeliveryDecision(send=False, reason="llm_explicit_suppression", forced_by=[])
