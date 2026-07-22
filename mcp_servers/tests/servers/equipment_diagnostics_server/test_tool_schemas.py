"""Integrity of the extracted Equipment Diagnostics tool schemas.

The 9 tool definitions used to be literals inside handle_list_tools. Now that
they are data in tool_schemas.py, nothing about them is checked by the compiler
— a truncated file or a dropped key would surface as tools quietly missing from
the server. These tests are that check.
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.equipment_diagnostics_server.tool_schemas import TOOL_SCHEMAS  # noqa: E402

REQUIRED_KEYS = {"name", "description", "inputSchema", "visible_to_customer"}


class TestSchemaIntegrity:
    def test_expected_tool_count(self):
        """Pins the count so a truncated or partially-merged file fails loudly."""
        assert len(TOOL_SCHEMAS) == 9

    def test_names_are_unique(self):
        names = [s["name"] for s in TOOL_SCHEMAS]
        assert len(names) == len(set(names))

    @pytest.mark.parametrize("schema", TOOL_SCHEMAS, ids=lambda s: s["name"])
    def test_every_schema_is_complete(self, schema):
        assert set(schema) == REQUIRED_KEYS
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
    """Equipment diagnostics exposes site telemetry and schedules field checks.

    Every tool is staff-only in code. `visible_to_customer` is what
    user_permissions.filter_tools_for_user reads, so a True slipping in here
    would put another operator's site data in front of a customer.
    """

    def test_all_tools_are_staff_only(self):
        exposed = [s["name"] for s in TOOL_SCHEMAS if s["visible_to_customer"] is not False]
        assert exposed == [], f"tools exposed to customers: {exposed}"

    def test_flag_is_strictly_boolean(self):
        """A truthy-but-not-False value (None, 0, "") must not pass for False."""
        for s in TOOL_SCHEMAS:
            assert isinstance(s["visible_to_customer"], bool), s["name"]


class TestMatchesServerOutput:
    def test_handle_list_tools_returns_every_schema(self):
        import asyncio

        import servers.equipment_diagnostics_server.equipment_diagnostics_mcp_server as srv

        tools = asyncio.run(srv.handle_list_tools())

        assert [t.name for t in tools] == [s["name"] for s in TOOL_SCHEMAS]
        assert all(t.visible_to_customer is False for t in tools)

    def test_returns_fresh_objects_per_call(self):
        """Schemas are shared data; the Tool objects built from them must not be,
        or one caller mutating a returned tool would affect the next."""
        import asyncio

        import servers.equipment_diagnostics_server.equipment_diagnostics_mcp_server as srv

        first = asyncio.run(srv.handle_list_tools())
        second = asyncio.run(srv.handle_list_tools())
        assert all(a is not b for a, b in zip(first, second))
