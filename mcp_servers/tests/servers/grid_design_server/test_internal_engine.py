"""Unit tests for the internal (Chat DB) backend of the grid_design MCP server.

The adapter must return the exact response contract the AppSheet-backed
workflow returned (AppSheet-style BOM row keys, energy_specs, cost_summary,
output.design_parameters, design.Id/Name aliases) so the LPP handlers keep
working unchanged.
"""

from unittest.mock import MagicMock, patch

import pytest

from mcp_servers.servers.grid_design_server import internal_engine
from mcp_servers.servers.grid_design_server.grid_design_mcp_server import (
    compute_bom_cost_summary,
)

# ── BOM shaping ──────────────────────────────────────────────────────────────

COMPONENTS = {
    "inv1": {
        "id": "inv1",
        "name": "Victron Quattro 15kVA",
        "component_type": "Main Energy Asset",
        "counting_unit": "pcs",
        "projected_cost": 3724.5,
        "ddp_cost": 3355.63,
    },
    "meter1": {
        "id": "meter1",
        "name": "Smart Meter - Single Phase",
        "component_type": "Metering",
        "counting_unit": "pcs",
        "projected_cost": 50.0,
        "ddp_cost": 45.0,
    },
    "tool1": {
        "id": "tool1",
        "name": "Crimping Tool",
        "component_type": "Tools",
        "counting_unit": "pcs",
        "projected_cost": 100.0,
        "ddp_cost": 90.0,
    },
}


def _bom_row(item, qty, qty_c, **extra):
    row = {
        "id": f"bom-{item}",
        "item": item,
        "qty": qty,
        "qty_with_contingency": qty_c,
        "subassembly": "Some Assembly",
        "unit_cost_ngn": 100.0,
        "total_cost_ngn": qty * 100.0,
    }
    row.update(extra)
    return row


def test_shape_bom_rows_appsheet_keys_and_costs():
    rows = internal_engine._shape_bom_rows(
        [_bom_row("inv1", 2, 2), _bom_row("meter1", 100, 105)], COMPONENTS
    )
    inv = next(r for r in rows if r["Item"] == "inv1")
    assert inv["Item Name"] == "Victron Quattro 15kVA"
    assert inv["Component Type"] == "Main Energy Asset"
    assert inv["Qty"] == 2
    assert inv["Qty With Contingency"] == 2
    assert inv["Projected Cost with contingency"] == 7449.0  # 2 x 3724.5
    assert inv["DDP Cost with contingency"] == 6711.26

    meter = next(r for r in rows if r["Item"] == "meter1")
    assert meter["Projected Cost with contingency"] == 5250.0  # 105 x 50


def test_shape_bom_rows_tools_have_zero_cost():
    rows = internal_engine._shape_bom_rows([_bom_row("tool1", 1, 1)], COMPONENTS)
    assert rows[0]["Projected Cost with contingency"] == 0
    assert rows[0]["DDP Cost with contingency"] == 0


def test_shaped_rows_feed_cost_summary_groups():
    rows = internal_engine._shape_bom_rows(
        [_bom_row("inv1", 2, 2), _bom_row("meter1", 100, 105), _bom_row("tool1", 1, 1)],
        COMPONENTS,
    )
    summary = compute_bom_cost_summary(rows)
    assert summary["main_energy_asset_cost"] == 7449.0
    assert summary["metering_cost"] == 5250.0
    assert summary["total_cost"] == 12699.0
    assert summary["item_counts"]["tools_excluded"] == 1


# ── Energy specs ─────────────────────────────────────────────────────────────


def test_energy_specs_from_design_and_subassemblies():
    design = {"kwp": 32.76, "kwh": 48.0, "kva": 25.0}
    subs = [
        {"class": "Inverter Charger", "qty": 2},
        {"class": "PV Inverter (+Panels)", "qty": 1},
        {"class": "Battery", "qty": 10},
        {"class": "MPPT (+Panels)", "qty": 6},
    ]
    specs = internal_engine._energy_specs(design, subs)
    assert specs["total_kwp"] == 32.76
    assert specs["total_kwh"] == 48.0
    assert specs["total_kva"] == 25.0
    assert specs["num_inverters"] == 3
    assert specs["num_batteries"] == 10


# ── update_design ────────────────────────────────────────────────────────────


def _patch_designs_repo():
    repo = MagicMock()
    repo.update.return_value = {"id": "d1", "avg_distance_to_pv_combiner": 15.5}
    repo.get.return_value = {"id": "d1"}
    return patch.object(internal_engine, "Repository", return_value=repo), repo


@pytest.mark.asyncio
async def test_update_design_maps_appsheet_labels():
    patcher, repo = _patch_designs_repo()
    with patcher:
        result = await internal_engine.update_design(
            "d1",
            {"Avg Distance to PV Combiner (m)": 15.5, "Distance to Feeder Pillar (m)": 12},
        )
    assert result["success"] is True
    assert result["backend"] == "internal"
    repo.update.assert_called_once_with(
        "d1", {"avg_distance_to_pv_combiner": 15.5, "distance_to_feeder_pillar": 12}
    )


@pytest.mark.asyncio
async def test_update_design_accepts_snake_case_names():
    patcher, repo = _patch_designs_repo()
    with patcher:
        result = await internal_engine.update_design("d1", {"avg_service_drop_length_m": 30})
    assert result["success"] is True
    repo.update.assert_called_once_with("d1", {"average_service_drop_length_m": 30})


