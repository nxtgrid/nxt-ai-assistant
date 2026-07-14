"""Unit tests for the internal (Chat DB) backend of the grid_design MCP server.

The adapter must return the exact response contract the AppSheet-backed
workflow returned (AppSheet-style BOM row keys, energy_specs, cost_summary,
output.design_parameters, design.Id/Name aliases) so the LPP handlers keep
working unchanged.
"""

from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

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


@pytest.mark.asyncio
async def test_update_design_guard_blocks_before_saving_updates():
    """Manual-edit protection should run before parameter updates are saved so
    a refused auto-design rerun cannot leave a partially changed design row."""
    patcher, repo = _patch_designs_repo()
    subs_repo = MagicMock()
    subs_repo.list.return_value = [{"id": "sub1", "manually_edited": True}]

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else repo

    with patch.object(internal_engine, "Repository", side_effect=repo_factory):
        with pytest.raises(ValueError, match="manually-edited") as exc_info:
            await internal_engine.update_design(
                "d1", {"max_connections": 150}, rerun_auto_design=True
            )
    assert "manually-edited" in str(exc_info.value)
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
async def test_design_and_bom_family_defaults_do_not_override_explicit_equipment():
    patches, mocks = _workflow_mocks()
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.design_and_bom(
            {
                "grid_name": "TestGrid",
                "design_name": "Test Deye design",
                "max_connections": 100,
                "technology_family": "deye",
                "inverter_type": "Deye SUN-50K",
            }
        )

    assert result["success"] is True
    payload = mocks["dw"].create_design.call_args[0][0]
    assert payload["technology_family"] == "deye"
    assert payload["inverter_type"] == "Deye SUN-50K"
    assert payload["battery_type"] == "Deye LV Bat 5kWh"
    mocks["ad"].assert_called_once_with("d1", technology_family="deye")


@pytest.mark.asyncio
async def test_design_and_bom_deye_family_applies_deye_defaults_without_explicit_equipment():
    patches, mocks = _workflow_mocks()
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.design_and_bom(
            {
                "grid_name": "TestGrid",
                "design_name": "Test Deye design",
                "max_connections": 100,
                "technology_family": "deye",
            }
        )

    assert result["success"] is True
    payload = mocks["dw"].create_design.call_args[0][0]
    assert payload["technology_family"] == "deye"
    assert payload["inverter_type"] == "Deye SUN-30K"
    assert payload["battery_type"] == "Deye LV Bat 5kWh"
    mocks["ad"].assert_called_once_with("d1", technology_family="deye")


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


# ── _translate_design_updates ───────────────────────────────────────────────


def test_translate_design_updates_accepts_legacy_aliases():
    mapped = internal_engine._translate_design_updates(
        {"Avg Distance to PV Combiner (m)": 15.5, "distance_to_feeder_pillar_m": 12}
    )
    assert mapped == {"avg_distance_to_pv_combiner": 15.5, "distance_to_feeder_pillar": 12}


def test_translate_design_updates_accepts_full_field_map_entries():
    mapped = internal_engine._translate_design_updates(
        {"max_connections": 150, "wp_per_conn_override": 140}
    )
    assert mapped == {"max_connections": 150, "wp_per_conn_override": 140}


def test_translate_design_updates_rejects_created_by():
    with pytest.raises(ValueError, match="created_by"):
        internal_engine._translate_design_updates({"created_by": "someone@example.com"})


def test_translate_design_updates_rejects_unknown_key():
    with pytest.raises(ValueError, match="Cannot update columns"):
        internal_engine._translate_design_updates({"not_a_real_field": 1})


def test_translate_design_updates_rejects_empty_dict():
    with pytest.raises(ValueError, match="No columns to update"):
        internal_engine._translate_design_updates({})


# ── widened update_design ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_design_accepts_full_field_map_column():
    """A field only in design_writer._DESIGN_FIELD_MAP (not the old 3-column
    whitelist) now works — this used to raise ValueError."""
    patcher, repo = _patch_designs_repo()
    with patcher:
        result = await internal_engine.update_design("d1", {"max_connections": 150})
    assert result["success"] is True
    repo.update.assert_called_once_with("d1", {"max_connections": 150})


def _patch_auto_design_and_bom(auto_design_ok=True, bom_ok=True):
    ad = MagicMock(
        return_value={"ok": True} if auto_design_ok else {"ok": False, "error": "sizing failed"}
    )
    gb = MagicMock(return_value={"ok": True} if bom_ok else {"ok": False, "error": "bom failed"})
    return (
        patch.object(internal_engine, "auto_design", ad),
        patch.object(internal_engine, "generate_bom", gb),
        ad,
        gb,
    )


@pytest.mark.asyncio
async def test_update_design_rerun_auto_design_no_manual_edits():
    patcher, repo = _patch_designs_repo()
    subs_repo = MagicMock()
    subs_repo.list.return_value = []
    ad_patch, gb_patch, ad, gb = _patch_auto_design_and_bom()

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else repo

    with patch.object(internal_engine, "Repository", side_effect=repo_factory), ad_patch, gb_patch:
        result = await internal_engine.update_design("d1", {}, rerun_auto_design=True)

    assert result["success"] is True
    assert result["auto_design_reran"] is True
    ad.assert_called_once_with("d1")
    gb.assert_not_called()


@pytest.mark.asyncio
async def test_update_design_rerun_auto_design_blocked_by_manual_edits():
    patcher, repo = _patch_designs_repo()
    subs_repo = MagicMock()
    subs_repo.list.return_value = [{"id": "sub1", "manually_edited": True}]
    ad_patch, gb_patch, ad, gb = _patch_auto_design_and_bom()

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else repo

    with patch.object(internal_engine, "Repository", side_effect=repo_factory), ad_patch, gb_patch:
        with pytest.raises(ValueError, match="manually-edited"):
            await internal_engine.update_design("d1", {}, rerun_auto_design=True)
    ad.assert_not_called()


