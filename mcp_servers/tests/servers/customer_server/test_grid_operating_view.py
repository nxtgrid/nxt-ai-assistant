"""``build_grid_operating_view`` -- the single seam where ``get_grid_status``
(and, through it, ``meter_information``) turns raw telemetry into the grid
status a chat model is allowed to see.

The contract has two halves and both trace to a real incident where a live
14.5 kW grid was reported to a customer as "not active (HPS is off)":

1. the ``service`` word is the same processed ``classify_grid_status`` verdict
   ``/grids`` renders -- FS / HPS / Isolated / Off / Unknown -- never a
   Timescale-only guess, and never "Down";
2. the raw ``is_hps_on`` / ``is_fs_active`` snapshot booleans are stripped out,
   so the only status signal left in the payload is the processed one.
"""

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.customer_server.client_grid_status import build_grid_operating_view  # noqa: E402


def test_live_grid_with_lagging_snapshot_flags_reports_hps_not_off():
    """The incident case: VRM says the inverter is producing 14.5 kW, but the
    15-minute snapshot booleans have not caught up and both read false. The
    processed verdict must follow the live reading."""
    service, latest_state = build_grid_operating_view(
        vrm_is_on=True,
        vrm_power_kw=14.526,
        hps_threshold_kw=5.0,
        ts_hps_on=False,
        ts_fs_on=False,
        latest_state={"is_hps_on": False, "is_fs_active": False},
    )

    assert service["service"] == "HPS"
    assert service["status_code"] == "hps_on"


def test_vrm_reporting_no_voltage_is_off():
    service, _ = build_grid_operating_view(
        vrm_is_on=False,
        vrm_power_kw=0.0,
        hps_threshold_kw=5.0,
        ts_hps_on=None,
        ts_fs_on=None,
        latest_state=None,
    )

    assert service["service"] == "Off"
    assert service["status_code"] == "off"


def test_missing_vrm_reading_is_unknown_even_when_snapshot_flags_say_on():
    """``/grids`` shows Unknown when there is no fresh VRM ON/OFF reading. The
    single-grid path must not fall back to a Timescale-only "HPS"/"FS" guess --
    that is exactly the stale signal the incident turned on."""
    service, _ = build_grid_operating_view(
        vrm_is_on=None,
        vrm_power_kw=None,
        hps_threshold_kw=5.0,
        ts_hps_on=True,
        ts_fs_on=True,
        latest_state=None,
    )

    assert service["service"] == "Unknown"
    assert service["status_code"] == "unknown"


def test_fs_precedes_hps_when_power_is_unknown_and_both_flags_are_on():
    service, _ = build_grid_operating_view(
        vrm_is_on=True,
        vrm_power_kw=None,
        hps_threshold_kw=None,
        ts_hps_on=True,
        ts_fs_on=True,
        latest_state=None,
    )

    assert service["service"] == "FS"


def test_producing_below_hps_threshold_is_isolated():
    service, _ = build_grid_operating_view(
        vrm_is_on=True,
        vrm_power_kw=1.0,
        hps_threshold_kw=5.0,
        ts_hps_on=True,
        ts_fs_on=True,
        latest_state=None,
    )

    assert service["service"] == "Isolated"
    assert service["status_code"] == "likely_isolated"


def test_latest_state_is_returned_without_the_raw_service_flags():
    _, latest_state = build_grid_operating_view(
        vrm_is_on=True,
        vrm_power_kw=14.5,
        hps_threshold_kw=5.0,
        ts_hps_on=False,
        ts_fs_on=False,
        latest_state={
            "is_hps_on": False,
            "is_fs_active": False,
            "should_fs_be_on": True,
            "battery_soc_pct": 60.0,
            "data_source": "vrm",
        },
    )

    assert "is_hps_on" not in latest_state
    assert "is_fs_active" not in latest_state
    # Everything else survives untouched.
    assert latest_state == {
        "should_fs_be_on": True,
        "battery_soc_pct": 60.0,
        "data_source": "vrm",
    }


def test_absent_latest_state_stays_absent():
    service, latest_state = build_grid_operating_view(
        vrm_is_on=True,
        vrm_power_kw=14.5,
        hps_threshold_kw=5.0,
        ts_hps_on=True,
        ts_fs_on=False,
        latest_state=None,
    )

    assert latest_state is None
    assert service["service"] == "HPS"