@pytest.mark.asyncio
async def test_update_design_rejects_unknown_columns():
    patcher, repo = _patch_designs_repo()
    with patcher, pytest.raises(ValueError):
        await internal_engine.update_design("d1", {"kwp": 999})
    repo.update.assert_not_called()


@pytest.mark.asyncio
async def test_update_design_rejects_empty_updates():
    patcher, repo = _patch_designs_repo()
    with patcher, pytest.raises(ValueError):
        await internal_engine.update_design("d1", {})
    repo.update.assert_not_called()


# ── design_and_bom workflow ──────────────────────────────────────────────────


def _workflow_mocks(auto_design_ok=True, bom_ok=True):
    """Patch engine collaborators; return (patches, mocks)."""
    design_row = {
        "id": "d1",
        "name": "Test LPP design",
        "kwp": 32.76,
        "kwh": 48.0,
        "kva": 25.0,
        "inverter_type": "Quattro 15kVA",
        "battery_type": "Pylontech UP5000",
        "mppt_type": "Victron 250/85 MPPT",
        "pv_type": "JA455W Panel",
        "max_connections": 100,
        "initial_residential_connections": 90,
        "initial_business_connections": 10,
        "initial_3_phase_connections": 0,
    }

    repos = {
        "designs": MagicMock(),
        "design_subassemblies": MagicMock(),
        "bom_items": MagicMock(),
        "components": MagicMock(),
    }
    repos["designs"].get.return_value = design_row
    repos["design_subassemblies"].list.return_value = [{"class": "Battery Inverter", "qty": 2}]
    repos["bom_items"].list.return_value = [_bom_row("inv1", 2, 2)]
    repos["components"].get_many.return_value = COMPONENTS

    dw = MagicMock()
    dw.find_grid_by_name.return_value = None
    dw.create_grid.return_value = {"id": "g1", "name": "TestGrid"}
    dw.create_design.return_value = {"id": "d1", "name": "Test LPP design"}

    ad = MagicMock(
        return_value=(
            {
                "ok": True,
                "design_id": "d1",
                "subassemblies": 5,
                "kwp": 32.76,
                "kwh": 48.0,
                "kva": 25.0,
            }
            if auto_design_ok
            else {"ok": False, "error": "No rentable item: Bad Inverter"}
        )
    )
    gb = MagicMock(
        return_value=(
            {"ok": True, "items": 1, "total_cost_ngn": 200.0, "exchange_rate": 1500.0}
            if bom_ok
            else {"ok": False, "error": "Design d1 not found"}
        )
    )

    patches = [
        patch.object(internal_engine, "Repository", side_effect=lambda t: repos[t]),
        patch.object(internal_engine, "design_writer", dw),
        patch.object(internal_engine, "auto_design", ad),
        patch.object(internal_engine, "generate_bom", gb),
    ]
    return patches, {"dw": dw, "ad": ad, "gb": gb, "repos": repos}


@pytest.mark.asyncio
async def test_design_and_bom_full_flow_contract():
    patches, mocks = _workflow_mocks()
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.design_and_bom(
            {
                "grid_name": "TestGrid",
                "design_name": "Test LPP design",
                "max_connections": 100,
                "wp_per_conn_override": 850,
            }
        )

    assert result["success"] is True
    # Aliases the LPP handler reads
    assert result["design"]["Id"] == "d1"
    assert result["design"]["Name"] == "Test LPP design"
    assert result["energy_specs"]["total_kwp"] == 32.76
    assert result["output"]["design_id"] == "d1"
    assert result["output"]["design_parameters"]["inverter_type"] == "Quattro 15kVA"
    assert result["cost_summary"]["total_cost"] == 7449.0
    assert result["bom"][0]["Item Name"] == "Victron Quattro 15kVA"
    # Grid was created since find returned None
    mocks["dw"].create_grid.assert_called_once()
    # wp_per_conn_override forwarded to create_design payload
    payload = mocks["dw"].create_design.call_args[0][0]
    assert payload["wp_per_conn_override"] == 850


@pytest.mark.asyncio
async def test_design_and_bom_skips_bom_when_wait_for_bom_false():
    patches, mocks = _workflow_mocks()
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.design_and_bom(
            {
                "grid_name": "TestGrid",
                "design_name": "Test LPP design",
                "max_connections": 100,
                "wait_for_bom": False,
            }
        )
    assert result["success"] is True
    mocks["gb"].assert_not_called()
    assert "cost_summary" not in result
    assert result["energy_specs"]["total_kwp"] == 32.76


@pytest.mark.asyncio
async def test_design_and_bom_surfaces_auto_design_error():
    patches, mocks = _workflow_mocks(auto_design_ok=False)
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.design_and_bom(
            {"grid_name": "TestGrid", "design_name": "X", "max_connections": 10}
        )
    assert result["success"] is False
    assert "No rentable item" in result["error"]
    mocks["gb"].assert_not_called()


@pytest.mark.asyncio
async def test_trigger_bom_contract():
    patches, mocks = _workflow_mocks()
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.trigger_bom("d1", "TestGrid")
    assert result["success"] is True
    assert result["cost_summary"]["total_cost"] == 7449.0
    assert result["energy_specs"]["total_kwh"] == 48.0
    assert result["output"]["design_parameters"]["battery_type"] == "Pylontech UP5000"
    assert result["bom"][0]["Component Type"] == "Main Energy Asset"
    mocks["gb"].assert_called_once_with("d1")