@pytest.mark.asyncio
async def test_update_design_rerun_auto_design_force_overrides_guard():
    patcher, repo = _patch_designs_repo()
    subs_repo = MagicMock()
    subs_repo.list.return_value = [{"id": "sub1", "manually_edited": True}]
    ad_patch, gb_patch, ad, gb = _patch_auto_design_and_bom()

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else repo

    with patch.object(internal_engine, "Repository", side_effect=repo_factory), ad_patch, gb_patch:
        result = await internal_engine.update_design("d1", {}, rerun_auto_design=True, force=True)
    assert result["success"] is True
    ad.assert_called_once_with("d1")


@pytest.mark.asyncio
async def test_update_design_regenerate_bom_calls_generate_bom():
    patcher, repo = _patch_designs_repo()
    ad_patch, gb_patch, ad, gb = _patch_auto_design_and_bom()
    with patcher, ad_patch, gb_patch:
        result = await internal_engine.update_design("d1", {}, regenerate_bom=True)
    assert result["success"] is True
    assert result["bom_regenerated"] is True
    gb.assert_called_once_with("d1")
    ad.assert_not_called()


@pytest.mark.asyncio
async def test_update_design_auto_design_failure_raises():
    patcher, repo = _patch_designs_repo()
    subs_repo = MagicMock()
    subs_repo.list.return_value = []
    ad_patch, gb_patch, ad, gb = _patch_auto_design_and_bom(auto_design_ok=False)

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else repo

    with patch.object(internal_engine, "Repository", side_effect=repo_factory), ad_patch, gb_patch:
        with pytest.raises(ValueError, match="sizing failed"):
            await internal_engine.update_design("d1", {}, rerun_auto_design=True)


@pytest.mark.asyncio
async def test_update_design_rolls_back_updates_when_auto_design_fails():
    designs_repo = MagicMock()
    designs_repo.get.return_value = {
        "id": "d1",
        "inverter_type": "Quattro 15kVA",
        "battery_type": "Pylontech UP5000",
    }
    designs_repo.update.side_effect = [
        {"id": "d1", "inverter_type": "Deye SUN-30K", "battery_type": "Deye LV Bat 5kWh"},
        {"id": "d1", "inverter_type": "Quattro 15kVA", "battery_type": "Pylontech UP5000"},
    ]
    subs_repo = MagicMock()
    subs_repo.list.return_value = []
    ad = MagicMock(return_value={"ok": False, "error": "sizing failed"})

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else designs_repo

    with (
        patch.object(internal_engine, "Repository", side_effect=repo_factory),
        patch.object(internal_engine, "auto_design", ad),
    ):
        with pytest.raises(ValueError, match="sizing failed"):
            await internal_engine.update_design(
                "d1",
                {"inverter_type": "Deye SUN-30K", "battery_type": "Deye LV Bat 5kWh"},
                rerun_auto_design=True,
            )

    assert designs_repo.update.call_args_list[0].args == (
        "d1",
        {"inverter_type": "Deye SUN-30K", "battery_type": "Deye LV Bat 5kWh"},
    )
    assert designs_repo.update.call_args_list[1].args == (
        "d1",
        {"inverter_type": "Quattro 15kVA", "battery_type": "Pylontech UP5000"},
    )


@pytest.mark.asyncio
async def test_update_design_bom_failure_raises():
    patcher, repo = _patch_designs_repo()
    ad_patch, gb_patch, ad, gb = _patch_auto_design_and_bom(bom_ok=False)
    with patcher, ad_patch, gb_patch:
        with pytest.raises(ValueError, match="bom failed"):
            await internal_engine.update_design("d1", {}, regenerate_bom=True)


@pytest.mark.asyncio
async def test_update_design_restores_subassemblies_when_bom_fails_after_auto_design():
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1", "inverter_type": "Quattro 15kVA"}
    designs_repo.update.return_value = {"id": "d1", "inverter_type": "Deye SUN-30K"}
    subs_repo = MagicMock()
    subs_repo.list.side_effect = [
        [],
        [{"id": "old-sub", "design": "d1", "subassembly": "victron", "active": True}],
        [{"id": "new-sub", "design": "d1", "subassembly": "deye", "active": True}],
    ]
    ad = MagicMock(return_value={"ok": True, "subassemblies": 2})
    gb = MagicMock(return_value={"ok": False, "error": "bom failed"})

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else designs_repo

    with (
        patch.object(internal_engine, "Repository", side_effect=repo_factory),
        patch.object(internal_engine, "auto_design", ad),
        patch.object(internal_engine, "generate_bom", gb),
    ):
        with pytest.raises(ValueError, match="bom failed"):
            await internal_engine.update_design(
                "d1",
                {"inverter_type": "Deye SUN-30K"},
                rerun_auto_design=True,
                regenerate_bom=True,
            )

    subs_repo.soft_delete.assert_called_once_with("new-sub")
    subs_repo.upsert.assert_called_once_with(
        [{"id": "old-sub", "design": "d1", "subassembly": "victron", "active": True}]
    )


# ── get_design ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_design_found_shapes_response():
    design_row = {
        "id": "d1",
        "name": "Test design",
        "kwp": 10.0,
        "kwh": 20.0,
        "kva": 8.0,
        "inverter_type": "Quattro 15kVA",
        "wp_per_conn_override": 120,
    }
    repos = {
        "designs": MagicMock(),
        "design_subassemblies": MagicMock(),
    }
    repos["designs"].get.return_value = design_row
    repos["design_subassemblies"].list.return_value = [{"class": "Battery", "qty": 4}]

    with patch.object(internal_engine, "Repository", side_effect=lambda t: repos[t]):
        result = await internal_engine.get_design("d1")

    assert result["success"] is True
    assert result["design"]["Id"] == "d1"
    assert result["design"]["Name"] == "Test design"
    assert result["energy_specs"]["total_kwp"] == 10.0
    assert result["energy_specs"]["num_batteries"] == 4
    assert result["design_parameters"]["wp_per_connection"] == 120


