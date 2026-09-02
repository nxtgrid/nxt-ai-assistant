"""``get_equipment_status`` must not present a stale reading as current.

Two defects, both from the same cause -- the tool trusted VRM's diagnostics
values without checking gateway liveness:

1. ``timestamp`` was ``datetime.utcnow()`` (when the tool ran), so every
   response looked freshly observed even when the gateway had been silent for
   hours.
2. ``inverter`` / ``grid`` / ``pv`` / ``battery`` power values were passed
   through verbatim. VRM keeps serving the last sample after a gateway goes
   dark, so an offline site reported a frozen "7.1 kW" like ``/grids`` did.

The fix gates the live numeric readings on ``is_site_online`` (Gateway
``lastConnection`` within 15 min) and reports ``gateway_last_seen`` as the
recency signal. Alarms and identity fields still come through.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
_MCP_ROOT = os.path.join(_REPO_ROOT, "mcp_servers")
for _p in (_MCP_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.equipment_diagnostics_server import (  # noqa: E402
    equipment_diagnostics_mcp_server as mod,
)
from servers.equipment_diagnostics_server.platforms.base_platform import (  # noqa: E402
    Alarm,
    BatteryStatus,
    EquipmentStatus,
    GridStatus,
    PowerReading,
)

_STAFF_ORG_ID = mod.STAFF_ORG_ID


def _status(*, is_online: bool, last_seen: datetime) -> EquipmentStatus:
    now = datetime.now(timezone.utc)
    return EquipmentStatus(
        grid_name="",
        site_id="site-xyz",
        timestamp=now,
        is_online=is_online,
        gateway_last_seen=last_seen,
        inverter=PowerReading(
            timestamp=now, l1_power_w=2400.0, l2_power_w=2300.0, l3_power_w=2400.0, total_power_w=7100.0
        ),
        battery=BatteryStatus(
            timestamp=now, soc_percent=64.0, voltage_v=52.1, current_a=-3.2, power_w=-167.0, charging=True
        ),
        grid=GridStatus(
            timestamp=now, connected=True, l1_power_w=1000.0, l2_power_w=900.0, l3_power_w=1100.0, total_power_w=3000.0
        ),
        pv=PowerReading(timestamp=now, total_power_w=4200.0),
        alarms=[Alarm(code="#8", description="High DC ripple", device="Inverter", severity="alarm")],
    )


class _FakePlatform:
    def __init__(self, status: EquipmentStatus):
        self._status = status

    async def get_site_id_for_grid(self, grid_name):
        return ("site-xyz", True)

    async def get_equipment_status(self, site_id, metrics=None):
        self._status.site_id = site_id
        return self._status


def _run(status: EquipmentStatus, monkeypatch) -> dict:
    monkeypatch.setattr(mod, "_get_grid_timezone", _fake_tz)
    out = asyncio.run(
        mod._handle_get_equipment_status(
            _FakePlatform(status),
            {"grid_name": "Testville", "organization_id": _STAFF_ORG_ID},
        )
    )
    return json.loads(out[0].text)


async def _fake_tz(grid_name: str) -> str:
    return "UTC"


def test_offline_gateway_nulls_the_live_power_readings(monkeypatch):
    last_seen = datetime.now(timezone.utc) - timedelta(hours=2)
    result = _run(_status(is_online=False, last_seen=last_seen), monkeypatch)

    assert result["data_stale"] is True
    assert result["stale_reason"] == "gateway_offline"
    assert result["inverter"]["total_power_w"] is None
    assert result["inverter"]["l1_power_w"] is None
    assert result["grid"]["total_power_w"] is None
    assert result["pv"]["total_power_w"] is None
    assert result["battery"]["soc_percent"] is None
    assert result["battery"]["power_w"] is None


def test_offline_gateway_reports_last_seen_not_the_fetch_time(monkeypatch):
    last_seen = datetime.now(timezone.utc) - timedelta(hours=2)
    result = _run(_status(is_online=False, last_seen=last_seen), monkeypatch)

    assert result["gateway_last_seen"] == last_seen.isoformat()
    # The ambiguous "timestamp" field, if still emitted, is data recency -- not "now".
    assert result["timestamp"] == last_seen.isoformat()


def test_alarms_and_identity_survive_an_offline_gateway(monkeypatch):
    last_seen = datetime.now(timezone.utc) - timedelta(hours=2)
    result = _run(_status(is_online=False, last_seen=last_seen), monkeypatch)

    assert result["grid_name"] == "Testville"
    assert result["site_id"] == "site-xyz"
    assert [a["code"] for a in result["alarms"]] == ["#8"]


def test_online_gateway_passes_live_readings_through(monkeypatch):
    last_seen = datetime.now(timezone.utc) - timedelta(minutes=1)
    result = _run(_status(is_online=True, last_seen=last_seen), monkeypatch)

    assert result.get("data_stale") in (None, False)
    assert result["inverter"]["total_power_w"] == 7100.0
    assert result["battery"]["soc_percent"] == 64.0
    assert result["gateway_last_seen"] == last_seen.isoformat()


def test_platform_derives_online_and_last_seen_from_one_gateway_lookup():
    """``get_equipment_status`` should populate ``gateway_last_seen`` and derive
    ``is_online`` from it (within 15 min), using a single gateway lookup."""

    class _VRM(mod.VRMPlatform):
        def __init__(self, last_conn):
            super().__init__(token="t", user_id="u")
            self._last_conn = last_conn
            self.calls = 0

        async def _gateway_last_connection(self, site_id):
            self.calls += 1
            return self._last_conn

        async def get_current_inverter_power(self, site_id):
            return PowerReading(timestamp=datetime.now(timezone.utc), total_power_w=5000.0)

        async def get_current_battery_status(self, site_id):
            return BatteryStatus(timestamp=datetime.now(timezone.utc), soc_percent=50.0)

        async def get_current_grid_status(self, site_id):
            return GridStatus(timestamp=datetime.now(timezone.utc), connected=True)

        async def get_current_pv_power(self, site_id):
            return PowerReading(timestamp=datetime.now(timezone.utc), total_power_w=0.0)

        async def get_active_alarms(self, site_id):
            return []

    recent = datetime.now(timezone.utc) - timedelta(minutes=3)
    vrm = _VRM(recent)
    status = asyncio.run(vrm.get_equipment_status("site-1"))
    assert status.gateway_last_seen == recent
    assert status.is_online is True
    assert vrm.calls == 1

    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    status2 = asyncio.run(_VRM(stale).get_equipment_status("site-1"))
    assert status2.gateway_last_seen == stale
    assert status2.is_online is False
