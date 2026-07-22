"""Behavior of the declarative ToolRegistry shared by MCP servers.

These pin the contract every migrated server relies on: schema/name mismatches
fail at registration, list/call results are correctly shaped, gating hides and
optionally refuses tools, and unknown tools get one consistent JSON envelope
instead of the three different styles servers used to hand-roll.
"""

import os
import sys

import pytest

_MCP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
_REPO_ROOT = os.path.abspath(os.path.join(_MCP_ROOT, ".."))
for _p in (_MCP_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mcp.types import TextContent  # noqa: E402
from shared_code.tool_registry import ToolRegistry  # noqa: E402

SCHEMA = {
    "name": "echo",
    "description": "Echo the input back.",
    "inputSchema": {"type": "object", "properties": {}},
    "visible_to_customer": False,
}


def _registry_with_echo(**tool_kwargs):
    registry = ToolRegistry("demo")

    @registry.tool("echo", SCHEMA, **tool_kwargs)
    async def _echo(arguments):
        return [TextContent(type="text", text="echoed")]

    return registry


class TestRegistration:
    def test_name_schema_mismatch_raises_at_registration(self):
        registry = ToolRegistry("demo")
        with pytest.raises(ValueError, match="does not match"):

            @registry.tool("echo", {**SCHEMA, "name": "not_echo"})
            async def _handler(arguments):
                return []

    def test_duplicate_registration_raises(self):
        registry = ToolRegistry("demo")

        @registry.tool("echo", SCHEMA)
        async def _handler(arguments):
            return []

        with pytest.raises(ValueError, match="registered twice"):

            @registry.tool("echo", SCHEMA)
            async def _handler2(arguments):
                return []


class TestListTools:
    @pytest.mark.asyncio
    async def test_lists_registered_tool(self):
        registry = _registry_with_echo()
        tools = await registry.handle_list_tools()
        assert [t.name for t in tools] == ["echo"]
        assert tools[0].visible_to_customer is False

    @pytest.mark.asyncio
    async def test_returns_fresh_objects_per_call(self):
        registry = _registry_with_echo()
        first = await registry.handle_list_tools()
        second = await registry.handle_list_tools()
        assert all(a is not b for a, b in zip(first, second))

    @pytest.mark.asyncio
    async def test_gated_tool_hidden_when_actions_disabled(self, monkeypatch):
        monkeypatch.setenv("DEMO_ACTIONS_ENABLED", "false")
        registry = _registry_with_echo(gated=True)
        tools = await registry.handle_list_tools()
        assert tools == []

    @pytest.mark.asyncio
    async def test_gated_tool_listed_when_actions_enabled(self, monkeypatch):
        monkeypatch.setenv("DEMO_ACTIONS_ENABLED", "true")
        registry = _registry_with_echo(gated=True)
        tools = await registry.handle_list_tools()
        assert [t.name for t in tools] == ["echo"]


class TestCallTool:
    @pytest.mark.asyncio
    async def test_calls_registered_handler(self):
        registry = _registry_with_echo()
        result = await registry.handle_call_tool("echo", {})
        assert result[0].text == "echoed"

    @pytest.mark.asyncio
    async def test_resolves_server_prefixed_name(self):
        registry = _registry_with_echo()
        result = await registry.handle_call_tool("demo_echo", {})
        assert result[0].text == "echoed"

    @pytest.mark.asyncio
    async def test_resolves_alias(self):
        registry = _registry_with_echo(aliases=("say",))
        result = await registry.handle_call_tool("say", {})
        assert result[0].text == "echoed"

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_one_json_envelope(self):
        registry = _registry_with_echo()
        result = await registry.handle_call_tool("does_not_exist", {})
        assert len(result) == 1
        assert result[0].type == "text"
        assert '"success": false' in result[0].text
        assert "Unknown tool: does_not_exist" in result[0].text

    @pytest.mark.asyncio
    async def test_gated_tool_refused_when_disabled_by_default(self, monkeypatch):
        monkeypatch.setenv("DEMO_ACTIONS_ENABLED", "false")
        registry = _registry_with_echo(gated=True)
        result = await registry.handle_call_tool("echo", {})
        assert '"success": false' in result[0].text
        assert "DEMO_ACTIONS_ENABLED" in result[0].text

    @pytest.mark.asyncio
    async def test_gated_tool_not_refused_when_refuse_when_disabled_is_false(self, monkeypatch):
        """jira's handlers already carry their own disabled-state check with
        their own message — the registry must not add a second, generic one."""
        monkeypatch.setenv("DEMO_ACTIONS_ENABLED", "false")
        registry = _registry_with_echo(gated=True, refuse_when_disabled=False)
        result = await registry.handle_call_tool("echo", {})
        assert result[0].text == "echoed"

    @pytest.mark.asyncio
    async def test_handler_exception_becomes_compose_error_response(self):
        registry = ToolRegistry("demo")

        @registry.tool("boom", {**SCHEMA, "name": "boom"})
        async def _boom(arguments):
            raise ValueError("kaboom")

        result = await registry.handle_call_tool("boom", {})
        assert len(result) == 1
        assert result[0].text == "Error: kaboom"

    @pytest.mark.asyncio
    async def test_pre_dispatch_hook_short_circuits(self):
        registry = _registry_with_echo()

        @registry.pre_dispatch
        async def _gate(name, arguments):
            return [TextContent(type="text", text="server disabled")]

        result = await registry.handle_call_tool("echo", {})
        assert result[0].text == "server disabled"

    @pytest.mark.asyncio
    async def test_pre_dispatch_hook_passthrough_when_none(self):
        registry = _registry_with_echo()

        @registry.pre_dispatch
        async def _gate(name, arguments):
            return None

        result = await registry.handle_call_tool("echo", {})
        assert result[0].text == "echoed"
