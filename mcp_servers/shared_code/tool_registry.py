"""Declarative tool registry for MCP servers.

Each server used to maintain the same tool three times over: a schema list
(``handle_list_tools``), an if/elif dispatch chain (``handle_call_tool``), and
per-branch result wrapping — three hand-synced copies with nothing forcing
them to agree. That was the root cause of the tool-manifest drift
``tests/test_tool_manifest_sync.py`` now guards against: tools existed in one
copy but not another.

``ToolRegistry`` collapses this to one declaration per tool — name, schema,
and handler together — and generates ``handle_list_tools``/``handle_call_tool``
from it. A mismatch between a tool's registered name and its own schema is a
``ValueError`` raised at import time, not a manifest that silently drifts.

Usage::

    from shared_code.tool_registry import ToolRegistry

    registry = ToolRegistry("solar")

    @registry.tool("get_solar_potential", GET_SOLAR_POTENTIAL_SCHEMA)
    async def _get_solar_potential(arguments: dict) -> list[TextContent]:
        ...

    handle_list_tools = registry.handle_list_tools
    handle_call_tool = registry.handle_call_tool

``server_registry.py`` only ever calls ``handle_list_tools()`` /
``handle_call_tool()`` at module level — those two names are all a
registry-based server needs to export for in-process use. For stdio mode,
wrap the same functions with ``@server.list_tools()`` / ``@server.call_tool()``.

Name resolution strips a leading ``{server_name}_`` prefix before giving up,
so callers may use either the bare name or the server-prefixed name the
orchestrator advertises — this replaces the ad hoc prefix-handling each of
jira and schedule used to hand-roll.
"""

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from shared_code.config.action_flags import ActionFlags

from shared.utils.response_formatters import compose_error_response, compose_json_response

Handler = Callable[[Dict[str, Any]], Awaitable[List[Any]]]
PreDispatchHook = Callable[[str, Dict[str, Any]], Awaitable[Optional[List[Any]]]]


@dataclass
class _Registration:
    name: str
    schema: Dict[str, Any]
    handler: Handler
    gated: bool
    refuse_when_disabled: bool


class ToolRegistry:
    """Name -> (schema, handler) registry for one MCP server's tools."""

    def __init__(self, server_name: str):
        self.server_name = server_name
        self._registrations: Dict[str, _Registration] = {}
        self._aliases: Dict[str, str] = {}
        self._pre_dispatch: Optional[PreDispatchHook] = None
        self._logger = logging.getLogger(f"{server_name}-server")

    def tool(
        self,
        name: str,
        schema: Dict[str, Any],
        *,
        gated: bool = False,
        refuse_when_disabled: bool = True,
        aliases: Tuple[str, ...] = (),
    ) -> Callable[[Handler], Handler]:
        """Register a tool: its schema and its handler, together.

        Args:
            name: The tool's dispatch name. Must equal ``schema["name"]`` —
                checked here, at import time, instead of surfacing later as a
                drifted manifest.
            schema: The tool's JSON-Schema dict (as passed to ``Tool(**schema)``).
            gated: If True, this tool is hidden from ``handle_list_tools`` and
                (when `refuse_when_disabled`) refused by ``handle_call_tool``
                whenever ``ActionFlags.is_actions_enabled(server_name)`` is
                false — for servers whose write-tools were previously listed
                conditionally (e.g. jira's ``ACTION_TOOL_SCHEMAS``).
            refuse_when_disabled: Set False when the handler already contains
                its own disabled-state check with its own message (jira's
                per-branch guards) — then `gated` only controls list-hiding,
                and the handler's own check still runs unchanged.
            aliases: Additional names this tool also dispatches under.
        """
        schema_name = schema.get("name")
        if schema_name != name:
            raise ValueError(
                f"{self.server_name}: tool name {name!r} does not match "
                f"schema['name'] {schema_name!r}"
            )
        if name in self._registrations:
            raise ValueError(f"{self.server_name}: tool {name!r} registered twice")

        def decorator(handler: Handler) -> Handler:
            self._registrations[name] = _Registration(
                name=name,
                schema=schema,
                handler=handler,
                gated=gated,
                refuse_when_disabled=refuse_when_disabled,
            )
            for alias in aliases:
                self._aliases[alias] = name
            return handler

        return decorator

    def pre_dispatch(self, hook: PreDispatchHook) -> PreDispatchHook:
        """Register a server-level gate checked before any tool dispatches.

        If `hook` returns a non-None list, that IS the response and no tool
        handler runs. For whole-server disabled-state checks that previously
        sat at the top of a hand-written ``handle_call_tool`` (equipment_control,
        meta, grid_design) — migrate those verbatim here, exact message text
        included, rather than converging on a generic one.
        """
        self._pre_dispatch = hook
        return hook

    def _actions_enabled(self) -> bool:
        return ActionFlags.is_actions_enabled(self.server_name)

    async def handle_list_tools(self) -> List[Any]:
        """Build fresh Tool objects per call.

        Never share Tool instances across calls — one caller mutating a
        returned tool must not affect the next (the convention already
        documented in every extracted ``tool_schemas.py`` module).
        """
        from mcp.types import Tool

        actions_enabled = self._actions_enabled()
        tools = [
            Tool(**reg.schema)
            for reg in self._registrations.values()
            if not reg.gated or actions_enabled
        ]
        self._logger.info(f"{self.server_name} server: {len(tools)} tools available")
        return tools

    def _resolve(self, name: str) -> Optional[_Registration]:
        if name in self._registrations:
            return self._registrations[name]
        if name in self._aliases:
            return self._registrations.get(self._aliases[name])
        prefix = f"{self.server_name}_"
        if name.startswith(prefix):
            return self._resolve(name[len(prefix) :])
        return None

    async def handle_call_tool(self, name: str, arguments: Dict[str, Any]) -> List[Any]:
        try:
            if self._pre_dispatch is not None:
                hook_result = await self._pre_dispatch(name, arguments)
                if hook_result is not None:
                    return hook_result

            reg = self._resolve(name)
            if reg is None:
                return list(
                    compose_json_response({"success": False, "error": f"Unknown tool: {name}"})
                )

            if reg.gated and reg.refuse_when_disabled and not self._actions_enabled():
                env_var = ActionFlags.get_env_var_name(self.server_name)
                return list(
                    compose_json_response(
                        {
                            "success": False,
                            "error": f"{self.server_name} actions are disabled. Set {env_var}=true to enable.",
                        }
                    )
                )

            return await reg.handler(arguments)
        except Exception as e:
            self._logger.error(f"Error in tool {name}: {e}", exc_info=True)
            return list(compose_error_response(e))