@pytest.mark.asyncio
async def test_get_design_not_found():
    repos = {
        "designs": MagicMock(),
        "design_subassemblies": MagicMock(),
    }
    repos["designs"].get.return_value = None
    repos["design_subassemblies"].list.return_value = []

    with patch.object(internal_engine, "Repository", side_effect=lambda t: repos[t]):
        result = await internal_engine.get_design("missing")

    assert result["success"] is False
    assert "not found" in result["error"]


# ── _has_manual_subassembly_edits ───────────────────────────────────────────


def test_has_manual_subassembly_edits_true_when_matching_row_exists():
    repo = MagicMock()
    repo.list.return_value = [{"id": "sub1", "manually_edited": True}]
    with patch.object(internal_engine, "Repository", return_value=repo):
        assert internal_engine._has_manual_subassembly_edits("d1") is True
    repo.list.assert_called_once_with(
        active_only=True, filters={"design": "d1", "manually_edited": True}
    )


def test_has_manual_subassembly_edits_false_when_no_matching_row():
    repo = MagicMock()
    repo.list.return_value = []
    with patch.object(internal_engine, "Repository", return_value=repo):
        assert internal_engine._has_manual_subassembly_edits("d1") is False


def test_has_manual_subassembly_edits_respects_active_only():
    """An inactive manually-edited row must not count — the Repository call
    itself is what enforces active_only=True; we assert the call shape here so
    a regression that drops the flag (and would surface the inactive row) is
    caught even though the mock can't filter by activity itself."""
    repo = MagicMock()
    repo.list.return_value = []  # simulates the inactive row being excluded
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = internal_engine._has_manual_subassembly_edits("d1")
    assert result is False
    _, kwargs = repo.list.call_args
    assert kwargs["active_only"] is True


# ── run_auto_design ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_auto_design_applies_overrides_then_runs():
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1", "name": "D1", "max_connections": 150}
    subs_repo = MagicMock()
    subs_repo.list.return_value = []

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else designs_repo

    ad = MagicMock(return_value={"ok": True, "subassemblies": 5})

    call_order = []
    designs_repo.update.side_effect = lambda *a, **k: call_order.append("update")
    ad.side_effect = lambda *a, **k: (
        call_order.append("auto_design"),
        {"ok": True, "subassemblies": 5},
    )[1]

    with (
        patch.object(internal_engine, "Repository", side_effect=repo_factory),
        patch.object(internal_engine, "auto_design", ad),
    ):
        result = await internal_engine.run_auto_design("d1", {"max_connections": 150})

    assert result["success"] is True
    assert result["subassemblies_created"] == 5
    designs_repo.update.assert_called_once_with("d1", {"max_connections": 150})
    ad.assert_called_once_with("d1")
    assert call_order == ["update", "auto_design"]


@pytest.mark.asyncio
async def test_run_auto_design_blocked_by_manual_edits_without_force():
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1"}
    subs_repo = MagicMock()
    subs_repo.list.return_value = [{"id": "sub1", "manually_edited": True}]

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else designs_repo

    ad = MagicMock(return_value={"ok": True, "subassemblies": 5})

    with (
        patch.object(internal_engine, "Repository", side_effect=repo_factory),
        patch.object(internal_engine, "auto_design", ad),
    ):
        with pytest.raises(ValueError, match="manually-edited"):
            await internal_engine.run_auto_design("d1", {})
    ad.assert_not_called()


@pytest.mark.asyncio
async def test_run_auto_design_guard_blocks_before_saving_overrides():
    """Manual-edit protection should run before overrides are saved."""
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1"}
    subs_repo = MagicMock()
    subs_repo.list.return_value = [{"id": "sub1", "manually_edited": True}]

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else designs_repo

    ad = MagicMock(return_value={"ok": True, "subassemblies": 5})

    with (
        patch.object(internal_engine, "Repository", side_effect=repo_factory),
        patch.object(internal_engine, "auto_design", ad),
    ):
        with pytest.raises(ValueError, match="manually-edited") as exc_info:
            await internal_engine.run_auto_design("d1", {"max_connections": 150})
    assert "manually-edited" in str(exc_info.value)
    designs_repo.update.assert_not_called()
    ad.assert_not_called()


@pytest.mark.asyncio
async def test_run_auto_design_force_proceeds_despite_manual_edits():
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1"}
    subs_repo = MagicMock()
    subs_repo.list.return_value = [{"id": "sub1", "manually_edited": True}]

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else designs_repo

    ad = MagicMock(return_value={"ok": True, "subassemblies": 3})

    with (
        patch.object(internal_engine, "Repository", side_effect=repo_factory),
        patch.object(internal_engine, "auto_design", ad),
    ):
        result = await internal_engine.run_auto_design("d1", {}, force=True)
    assert result["success"] is True
    ad.assert_called_once_with("d1")


@pytest.mark.asyncio
async def test_run_auto_design_failure_surfaces_error_dict():
    designs_repo = MagicMock()
    designs_repo.get.return_value = {"id": "d1"}
    subs_repo = MagicMock()
    subs_repo.list.return_value = []

    def repo_factory(table):
        return subs_repo if table == "design_subassemblies" else designs_repo

    ad = MagicMock(return_value={"ok": False, "error": "No rentable item: Bad Inverter"})

    with (
        patch.object(internal_engine, "Repository", side_effect=repo_factory),
        patch.object(internal_engine, "auto_design", ad),
    ):
        result = await internal_engine.run_auto_design("d1", {})
    assert result["success"] is False
    assert "No rentable item" in result["error"]


