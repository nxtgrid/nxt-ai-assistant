from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.services.ticketing.alert_delivery_policy import (
    all_phases_zero_for_override,
    decide_alert_delivery,
)
from orchestrator.services.ticketing.alert_judgment import (
    AlertJudgment,
    AlertJudgmentResult,
    GridImpact,
    LikelyUserAction,
    LikelyUserActionCategory,
    NotificationJudgment,
    RootCauseKind,
    TicketAction,
    TicketJudgment,
    TicketRelationship,
)
from orchestrator.services.ticketing.alert_judgment_context import (
    AlertJudgmentContext,
    AlertTelemetry,
    ContextSourceResult,
    ContextStatus,
)
from orchestrator.services.ticketing.notify_alert_delivery_repository import PriorAlertMessage
from shared.grid_status import SiteStatus

NOW = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)


def _result(*, prior: SiteStatus = SiteStatus.ON, current: SiteStatus = SiteStatus.ON, send: bool = False) -> AlertJudgmentResult:
    return AlertJudgmentResult(
        valid=True,
        judgment=AlertJudgment(
            grid_impact=GridImpact(
                prior_known_status=prior,
                current_assessed_status=current,
                material_status_change=prior != current,
                summary="Grid status assessed.",
                confidence=0.9,
            ),
            notification=NotificationJudgment(send_telegram=send, reason="Decision."),
            ticket=TicketJudgment(
                action=TicketAction.CREATE_NEW,
                target_ticket_ref=None,
                change_title=False,
                proposed_title=None,
                change_description=False,
                description_addition=None,
                relationship=TicketRelationship.NEW_ISSUE,
                root_cause_kind=RootCauseKind.COMPONENT,
                reason="New issue.",
                confidence=0.9,
            ),
            likely_user_action=LikelyUserAction(
                category=LikelyUserActionCategory.MONITOR, summary="Monitor.", confidence=0.8
            ),
        ),
    )


def _context(*, telemetry: AlertTelemetry | None = None, failed: str | None = None) -> AlertJudgmentContext:
    availability = {
        name: ContextSourceResult(status=ContextStatus.AVAILABLE)
        for name in (
            "deterministic_findings", "open_tickets", "telemetry", "prior_alerts", "om_messages"
        )
    }
    if failed:
        availability[failed] = ContextSourceResult(status=ContextStatus.FAILED)
    return AlertJudgmentContext(telemetry=telemetry or AlertTelemetry(
        generation_management="managed", grid_status="hps_on", site_status="on", fresh=True
    ), availability=availability)


def test_only_complete_valid_explicit_no_suppresses() -> None:
    decision = decide_alert_delivery(_result(), _context(), enforcement_enabled=True, now=NOW)

    assert decision.send is False
    assert decision.reason == "llm_explicit_suppression"


@pytest.mark.parametrize(
    "judgment,context,force_reason",
    [
        (AlertJudgmentResult(valid=False, error_code="timed_out"), _context(), "llm_failed"),
        (AlertJudgmentResult(valid=False, error_code="invalid_json"), _context(), "llm_invalid"),
        (_result(prior=SiteStatus.UNKNOWN), _context(), "status_unknown"),
        (_result(), _context(failed="open_tickets"), "context_failed:open_tickets"),
    ],
)
def test_every_failure_forces_send(judgment, context, force_reason) -> None:
    decision = decide_alert_delivery(judgment, context, enforcement_enabled=True, now=NOW)

    assert decision.send is True
    assert force_reason in decision.forced_by


def test_shadow_mode_always_sends() -> None:
    decision = decide_alert_delivery(_result(), _context(), enforcement_enabled=False, now=NOW)

    assert decision.send is True
    assert decision.forced_by == ["shadow_mode"]


def test_all_phase_zero_after_eight_hours_forces_a_reminder() -> None:
    telemetry = AlertTelemetry(
        generation_management="managed",
        grid_status="off",
        site_status="off",
        fresh=True,
        l1_voltage_v=0.0,
        l2_voltage_v=0.0,
        l3_voltage_v=0.0,
    )
    prior = PriorAlertMessage(
        external_chat_id="-1001",
        external_message_id=1,
        sent_at=(NOW - timedelta(hours=8, seconds=1)).isoformat(),
    )

    decision = decide_alert_delivery(
        _result(prior=SiteStatus.OFF, current=SiteStatus.OFF),
        _context(telemetry=telemetry),
        latest_prior_alert=prior,
        enforcement_enabled=True,
        now=NOW,
    )

    assert all_phases_zero_for_override(telemetry) is True
    assert decision.send is True
    assert "all_phase_zero_reminder" in decision.forced_by


@pytest.mark.parametrize("phase", [(0.0, 0.0, 1.0), (0.0, None, 0.0)])
def test_partial_or_missing_phase_does_not_trigger_override(phase) -> None:
    telemetry = AlertTelemetry(
        generation_management="managed", fresh=True,
        l1_voltage_v=phase[0], l2_voltage_v=phase[1], l3_voltage_v=phase[2],
    )

    assert all_phases_zero_for_override(telemetry) is False
