from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.services.ticketing.alert_delivery_policy import (
    FAIL_OPEN_REMINDER_INTERVAL,
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


def _unmanaged_telemetry() -> AlertTelemetry:
    """What client_grid_status returns for a grid whose generation we do not
    manage: _unavailable_live_telemetry("unmanaged"), which reports UNKNOWN
    because there is no plant of ours to read -- not because a read failed."""
    return AlertTelemetry(
        generation_management="unmanaged",
        grid_status="unknown",
        site_status="unknown",
        fresh=False,
    )


def test_unmanaged_generation_does_not_force_on_unknown_status() -> None:
    """An unmanaged grid's UNKNOWN status is its configuration, not a doubt.

    Nothing about it will ever become knowable, so treating it as a reason to
    override the judgment forces every alert on that grid forever. Two unmanaged
    sites re-fired the same meter alert onto one ticket every hour on exactly
    this rung.
    """
    decision = decide_alert_delivery(
        _result(prior=SiteStatus.UNKNOWN, current=SiteStatus.UNKNOWN),
        _context(telemetry=_unmanaged_telemetry()),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.forced_by == []
    assert decision.send is False
    assert decision.reason == "llm_explicit_suppression"


def test_unmanaged_generation_still_forces_on_every_other_doubt() -> None:
    """Only the status rung is excused. An unmanaged grid whose LLM call failed,
    or whose required context is missing, still fails open like any other."""
    unmanaged = _unmanaged_telemetry()

    llm_failed = decide_alert_delivery(
        AlertJudgmentResult(valid=False, error_code="timed_out"),
        _context(telemetry=unmanaged),
        enforcement_enabled=True,
        now=NOW,
    )
    context_failed = decide_alert_delivery(
        _result(prior=SiteStatus.UNKNOWN, current=SiteStatus.UNKNOWN),
        _context(telemetry=unmanaged, failed="om_messages"),
        enforcement_enabled=True,
        now=NOW,
    )

    assert llm_failed.send is True and "llm_failed" in llm_failed.forced_by
    assert context_failed.send is True
    assert "context_failed:om_messages" in context_failed.forced_by


def test_managed_grid_with_unknown_status_still_forces() -> None:
    """The rung that matters stays intact: on a grid we do manage, an UNKNOWN
    status means we genuinely cannot tell what is happening, and silence is not
    ours to choose."""
    managed_but_dark = AlertTelemetry(
        generation_management="managed",
        grid_status="unknown",
        site_status="unknown",
        fresh=False,
    )

    decision = decide_alert_delivery(
        _result(prior=SiteStatus.UNKNOWN, current=SiteStatus.UNKNOWN),
        _context(telemetry=managed_but_dark),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is True
    assert "status_unknown" in decision.forced_by


def _on_ticket(
    ref: str,
    *,
    send: bool = False,
    action: TicketAction = TicketAction.RECORD_OCCURRENCE,
    prior: SiteStatus = SiteStatus.UNKNOWN,
    current: SiteStatus = SiteStatus.UNKNOWN,
) -> AlertJudgmentResult:
    """A judgment that puts this alert onto an existing ticket."""
    result = _result(prior=prior, current=current, send=send)
    assert result.judgment is not None
    return result.model_copy(
        update={
            "judgment": result.judgment.model_copy(
                update={
                    "ticket": result.judgment.ticket.model_copy(
                        update={"action": action, "target_ticket_ref": ref}
                    )
                }
            )
        }
    )


def _spoke_about(ref: str, *, minutes_ago: int) -> PriorAlertMessage:
    sent = NOW - timedelta(minutes=minutes_ago)
    return PriorAlertMessage(
        external_chat_id="-100",
        external_message_id=1,
        sent_at=sent.isoformat(),
        ticket_ref=ref,
    )


def _with_history(*prior: PriorAlertMessage, failed: str | None = None) -> AlertJudgmentContext:
    context = _context(failed=failed)
    return context.model_copy(update={"prior_alerts": list(prior)})


def test_stale_telemetry_no_longer_forces_every_alert() -> None:
    """A plant that stopped reporting is handled by the downtime floor, once a
    day. Forcing here as well put one message per device on top of it."""
    stale = AlertTelemetry(
        generation_management="managed",
        grid_status="unknown",
        site_status="unknown",
        unavailable_reason="stale",
        fresh=False,
    )

    decision = decide_alert_delivery(
        _result(prior=SiteStatus.UNKNOWN, current=SiteStatus.UNKNOWN),
        _context(telemetry=stale),
        enforcement_enabled=True,
        now=NOW,
    )

    assert "status_unknown" not in decision.forced_by


def test_doubt_only_force_is_capped_once_we_have_already_said_it() -> None:
    """Fail-open owes the topic *a* message, not one per alert.

    Seven MPPT warnings landing on one ticket inside a minute are seven reasons
    to doubt, not seven things to say.
    """
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000"),
        _with_history(_spoke_about("OPS-1000", minutes_ago=3)),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is False
    assert decision.reason == "fail_open_capped"
    assert "status_unknown" in decision.forced_by, "the audit still records what forced it"


def test_the_first_doubt_still_gets_through() -> None:
    """The guarantee half: nothing said about this ticket yet, so say it."""
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000"), _with_history(), enforcement_enabled=True, now=NOW
    )

    assert decision.send is True
    assert decision.reason == "fail_open"


def test_cap_expires_with_the_window() -> None:
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000"),
        _with_history(_spoke_about("OPS-1000", minutes_ago=int(FAIL_OPEN_REMINDER_INTERVAL.total_seconds() // 60) + 1)),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is True


def test_cap_is_per_ticket_not_per_grid() -> None:
    """A different ticket is a different problem, however recently we spoke."""
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000"),
        _with_history(_spoke_about("OPS-9999", minutes_ago=1)),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is True


def test_a_material_status_change_is_never_capped() -> None:
    """The cap only ever quiets doubt. A grid that changed state is a finding,
    and it goes out however recently we last spoke."""
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000", send=True, prior=SiteStatus.ON, current=SiteStatus.OFF),
        _with_history(_spoke_about("OPS-1000", minutes_ago=1)),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is True
    assert "material_status_change" in decision.forced_by


def test_an_llm_send_request_is_evidence_only_when_the_ticket_changed() -> None:
    """An UPDATE_EXISTING carries something the topic has not heard: the ticket
    itself is about to change. That request outranks the cap, as it always has."""
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000", send=True, action=TicketAction.UPDATE_EXISTING),
        _with_history(_spoke_about("OPS-1000", minutes_ago=1)),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is True
    assert "llm_requested_delivery" in decision.forced_by


def test_an_llm_send_request_on_a_pure_re_fire_is_capped_like_any_other_doubt() -> None:
    """RECORD_OCCURRENCE is the model saying, in its own vocabulary, that
    nothing about this ticket changed. A send request on top of that is not a
    finding -- it is the same doubt the cap exists to quiet, arriving through
    the model instead of through the policy.

    This is the door the 2026-08-29 cap left open. An unmanaged plant reports
    UNKNOWN forever, ``_status_unknown_is_explained`` correctly stops the
    policy forcing on it, and the correlation prompt then asks for delivery on
    exactly the same UNKNOWN -- so the alert re-fired into the topic on every
    scheduled run, indefinitely, with the cap standing aside for it.
    """
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000", send=True),
        _with_history(_spoke_about("OPS-1000", minutes_ago=1)),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is False
    assert decision.reason == "fail_open_capped"
    assert "llm_requested_delivery" in decision.forced_by, "the audit still records it"


def test_a_capped_re_fire_still_speaks_again_once_the_window_passes() -> None:
    """The guarantee half of the cap: quieter, never silent."""
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000", send=True),
        _with_history(
            _spoke_about(
                "OPS-1000",
                minutes_ago=int(FAIL_OPEN_REMINDER_INTERVAL.total_seconds() // 60) + 1,
            )
        ),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is True


def test_cap_needs_reliable_history_to_apply() -> None:
    """If we could not read what we already sent, we cannot know we are
    repeating ourselves -- so we repeat ourselves. Fail open, as everywhere."""
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000"),
        _with_history(_spoke_about("OPS-1000", minutes_ago=1), failed="prior_alerts"),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is True


def test_a_brand_new_ticket_is_never_capped() -> None:
    """No ticket from either source means nothing was said about it yet.

    The caller can still supply one it resolved itself -- see
    ``correlated_ticket_ref`` below -- but with neither, there is nothing to
    have already spoken about and the cap must decline.
    """
    decision = decide_alert_delivery(
        _result(prior=SiteStatus.UNKNOWN, current=SiteStatus.UNKNOWN),
        _with_history(_spoke_about("OPS-1000", minutes_ago=1)),
        enforcement_enabled=True,
        now=NOW,
    )

    assert decision.send is True


# --------------------------------------------------------------------------- #
# The caller's own correlation result
#
# The model names a target ticket only when it decides UPDATE/DUPLICATE. A
# judgment that failed outright, or one asserting CREATE_NEW, leaves the cap
# with no ref to check -- which is how a device storm came to speak once per
# device: every alert in it looked like the first thing said about a ticket
# that did not exist yet. The caller resolves the ticket deterministically
# (exact signature match) before asking, and passes it in.
# --------------------------------------------------------------------------- #


def test_a_deterministically_correlated_alert_is_capped_like_any_other() -> None:
    """The judgment has no target; the caller resolved one anyway."""
    decision = decide_alert_delivery(
        _result(prior=SiteStatus.UNKNOWN, current=SiteStatus.UNKNOWN),
        _with_history(_spoke_about("OPS-1000", minutes_ago=3)),
        enforcement_enabled=True,
        now=NOW,
        correlated_ticket_ref="OPS-1000",
    )

    assert decision.send is False
    assert decision.reason == "fail_open_capped"


def test_a_correlated_ticket_not_yet_spoken_about_still_sends() -> None:
    """The guarantee half, unchanged: the cap shortens, it never silences."""
    decision = decide_alert_delivery(
        _result(prior=SiteStatus.UNKNOWN, current=SiteStatus.UNKNOWN),
        _with_history(_spoke_about("OPS-2000", minutes_ago=3)),
        enforcement_enabled=True,
        now=NOW,
        correlated_ticket_ref="OPS-1000",
    )

    assert decision.send is True
    assert decision.reason == "fail_open"


def test_the_judgments_own_target_still_wins_when_it_has_one() -> None:
    """The caller's ref fills a gap; it does not override a stated target."""
    decision = decide_alert_delivery(
        _on_ticket("OPS-1000"),
        _with_history(_spoke_about("OPS-1000", minutes_ago=3)),
        enforcement_enabled=True,
        now=NOW,
        correlated_ticket_ref="OPS-9999",
    )

    assert decision.send is False
    assert decision.reason == "fail_open_capped"
