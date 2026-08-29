"""A VRM Status-widget reading of exactly zero must read as zero, not "no data".

``_extract_widget_value`` used ``rawValue or formattedValue``. ``rawValue`` of
``0.0`` is falsy, so a genuine zero fell through to ``formattedValue`` -- which
VRM returns unit-suffixed ("0 W", "0.00 V") -- and ``float("0 W")`` raised,
leaving the caller with ``None``. Every zero reading was therefore reported as
missing data.

The per-phase lookups had the same shape independently
(``_extract_widget_value(records, "29") or _extract_widget_value(records, code="OP1")``):
a phase genuinely at 0 W was dropped from the reading *and* from the total.
"""

import asyncio
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
_MCP_ROOT = os.path.join(_REPO_ROOT, "mcp_servers")
for _p in (_MCP_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.equipment_diagnostics_server.platforms.vrm_platform import (  # noqa: E402
    VRMPlatform,
)


def _platform() -> VRMPlatform:
    """A VRMPlatform that never touches the network."""
    return VRMPlatform(token="test-token", user_id="test-user")


def _field(code, raw_value=..., formatted_value=None, seconds_ago=5):
    """One VRM Status-widget record. ``raw_value=...`` omits the key entirely."""
    field = {"code": code, "description": code, "secondsAgo": seconds_ago}
    if raw_value is not ...:
        field["rawValue"] = raw_value
    if formatted_value is not None:
        field["formattedValue"] = formatted_value
    return field


def _status_records(fields):
    """VRM wraps Status-widget fields in ``records.data`` keyed by field id."""
    return {"data": {fid: field for fid, field in fields.items()}}


class TestExtractWidgetValue:
    def test_raw_value_of_zero_is_zero_not_missing(self):
        records = _status_records({"29": _field("OP1", raw_value=0.0, formatted_value="0 W")})

        assert _platform()._extract_widget_value(records, "29") == 0.0

    def test_raw_value_of_zero_is_zero_when_looked_up_by_code(self):
        records = _status_records({"12": _field("OV1", raw_value=0.0, formatted_value="0.00 V")})

        assert _platform()._extract_widget_value(records, code="OV1") == 0.0

    def test_formatted_value_unit_suffix_is_stripped_when_raw_value_absent(self):
        records = _status_records({"29": _field("OP1", formatted_value="0 W")})

        assert _platform()._extract_widget_value(records, "29") == 0.0

    def test_formatted_value_unit_suffix_is_stripped_for_a_nonzero_reading(self):
        records = _status_records({"12": _field("OV1", formatted_value="230.45 V")})

        assert _platform()._extract_widget_value(records, code="OV1") == 230.45

    def test_raw_value_wins_over_formatted_value(self):
        records = _status_records({"29": _field("OP1", raw_value=42.5, formatted_value="43 W")})

        assert _platform()._extract_widget_value(records, "29") == 42.5

    def test_negative_raw_value_survives(self):
        """AC-coupled PV drives OP1 negative -- that must not read as missing."""
        records = _status_records({"29": _field("OP1", raw_value=-120.0, formatted_value="-120 W")})

        assert _platform()._extract_widget_value(records, "29") == -120.0

    def test_explicit_null_raw_value_falls_back_to_formatted_value(self):
        records = _status_records({"29": _field("OP1", raw_value=None, formatted_value="7 W")})

        assert _platform()._extract_widget_value(records, "29") == 7.0

    def test_unparseable_value_is_none(self):
        records = _status_records({"29": _field("OP1", formatted_value="N/A")})

        assert _platform()._extract_widget_value(records, "29") is None

    def test_missing_field_is_none(self):
        records = _status_records({"29": _field("OP1", raw_value=0.0)})

        assert _platform()._extract_widget_value(records, "31") is None

    def test_list_wrapped_field_of_zero_is_zero(self):
        """VRM sometimes wraps a field's record in a single-element list."""
        records = {"data": {"29": [_field("OP1", raw_value=0.0, formatted_value="0 W")]}}

        assert _platform()._extract_widget_value(records, "29") == 0.0


class _FakeVRM(VRMPlatform):
    """VRMPlatform with the two network calls replaced by canned records."""

    def __init__(self, status_records, consumption=None):
        super().__init__(token="test-token", user_id="test-user")
        self._status_records = status_records
        self._consumption = consumption

    async def _get_widget_data(self, site_id, widget, instance=None):
        return {"records": self._status_records}

    async def _get_output_consumption(self, site_id):
        return self._consumption


_ZERO_PHASE_STATUS = _status_records(
    {
        "29": _field("OP1", raw_value=0.0, formatted_value="0 W"),
        "30": _field("OP2", raw_value=0.0, formatted_value="0 W"),
        "31": _field("OP3", raw_value=0.0, formatted_value="0 W"),
        "12": _field("OV1", raw_value=0.0, formatted_value="0.00 V"),
        "13": _field("OV2", raw_value=0.0, formatted_value="0.00 V"),
        "14": _field("OV3", raw_value=0.0, formatted_value="0.00 V"),
    }
)


class TestPerPhaseFallback:
    def test_zero_phase_power_is_reported_not_dropped(self):
        """The ``field_id or code`` fallback dropped a phase genuinely at 0 W."""
        reading = asyncio.run(_FakeVRM(_ZERO_PHASE_STATUS).get_current_inverter_power("site-1"))

        assert reading.l1_power_w == 0.0
        assert reading.l2_power_w == 0.0
        assert reading.l3_power_w == 0.0

    def test_code_fallback_still_runs_when_the_field_id_is_absent(self):
        records = _status_records(
            {
                "88": _field("OP1", raw_value=0.0, formatted_value="0 W"),
                "89": _field("OP2", raw_value=150.0, formatted_value="150 W"),
            }
        )

        reading = asyncio.run(_FakeVRM(records).get_current_inverter_power("site-1"))

        assert reading.l1_power_w == 0.0
        assert reading.l2_power_w == 150.0
        assert reading.l3_power_w is None

    def test_zero_phase_is_included_in_total_power_kw(self):
        records = _status_records(
            {
                "29": _field("OP1", raw_value=0.0, formatted_value="0 W"),
                "30": _field("OP2", raw_value=1000.0, formatted_value="1000 W"),
                "31": _field("OP3", raw_value=2000.0, formatted_value="2000 W"),
            }
        )

        voltage = asyncio.run(_FakeVRM(records).get_current_inverter_voltage("site-1"))

        assert voltage.l1_power_w == 0.0
        assert voltage.total_power_kw == 3.0


class TestZeroPhaseVoltages:
    def test_all_three_phase_voltages_read_zero(self):
        voltage = asyncio.run(_FakeVRM(_ZERO_PHASE_STATUS).get_current_inverter_voltage("site-1"))

        assert (voltage.l1_voltage_v, voltage.l2_voltage_v, voltage.l3_voltage_v) == (0.0, 0.0, 0.0)

    def test_zero_phase_voltages_read_zero_from_formatted_value_alone(self):
        """VRM omitting rawValue is the case ``float("0.00 V")`` used to blow up on."""
        records = _status_records(
            {
                "12": _field("OV1", formatted_value="0.00 V"),
                "13": _field("OV2", formatted_value="0.00 V"),
                "14": _field("OV3", formatted_value="0.00 V"),
            }
        )

        voltage = asyncio.run(_FakeVRM(records).get_current_inverter_voltage("site-1"))

        assert (voltage.l1_voltage_v, voltage.l2_voltage_v, voltage.l3_voltage_v) == (0.0, 0.0, 0.0)

    def test_zero_phase_voltages_are_not_producing(self):
        voltage = asyncio.run(_FakeVRM(_ZERO_PHASE_STATUS).get_current_inverter_voltage("site-1"))

        assert voltage.is_producing is False

    def test_live_phase_voltages_still_read_as_producing(self):
        records = _status_records(
            {
                "12": _field("OV1", raw_value=230.1, formatted_value="230.10 V"),
                "13": _field("OV2", raw_value=229.8, formatted_value="229.80 V"),
                "14": _field("OV3", raw_value=231.0, formatted_value="231.00 V"),
            }
        )

        voltage = asyncio.run(_FakeVRM(records).get_current_inverter_voltage("site-1"))

        assert voltage.is_producing is True
        assert voltage.l1_voltage_v == 230.1
