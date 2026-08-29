"""Downtime-aware delivery floor: a down grid is never silent for a whole day.

The correlation ladder deliberately silences a re-fire of an alert already
recorded on an open ticket. That is right for equipment noise and wrong for
downtime: a grid whose inverter is in fault (or whose phases read 0 V) must
still surface in its Telegram topic when it goes down, and again every day it
stays down, however stale the ticket absorbing the alert happens to be.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.services.ticketing.alert_judgment_context import AlertTelemetry
from orchestrator.services.ticketing.downtime_alert_policy import (
    DOWNTIME_ALERT_INTERVAL,
    DowntimeState,
    assess_downtime,
    decide_downtime_override,
)
from shared.grid_status import GridStatus, SiteStatus

NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def _telemetry(**overrides) -> AlertTelemetry:
    base = {
        "generation_management": "managed",
        "grid_status": GridStatus.FS_ON,
        "site_status": SiteStatus.ON,
        "output_kw": 12.0,
        "l1_voltage_v": 230.0,
        "l2_voltage_v": 231.0,
        "l3_voltage_v": 229.0,
        "fresh": True,
    }
    base.update(overrides)
    return AlertTelemetry(**base)


class TestAssessDowntime:
    def test_stale_telemetry_is_not_a_downtime_verdict(self):
        state = assess_downtime(_telemetry(fresh=False, site_status=SiteStatus.OFF))
        assert state.known is False
        assert state.down is False

    def test_unknown_site_status_without_voltages_is_not_known(self):
        state = assess_downtime(
            _telemetry(
                site_status=SiteStatus.UNKNOWN,
                l1_voltage_v=None,
                l2_voltage_v=None,
                l3_voltage_v=None,
            )
        )
        assert state.known is False

    def test_inverter_fault_is_downtime(self):
        state = assess_downtime(_telemetry(site_status=SiteStatus.OFF, output_kw=0.0))
        assert state.known is True
        assert state.down is True
        assert "inverter_fault" in state.reasons

    def test_all_phase_voltages_zero_is_downtime(self):
        state = assess_downtime(
            _telemetry(l1_voltage_v=0.0, l2_voltage_v=0.0, l3_voltage_v=0.0)
        )
        assert state.down is True
        assert "zero_output_voltage" in state.reasons

    def test_zero_voltage_counts_even_when_status_still_reads_on(self):
        """"inverter is in fault and/or voltage output is 0" -- either alone."""
        state = assess_downtime(
            _telemetry(
                site_status=SiteStatus.ON,
                l1_voltage_v=0.0,
                l2_voltage_v=0.0,
                l3_voltage_v=0.0,
            )
        )
        assert state.down is True

    def test_healthy_grid_is_not_downtime(self):
        state = assess_downtime(_telemetry())
        assert state.known is True
        assert state.down is False

    def test_zero_output_kw_alone_is_not_downtime(self):
        """Night on a solar site: no PV output, but the grid is still up."""
        state = assess_downtime(_telemetry(output_kw=0.0))
        assert state.down is False

    def test_isolated_site_is_not_downtime(self):
        state = assess_downtime(_telemetry(site_status=SiteStatus.ISOLATED))
        assert state.down is False


class TestDecideDowntimeOverride:
    def test_newly_down_with_no_prior_alert_sends(self):
        decision = decide_downtime_override(
            DowntimeState(down=True, known=True, reasons=("inverter_fault",)),
            last_downtime_alert_at=None,
            now=NOW,
        )
        assert decision.send is True
        assert decision.reason == "newly_down"

    def test_still_down_after_a_full_day_sends_again(self):
        decision = decide_downtime_override(
            DowntimeState(down=True, known=True, reasons=("inverter_fault",)),
            last_downtime_alert_at=NOW - DOWNTIME_ALERT_INTERVAL - timedelta(minutes=1),
            now=NOW,
        )
        assert decision.send is True
        assert decision.reason == "still_down_daily_reminder"

    def test_second_downtime_alert_the_same_day_is_held(self):
        decision = decide_downtime_override(
            DowntimeState(down=True, known=True, reasons=("inverter_fault",)),
            last_downtime_alert_at=NOW - timedelta(hours=3),
            now=NOW,
        )
        assert decision.send is False
        assert decision.reason == "already_alerted_today"

    def test_healthy_grid_never_overrides(self):
        decision = decide_downtime_override(
            DowntimeState(down=False, known=True, reasons=()),
            last_downtime_alert_at=None,
            now=NOW,
        )
        assert decision.send is False
        assert decision.reason == "not_down"

    def test_unknown_state_never_overrides(self):
        decision = decide_downtime_override(
            DowntimeState(down=False, known=False, reasons=()),
            last_downtime_alert_at=None,
            now=NOW,
        )
        assert decision.send is False
        assert decision.reason == "downtime_unknown"

    def test_unreadable_prior_timestamp_fails_open(self):
        decision = decide_downtime_override(
            DowntimeState(down=True, known=True, reasons=("inverter_fault",)),
            last_downtime_alert_at="not-a-timestamp",
            now=NOW,
        )
        assert decision.send is True
        assert decision.reason == "downtime_timestamp_invalid"

    def test_naive_prior_timestamp_is_read_as_utc(self):
        decision = decide_downtime_override(
            DowntimeState(down=True, known=True, reasons=("inverter_fault",)),
            last_downtime_alert_at="2026-08-28T09:00:00",
            now=NOW,
        )
        assert decision.send is False
        assert decision.reason == "already_alerted_today"


def test_a_plant_that_stopped_reporting_is_down_not_unknowable() -> None:
    """VRM served a reading whose gateway timestamp is over 30 minutes old.

    That is not absence of evidence -- it is evidence: the plant is dark. It
    used to land in the same UNKNOWN bucket as "we could not reach VRM at all",
    so the floor stayed silent and every individual device alert was force-sent
    instead. One comms-down message a day is the honest replacement.
    """
    state = assess_downtime(
        AlertTelemetry(
            generation_management="managed",
            grid_status="unknown",
            site_status="unknown",
            unavailable_reason="stale",
            fresh=False,
        )
    )

    assert state.known is True
    assert state.down is True
    assert state.reasons == ("plant_comms_down",)


@pytest.mark.parametrize(
    "reason", ["fetch_failed", "management_unknown", "no_vrm_site", "grid_not_found"]
)
def test_other_unavailable_reasons_remain_unknowable(reason: str) -> None:
    """Only "stale" carries evidence. Everything else is a gap in our own
    knowledge -- we cannot assert the plant is down, so the floor stays out of
    it and the fail-open gate keeps its say."""
    state = assess_downtime(
        AlertTelemetry(
            generation_management="managed",
            grid_status="unknown",
            site_status="unknown",
            unavailable_reason=reason,
            fresh=False,
        )
    )

    assert state.known is False
    assert state.down is False


def test_unmanaged_is_never_down() -> None:
    """We do not manage this plant, so we are in no position to call it dark."""
    state = assess_downtime(
        AlertTelemetry(
            generation_management="unmanaged",
            grid_status="unknown",
            site_status="unknown",
            unavailable_reason="unmanaged",
            fresh=False,
        )
    )

    assert state.known is False
    assert state.down is False