# ── technology families ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_design_technology_families_groups_design_types_and_layouts():
    repos = {
        "unit_rental_prices": MagicMock(),
        "components": MagicMock(),
        "subassemblies": MagicMock(),
    }
    repos["unit_rental_prices"].list.return_value = [
        {
            "item": "Deye SUN-30K",
            "engineering_item_name": "Deye Hybrid Inverter SUN-30K-SG01HP3-EU-BM3",
            "unit_monthly_rental": 94.76,
            "active": True,
        }
    ]
    repos["components"].list.return_value = [
        {
            "id": "inv-deye",
            "name": "Deye Hybrid Inverter SUN-30K-SG01HP3-EU-BM3",
            "component_type": "Main Energy Asset",
            "active": True,
        }
    ]
    repos["subassemblies"].list.return_value = [
        {
            "id": "hybrid",
            "description": "Deye GE-F60 Hybrid ESS with SUN-30K",
            "assembly_class": "Hybrid ESS",
            "design_types": "Deye",
            "main_component": "inv-deye",
            "active": True,
        },
        {
            "id": "meter",
            "description": "LoRaWAN Smart Meter - Single Phase",
            "assembly_class": "Metering",
            "design_types": "Victron and Deye",
            "active": True,
        },
    ]

    with patch.object(internal_engine, "Repository", side_effect=lambda t: repos[t]):
        result = await internal_engine.list_design_technology_families()

    assert result["success"] is True
    deye = next(f for f in result["technology_families"] if f["family"] == "deye")
    assert deye["display_name"] == "Deye"
    assert deye["site_layout_type"] == "ess"
    assert "Hybrid ESS" in deye["assembly_classes"]
    assert "Metering" in deye["assembly_classes"]


@pytest.mark.asyncio
async def test_change_design_technology_applies_defaults_and_returns_layout_hint():
    update_mock = MagicMock(
        return_value={
            "success": True,
            "backend": "internal",
            "updated": {"id": "d1"},
            "auto_design_reran": True,
            "bom_regenerated": True,
        }
    )
    with patch.object(internal_engine, "_update_design_sync", update_mock):
        result = await internal_engine.change_design_technology("d1", "deye")

    assert result["success"] is True
    assert result["technology_family"] == "deye"
    assert result["site_layout_type"] == "ess"
    updates = update_mock.call_args.args[1]
    assert updates == {"technology_family": "deye"}
    assert update_mock.call_args.kwargs == {
        "rerun_auto_design": True,
        "regenerate_bom": True,
        "force": False,
    }


# ── duplicate_design ─────────────────────────────────────────────────────────


def _patch_duplicate_design_collaborators(
    source=None, new_design=None, auto_design_ok=True, bom_ok=True
):
    source = (
        source
        if source is not None
        else {
            "id": "d1",
            "grid": "g1",
            "name": "Original",
            "max_connections": 100,
            "wp_per_conn_override": 120,
            "created_by": "original_author@example.com",
        }
    )
    new_design = new_design if new_design is not None else {"id": "d2", "name": "Copy"}

    dw = MagicMock()
    dw.get_design.return_value = source

    # Use the real design_row_to_payload/create_design-adjacent behavior via a
    # simple faithful stand-in so tests assert on payload contents, not internals.
    def design_row_to_payload(row):
        mapping = {
            "name": "design_name",
            "max_connections": "max_connections",
            "wp_per_conn_override": "wp_per_conn_override",
            "created_by": "created_by",
        }
        return {mapping[k]: v for k, v in row.items() if k in mapping and v is not None}

    dw.design_row_to_payload.side_effect = design_row_to_payload
    dw.create_design.return_value = new_design

    designs_repo = MagicMock()
    designs_repo.get.return_value = new_design
    subs_repo = MagicMock()
    subs_repo.list.return_value = []
    bom_repo = MagicMock()
    bom_repo.list.return_value = [_bom_row("inv1", 2, 2)]
    components_repo = MagicMock()
    components_repo.get_many.return_value = COMPONENTS

    repos = {
        "designs": designs_repo,
        "design_subassemblies": subs_repo,
        "bom_items": bom_repo,
        "components": components_repo,
    }

    ad = MagicMock(
        return_value={"ok": True} if auto_design_ok else {"ok": False, "error": "sizing failed"}
    )
    gb = MagicMock(return_value={"ok": True} if bom_ok else {"ok": False, "error": "bom failed"})

    patches = [
        patch.object(internal_engine, "design_writer", dw),
        patch.object(internal_engine, "Repository", side_effect=lambda t: repos[t]),
        patch.object(internal_engine, "auto_design", ad),
        patch.object(internal_engine, "generate_bom", gb),
    ]
    return patches, {"dw": dw, "ad": ad, "gb": gb, "repos": repos}


@pytest.mark.asyncio
async def test_duplicate_design_builds_payload_with_overrides_and_new_name():
    patches, mocks = _patch_duplicate_design_collaborators()
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.duplicate_design(
            "d1", "Copy of Original", {"wp_per_conn_override": 140}
        )

    assert result["success"] is True
    payload = mocks["dw"].create_design.call_args[0][0]
    assert payload["design_name"] == "Copy of Original"
    assert payload["wp_per_conn_override"] == 140  # override wins over source's 120
    assert payload["max_connections"] == 100  # carried over from source
    assert "created_by" not in payload  # not carried over from source
    grid_id_arg = mocks["dw"].create_design.call_args[0][1]
    assert grid_id_arg == "g1"  # same grid as source
    assert result["source_design_id"] == "d1"


@pytest.mark.asyncio
async def test_duplicate_design_param_overrides_cannot_reintroduce_created_by():
    """param_overrides here is directly caller-controlled (a future MCP tool
    forwards whatever the LLM/user supplies), so a caller passing
    created_by in param_overrides must NOT have it survive into the payload
    handed to create_design — that would let a caller spoof the new design's
    creator. wp_per_conn_override in the same overrides dict must still come
    through untouched."""
    patches, mocks = _patch_duplicate_design_collaborators()
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.duplicate_design(
            "d1",
            "Copy of Original",
            {"created_by": "attacker@example.com", "wp_per_conn_override": 150},
        )

    assert result["success"] is True
    payload = mocks["dw"].create_design.call_args[0][0]
    assert "created_by" not in payload
    assert payload.get("created_by") != "attacker@example.com"
    assert payload["wp_per_conn_override"] == 150


