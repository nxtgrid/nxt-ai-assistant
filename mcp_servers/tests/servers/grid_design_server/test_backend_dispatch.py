"""Tests for grid_design MCP server backend dispatch and tool schemas.

GRID_DESIGN_BACKEND=internal (default) must route every tool to the internal
engine; the schema must expose the full AppSheet form parameter set so the
LLM/user can drive design creation interactively.

Also covers the grid-level authorization wiring added in Phase A Task 4:
every grid/design/subassembly-anchored tool must check access BEFORE calling
into internal_engine, and catalogue-level (subassembly template) tools must
be staff-only.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from mcp_servers.servers.grid_design_server import grid_design_mcp_server as gd
from shared.auth.auth_service import STAFF_ORG_ID

CUSTOMER_ORG_ID = 42
_OTHER_ORG_GRID_DENIAL = "You don't have access to grid 'OtherOrgGrid'"


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

    # New tools from Phase A Task 4 must all be present.
    for name in (
        "create_design",
        "get_design",
        "run_auto_design",
        "change_design_technology",
        "duplicate_design",
        "list_design_subassemblies",
        "add_subassembly",
        "remove_subassembly",
        "set_subassembly_qty",
        "list_subassembly_components",
        "add_subassembly_component",
        "remove_subassembly_component",
        "set_subassembly_component_qty",
        "duplicate_subassembly",
    ):
        assert name in by_name, f"missing new tool {name}"
    assert "list_design_technology_families" in by_name

    # Widened update_design schema.
    update_props = by_name["update_design"].inputSchema["properties"]
    for param in ("rerun_auto_design", "regenerate_bom", "force"):
        assert param in update_props, f"missing {param}"
    assert by_name["change_design_technology"].inputSchema["properties"]["technology_family"][
        "enum"
    ] == ["victron", "deye"]

    # Gemini constraint: no bare object-typed params anywhere (breaks ALL staff
    # chat when violated) — update_design.updates must be a JSON string now.
    for tool in tools:
        for name, prop in tool.inputSchema["properties"].items():
            assert prop.get("type") != "object", f"{tool.name}.{name} is object-typed"


@pytest.mark.asyncio
async def test_list_tools_exposes_artifact_history_tools():
    """Phase B Task 2: list_design_artifacts / get_design_artifact schemas."""
    with patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True):
        tools = await gd.handle_list_tools()
    by_name = {t.name: t for t in tools}

    assert "list_design_artifacts" in by_name
    assert not by_name["list_design_artifacts"].visible_to_customer
    assert by_name["list_design_artifacts"].inputSchema["required"] == ["design_id"]

    assert "get_design_artifact" in by_name
    get_artifact = by_name["get_design_artifact"]
    assert not get_artifact.visible_to_customer
    assert set(get_artifact.inputSchema["required"]) == {"design_id", "artifact_type"}
    assert get_artifact.inputSchema["properties"]["version"]["type"] == "integer"
    assert get_artifact.inputSchema["properties"]["version"]["default"] == 0


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
            {
                "grid_name": "G",
                "design_name": "D",
                "max_connections": 50,
                "organization_id": STAFF_ORG_ID,
            },
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
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="G"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd.handle_call_tool(
            "update_design",
            {
                "design_id": "d1",
                "updates": json.dumps({"Avg Distance to PV Combiner (m)": 15.5}),
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    mock.assert_awaited_once_with(
        "d1",
        {"Avg Distance to PV Combiner (m)": 15.5},
        rerun_auto_design=False,
        regenerate_bom=False,
        force=False,
    )


@pytest.mark.asyncio
async def test_list_design_technology_families_routes_to_internal_engine():
    mock = AsyncMock(return_value={"success": True, "technology_families": []})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "list_design_technology_families", mock),
    ):
        result = await gd.handle_call_tool(
            "list_design_technology_families",
            {"organization_id": STAFF_ORG_ID},
        )
    assert _parse(result)["success"] is True
    mock.assert_awaited_once()


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
        result = await gd.handle_call_tool(
            "find_grid", {"grid_name": "G", "organization_id": STAFF_ORG_ID}
        )
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


# ── Grid-name-direct auth (design_and_bom, create_design, find_grid) ────────


@pytest.mark.asyncio
async def test_design_and_bom_denied_for_non_staff_when_access_check_fails():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "design_and_bom", engine_mock),
        patch.object(
            gd.gd_auth,
            "assert_grid_access",
            AsyncMock(side_effect=gd.gd_auth.GridAccessDenied("nope")),
        ),
    ):
        result = await gd.handle_call_tool(
            "design_and_bom",
            {
                "grid_name": "OtherOrgGrid",
                "design_name": "D",
                "max_connections": 50,
                "organization_id": CUSTOMER_ORG_ID,
            },
        )
    parsed = _parse(result)
    assert parsed["success"] is False
    assert "nope" in parsed["error"]
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_grid_denied_for_non_staff_when_access_check_fails():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "find_grid", engine_mock),
        patch.object(
            gd.gd_auth,
            "assert_grid_access",
            AsyncMock(side_effect=gd.gd_auth.GridAccessDenied("nope")),
        ),
    ):
        result = await gd.handle_call_tool(
            "find_grid", {"grid_name": "OtherOrgGrid", "organization_id": CUSTOMER_ORG_ID}
        )
    parsed = _parse(result)
    assert parsed["success"] is False
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_design_and_bom_calls_assert_grid_access_with_grid_name():
    engine_mock = AsyncMock(return_value={"success": True})
    access_mock = AsyncMock(return_value=None)
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "design_and_bom", engine_mock),
        patch.object(gd.gd_auth, "assert_grid_access", access_mock),
    ):
        result = await gd.handle_call_tool(
            "design_and_bom",
            {
                "grid_name": "MyGrid",
                "design_name": "D",
                "max_connections": 50,
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)
    engine_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_design_calls_assert_grid_access_with_grid_name():
    engine_mock = AsyncMock(return_value={"success": True})
    access_mock = AsyncMock(return_value=None)
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "design_and_bom", engine_mock),
        patch.object(gd.gd_auth, "assert_grid_access", access_mock),
    ):
        result = await gd.handle_call_tool(
            "create_design",
            {
                "grid_name": "MyGrid",
                "design_name": "D",
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)


# ── create_design dispatch (arg-building) ────────────────────────────────────


@pytest.mark.asyncio
async def test_create_design_builds_args_with_opposite_defaults():
    mock = AsyncMock(return_value={"success": True, "backend": "internal"})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "design_and_bom", mock),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        result = await gd.handle_call_tool(
            "create_design",
            {
                "grid_name": "G",
                "design_name": "D",
                "params": json.dumps({"wp_per_conn_override": 150}),
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    args = mock.call_args[0][0]
    assert args["grid_name"] == "G"
    assert args["design_name"] == "D"
    assert args["wp_per_conn_override"] == 150
    # Defaults are OPPOSITE of design_and_bom's (which default both to True).
    assert args["auto_design"] is False
    assert args["wait_for_bom"] is False


@pytest.mark.asyncio
async def test_create_design_run_auto_design_and_generate_bom_map_through():
    mock = AsyncMock(return_value={"success": True, "backend": "internal"})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "design_and_bom", mock),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)),
    ):
        await gd.handle_call_tool(
            "create_design",
            {
                "grid_name": "G",
                "design_name": "D",
                "run_auto_design": True,
                "generate_bom": True,
                "organization_id": STAFF_ORG_ID,
            },
        )
    args = mock.call_args[0][0]
    assert args["auto_design"] is True
    assert args["wait_for_bom"] is True


# ── Design-id-resolved auth (get_design, run_auto_design, update_design, etc) ─


@pytest.mark.asyncio
async def test_get_design_resolves_grid_and_checks_access():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "get_design", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="MyGrid"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "get_design", {"design_id": "d1", "organization_id": STAFF_ORG_ID}
        )
    assert _parse(result)["success"] is True
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)
    engine_mock.assert_awaited_once_with("d1")


@pytest.mark.asyncio
async def test_list_design_artifacts_resolves_grid_and_checks_access():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "list_design_artifacts", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="MyGrid"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "list_design_artifacts", {"design_id": "d1", "organization_id": STAFF_ORG_ID}
        )
    assert _parse(result)["success"] is True
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)
    engine_mock.assert_awaited_once_with("d1")


@pytest.mark.asyncio
async def test_list_design_artifacts_denied_never_reaches_engine():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "list_design_artifacts", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value=None),
    ):
        result = await gd.handle_call_tool(
            "list_design_artifacts", {"design_id": "d1", "organization_id": CUSTOMER_ORG_ID}
        )
    assert _parse(result)["success"] is False
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_design_artifact_resolves_grid_and_checks_access():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "get_design_artifact", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="MyGrid"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "get_design_artifact",
            {
                "design_id": "d1",
                "artifact_type": "distribution_map",
                "version": 1,
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)
    engine_mock.assert_awaited_once_with("d1", "distribution_map", 1)


@pytest.mark.asyncio
async def test_get_design_artifact_denied_never_reaches_engine():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "get_design_artifact", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="OtherOrgGrid"),
        patch.object(
            gd.gd_auth,
            "assert_grid_access",
            AsyncMock(side_effect=gd.gd_auth.GridAccessDenied(_OTHER_ORG_GRID_DENIAL)),
        ),
    ):
        result = await gd.handle_call_tool(
            "get_design_artifact",
            {
                "design_id": "d1",
                "artifact_type": "distribution_map",
                "organization_id": CUSTOMER_ORG_ID,
            },
        )
    assert _parse(result)["success"] is False
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_auto_design_resolves_grid_and_checks_access():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "run_auto_design", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="MyGrid"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "run_auto_design",
            {
                "design_id": "d1",
                "param_overrides": json.dumps({"wp_per_conn_override": 150}),
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)
    engine_mock.assert_awaited_once_with("d1", {"wp_per_conn_override": 150}, False)


@pytest.mark.asyncio
async def test_update_design_resolves_grid_and_checks_access_and_threads_kwargs():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "update_design", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="MyGrid"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "update_design",
            {
                "design_id": "d1",
                "updates": json.dumps({"max_connections": 200}),
                "rerun_auto_design": True,
                "regenerate_bom": True,
                "force": True,
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)
    engine_mock.assert_awaited_once_with(
        "d1",
        {"max_connections": 200},
        rerun_auto_design=True,
        regenerate_bom=True,
        force=True,
    )


@pytest.mark.asyncio
async def test_change_design_technology_resolves_grid_and_threads_flags():
    engine_mock = AsyncMock(return_value={"success": True, "technology_family": "deye"})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "change_design_technology", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="MyGrid"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "change_design_technology",
            {
                "design_id": "d1",
                "technology_family": "deye",
                "rerun_auto_design": True,
                "regenerate_bom": False,
                "force": True,
                "organization_id": CUSTOMER_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    access_mock.assert_awaited_once_with("MyGrid", CUSTOMER_ORG_ID)
    engine_mock.assert_awaited_once_with(
        "d1",
        "deye",
        rerun_auto_design=True,
        regenerate_bom=False,
        force=True,
    )


@pytest.mark.asyncio
async def test_design_not_found_denies_without_calling_assert_grid_access():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "get_design", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value=None),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "get_design", {"design_id": "missing", "organization_id": STAFF_ORG_ID}
        )
    parsed = _parse(result)
    assert parsed["success"] is False
    access_mock.assert_not_awaited()
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_design_not_found_and_design_denied_raise_identical_message():
    """Security regression test: a design_id that doesn't exist and a
    design_id that exists but belongs to a grid the caller can't access must
    be indistinguishable from the response text alone — otherwise a caller
    can (a) use the response as an existence oracle for design_ids they don't
    own, and (b) learn another organization's real grid name via the denial
    message (assert_grid_access's own message embeds the grid name, which is
    fine for callers who typed the grid name themselves, but not here where
    the grid name was resolved server-side from an opaque design_id)."""
    engine_mock = AsyncMock(return_value={"success": True})

    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "get_design", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value=None),
    ):
        not_found_result = await gd.handle_call_tool(
            "get_design", {"design_id": "d1", "organization_id": CUSTOMER_ORG_ID}
        )

    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "get_design", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="OtherOrgGrid"),
        patch.object(
            gd.gd_auth,
            "assert_grid_access",
            AsyncMock(side_effect=gd.gd_auth.GridAccessDenied(_OTHER_ORG_GRID_DENIAL)),
        ),
    ):
        denied_result = await gd.handle_call_tool(
            "get_design", {"design_id": "d1", "organization_id": CUSTOMER_ORG_ID}
        )

    not_found_parsed = _parse(not_found_result)
    denied_parsed = _parse(denied_result)
    assert not_found_parsed["success"] is False
    assert denied_parsed["success"] is False
    # Identical wording for both failure modes.
    assert not_found_parsed["error"] == denied_parsed["error"]
    # Neither leaks the resolved grid name.
    assert "OtherOrgGrid" not in not_found_parsed["error"]
    assert "OtherOrgGrid" not in denied_parsed["error"]
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_subassembly_row_not_found_and_row_denied_raise_identical_message():
    """Same existence-oracle regression, one layer down: a row_id that
    doesn't exist vs. a row_id that resolves to a design/grid the caller
    can't access must produce the same wording (and not the design-level
    wrapper's own message, which would otherwise leak a different design_id
    and re-introduce the oracle one level down)."""
    engine_mock = AsyncMock(return_value={"success": True})

    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "remove_subassembly", engine_mock),
        patch.object(
            gd.internal_engine, "get_design_id_for_subassembly_row", AsyncMock(return_value=None)
        ),
    ):
        not_found_result = await gd.handle_call_tool(
            "remove_subassembly",
            {"design_subassembly_row_id": "row1", "organization_id": CUSTOMER_ORG_ID},
        )

    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "remove_subassembly", engine_mock),
        patch.object(
            gd.internal_engine, "get_design_id_for_subassembly_row", AsyncMock(return_value="d1")
        ),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="OtherOrgGrid"),
        patch.object(
            gd.gd_auth,
            "assert_grid_access",
            AsyncMock(side_effect=gd.gd_auth.GridAccessDenied(_OTHER_ORG_GRID_DENIAL)),
        ),
    ):
        denied_result = await gd.handle_call_tool(
            "remove_subassembly",
            {"design_subassembly_row_id": "row1", "organization_id": CUSTOMER_ORG_ID},
        )

    not_found_parsed = _parse(not_found_result)
    denied_parsed = _parse(denied_result)
    assert not_found_parsed["success"] is False
    assert denied_parsed["success"] is False
    assert not_found_parsed["error"] == denied_parsed["error"]
    assert "OtherOrgGrid" not in not_found_parsed["error"]
    assert "OtherOrgGrid" not in denied_parsed["error"]
    assert "d1" not in denied_parsed["error"]
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_design_gates_on_source_design_id():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "duplicate_design", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="MyGrid"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "duplicate_design",
            {
                "source_design_id": "d1",
                "new_design_name": "D2",
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)
    engine_mock.assert_awaited_once_with("d1", "D2", {}, True, True)


# ── Row-id-resolved auth (remove_subassembly, set_subassembly_qty) ──────────


@pytest.mark.asyncio
async def test_remove_subassembly_resolves_row_then_design_then_grid():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "remove_subassembly", engine_mock),
        patch.object(
            gd.internal_engine, "get_design_id_for_subassembly_row", AsyncMock(return_value="d1")
        ) as row_mock,
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="MyGrid"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "remove_subassembly",
            {"design_subassembly_row_id": "row1", "organization_id": STAFF_ORG_ID},
        )
    assert _parse(result)["success"] is True
    row_mock.assert_awaited_once_with("row1")
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)
    engine_mock.assert_awaited_once_with("row1")


@pytest.mark.asyncio
async def test_set_subassembly_qty_resolves_row_then_design_then_grid():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "set_subassembly_qty", engine_mock),
        patch.object(
            gd.internal_engine, "get_design_id_for_subassembly_row", AsyncMock(return_value="d1")
        ) as row_mock,
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="MyGrid"),
        patch.object(gd.gd_auth, "assert_grid_access", AsyncMock(return_value=None)) as access_mock,
    ):
        result = await gd.handle_call_tool(
            "set_subassembly_qty",
            {"design_subassembly_row_id": "row1", "qty": 3, "organization_id": STAFF_ORG_ID},
        )
    assert _parse(result)["success"] is True
    row_mock.assert_awaited_once_with("row1")
    access_mock.assert_awaited_once_with("MyGrid", STAFF_ORG_ID)
    engine_mock.assert_awaited_once_with("row1", 3)


# ── Staff-only catalogue auth ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_subassembly_components_denied_for_non_staff():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "list_subassembly_components", engine_mock),
    ):
        result = await gd.handle_call_tool(
            "list_subassembly_components",
            {"subassembly_id": "s1", "organization_id": CUSTOMER_ORG_ID},
        )
    parsed = _parse(result)
    assert parsed["success"] is False
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_subassembly_component_denied_for_non_staff():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "add_subassembly_component", engine_mock),
    ):
        result = await gd.handle_call_tool(
            "add_subassembly_component",
            {
                "subassembly_id": "s1",
                "component_name": "Widget",
                "organization_id": CUSTOMER_ORG_ID,
            },
        )
    parsed = _parse(result)
    assert parsed["success"] is False
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_subassembly_components_succeeds_for_staff():
    engine_mock = AsyncMock(return_value={"success": True, "components": []})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "list_subassembly_components", engine_mock),
    ):
        result = await gd.handle_call_tool(
            "list_subassembly_components",
            {"subassembly_id": "s1", "organization_id": STAFF_ORG_ID},
        )
    assert _parse(result)["success"] is True
    engine_mock.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_add_subassembly_component_succeeds_for_staff():
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "add_subassembly_component", engine_mock),
    ):
        result = await gd.handle_call_tool(
            "add_subassembly_component",
            {
                "subassembly_id": "s1",
                "component_name": "Widget",
                "organization_id": STAFF_ORG_ID,
            },
        )
    assert _parse(result)["success"] is True
    engine_mock.assert_awaited_once_with("s1", "Widget", None, 1, None)


# ── Unknown tool name / missing organization_id (code-review follow-up) ─────


@pytest.mark.asyncio
async def test_unknown_tool_name_returns_explicit_error_not_silent_fallthrough():
    """A typo'd/unrecognized tool name must hit the dispatch's explicit
    catch-all branch, not silently fall through to some other tool's logic."""
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
    ):
        result = await gd.handle_call_tool("design_and_bomm", {"organization_id": STAFF_ORG_ID})
    assert _parse(result) == {"success": False, "error": "Unknown tool: design_and_bomm"}


