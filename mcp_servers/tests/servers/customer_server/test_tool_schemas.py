"""Integrity of the extracted Customer tool schemas.

The tool definitions used to be literals inside handle_list_tools. Now that
they are data in tool_schemas.py, nothing about them is checked by the compiler
— a truncated file or a dropped key would surface as tools quietly missing from
the server. These tests are that check.

The list is the full 22-tool production manifest (reconciled from
tool_definitions.json), which includes meter-mutating tools. Whether each of
those is customer-visible is a deliberate product decision backed by
server-side org scoping, rate limits, and the CUSTOMER_METER_ACTIONS_ENABLED
gate — so the visibility flags are pinned exactly here, and changing one must
be a conscious edit to this test.
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.customer_server.tool_schemas import TOOL_SCHEMAS  # noqa: E402

REQUIRED_KEYS = {"name", "description", "inputSchema", "visible_to_customer"}
# Non-standard flags that ride along into the manifest and are filtered on
# by user_permissions / the orchestrator.
OPTIONAL_KEYS = {"command_gated"}

MUTATING_PREFIXES = ("turn_meter_", "resend_", "set_meter_", "retry_commissioning", "unassign_")

# Production visibility of every meter-mutating tool, pinned. True entries are
# customer-reachable by design (org-scoped server-side); flipping any of these
# changes the customer-facing surface and must be deliberate.
EXPECTED_MUTATING_VISIBILITY = {
    "retry_commissioning": False,
    "unassign_meter": True,
    "set_meter_power_limit": False,
    "set_meter_date": True,
    "turn_meter_on": False,
    "turn_meter_off": False,
    "resend_meter_token": True,
    "resend_clear_tamper_token": False,
    "resend_power_limit_token": False,
}


class TestSchemaIntegrity:
    def test_expected_tool_count(self):
        """Pins the count so a truncated or partially-merged file fails loudly."""
        assert len(TOOL_SCHEMAS) == 22

    def test_names_are_unique(self):
        names = [s["name"] for s in TOOL_SCHEMAS]
        assert len(names) == len(set(names))

    @pytest.mark.parametrize("schema", TOOL_SCHEMAS, ids=lambda s: s["name"])
    def test_every_schema_is_complete(self, schema):
        assert REQUIRED_KEYS <= set(schema)
        assert set(schema) <= REQUIRED_KEYS | OPTIONAL_KEYS
        assert schema["name"] and isinstance(schema["name"], str)
        assert schema["description"] and isinstance(schema["description"], str)

    @pytest.mark.parametrize("schema", TOOL_SCHEMAS, ids=lambda s: s["name"])
    def test_input_schema_is_well_formed(self, schema):
        """Each inputSchema must be a JSON-Schema object whose `required` entries exist."""
        s = schema["inputSchema"]
        assert s["type"] == "object"
        props = s.get("properties", {})
        assert isinstance(props, dict)
        for req in s.get("required", []):
            assert req in props, f"{schema['name']}: required '{req}' is not a declared property"


class TestCustomerVisibility:
    def test_flag_is_strictly_boolean(self):
        """A truthy-but-not-True value (1, "yes") must not pass for True."""
        for s in TOOL_SCHEMAS:
            assert isinstance(s["visible_to_customer"], bool), s["name"]

    def test_mutating_tool_visibility_is_pinned(self):
        """Every meter-mutating tool's customer visibility matches the pinned
        production surface — no new mutating tool may appear without an entry
        here, and no flag may flip silently."""
        actual = {
            s["name"]: s["visible_to_customer"]
            for s in TOOL_SCHEMAS
            if s["name"].startswith(MUTATING_PREFIXES)
        }
        assert actual == EXPECTED_MUTATING_VISIBILITY

    def test_read_only_tools_say_so(self):
        """Non-mutating tools carry the [READ-ONLY] marker the LLM relies on."""
        unmarked = [
            s["name"]
            for s in TOOL_SCHEMAS
            if not s["name"].startswith(MUTATING_PREFIXES)
            and not s["description"].startswith(("[READ-ONLY]", "[ACTION"))
        ]
        # get_my_open_issues and the customer_get_* dashboards predate the
        # marker convention; pin them so new unmarked tools still fail.
        assert set(unmarked) <= {
            "customer_get_grid_chat_chronology",
            "customer_get_last_gtr_summary",
            "customer_get_fs_daily_summary",
            "get_my_open_issues",
        }, f"tools without a [READ-ONLY]/[ACTION] marker: {unmarked}"


class TestMatchesServerOutput:
    def test_handle_list_tools_returns_every_schema(self):
        import asyncio

        import servers.customer_server.customer_mcp_server as srv

        tools = asyncio.run(srv.handle_list_tools())

        assert [t.name for t in tools] == [s["name"] for s in TOOL_SCHEMAS]
        assert [t.visible_to_customer for t in tools] == [
            s["visible_to_customer"] for s in TOOL_SCHEMAS
        ]

    def test_returns_fresh_objects_per_call(self):
        """Schemas are shared data; the Tool objects built from them must not be,
        or one caller mutating a returned tool would affect the next."""
        import asyncio

        import servers.customer_server.customer_mcp_server as srv

        first = asyncio.run(srv.handle_list_tools())
        second = asyncio.run(srv.handle_list_tools())
        assert all(a is not b for a, b in zip(first, second))
