"""``customer_get_grid_status`` must not present a stale battery/solar/
consumption reading as current.

Same defect as ``test_grid_status_stale_power.py``, on the readings that
weren't covered by that fix: a site whose gateway has gone dark for hours is
correctly classified ``service: "Unknown"`` / ``is_stale: true`` from the
aged-out inverter voltage reading, but ``battery_soc_pct``, ``battery_current_a``,
``solar_power_w``, and ``grid_consumption_w`` were read straight off VRM with no
freshness check at all -- ``BatteryStatus``/``PowerReading``/``GridStatus``
carry no gateway report time of their own (see
``_current_battery_voltage_v``'s docstring), so nothing aged them out. A
customer asking about a grid whose gateway had been dark for 17 hours got back
a confident "currently powered, battery at 75%, discharging" sitting right
next to the (correctly computed) "Unknown"/stale flags.

``live_metrics_view`` closes this the same way ``inverter_power_view`` already
does for inverter output: reuse the same request's voltage-reading staleness
as the shared proxy for the whole gateway, since all four readings come from
one site's gateway in one request.
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.customer_server.client_grid_status import live_metrics_view  # noqa: E402


@dataclass
class _Voltage:
    data_timestamp: Optional[datetime] = None
    total_power_kw: Optional[float] = None
    error: Optional[str] = None


@dataclass
class _Battery:
    soc_percent: Optional[float] = None
    current_a: Optional[float] = None


@dataclass
class _Power:
    total_power_w: Optional[float] = None


_ALL_BLANK = {
    "battery_soc_pct": None,
    "battery_current_a": None,
    "solar_power_w": None,
    "grid_consumption_w": None,
}


def _fresh_voltage(**kw) -> _Voltage:
    kw.setdefault("data_timestamp", datetime.utcnow() - timedelta(minutes=2))
    return _Voltage(**kw)


def test_stale_gateway_surfaces_no_battery_or_power_readings():
    """The reported case: gateway dark for hours, VRM still holds a frozen SOC."""
    stale_voltage = _Voltage(data_timestamp=datetime.utcnow() - timedelta(hours=17))
    battery = _Battery(soc_percent=75.0, current_a=-23.6)
    pv = _Power(total_power_w=0.0)
    grid_status_vrm = _Power(total_power_w=1172.0)

    view = live_metrics_view(
        voltage=stale_voltage, battery=battery, pv=pv, grid_status_vrm=grid_status_vrm
    )

    assert view == _ALL_BLANK


def test_missing_voltage_surfaces_no_battery_or_power_readings():
    battery = _Battery(soc_percent=75.0, current_a=-23.6)

    view = live_metrics_view(voltage=None, battery=battery, pv=None, grid_status_vrm=None)

    assert view == _ALL_BLANK


def test_errored_voltage_surfaces_no_battery_or_power_readings():
    errored = _Voltage(data_timestamp=datetime.utcnow(), error="vrm 503")
    battery = _Battery(soc_percent=75.0, current_a=-23.6)

    view = live_metrics_view(voltage=errored, battery=battery, pv=None, grid_status_vrm=None)

    assert view == _ALL_BLANK


def test_fresh_voltage_surfaces_battery_and_power_readings():
    voltage = _fresh_voltage()
    battery = _Battery(soc_percent=75.0, current_a=-23.6)
    pv = _Power(total_power_w=0.0)
    grid_status_vrm = _Power(total_power_w=1172.0)

    view = live_metrics_view(voltage=voltage, battery=battery, pv=pv, grid_status_vrm=grid_status_vrm)

    assert view == {
        "battery_soc_pct": 75.0,
        "battery_current_a": -23.6,
        "solar_power_w": 0.0,
        "grid_consumption_w": 1172.0,
    }


def test_fresh_voltage_with_missing_battery_reading_blanks_only_battery():
    voltage = _fresh_voltage()
    pv = _Power(total_power_w=500.0)

    view = live_metrics_view(voltage=voltage, battery=None, pv=pv, grid_status_vrm=None)

    assert view["battery_soc_pct"] is None
    assert view["battery_current_a"] is None
    assert view["solar_power_w"] == 500.0
    assert view["grid_consumption_w"] is None


def test_fresh_voltage_keeps_a_genuine_zero_solar_reading():
    """PV truly producing 0 W (e.g. at night) is a reading, not missing data."""
    voltage = _fresh_voltage()
    pv = _Power(total_power_w=0.0)

    view = live_metrics_view(voltage=voltage, battery=None, pv=pv, grid_status_vrm=None)

    assert view["solar_power_w"] == 0.0


def test_exception_voltage_surfaces_no_battery_or_power_readings():
    battery = _Battery(soc_percent=75.0, current_a=-23.6)

    view = live_metrics_view(
        voltage=TimeoutError("vrm timeout"), battery=battery, pv=None, grid_status_vrm=None
    )

    assert view == _ALL_BLANK


def test_battery_reading_that_is_itself_an_exception_is_blanked():
    voltage = _fresh_voltage()

    view = live_metrics_view(
        voltage=voltage, battery=TimeoutError("vrm timeout"), pv=None, grid_status_vrm=None
    )

    assert view["battery_soc_pct"] is None
    assert view["battery_current_a"] is None