@pytest.mark.asyncio
async def test_get_design_denied_when_organization_id_omitted():
    """organization_id is injected non-LLM-controllably by tool_executor.py and
    should always be present, but the auth wrapper must fail closed rather than
    error out or silently proceed if it's ever missing from arguments. Only
    the Chat DB grid-name lookup is mocked (isolating this to the auth
    wrapper); assert_grid_access runs for real and short-circuits on
    organization_id=None without touching the Auth DB."""
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "get_design", engine_mock),
        patch.object(gd.gd_auth, "resolve_grid_name_for_design", return_value="SomeGrid"),
    ):
        result = await gd.handle_call_tool("get_design", {"design_id": "d1"})
    parsed = _parse(result)
    assert parsed["success"] is False
    engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_subassembly_component_denied_when_organization_id_omitted():
    """Same fail-closed expectation as above, through the _require_staff_org
    path rather than _require_grid_access_for_design."""
    engine_mock = AsyncMock(return_value={"success": True})
    with (
        patch.object(gd, "GRID_DESIGN_ACTIONS_ENABLED", True),
        patch.object(gd, "GRID_DESIGN_BACKEND", "internal"),
        patch.object(gd.internal_engine, "add_subassembly_component", engine_mock),
    ):
        result = await gd.handle_call_tool(
            "add_subassembly_component",
            {"subassembly_id": "s1", "component_name": "Widget"},
        )
    parsed = _parse(result)
    assert parsed["success"] is False
    engine_mock.assert_not_awaited()
