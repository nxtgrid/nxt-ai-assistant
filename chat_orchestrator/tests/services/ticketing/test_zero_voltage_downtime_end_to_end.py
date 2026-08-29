"""The zero-voltage downtime signals must be reachable from a real VRM reading.

``_all_phases_zero`` and ``all_phases_zero_for_override`` both require all three
phase voltages to equal exactly ``0.0``. Their unit tests construct
``AlertTelemetry`` directly and so have always passed -- but nothing upstream
could ever produce that input: ``VRMPlatform._extract_widget_value`` read
``rawValue or formattedValue``, so a phase at 0 V fell through to VRM's
unit-suffixed ``"0.00 V"``, ``float()`` raised, and the voltage arrived as
``None``. Both zero-specific paths were dead code in production.

This walks the whole seam -- VRM Status-widget records, through the platform
adapter, into ``AlertTelemetry``, into the two policies -- so the gap cannot
reopen silently at the extraction end.

``is_producing`` is asserted too: it was *not* affected by the bug (an empty
voltage list already classifies as not producing), so the fix must leave the
OFF classification exactly where it was.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from mcp_servers.servers.equipment_diagnostics_server.platforms.vrm_platform import VRMPlatform
from orchestrator.services.ticketing.alert_delivery_policy import all_phases_zero_for_override
from orchestrator.services.ticketing.alert_judgment_context import AlertTelemetry
from orchestrator.services.ticketing.downtime_alert_policy import assess_downtime
from shared.grid_status import GridStatus, SiteStatus


def _voltage_field(code: str, formatted: str, raw=...) -> dict:
    """One VRM Status-widget phase-voltage record.

    ``raw=...`` omits ``rawValue`` entirely, which is the case that used to
    reach ``float("0.00 V")``.
    """
    field = {"code": code, "description": code, "formattedValue": formatted, "secondsAgo": 30}
    if raw is not ...:
        field["rawValue"] = raw
    return field


class _StubVRM(VRMPlatform):
    """VRMPlatform serving canned Status-widget records, no network, no DB."""

    def __init__(self, status_records: dict):
        super().__init__(token="test-token", user_id="test-user")
        self._status_records = status_records

    async def _get_widget_data(self, site_id, widget, instance=None):
        return {"records": self._status_records}

    async def _get_output_consumption(self, site_id):
        return None


def _telemetry_from_vrm(status_records: dict) -> AlertTelemetry:
    """Mirror how ``client_grid_status.get_live_telemetry`` maps a VRM reading.

    Its ``fresh_voltage`` closure (mcp_servers/servers/customer_server/
    client_grid_status.py) is ``float(value) if value is not None else None``
    -- the mapping is faithful to a 0.0, so whether these policies can ever
    see zero is decided entirely by what the platform adapter returns.
    """
    reading = asyncio.run(_StubVRM(status_records).get_current_inverter_voltage("site-1"))

    # secondsAgo=30 above puts the gateway report well inside the 30-minute
    # staleness window, so this reading is fresh.
    assert reading.data_timestamp is not None
    assert datetime.utcnow() - reading.data_timestamp < timedelta(minutes=30)

    return AlertTelemetry(
        unavailable_reason="",
        generation_management="managed",
        grid_status=GridStatus.UNKNOWN,
        site_status=SiteStatus.ON if reading.is_producing else SiteStatus.OFF,
        output_kw=reading.total_power_kw,
        l1_voltage_v=reading.l1_voltage_v,
        l2_voltage_v=reading.l2_voltage_v,
        l3_voltage_v=reading.l3_voltage_v,
        observed_at=reading.data_timestamp.replace(tzinfo=timezone.utc).isoformat(),
        fresh=True,
    )


_ALL_PHASES_ZERO = {
    "data": {
        "12": _voltage_field("OV1", "0.00 V"),
        "13": _voltage_field("OV2", "0.00 V"),
        "14": _voltage_field("OV3", "0.00 V"),
    }
}

_ALL_PHASES_LIVE = {
    "data": {
        "12": _voltage_field("OV1", "230.10 V", raw=230.1),
        "13": _voltage_field("OV2", "229.80 V", raw=229.8),
        "14": _voltage_field("OV3", "231.00 V", raw=231.0),
    }
}


class TestZeroVoltageReachesTheDowntimePolicies:
    def test_all_three_zero_phases_arrive_as_zero_not_none(self):
        telemetry = _telemetry_from_vrm(_ALL_PHASES_ZERO)

        assert telemetry.l1_voltage_v == 0.0
        assert telemetry.l2_voltage_v == 0.0
        assert telemetry.l3_voltage_v == 0.0

    def test_zero_output_voltage_is_reported_as_downtime(self):
        state = assess_downtime(_telemetry_from_vrm(_ALL_PHASES_ZERO))

        assert state.known is True
        assert state.down is True
        assert "zero_output_voltage" in state.reasons

    def test_all_phase_zero_reminder_override_fires(self):
        assert all_phases_zero_for_override(_telemetry_from_vrm(_ALL_PHASES_ZERO)) is True

    def test_zero_phases_still_classify_as_not_producing(self):
        """Unchanged by the fix -- OFF classification never depended on it."""
        reading = asyncio.run(_StubVRM(_ALL_PHASES_ZERO).get_current_inverter_voltage("site-1"))

        assert reading.is_producing is False


class TestLiveVoltageIsUnaffected:
    def test_live_phases_are_not_downtime(self):
        state = assess_downtime(_telemetry_from_vrm(_ALL_PHASES_LIVE))

        assert state.down is False

    def test_live_phases_do_not_force_the_reminder(self):
        assert all_phases_zero_for_override(_telemetry_from_vrm(_ALL_PHASES_LIVE)) is False
