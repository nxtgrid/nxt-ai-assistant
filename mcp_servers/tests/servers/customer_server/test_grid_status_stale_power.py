"""``/grids`` and ``/grid`` must never surface a stale inverter power reading.

A site whose gateway has gone silent is classified ``Unknown`` from the aged-out
Timescale snapshots, but VRM keeps serving the *last* o1/o2/o3 power sample it
saw -- only the OV1 ``secondsAgo`` behind ``_inverter_voltage_is_stale`` ages
out. Both commands read ``total_power_kw`` (and, on ``/grid``, the per-phase
watts) straight off that reading, so a grid with no comms was rendered with a
live-looking kW figure sitting under the "Unknown" heading.

``inverter_power_view`` is the single seam both commands resolve the
model-visible inverter power through, so the two cannot drift: a missing,
errored, or >30-minute-old reading yields ``None`` for every field, exactly the
way ``get_live_telemetry`` already gates the alert path via
``_fresh_inverter_output_kw``.
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.customer_server.client_grid_status import (  # noqa: E402
    _inverter_voltage_is_stale,
    inverter_power_view,
)


@dataclass
class _Voltage:
    """The subset of ``InverterVoltage`` the power view reads."""

    data_timestamp: Optional[datetime] = None
    total_power_kw: Optional[float] = None
    l1_power_w: Optional[float] = None
    l2_power_w: Optional[float] = None
    l3_power_w: Optional[float] = None
    error: Optional[str] = None


_ALL_BLANK = {
    "inverter_power_kw": None,
    "inverter_l1_power_kw": None,
    "inverter_l2_power_kw": None,
    "inverter_l3_power_kw": None,
}


def _fresh(**kw) -> _Voltage:
    kw.setdefault("data_timestamp", datetime.utcnow() - timedelta(minutes=2))
    return _Voltage(**kw)


def test_stale_reading_surfaces_no_inverter_power():
    """The reported case: gateway silent for hours, VRM still holds a stale kW."""
    stale = _Voltage(
        data_timestamp=datetime.utcnow() - timedelta(hours=3),
        total_power_kw=7.1,
        l1_power_w=2400.0,
        l2_power_w=2300.0,
        l3_power_w=2400.0,
    )

    assert inverter_power_view(stale) == _ALL_BLANK


def test_missing_reading_surfaces_no_inverter_power():
    assert inverter_power_view(None) == _ALL_BLANK


def test_errored_reading_surfaces_no_inverter_power():
    errored = _Voltage(
        data_timestamp=datetime.utcnow() - timedelta(minutes=1),
        total_power_kw=7.1,
        error="vrm 503",
    )

    assert inverter_power_view(errored) == _ALL_BLANK


def test_reading_with_no_report_time_surfaces_no_inverter_power():
    """No OV1 ``secondsAgo`` -> no staleness evidence -> the reading is dropped,
    the same way ``_inverter_voltage_is_stale`` drops it."""
    no_report_time = _Voltage(data_timestamp=None, total_power_kw=7.1, l1_power_w=2400.0)

    assert inverter_power_view(no_report_time) == _ALL_BLANK


def test_exception_reading_surfaces_no_inverter_power():
    assert inverter_power_view(TimeoutError("vrm timeout")) == _ALL_BLANK


def test_fresh_reading_surfaces_total_and_per_phase_in_kw():
    reading = _fresh(
        total_power_kw=7.1,
        l1_power_w=2400.0,
        l2_power_w=2300.0,
        l3_power_w=2400.0,
    )

    assert inverter_power_view(reading) == {
        "inverter_power_kw": 7.1,
        "inverter_l1_power_kw": 2.4,
        "inverter_l2_power_kw": 2.3,
        "inverter_l3_power_kw": 2.4,
    }


def test_fresh_reading_with_no_power_value_surfaces_none_total():
    assert inverter_power_view(_fresh(total_power_kw=None)) == _ALL_BLANK


def test_fresh_reading_keeps_a_genuine_zero_phase():
    """A phase truly at 0 W is a reading, not missing data."""
    view = inverter_power_view(_fresh(total_power_kw=0.0, l1_power_w=0.0))

    assert view["inverter_power_kw"] == 0.0
    assert view["inverter_l1_power_kw"] == 0.0
    assert view["inverter_l2_power_kw"] is None


def test_boundary_just_under_thirty_minutes_is_still_fresh():
    reading = _fresh(
        data_timestamp=datetime.utcnow() - timedelta(minutes=29),
        total_power_kw=5.0,
    )

    assert inverter_power_view(reading)["inverter_power_kw"] == 5.0


def test_timezone_aware_report_time_is_compared_correctly():
    """``/grid`` used a naive ``datetime.utcnow() - voltage.data_timestamp``
    that raises on a tz-aware timestamp; the shared predicate must not."""
    aware_stale = datetime.now(timezone.utc) - timedelta(hours=2)
    aware_fresh = datetime.now(timezone.utc) - timedelta(minutes=1)

    assert _inverter_voltage_is_stale(_Voltage(data_timestamp=aware_stale)) is True
    assert _inverter_voltage_is_stale(_Voltage(data_timestamp=aware_fresh)) is False
