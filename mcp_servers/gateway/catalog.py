"""Which tools a session may see, and may call.

is_tool_exposed is the single predicate; assert_tool_callable re-runs it at
call time. They must never diverge: server_registry.call_tool has no gate of
its own, so anything not re-checked here is reachable by name.
"""

from __future__ import annotations

from typing import Any, Dict, List

from gateway.session import GatewaySession
from gateway.tiers import is_server_allowed
from shared_code.config.action_flags import ActionFlags


class ToolDenied(Exception):
    """The session may not call this tool."""


def is_tool_exposed(server_name: str, tool: Dict[str, Any], session: GatewaySession) -> bool:
    """Whether ``tool`` is available to ``session``."""
    if not is_server_allowed(server_name):
        return False

    if tool.get("internal_only", False) or tool.get("persistent_only", False):
        return False

    # Same operator kill-switches the orchestrator honours, so disabling a
    # server or tool takes effect on this transport too.
    if not ActionFlags.is_server_enabled(server_name):
        return False

    if ActionFlags.is_tool_disabled(server_name, tool.get("name", "")):
        return False

    if not session.is_staff and not tool.get("visible_to_customer", False):
        return False

    return True


def assert_tool_callable(
    server_name: str, tool: Dict[str, Any], session: GatewaySession
) -> None:
    """Raise ToolDenied unless ``session`` may call ``tool``."""
    if not is_tool_exposed(server_name, tool, session):
        raise ToolDenied(f"{server_name}.{tool.get('name', '?')} is not available to this user")


def list_exposed_tools(
    tools_by_server: Dict[str, List[Dict[str, Any]]], session: GatewaySession
) -> List[Dict[str, Any]]:
    """Flatten to MCP tool definitions this session may see.

    Names are namespaced ``{server}__{tool}`` so the gateway can route a call
    back to its server without a second lookup.
    """
    exposed: List[Dict[str, Any]] = []

    for server_name, server_tools in (tools_by_server or {}).items():
        for tool in server_tools or []:
            if not is_tool_exposed(server_name, tool, session):
                continue
            exposed.append(
                {
                    "name": f"{server_name}__{tool['name']}",
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                }
            )

    return exposed