@pytest.mark.asyncio
async def test_duplicate_design_bom_failure_records_step_and_returns_error():
    patches, mocks = _patch_duplicate_design_collaborators(bom_ok=False)
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.duplicate_design("d1", "Copy", {})

    assert result["success"] is False
    assert "bom failed" in result["error"]
    bom_steps = [s for s in result["steps"] if s["step"] == "generate_bom"]
    assert len(bom_steps) == 1
    assert bom_steps[0]["status"] == "failed"
    assert "bom failed" in bom_steps[0]["reason"]


@pytest.mark.asyncio
async def test_duplicate_design_skips_auto_design_and_bom_when_disabled():
    patches, mocks = _patch_duplicate_design_collaborators()
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.duplicate_design(
            "d1",
            "Copy",
            {},
            run_auto_design_flag=False,
            generate_bom_flag=False,
        )
    assert result["success"] is True
    mocks["ad"].assert_not_called()
    mocks["gb"].assert_not_called()
    assert "bom" not in result


@pytest.mark.asyncio
async def test_duplicate_design_source_not_found():
    patches, mocks = _patch_duplicate_design_collaborators(source=None)
    mocks["dw"].get_design.return_value = None
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.duplicate_design("missing", "Copy", {})
    assert result["success"] is False
    assert "not found" in result["error"]
    mocks["dw"].create_design.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_design_auto_design_failure_short_circuits_before_bom():
    patches, mocks = _patch_duplicate_design_collaborators(auto_design_ok=False)
    with patches[0], patches[1], patches[2], patches[3]:
        result = await internal_engine.duplicate_design("d1", "Copy", {})
    assert result["success"] is False
    assert "sizing failed" in result["error"]
    mocks["gb"].assert_not_called()


# ── list_design_subassemblies ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_design_subassemblies_returns_rows():
    repo = MagicMock()
    rows = [{"id": "ds1", "design": "d1", "qty": 2, "named": "2 x Battery"}]
    repo.list.return_value = rows
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.list_design_subassemblies("d1")
    assert result["success"] is True
    assert result["subassemblies"] == rows
    assert result["count"] == 1
    repo.list.assert_called_once_with(active_only=True, filters={"design": "d1"})


# ── add_subassembly ──────────────────────────────────────────────────────────


def _patch_repos_for_add_subassembly(description="Battery Rack 5kWh"):
    subassemblies_repo = MagicMock()
    subassemblies_repo.list.return_value = [
        {
            "id": "sub1",
            "description": description,
            "assembly_class": "Battery",
            "assembly_reference_image": None,
            "spec1_name": "Capacity",
            "spec1_value": "5",
            "spec1_unit": "kWh",
            "spec2_name": None,
            "spec2_value": None,
            "spec2_unit": None,
            "spec3_name": None,
            "spec3_value": None,
            "spec3_unit": None,
        },
        {
            "id": "sub2",
            "description": "Inverter Charger 15kVA",
            "assembly_class": "Inverter Charger",
        },
    ]
    design_subs_repo = MagicMock()
    design_subs_repo.insert.side_effect = lambda row: row

    def repo_factory(table):
        return {
            "subassemblies": subassemblies_repo,
            "design_subassemblies": design_subs_repo,
        }[table]

    return repo_factory, subassemblies_repo, design_subs_repo


@pytest.mark.asyncio
async def test_add_subassembly_happy_path_sets_manually_edited_and_computes_specs():
    repo_factory, subs_repo, ds_repo = _patch_repos_for_add_subassembly()
    sub_components = [
        {"subassembly": "sub1", "active": True, "spec1_unit": "kWh", "spec1_value": "5"},
    ]
    with (
        patch.object(internal_engine, "Repository", side_effect=repo_factory),
        patch.object(internal_engine, "load", return_value=sub_components),
    ):
        result = await internal_engine.add_subassembly("d1", "Battery Rack 5kWh", 2)

    assert result["success"] is True
    inserted = result["subassembly_added"]
    assert inserted["design"] == "d1"
    assert inserted["subassembly"] == "sub1"
    assert inserted["qty"] == 2
    assert inserted["kwh"] == 10  # 2 x 5 kWh
    assert inserted["manually_edited"] is True
    ds_repo.insert.assert_called_once()


@pytest.mark.asyncio
async def test_add_subassembly_no_match_raises_with_candidates():
    repo_factory, subs_repo, ds_repo = _patch_repos_for_add_subassembly()
    with patch.object(internal_engine, "Repository", side_effect=repo_factory):
        with pytest.raises(ValueError, match="No subassembly matching") as exc_info:
            await internal_engine.add_subassembly("d1", "Completely Unrelated Widget Zzz", 1)
    # Candidates from the catalogue should be surfaced for usability.
    assert "Closest matches" in str(exc_info.value)
    ds_repo.insert.assert_not_called()


# ── remove_subassembly ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_subassembly_flags_unflagged_siblings_manually_edited():
    repo = MagicMock()
    repo.get.return_value = {"id": "ds1", "design": "d1", "manually_edited": False}
    siblings = [
        {"id": "ds2", "design": "d1", "manually_edited": False},
        {"id": "ds3", "design": "d1", "manually_edited": True},
    ]
    repo.list.return_value = siblings

    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.remove_subassembly("ds1")

    assert result["success"] is True
    assert result["removed_id"] == "ds1"
    assert result["design_id"] == "d1"
    repo.soft_delete.assert_called_once_with("ds1")
    repo.list.assert_called_once_with(active_only=True, filters={"design": "d1"})
    # Only the unflagged sibling (ds2) should be updated; ds3 was already flagged.
    repo.update.assert_called_once_with("ds2", {"manually_edited": True})


@pytest.mark.asyncio
async def test_remove_subassembly_not_found():
    repo = MagicMock()
    repo.get.return_value = None
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.remove_subassembly("missing")
    assert result["success"] is False
    repo.soft_delete.assert_not_called()


