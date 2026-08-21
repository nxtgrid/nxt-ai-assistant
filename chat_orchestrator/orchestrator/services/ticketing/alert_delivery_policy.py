"""Pure, fail-open policy for LLM-governed alert delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from shared.grid_status import SiteStatus

from .alert_judgment import AlertJudgmentResult
from .alert_judgment_context import AlertJudgmentContext, AlertTelemetry, ContextStatus
from .notify_alert_delivery_repository import PriorAlertMessage

OUTAGE_REMINDER_INTERVAL = timedelta(hours=8)
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
) -> DeliveryDecision:
    """Suppress only the explicitly healthy LLM verdict; every doubt sends."""
    if not enforcement_enabled:
        return DeliveryDecision(send=True, reason="shadow_mode", forced_by=["shadow_mode"])

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
        if impact.prior_known_status not in known or impact.current_assessed_status not in known:
            force_reasons.append("status_unknown")
        if impact.material_status_change:
            force_reasons.append("material_status_change")
        if judgment.notification.send_telegram:
            force_reasons.append("llm_requested_delivery")

    if all_phases_zero_for_override(context.telemetry):
        if latest_prior_alert is None:
            force_reasons.append("all_phase_zero_reminder")
        else:
            old = _prior_is_older_than_interval(latest_prior_alert, now or datetime.now(timezone.utc))
            if old is None:
                force_reasons.append("prior_alert_timestamp_invalid")
            elif old:
                force_reasons.append("all_phase_zero_reminder")

    if force_reasons:
        return DeliveryDecision(send=True, reason="fail_open", forced_by=force_reasons)
    return DeliveryDecision(send=False, reason="llm_explicit_suppression", forced_by=[])
