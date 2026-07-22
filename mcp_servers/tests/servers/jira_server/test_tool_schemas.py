"""Integrity of the extracted Jira tool schemas.

The 9 tool definitions used to be literals inside handle_list_tools. Now that
they are data in tool_schemas.py, nothing about them is checked by the compiler
— a truncated file or a dropped key would surface as tools quietly missing from
the server. These tests are that check.

Names are unprefixed (``get_issue``, not ``jira_get_issue``) to match
tool_definitions.json — the orchestrator adds the server prefix when
advertising, and handle_call_tool normalizes it back on dispatch.

The gating half matters most: five of the nine are only listed when
JIRA_ACTIONS_ENABLED is on, and four of those write to Jira.
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.jira_server.tool_schemas import (  # noqa: E402
    ACTION_TOOL_SCHEMAS,
    READ_ONLY_TOOL_SCHEMAS,
)

ALL_SCHEMAS = READ_ONLY_TOOL_SCHEMAS + ACTION_TOOL_SCHEMAS

# The five tools JIRA_ACTIONS_ENABLED gates. Named explicitly rather than derived
# so that moving a tool between the two lists fails here instead of silently
# changing what an operator gets when they turn actions off.
GATED_NAMES = [
    "add_comment",
    "get_transitions",
    "change_status",
    "assign_issue",
    "add_on_call_override",
]


class TestSchemaIntegrity:
    def test_expected_tool_counts(self):
        """Pins the counts so a truncated or partially-merged file fails loudly."""
        assert len(READ_ONLY_TOOL_SCHEMAS) == 4
        assert len(ACTION_TOOL_SCHEMAS) == 5

    def test_names_are_unique_across_both_lists(self):
        names = [s["name"] for s in ALL_SCHEMAS]
        assert len(names) == len(set(names))

    @pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=lambda s: s["name"])
    def test_every_schema_is_complete(self, schema):
        assert set(schema) == {"name", "description", "inputSchema", "visible_to_customer"}
        assert schema["name"] and isinstance(schema["name"], str)
        assert schema["description"] and isinstance(schema["description"], str)

    @pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=lambda s: s["name"])
    def test_input_schema_is_well_formed(self, schema):
        """Each inputSchema must be a JSON-Schema object whose `required` entries exist."""
        s = schema["inputSchema"]
        assert s["type"] == "object"
        props = s.get("properties", {})
        assert isinstance(props, dict)
        for req in s.get("required", []):
            assert req in props, f"{schema['name']}: required '{req}' is not a declared property"


class TestActionGating:
    """JIRA_ACTIONS_ENABLED=false must withhold every write-capable tool."""

    def test_gated_list_is_exactly_the_expected_tools(self):
        assert [s["name"] for s in ACTION_TOOL_SCHEMAS] == GATED_NAMES

    def test_no_gated_tool_leaks_into_the_always_on_list(self):
        leaked = [s["name"] for s in READ_ONLY_TOOL_SCHEMAS if s["name"] in GATED_NAMES]
        assert leaked == [], f"write-capable tools listed unconditionally: {leaked}"


class TestCustomerVisibility:
    """Jira is staff-only, and every schema must say so explicitly.

    A schema that omits `visible_to_customer` is not neutral:
    server_registry.list_tools defaults a missing flag to True, so on the code
    path the tool reads as customer-visible. Twelve of these fourteen used to
    omit it, closed only by tool_definitions.json carrying an explicit `false`.
    These tests keep the code standing on its own.
    """

    def test_every_schema_declares_the_flag(self):
        """The fail-open case: a missing flag becomes True downstream."""
        missing = [s["name"] for s in ALL_SCHEMAS if "visible_to_customer" not in s]
        assert missing == [], f"schemas that would default to customer-visible: {missing}"

    def test_no_schema_declares_itself_customer_visible(self):
        exposed = [s["name"] for s in ALL_SCHEMAS if s["visible_to_customer"] is not False]
        assert exposed == [], f"Jira is staff-only, but these claim otherwise: {exposed}"

    def test_flag_is_strictly_boolean(self):
        """A falsy-but-not-False value (None, 0, "") must not pass for False."""
        for s in ALL_SCHEMAS:
            assert isinstance(s["visible_to_customer"], bool), s["name"]


class TestMatchesServerOutput:
    def _list_tools(self, actions_enabled: bool):
        import asyncio

        os.environ["JIRA_ACTIONS_ENABLED"] = "true" if actions_enabled else "false"
        import servers.jira_server.jira_mcp_server as srv

        return asyncio.run(srv.handle_list_tools())

    def test_actions_on_returns_every_schema(self):
        tools = self._list_tools(True)
        assert [t.name for t in tools] == [s["name"] for s in ALL_SCHEMAS]

    def test_served_tools_carry_the_staff_only_flag(self):
        """End-to-end: the flag survives types.Tool construction, so
        server_registry never has to fall back to its True default."""
        for t in self._list_tools(True):
            assert getattr(t, "visible_to_customer", None) is False, t.name

    def test_actions_off_withholds_the_gated_tools(self):
        tools = self._list_tools(False)
        names = [t.name for t in tools]
        assert names == [s["name"] for s in READ_ONLY_TOOL_SCHEMAS]
        assert not set(names) & set(GATED_NAMES)

    def test_returns_fresh_objects_per_call(self):
        """Schemas are shared data; the Tool objects built from them must not be,
        or one caller mutating a returned tool would affect the next."""
        first = self._list_tools(True)
        second = self._list_tools(True)
        assert all(a is not b for a, b in zip(first, second))