# ── set_subassembly_qty ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_subassembly_qty_scales_kwp_kwh_kva_exactly():
    repo = MagicMock()
    repo.get.return_value = {"id": "ds1", "qty": 2, "kwp": 10, "kwh": 6, "kva": 4}
    repo.update.return_value = {"id": "ds1"}
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.set_subassembly_qty("ds1", 4)
    assert result["success"] is True
    repo.update.assert_called_once_with(
        "ds1", {"qty": 4, "manually_edited": True, "kwp": 20, "kwh": 12, "kva": 8}
    )


@pytest.mark.asyncio
async def test_set_subassembly_qty_zero_old_qty_avoids_divide_by_zero():
    repo = MagicMock()
    repo.get.return_value = {"id": "ds1", "qty": 0, "kwp": 10, "kwh": 6, "kva": 4}
    repo.update.return_value = {"id": "ds1"}
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.set_subassembly_qty("ds1", 5)
    assert result["success"] is True
    # kwp/kwh/kva omitted entirely -- DB keeps existing stored values.
    repo.update.assert_called_once_with("ds1", {"qty": 5, "manually_edited": True})


@pytest.mark.asyncio
async def test_set_subassembly_qty_not_found():
    repo = MagicMock()
    repo.get.return_value = None
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.set_subassembly_qty("missing", 5)
    assert result["success"] is False


# ── list_subassembly_components ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_subassembly_components_shapes_both_child_types():
    sac_repo = MagicMock()
    sac_repo.list.return_value = [
        {
            "id": "sc1",
            "subassembly": "sub1",
            "component": "comp1",
            "component_subassembly": None,
            "qty": 3,
        },
        {
            "id": "sc2",
            "subassembly": "sub1",
            "component": None,
            "component_subassembly": "sub2",
            "qty": 1,
        },
    ]
    components_repo = MagicMock()
    components_repo.get_many.return_value = {"comp1": {"id": "comp1", "name": "MC4 Connector"}}
    subassemblies_repo = MagicMock()
    subassemblies_repo.get_many.return_value = {
        "sub2": {"id": "sub2", "description": "Cable Harness"}
    }

    def repo_factory(table):
        return {
            "subassembly_components": sac_repo,
            "components": components_repo,
            "subassemblies": subassemblies_repo,
        }[table]

    with patch.object(internal_engine, "Repository", side_effect=repo_factory):
        result = await internal_engine.list_subassembly_components("sub1")

    assert result["success"] is True
    assert result["count"] == 2
    comp_row = next(r for r in result["components"] if r["id"] == "sc1")
    assert comp_row["child_type"] == "component"
    assert comp_row["child_name"] == "MC4 Connector"
    sub_row = next(r for r in result["components"] if r["id"] == "sc2")
    assert sub_row["child_type"] == "subassembly"
    assert sub_row["child_name"] == "Cable Harness"


# ── add_subassembly_component ────────────────────────────────────────────────


def _patch_repos_for_add_subassembly_component():
    components_repo = MagicMock()
    components_repo.list.return_value = [{"id": "comp1", "name": "MC4 Connector"}]
    subassemblies_repo = MagicMock()
    subassemblies_repo.list.return_value = [{"id": "sub2", "description": "Cable Harness"}]
    sac_repo = MagicMock()
    sac_repo.insert.side_effect = lambda row: row
    sac_repo.list.return_value = []  # no nested children -- cycle guard finds nothing

    def repo_factory(table):
        return {
            "components": components_repo,
            "subassemblies": subassemblies_repo,
            "subassembly_components": sac_repo,
        }[table]

    return repo_factory, components_repo, subassemblies_repo, sac_repo


@pytest.mark.asyncio
async def test_add_subassembly_component_by_component_name():
    repo_factory, *_ = _patch_repos_for_add_subassembly_component()
    with patch.object(internal_engine, "Repository", side_effect=repo_factory):
        result = await internal_engine.add_subassembly_component(
            "sub1", component_name="MC4 Connector", qty=4
        )
    assert result["success"] is True
    inserted = result["component_added"]
    assert inserted["subassembly"] == "sub1"
    assert inserted["component"] == "comp1"
    assert inserted["component_subassembly"] is None
    assert inserted["qty"] == 4


@pytest.mark.asyncio
async def test_add_subassembly_component_by_child_subassembly_name():
    repo_factory, *_ = _patch_repos_for_add_subassembly_component()
    with patch.object(internal_engine, "Repository", side_effect=repo_factory):
        result = await internal_engine.add_subassembly_component(
            "sub1", child_subassembly_name="Cable Harness", qty=1
        )
    assert result["success"] is True
    inserted = result["component_added"]
    assert inserted["subassembly"] == "sub1"
    assert inserted["component"] is None
    assert inserted["component_subassembly"] == "sub2"


@pytest.mark.asyncio
async def test_add_subassembly_component_both_given_raises():
    with pytest.raises(ValueError, match="Exactly one"):
        await internal_engine.add_subassembly_component(
            "sub1", component_name="X", child_subassembly_name="Y"
        )


@pytest.mark.asyncio
async def test_add_subassembly_component_neither_given_raises():
    with pytest.raises(ValueError, match="Exactly one"):
        await internal_engine.add_subassembly_component("sub1")


@pytest.mark.asyncio
async def test_add_subassembly_component_direct_self_reference_raises_without_query():
    subassemblies_repo = MagicMock()
    subassemblies_repo.list.return_value = [{"id": "X", "description": "Assembly X"}]
    sac_repo = MagicMock()

    def repo_factory(table):
        return {"subassemblies": subassemblies_repo, "subassembly_components": sac_repo}[table]

    with patch.object(internal_engine, "Repository", side_effect=repo_factory):
        with pytest.raises(ValueError, match="circular"):
            await internal_engine.add_subassembly_component(
                "X", child_subassembly_name="Assembly X"
            )
    # Self-reference is caught before any subassembly_components query.
    sac_repo.list.assert_not_called()


