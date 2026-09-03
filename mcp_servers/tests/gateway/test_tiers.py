"""Server tiering.

Tier 3 is denied because those servers are side-effecting AND carry no
organization_id handling of their own, so Class A injection enforces nothing
for them.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from gateway.tiers import TIER_1, TIER_2, TIER_3_DENIED, is_server_allowed


def test_org_aware_servers_are_tier_1():
    assert "customer" in TIER_1
    assert "meters" in TIER_1
    assert is_server_allowed("customer") is True


def test_grid_shaped_servers_are_tier_2():
    assert "vrm" in TIER_2
    assert "grafana" in TIER_2
    assert is_server_allowed("vrm") is True


def test_side_effecting_unscoped_servers_are_denied():
    assert "equipment_control" in TIER_3_DENIED
    assert "payment_processor" in TIER_3_DENIED
    assert "messaging" in TIER_3_DENIED
    assert is_server_allowed("equipment_control") is False


def test_unknown_server_is_denied():
    assert is_server_allowed("some_new_server") is False


def test_tiers_do_not_overlap():
    assert not (TIER_1 & TIER_2)
    assert not (TIER_1 & TIER_3_DENIED)
    assert not (TIER_2 & TIER_3_DENIED)
