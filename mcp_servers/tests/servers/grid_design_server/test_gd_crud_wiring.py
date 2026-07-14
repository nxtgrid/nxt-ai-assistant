"""MCP-server-level wiring tests for the generic gd_* CRUD tools (Phase E task 3).

These tests cover the grid_design_mcp_server.py surface — types.Tool
declarations in handle_list_tools() and dispatch in _handle_internal_tool —
NOT the gd_crud.py logic itself (already covered by test_gd_crud.py). Mocking
follows test_backend_dispatch.py's convention: patch functions on the `gd`
(grid_design_mcp_server) module's imported `gd_crud` reference, and drive
calls through `handle_call_tool` so the GRID_DESIGN_BACKEND branch and
GRID_DESIGN_ACTIONS_ENABLED gate are exercised exactly as in production.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from mcp_servers.servers.grid_design_server import grid_design_mcp_server as gd
from shared.auth.auth_service import STAFF_ORG_ID

CUSTOMER_ORG_ID = 42

_NEW_TOOL_NAMES = (
    "gd_describe_tables",
    "gd_list_rows",
    "gd_get_row",
    "gd_upsert_row",
    "gd_delete_row",
)


def _parse(result):
    return json.loads(result[0].text)


def _assert_no_bare_object_schema(schema, tool_name, path="", is_root=False):
    """Recursively assert no *nested* property is `"type": "object"` without a
    non-empty `"properties"` — the Gemini inputSchema constraint from this
    repo's CLAUDE.md. The tool's own root inputSchema is exempt from the
    "non-empty" requirement: `{"type": "object", "properties": {}}` is the
    established, valid shape for a no-argument tool (e.g. list_design_options,
    gd_describe_tables) — Gemini only rejects a bare `"type": "object"` used
    as a *property value* inside `properties`, not the outer schema envelope.
    Mirrors test_backend_dispatch.py's flatter check (`prop.get("type") !=
    "object"`) but walks nested schemas too, since none of the new tools nest
    objects but the check should still hold generally.
    """
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object":
        props = schema.get("properties")
        if not is_root:
            assert props, f"{tool_name}.{path} is object-typed with no nested properties"
        for key, sub in (props or {}).items():
            _assert_no_bare_object_schema(sub, tool_name, f"{path}.{key}" if path else key)
    elif schema.get("type") == "array":
        _assert_no_bare_object_schema(schema.get("items"), tool_name, f"{path}[]")


# ── handle_list_tools: declarations ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tools_includes_all_five_gd_crud_tools():
    with patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True):
        tools = await gd.handle_list_tools()
    by_name = {t.name: t for t in tools}
    for name in _NEW_TOOL_NAMES:
        assert name in by_name, f"missing new tool {name}"
        assert by_name[name].visible_to_customer is False


@pytest.mark.asyncio
async def test_gd_crud_tool_schemas_have_no_bare_object_properties():
    with patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True):
        tools = await gd.handle_list_tools()
    by_name = {t.name: t for t in tools}
    for name in _NEW_TOOL_NAMES:
        schema = by_name[name].inputSchema
        _assert_no_bare_object_schema(schema, name, is_root=True)
        # Same flat check test_backend_dispatch.py uses across every tool,
        # applied here specifically to the new ones for a belt-and-suspenders
        # match with the existing suite's convention.
        for prop_name, prop in schema.get("properties", {}).items():
            assert prop.get("type") != "object", f"{name}.{prop_name} is object-typed"


@pytest.mark.asyncio
async def test_gd_describe_tables_schema_has_no_required_args():
    with patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True):
        tools = await gd.handle_list_tools()
    by_name = {t.name: t for t in tools}
    schema = by_name["gd_describe_tables"].inputSchema
    assert schema["properties"] == {}
    assert "required" not in schema or schema["required"] == []


@pytest.mark.asyncio
async def test_gd_list_rows_schema_required_and_property_types():
    with patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True):
        tools = await gd.handle_list_tools()
    by_name = {t.name: t for t in tools}
    schema = by_name["gd_list_rows"].inputSchema
    assert schema["required"] == ["table"]
    props = schema["properties"]
    assert props["table"]["type"] == "string"
    assert props["grid_name"]["type"] == "string"
    assert props["filters"]["type"] == "string"
    assert props["limit"]["type"] == "number"
    assert props["include_inactive"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_gd_upsert_row_schema_requires_table_and_values_not_row_id():
    with patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True):
        tools = await gd.handle_list_tools()
    by_name = {t.name: t for t in tools}
    schema = by_name["gd_upsert_row"].inputSchema
    assert set(schema["required"]) == {"table", "values"}
    assert schema["properties"]["values"]["type"] == "string"
    assert schema["properties"]["row_id"]["type"] == "string"


@pytest.mark.asyncio
async def test_gd_delete_row_and_get_row_schemas_require_table_and_row_id():
    with patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True):
        tools = await gd.handle_list_tools()
    by_name = {t.name: t for t in tools}
    for name in ("gd_get_row", "gd_delete_row"):
        schema = by_name[name].inputSchema
        assert set(schema["required"]) == {"table", "row_id"}


# ── _handle_internal_tool dispatch (via handle_call_tool, internal backend) ──


@pytest.mark.asyncio
async def test_gd_describe_tables_dispatches():
    mock = AsyncMock(return_value={"tables": []})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.gd_crud, "gd_describe_tables", mock),
    ):
        result = await gd.handle_call_tool("gd_describe_tables", {"organization_id": STAFF_ORG_ID})
    mock.assert_awaited_once_with()
    assert _parse(result) == {"tables": []}


@pytest.mark.asyncio
async def test_gd_list_rows_parses_filters_json_and_merges_grid_name():
    mock = AsyncMock(return_value={"success": True, "rows": [], "count": 0})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.gd_crud, "gd_list_rows", mock),
    ):
        result = await gd.handle_call_tool(
            "gd_list_rows",
            {
                "table": "designs",
                "grid_name": "MyGrid",
                "filters": json.dumps({"status": "active"}),
                "limit": 10,
                "include_inactive": True,
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    mock.assert_awaited_once_with(
        "designs",
        STAFF_ORG_ID,
        filters={"status": "active", "grid_name": "MyGrid"},
        limit=10,
        include_inactive=True,
    )


@pytest.mark.asyncio
async def test_gd_list_rows_top_level_grid_name_wins_over_filters_json():
    """Precedence: a dedicated top-level `grid_name` argument overrides any
    `grid_name` the model embedded inside the `filters` JSON string instead,
    since the top-level field is the purpose-built one."""
    mock = AsyncMock(return_value={"success": True, "rows": [], "count": 0})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.gd_crud, "gd_list_rows", mock),
    ):
        await gd.handle_call_tool(
            "gd_list_rows",
            {
                "table": "designs",
                "grid_name": "TopLevelGrid",
                "filters": json.dumps({"grid_name": "EmbeddedGrid"}),
                "organization_id": STAFF_ORG_ID,
            },
        )
    _, kwargs = mock.call_args
    assert kwargs["filters"]["grid_name"] == "TopLevelGrid"


@pytest.mark.asyncio
async def test_gd_list_rows_defaults_limit_and_include_inactive():
    mock = AsyncMock(return_value={"success": True, "rows": [], "count": 0})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.gd_crud, "gd_list_rows", mock),
    ):
        await gd.handle_call_tool(
            "gd_list_rows",
            {"table": "components", "organization_id": STAFF_ORG_ID},
        )
    _, kwargs = mock.call_args
    assert kwargs["limit"] == 50
    assert kwargs["include_inactive"] is False
    assert kwargs["filters"] == {}


@pytest.mark.asyncio
async def test_gd_get_row_dispatches():
    mock = AsyncMock(return_value={"success": True, "row": {"id": "r1"}})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.gd_crud, "gd_get_row", mock),
    ):
        result = await gd.handle_call_tool(
            "gd_get_row",
            {"table": "designs", "row_id": "r1", "organization_id": STAFF_ORG_ID},
        )
    assert _parse(result)["success"] is True
    mock.assert_awaited_once_with("designs", "r1", STAFF_ORG_ID)


@pytest.mark.asyncio
async def test_gd_upsert_row_create_case_parses_values_and_passes_user_email():
    """row_id absent from arguments => create; the call to gd_crud.gd_upsert_row
    must receive row_id=None."""
    mock = AsyncMock(return_value={"success": True, "created": {"id": "new1"}})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.gd_crud, "gd_upsert_row", mock),
    ):
        result = await gd.handle_call_tool(
            "gd_upsert_row",
            {
                "table": "designs",
                "values": json.dumps({"name": "NewDesign", "grid": "g1"}),
                "user_email": "staff@example.com",
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    mock.assert_awaited_once_with(
        "designs",
        STAFF_ORG_ID,
        "staff@example.com",
        row_id=None,
        values={"name": "NewDesign", "grid": "g1"},
    )


@pytest.mark.asyncio
async def test_gd_upsert_row_update_case_passes_row_id_through():
    """row_id present in arguments => update; passed through unchanged."""
    mock = AsyncMock(return_value={"success": True, "updated": {"id": "r1"}})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.gd_crud, "gd_upsert_row", mock),
    ):
        result = await gd.handle_call_tool(
            "gd_upsert_row",
            {
                "table": "designs",
                "row_id": "r1",
                "values": json.dumps({"name": "Renamed"}),
                "user_email": "staff@example.com",
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    mock.assert_awaited_once_with(
        "designs",
        STAFF_ORG_ID,
        "staff@example.com",
        row_id="r1",
        values={"name": "Renamed"},
    )


@pytest.mark.asyncio
async def test_gd_upsert_row_handles_missing_user_email():
    """user_email is injected by tool_executor.py like organization_id, but
    the dispatch must not blow up if it's ever absent from arguments."""
    mock = AsyncMock(return_value={"success": True, "created": {"id": "new1"}})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.gd_crud, "gd_upsert_row", mock),
    ):
        await gd.handle_call_tool(
            "gd_upsert_row",
            {
                "table": "designs",
                "values": json.dumps({"name": "NewDesign"}),
                "organization_id": STAFF_ORG_ID,
            },
        )
    args, kwargs = mock.call_args
    assert args[2] is None  # user_email positional arg


@pytest.mark.asyncio
async def test_gd_delete_row_dispatches():
    mock = AsyncMock(return_value={"success": True, "deleted_id": "r1"})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.gd_crud, "gd_delete_row", mock),
    ):
        result = await gd.handle_call_tool(
            "gd_delete_row",
            {"table": "designs", "row_id": "r1", "organization_id": STAFF_ORG_ID},
        )
    assert _parse(result)["success"] is True
    mock.assert_awaited_once_with("designs", "r1", STAFF_ORG_ID)


# ── appsheet backend: new tools are internal-only, same as Phase A/B/C ──────


@pytest.mark.asyncio
async def test_gd_crud_tools_fall_through_to_unknown_tool_on_appsheet_backend():
    """Matches existing precedent (e.g. create_design, get_design): these
    tools have no appsheet-branch mapping, so with GRID_DESIGN_BACKEND=appsheet
    they must hit the generic 'Unknown tool' fallthrough rather than being
    specially routed anywhere."""
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "appsheet"),
        patch.object(gd, "GRID_DESIGN_APP_ID", "fake-app-id"),
        patch.object(gd, "GRID_DESIGN_APP_KEY", "fake-app-key"),
    ):
        for name in _NEW_TOOL_NAMES:
            result = await gd.handle_call_tool(
                name, {"table": "designs", "row_id": "r1", "organization_id": STAFF_ORG_ID}
            )
            assert _parse(result) == {"success": False, "error": f"Unknown tool: {name}"}