@pytest.mark.asyncio
async def test_add_subassembly_component_transitive_cycle_guard():
    """A contains B (existing edge subassembly=A -> component_subassembly=B).

    Attempting to nest A as a child of B must raise: B is the parent, A is the
    proposed child, and BFS from A's own descendants immediately hits B (the
    existing A->B edge), proving the cycle would close.
    """
    subassemblies_repo = MagicMock()
    subassemblies_repo.list.return_value = [{"id": "A", "description": "Assembly A"}]
    sac_repo = MagicMock()

    def sac_list(active_only=True, filters=None):
        if filters and filters.get("subassembly") == "A":
            return [{"subassembly": "A", "component_subassembly": "B", "component": None}]
        return []

    sac_repo.list.side_effect = sac_list

    def repo_factory(table):
        return {"subassemblies": subassemblies_repo, "subassembly_components": sac_repo}[table]

    with patch.object(internal_engine, "Repository", side_effect=repo_factory):
        with pytest.raises(ValueError, match="circular"):
            await internal_engine.add_subassembly_component(
                "B", child_subassembly_name="Assembly A"
            )


# ── remove_subassembly_component / set_subassembly_component_qty ───────────


@pytest.mark.asyncio
async def test_remove_subassembly_component_happy_path():
    repo = MagicMock()
    repo.get.return_value = {"id": "sc1", "subassembly": "sub1"}
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.remove_subassembly_component("sc1")
    assert result["success"] is True
    assert result["removed_id"] == "sc1"
    repo.soft_delete.assert_called_once_with("sc1")


@pytest.mark.asyncio
async def test_remove_subassembly_component_not_found():
    repo = MagicMock()
    repo.get.return_value = None
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.remove_subassembly_component("missing")
    assert result["success"] is False
    repo.soft_delete.assert_not_called()


@pytest.mark.asyncio
async def test_set_subassembly_component_qty_happy_path():
    repo = MagicMock()
    repo.get.return_value = {"id": "sc1", "qty": 2}
    repo.update.return_value = {"id": "sc1", "qty": 9}
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.set_subassembly_component_qty("sc1", 9)
    assert result["success"] is True
    repo.update.assert_called_once_with("sc1", {"qty": 9})


@pytest.mark.asyncio
async def test_set_subassembly_component_qty_not_found():
    repo = MagicMock()
    repo.get.return_value = None
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.set_subassembly_component_qty("missing", 9)
    assert result["success"] is False


# ── duplicate_subassembly ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_subassembly_happy_path_copies_fields_and_children():
    source = {
        "id": "sub1",
        "description": "Original",
        "assembly_class": "Battery",
        "main_component": "comp1",
        "spec1_value": "5",
    }
    subassemblies_repo = MagicMock()
    subassemblies_repo.get.return_value = source
    subassemblies_repo.insert.side_effect = lambda row: row
    sac_repo = MagicMock()
    sac_repo.list.return_value = [
        {
            "id": "sc1",
            "subassembly": "sub1",
            "component": "comp1",
            "component_subassembly": None,
            "qty": 2,
        }
    ]
    sac_repo.insert.side_effect = lambda row: row

    def repo_factory(table):
        return {"subassemblies": subassemblies_repo, "subassembly_components": sac_repo}[table]

    with patch.object(internal_engine, "Repository", side_effect=repo_factory):
        result = await internal_engine.duplicate_subassembly("sub1", "Copy of Original")

    assert result["success"] is True
    new_sub = result["subassembly"]
    assert new_sub["id"] != "sub1"
    assert new_sub["description"] == "Copy of Original"
    assert new_sub["assembly_class"] == "Battery"
    assert new_sub["main_component"] == "comp1"
    assert result["components_copied"] == 1

    inserted_child = sac_repo.insert.call_args[0][0]
    assert inserted_child["id"] != "sc1"
    assert inserted_child["subassembly"] == new_sub["id"]
    assert inserted_child["component"] == "comp1"
    assert inserted_child["component_subassembly"] is None
    assert inserted_child["qty"] == 2


@pytest.mark.asyncio
async def test_duplicate_subassembly_source_not_found():
    subassemblies_repo = MagicMock()
    subassemblies_repo.get.return_value = None
    with patch.object(internal_engine, "Repository", return_value=subassemblies_repo):
        result = await internal_engine.duplicate_subassembly("missing", "Copy")
    assert result["success"] is False


# ── list_design_artifacts ────────────────────────────────────────────────────


def _artifact_entry(drive_file_id, *, stale=False, label=None):
    return {
        "drive_file_id": drive_file_id,
        "web_view_link": f"https://drive.google.com/file/d/{drive_file_id}/view",
        "created_at": "2026-07-01T00:00:00+00:00",
        "packet_id": "packet-1",
        "label": label,
        "mime_type": "image/png",
        "stale": stale,
    }


@pytest.mark.asyncio
async def test_list_design_artifacts_design_not_found():
    repo = MagicMock()
    repo.get.return_value = None
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.list_design_artifacts("missing")
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_list_design_artifacts_empty_artifacts():
    repo = MagicMock()
    repo.get.return_value = {"id": "d1", "artifacts": None}
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.list_design_artifacts("d1")
    assert result["success"] is True
    assert result["artifact_types"] == {}


@pytest.mark.asyncio
async def test_list_design_artifacts_multiple_types_and_versions():
    repo = MagicMock()
    repo.get.return_value = {
        "id": "d1",
        "artifacts": {
            "distribution_map": [_artifact_entry("f2"), _artifact_entry("f1")],
            "qgis_project": [_artifact_entry("q1")],
        },
    }
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.list_design_artifacts("d1")
    assert result["success"] is True
    dm = result["artifact_types"]["distribution_map"]
    assert dm["version_count"] == 2
    assert dm["latest"]["drive_file_id"] == "f2"
    qp = result["artifact_types"]["qgis_project"]
    assert qp["version_count"] == 1
    assert qp["latest"]["drive_file_id"] == "q1"


# ── get_design_artifact ──────────────────────────────────────────────────────


