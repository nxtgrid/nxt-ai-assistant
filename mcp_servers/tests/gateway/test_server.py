"""End-to-end dispatch: guard applied, then delegate to the registry."""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.catalog import ToolDenied
from gateway.scope_guard import ScopeViolation
from gateway.server import dispatch_tool_call
from gateway.session import GatewaySession

SESSION = GatewaySession(
    email="user@example.com",
    user_id="u1",
    organization_id="4",
    organization_short_name="testorg",
    grid_names=frozenset({"Alpha Site"}),
    is_staff=False,
)

TOOLS = {
    "customer": [{"name": "get_status", "visible_to_customer": True}],
    "equipment_control": [{"name": "restart_inverter", "visible_to_customer": True}],
}


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, server_name, tool_name, arguments):
        self.calls.append((server_name, tool_name, arguments))
        return {"success": True}


@pytest.mark.asyncio
async def test_guarded_arguments_reach_the_registry():
    registry = _Recorder()

    await dispatch_tool_call(
        "customer__get_status",
        {"grid_name": "alpha site", "organization_id": 99},
        SESSION,
        TOOLS,
        registry,
    )

    server_name, tool_name, arguments = registry.calls[0]
    assert (server_name, tool_name) == ("customer", "get_status")
    assert arguments["organization_id"] == 4          # caller's 99 discarded
    assert arguments["grid_name"] == "Alpha Site"     # canonicalised
    assert arguments["user_email"] == "user@example.com"


@pytest.mark.asyncio
async def test_tier_3_tool_is_refused_before_the_registry():
    registry = _Recorder()

    with pytest.raises(ToolDenied):
        await dispatch_tool_call(
            "equipment_control__restart_inverter", {}, SESSION, TOOLS, registry
        )

    assert registry.calls == []


@pytest.mark.asyncio
async def test_out_of_scope_grid_is_refused_before_the_registry():
    registry = _Recorder()

    with pytest.raises(ScopeViolation):
        await dispatch_tool_call(
            "customer__get_status", {"grid_name": "Gamma Site"}, SESSION, TOOLS, registry
        )

    assert registry.calls == []


@pytest.mark.asyncio
async def test_unknown_tool_is_refused():
    registry = _Recorder()

    with pytest.raises(ToolDenied):
        await dispatch_tool_call("customer__nope", {}, SESSION, TOOLS, registry)

    assert registry.calls == []
