"""Gateway dispatch.

Order matters: resolve the tool, re-check the gate, apply the scope guard, and
only then delegate. Every refusal must happen before server_registry.call_tool,
which has no gate of its own.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List

from gateway.catalog import ToolDenied, assert_tool_callable
from gateway.scope_guard import apply_scope_guard
from gateway.session import GatewaySession

RegistryCall = Callable[[str, str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


def _split_tool_name(namespaced: str) -> tuple[str, str]:
    server_name, separator, tool_name = namespaced.partition("__")
    if not separator or not tool_name:
        raise ToolDenied(f"Malformed tool name: {namespaced!r}")
    return server_name, tool_name


def _find_tool(
    tools_by_server: Dict[str, List[Dict[str, Any]]], server_name: str, tool_name: str
) -> Dict[str, Any]:
    for tool in tools_by_server.get(server_name) or []:
        if tool.get("name") == tool_name:
            return tool
    raise ToolDenied(f"Unknown tool: {server_name}.{tool_name}")


async def dispatch_tool_call(
    namespaced_name: str,
    arguments: Dict[str, Any],
    session: GatewaySession,
    tools_by_server: Dict[str, List[Dict[str, Any]]],
    registry_call: RegistryCall,
) -> Dict[str, Any]:
    """Gate, guard, then delegate one tool call."""
    server_name, tool_name = _split_tool_name(namespaced_name)
    tool = _find_tool(tools_by_server, server_name, tool_name)

    assert_tool_callable(server_name, tool, session)
    guarded = apply_scope_guard(arguments or {}, session)

    return await registry_call(server_name, tool_name, guarded)
