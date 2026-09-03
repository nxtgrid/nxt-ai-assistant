"""Tool visibility, and the same gate enforced at call time."""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.catalog import ToolDenied, assert_tool_callable, is_tool_exposed
from gateway.session import GatewaySession

CUSTOMER = GatewaySession(
    email="c@example.com",
    user_id="u1",
    organization_id="4",
    organization_short_name="testorg",
    grid_names=frozenset({"Alpha Site"}),
    is_staff=False,
)
STAFF = GatewaySession(
    email="s@example.com",
    user_id="u2",
    organization_id="1",
    organization_short_name="staff",
    grid_names=frozenset({"Alpha Site"}),
    is_staff=True,
)

PUBLIC = {"name": "get_status", "visible_to_customer": True}
STAFF_ONLY = {"name": "get_internals", "visible_to_customer": False}
INTERNAL = {"name": "sync_cache", "visible_to_customer": True, "internal_only": True}
PERSISTENT = {"name": "watch_loop", "visible_to_customer": True, "persistent_only": True}


def test_customer_sees_only_customer_visible_tools():
    assert is_tool_exposed("customer", PUBLIC, CUSTOMER) is True
    assert is_tool_exposed("customer", STAFF_ONLY, CUSTOMER) is False


def test_staff_sees_staff_only_tools():
    assert is_tool_exposed("customer", STAFF_ONLY, STAFF) is True


def test_internal_and_persistent_tools_are_never_exposed():
    assert is_tool_exposed("customer", INTERNAL, STAFF) is False
    assert is_tool_exposed("customer", PERSISTENT, STAFF) is False


def test_tier_3_server_tools_are_never_exposed():
    assert is_tool_exposed("equipment_control", PUBLIC, STAFF) is False


def test_call_time_rejects_what_listing_hid():
    # The whole point: internal_only stays callable through server_registry,
    # so the gate must be re-checked here, not only at list time.
    with pytest.raises(ToolDenied):
        assert_tool_callable("customer", INTERNAL, STAFF)

    with pytest.raises(ToolDenied):
        assert_tool_callable("equipment_control", PUBLIC, STAFF)

    with pytest.raises(ToolDenied):
        assert_tool_callable("customer", STAFF_ONLY, CUSTOMER)


def test_call_time_allows_an_exposed_tool():
    assert_tool_callable("customer", PUBLIC, CUSTOMER)


def test_disabled_server_is_not_exposed(monkeypatch):
    monkeypatch.setenv("CUSTOMER_ENABLED", "false")
    assert is_tool_exposed("customer", PUBLIC, STAFF) is False


def test_disabled_tool_is_not_exposed(monkeypatch):
    # ActionFlags caches the parsed JSON but keys the cache on the raw env
    # string, so changing it here invalidates the cache automatically.
    monkeypatch.setenv("MCP_DISABLED_TOOLS", '["customer:get_status"]')
    assert is_tool_exposed("customer", PUBLIC, STAFF) is False
