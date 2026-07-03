"""Tests for grid_design MCP server backend dispatch and tool schemas.

GRID_DESIGN_BACKEND=internal (default) must route every tool to the internal
engine; the schema must expose the full AppSheet form parameter set so the
LLM/user can drive design creation interactively.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from mcp_servers.servers.grid_design_server import grid_design_mcp_server as gd


def _parse(result):
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_list_tools_exposes_full_parameter_set():
    with patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True):
        tools = await gd.handle_list_tools()
    by_name = {t.name: t for t in tools}

    assert "list_design_options" in by_name
    props = by_name["design_and_bom"].inputSchema["properties"]
    for param in (
        "avg_service_drop_length_m",
        "wp_per_conn_override",
        "regulation_constraint",
        "pue_hours_per_day",
        "daily_generation_potential_kwh_kwp",
        "target_tariff_usd",
        "max_distance_to_center_of_consumption_m",
        "avg_distance_to_pv_combiner_m",
        "distance_to_feeder_pillar_m",
        "spd_type",
        "auto_design",
    ):
        assert param in props, f"missing {param}"
    assert props["regulation_constraint"]["enum"] == ["None", "Nigeria - DARES"]
    assert len(props["spd_type"]["enum"]) == 2

    # Gemini constraint: no bare object-typed params anywhere (breaks ALL staff
    # chat when violated) — update_design.updates must be a JSON string now.
    for tool in tools:
        for name, prop in tool.inputSchema["properties"].items():
            assert prop.get("type") != "object", f"{tool.name}.{name} is object-typed"


@pytest.mark.asyncio
async def test_design_and_bom_routes_to_internal_engine():
    mock = AsyncMock(return_value={"success": True, "backend": "internal"})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "design_and_bom", mock),
    ):
        result = await gd.handle_call_tool(
            "design_and_bom",
            {"grid_name": "G", "design_name": "D", "max_connections": 50},
        )
    assert _parse(result)["success"] is True
    args = mock.call_args[0][0]
    assert args["grid_name"] == "G"
    assert args["max_connections"] == 50
    # Server-side technology defaults still applied for the internal engine
    assert args["inverter_type"] == "Quattro 15kVA"


@pytest.mark.asyncio
async def test_update_design_accepts_json_string_updates():
    mock = AsyncMock(return_value={"success": True, "backend": "internal", "updated": {}})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "update_design", mock),
    ):
        result = await gd.handle_call_tool(
            "update_design",
            {
                "design_id": "d1",
                "updates": json.dumps({"Avg Distance to PV Combiner (m)": 15.5}),
            },
        )
    assert _parse(result)["success"] is True
    mock.assert_awaited_once_with("d1", {"Avg Distance to PV Combiner (m)": 15.5})


@pytest.mark.asyncio
async def test_internal_backend_does_not_require_appsheet_credentials():
    mock = AsyncMock(return_value={"success": True, "grid": {"Id": "g1"}})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd, "GRID_DESIGN_APP_ID", ""),
        patch.object(gd, "GRID_DESIGN_APP_KEY", ""),
        patch.object(gd.internal_engine, "find_grid", mock),
    ):
        result = await gd.handle_call_tool("find_grid", {"grid_name": "G"})
    assert _parse(result)["success"] is True


@pytest.mark.asyncio
async def test_appsheet_backend_still_requires_credentials():
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "appsheet"),
        patch.object(gd, "GRID_DESIGN_APP_ID", ""),
        patch.object(gd, "GRID_DESIGN_APP_KEY", ""),
    ):
        result = await gd.handle_call_tool("find_grid", {"grid_name": "G"})
    parsed = _parse(result)
    assert parsed["success"] is False
    assert "GRID_DESIGN_APP_ID" in parsed["error"]


@pytest.mark.asyncio
async def test_list_design_options_routes_to_internal_engine():
    mock = AsyncMock(return_value={"success": True, "technology_options": []})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd.internal_engine, "list_design_options", mock),
    ):
        result = await gd.handle_call_tool("list_design_options", {})
    assert _parse(result)["success"] is True
    mock.assert_awaited_once()