def _mock_drive_service(status_by_file_id=None, exception_by_file_id=None):
    """Build a MagicMock Drive service whose files().get(fileId=...).execute()
    either succeeds, raises HttpError(status), or raises an arbitrary exception,
    keyed by fileId."""
    status_by_file_id = status_by_file_id or {}
    exception_by_file_id = exception_by_file_id or {}

    service = MagicMock()

    def _execute_for(file_id):
        def _execute():
            if file_id in exception_by_file_id:
                raise exception_by_file_id[file_id]
            status = status_by_file_id.get(file_id)
            if status is not None and status != 200:
                resp = MagicMock()
                resp.status = status
                raise HttpError(resp, b'{"error": {"message": "not found"}}')
            return {"id": file_id}

        return _execute

    def _get(fileId, fields=None, supportsAllDrives=None):
        m = MagicMock()
        m.execute.side_effect = _execute_for(fileId)
        return m

    service.files.return_value.get.side_effect = _get
    return service


@pytest.mark.asyncio
async def test_get_design_artifact_version_zero_happy_path():
    repo = MagicMock()
    repo.get.return_value = {
        "id": "d1",
        "artifacts": {"distribution_map": [_artifact_entry("f1")]},
    }
    service = _mock_drive_service()
    with (
        patch.object(internal_engine, "Repository", return_value=repo),
        patch.object(internal_engine, "_get_readonly_drive_service", return_value=service),
    ):
        result = await internal_engine.get_design_artifact("d1", "distribution_map", 0)
    assert result["success"] is True
    assert result["version_index"] == 0
    assert result["entry"]["drive_file_id"] == "f1"


@pytest.mark.asyncio
async def test_get_design_artifact_version_out_of_range():
    repo = MagicMock()
    repo.get.return_value = {
        "id": "d1",
        "artifacts": {"distribution_map": [_artifact_entry("f1")]},
    }
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.get_design_artifact("d1", "distribution_map", 5)
    assert result["success"] is False
    assert "out of range" in result["error"]


@pytest.mark.asyncio
async def test_get_design_artifact_type_with_no_entries():
    repo = MagicMock()
    repo.get.return_value = {"id": "d1", "artifacts": {}}
    with patch.object(internal_engine, "Repository", return_value=repo):
        result = await internal_engine.get_design_artifact("d1", "distribution_map", 0)
    assert result["success"] is False
    assert "No artifacts of type" in result["error"]


@pytest.mark.asyncio
async def test_get_design_artifact_skips_already_stale_without_drive_call():
    repo = MagicMock()
    repo.get.return_value = {
        "id": "d1",
        "artifacts": {
            "distribution_map": [
                _artifact_entry("f2", stale=True),
                _artifact_entry("f1"),
            ]
        },
    }
    service = _mock_drive_service()
    with (
        patch.object(internal_engine, "Repository", return_value=repo),
        patch.object(internal_engine, "_get_readonly_drive_service", return_value=service),
    ):
        result = await internal_engine.get_design_artifact("d1", "distribution_map", 0)
    assert result["success"] is True
    assert result["version_index"] == 1
    assert result["entry"]["drive_file_id"] == "f1"
    # The already-stale entry must never trigger a Drive lookup.
    service.files.return_value.get.assert_called_once()
    assert service.files.return_value.get.call_args.kwargs["fileId"] == "f1"


@pytest.mark.asyncio
async def test_get_design_artifact_404_falls_through_and_marks_stale():
    repo = MagicMock()
    repo.get.return_value = {
        "id": "d1",
        "artifacts": {
            "distribution_map": [
                _artifact_entry("f2"),
                _artifact_entry("f1"),
            ]
        },
    }
    service = _mock_drive_service(status_by_file_id={"f2": 404})
    mark_stale_mock = MagicMock(return_value={"distribution_map": []})
    with (
        patch.object(internal_engine, "Repository", return_value=repo),
        patch.object(internal_engine, "_get_readonly_drive_service", return_value=service),
        patch.object(internal_engine.artifact_log, "mark_artifact_stale", mark_stale_mock),
    ):
        result = await internal_engine.get_design_artifact("d1", "distribution_map", 0)
    assert result["success"] is True
    assert result["version_index"] == 1
    assert result["entry"]["drive_file_id"] == "f1"
    mark_stale_mock.assert_called_once_with("d1", "distribution_map", "f2")


@pytest.mark.asyncio
async def test_get_design_artifact_non_404_stops_walk_without_marking_stale():
    repo = MagicMock()
    repo.get.return_value = {
        "id": "d1",
        "artifacts": {
            "distribution_map": [
                _artifact_entry("f2"),
                _artifact_entry("f1"),
            ]
        },
    }
    service = _mock_drive_service(exception_by_file_id={"f2": ConnectionError("network blip")})
    mark_stale_mock = MagicMock()
    with (
        patch.object(internal_engine, "Repository", return_value=repo),
        patch.object(internal_engine, "_get_readonly_drive_service", return_value=service),
        patch.object(internal_engine.artifact_log, "mark_artifact_stale", mark_stale_mock),
    ):
        result = await internal_engine.get_design_artifact("d1", "distribution_map", 0)
    assert result["success"] is False
    assert "try again shortly" in result["error"]
    mark_stale_mock.assert_not_called()
    # Must not have cascaded to check f1 after the non-404 failure on f2.
    service.files.return_value.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_design_artifact_all_versions_stale_or_unreachable():
    repo = MagicMock()
    repo.get.return_value = {
        "id": "d1",
        "artifacts": {
            "distribution_map": [
                _artifact_entry("f2", stale=True),
                _artifact_entry("f1", stale=True),
            ]
        },
    }
    service = _mock_drive_service()
    with (
        patch.object(internal_engine, "Repository", return_value=repo),
        patch.object(internal_engine, "_get_readonly_drive_service", return_value=service),
    ):
        result = await internal_engine.get_design_artifact("d1", "distribution_map", 0)
    assert result["success"] is False
    assert "all marked stale or unreachable" in result["error"]
    service.files.return_value.get.assert_not_called()
