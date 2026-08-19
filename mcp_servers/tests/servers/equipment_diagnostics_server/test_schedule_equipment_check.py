"""schedule_equipment_check's delay_minutes default must match what its tool
description promises.

The description says: 12 min for check_type='site_online' (the check that
verifies a comms-chain restart's ~10 min reconnect window has closed), 5 min
otherwise. Before this test existed, the code had a single flat default of 5
regardless of check_type — see docs/superpowers/plans/2026-08-19-mcp-tool-description-audit.md
section 1.3.
"""

import asyncio
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
_MCP_ROOT = os.path.join(_REPO_ROOT, "mcp_servers")
for _p in (_MCP_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.equipment_diagnostics_server.equipment_diagnostics_mcp_server import (  # noqa: E402
    _handle_schedule_equipment_check,
)


class _FakePlatform:
    """Just enough of VRMPlatform for _handle_schedule_equipment_check."""

    async def get_site_id_for_grid(self, grid_name):
        return ("site-123", True)


def _run(arguments):
    result = asyncio.run(_handle_schedule_equipment_check(_FakePlatform(), arguments))
    return json.loads(result[0].text)


class TestScheduleEquipmentCheckDefaultDelay:
    def test_site_online_defaults_to_twelve_minutes(self):
        body = _run({"grid_name": "Kudi", "check_type": "site_online"})
        assert body["delay_minutes"] == 12

    @pytest.mark.parametrize(
        "check_type", ["full_status", "grid_consumption", "battery_status"]
    )
    def test_other_check_types_default_to_five_minutes(self, check_type):
        body = _run({"grid_name": "Kudi", "check_type": check_type})
        assert body["delay_minutes"] == 5

    def test_omitted_check_type_defaults_to_five_minutes(self):
        """check_type itself defaults to 'full_status', which is not the
        site_online long-delay case."""
        body = _run({"grid_name": "Kudi"})
        assert body["delay_minutes"] == 5

    def test_explicit_delay_minutes_always_wins(self):
        """An explicit delay_minutes must override the check_type default in
        both directions — this isn't just a fallback for a falsy value."""
        body = _run({"grid_name": "Kudi", "check_type": "site_online", "delay_minutes": 1})
        assert body["delay_minutes"] == 1

        body = _run({"grid_name": "Kudi", "check_type": "full_status", "delay_minutes": 30})
        assert body["delay_minutes"] == 30

    def test_explicit_zero_delay_minutes_is_respected(self):
        """0 is falsy but a legitimate explicit value (check immediately) —
        must not be treated as 'omitted' and replaced by a check_type default."""
        body = _run({"grid_name": "Kudi", "check_type": "site_online", "delay_minutes": 0})
        assert body["delay_minutes"] == 0
