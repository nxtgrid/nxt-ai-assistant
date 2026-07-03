"""Internal (Chat DB) backend for the grid_design MCP server.

Runs the design/BOM workflow against the shared engine (``shared/grid_design``,
ported from AppSheet's Apps Script) and the ``gd_*`` tables instead of the
AppSheet REST API. Response payloads keep the exact contract the AppSheet
workflow returned — AppSheet-style BOM row keys, ``energy_specs``,
``cost_summary``, ``output.design_parameters`` and ``design.Id``/``Name``
aliases — so the LPP package handlers work unchanged.

The engine is synchronous (supabase-py); every public function here wraps the
sync core in ``asyncio.to_thread`` to keep the event loop free.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from shared.grid_design import design_writer
from shared.grid_design.auto_designer import auto_design
from shared.grid_design.bom_generator import generate_bom
from shared.grid_design.data import num
from shared.grid_design.db import Repository

# Accepted update_design keys (AppSheet-era labels, MCP arg names, and internal
# column names) -> gd_designs column. Mirrors the AppSheet path's
# ALLOWED_UPDATE_COLUMNS whitelist: only layout-derived distances are editable.
_UPDATE_COLUMN_ALIASES: Dict[str, str] = {
    "Avg Distance to PV Combiner (m)": "avg_distance_to_pv_combiner",
    "avg_distance_to_pv_combiner_m": "avg_distance_to_pv_combiner",
    "avg_distance_to_pv_combiner": "avg_distance_to_pv_combiner",
    "Distance to Feeder Pillar (m)": "distance_to_feeder_pillar",
    "distance_to_feeder_pillar_m": "distance_to_feeder_pillar",
    "distance_to_feeder_pillar": "distance_to_feeder_pillar",
    "Average Service Drop Length (m)": "average_service_drop_length_m",
    "avg_service_drop_length_m": "average_service_drop_length_m",
    "average_service_drop_length_m": "average_service_drop_length_m",
}


# ── Shaping helpers ──────────────────────────────────────────────────────────


def _with_appsheet_aliases(row: Optional[dict]) -> dict:
    """Add the AppSheet ``Id``/``Name`` keys handlers read from grid/design rows."""
    row = dict(row or {})
    if "id" in row:
        row.setdefault("Id", row["id"])
    if "name" in row:
        row.setdefault("Name", row["name"])
    return row


def _shape_bom_rows(rows: List[dict], components: Dict[str, dict]) -> List[dict]:
    """gd_bom_items rows -> AppSheet-style BOM dicts the LPP handlers consume.

    Cost formulas mirror the AppSheet virtual columns (see
    anansi_app/grid_app/entities/virtual.py): line cost = qty-with-contingency x
    per-unit component cost, Tools always 0.
    """
    shaped: List[dict] = []
    for row in rows:
        comp = components.get(str(row.get("item"))) or {}
        ctype = str(comp.get("component_type") or "")
        is_tools = "tools" in ctype.lower()
        qty = num(row.get("qty"))
        qty_c = num(row.get("qty_with_contingency")) or qty
        projected = 0 if is_tools else round(qty_c * num(comp.get("projected_cost")), 2)
        ddp = 0 if is_tools else round(qty_c * num(comp.get("ddp_cost")), 2)
        shaped.append(
            {
                "Id": row.get("id"),
                "Item": row.get("item"),
                "Item Name": comp.get("name", ""),
                "Component Type": ctype,
                "Counting Unit": comp.get("counting_unit", ""),
                "Qty": qty,
                "Qty With Contingency": qty_c,
                "Projected Cost with contingency": projected,
                "DDP Cost with contingency": ddp,
                "Subassembly": row.get("subassembly", ""),
                "Unit Cost NGN": row.get("unit_cost_ngn"),
                "Total Cost NGN": row.get("total_cost_ngn"),
                "Monthly Rental USD": row.get("monthly_rental_usd"),
            }
        )
    return shaped


def _count_class(subs: List[dict], needle: str, exact: bool = False) -> Any:
    def matches(s: dict) -> bool:
        cls = str(s.get("class") or "").lower()
        return cls == needle if exact else needle in cls

    total = sum(num(s.get("qty")) for s in subs if matches(s))
    return int(total) if total else ""


def _energy_specs(design: dict, subs: List[dict]) -> dict:
    """Energy specs in the shape the AppSheet path emitted.

    kWp/kWh/kVA come straight off the design row (written by auto_design);
    inverter/battery counts are derived from the design subassembly classes.
    Subsystem/panel counts were never populated by the AppSheet path either.
    """
    return {
        "total_kwp": design.get("kwp", ""),
        "total_kwh": design.get("kwh", ""),
        "total_kva": design.get("kva", ""),
        "num_subsystems": "",
        # Live assembly classes: batteries are exactly "Battery"; inverter
        # classes are "Inverter Charger" / "PV Inverter (+Panels)" / etc.
        "num_inverters": _count_class(subs, "nverter"),
        "num_batteries": _count_class(subs, "battery", exact=True),
        "num_panels": "",
    }


def _design_parameters(design: dict) -> dict:
    """Design parameters block (superset of what the AppSheet path emitted)."""
    return {
        "inverter_type": design.get("inverter_type", ""),
        "battery_type": design.get("battery_type", ""),
        "mppt_type": design.get("mppt_type", ""),
        "pv_type": design.get("pv_type", ""),
        "pv_inverter_type": design.get("pv_inverter_type", ""),
        "max_connections": design.get("max_connections", ""),
        "residential_connections": design.get("initial_residential_connections", ""),
        "business_connections": design.get("initial_business_connections", ""),
        "three_phase_connections": design.get("initial_3_phase_connections", ""),
        "wp_per_connection": design.get("wp_per_conn_override", ""),
        "pv_area_m2": design.get("pv_area_sqm", ""),
        "regulation_constraint": design.get("constrain_design_to_known_regulation", ""),
        "force_3phase": design.get("force_3_phase", ""),
        "anchor_load_kw": design.get("anchor_load_kw", ""),
        "pue_hours_per_day": design.get("pue_hours_per_day", ""),
        "target_tariff_usd": design.get("target_tariff_usd", ""),
        "spd_type": design.get("spd_type", ""),
        "phases": design.get("phases", ""),
        "avg_service_drop_length_m": design.get("average_service_drop_length_m", ""),
        "avg_distance_to_pv_combiner_m": design.get("avg_distance_to_pv_combiner", ""),
        "distance_to_feeder_pillar_m": design.get("distance_to_feeder_pillar", ""),
        "number_of_subsystems": "",
        "subsystem_size_kva": "",
    }


def compute_bom_cost_summary(bom_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute cost summary by component type groups.

    Groups:
    - Main Energy Asset: Items with Component Type containing "Main Energy Asset"
    - Metering: Items with Component Type containing "Metering"
    - BoS (Balance of System): All other items

    Excludes: Items with Component Type = "Tools"

    Used by both the internal and AppSheet backends (rows carry the same
    AppSheet-style keys either way).

    Returns:
        Dict with cost totals per group and item counts
    """
    costs = {
        "main_energy_asset": 0.0,
        "metering": 0.0,
        "bos": 0.0,
    }
    counts = {
        "main_energy_asset": 0,
        "metering": 0,
        "bos": 0,
        "tools_excluded": 0,
    }

    for item in bom_items:
        component_type = str(item.get("Component Type", "")).strip()

        # Skip Tools
        if "tools" in component_type.lower():
            counts["tools_excluded"] += 1
            continue

        # Parse the line total (handle empty strings and currency formatting)
        est_cost_str = (
            str(item.get("Projected Cost with contingency", "0")).replace(",", "").strip()
        )
        try:
            item_total = float(est_cost_str) if est_cost_str else 0.0
        except ValueError:
            item_total = 0.0

        if "main energy asset" in component_type.lower():
            costs["main_energy_asset"] += item_total
            counts["main_energy_asset"] += 1
        elif "metering" in component_type.lower():
            costs["metering"] += item_total
            counts["metering"] += 1
        else:
            costs["bos"] += item_total
            counts["bos"] += 1

    total_cost = costs["main_energy_asset"] + costs["metering"] + costs["bos"]

    return {
        "main_energy_asset_cost": round(costs["main_energy_asset"], 2),
        "metering_cost": round(costs["metering"], 2),
        "bos_cost": round(costs["bos"], 2),
        "total_cost": round(total_cost, 2),
        "item_counts": counts,
    }


def _load_design_context(design_id: str) -> tuple[dict, List[dict]]:
    design = Repository("designs").get(design_id) or {}
    subs = Repository("design_subassemblies").list(active_only=True, filters={"design": design_id})
    return design, subs


def _load_shaped_bom(design_id: str) -> List[dict]:
    rows = Repository("bom_items").list(active_only=True, filters={"design": design_id})
    components = Repository("components").get_many([str(r.get("item")) for r in rows])
    return _shape_bom_rows(rows, components)


# ── Sync workflow cores ──────────────────────────────────────────────────────


def _design_and_bom_sync(args: Dict[str, Any]) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    result: Dict[str, Any] = {
        "steps": steps,
        "grid": None,
        "design": None,
        "bom": [],
        "success": False,
        "backend": "internal",
    }

    grid_name = args["grid_name"]
    grid = design_writer.find_grid_by_name(grid_name)
    if grid:
        steps.append({"step": "find_grid", "status": "found", "grid_id": grid.get("id")})
    else:
        grid = design_writer.create_grid(grid_name, args.get("community"))
        steps.append({"step": "create_grid", "status": "created", "grid_id": grid.get("id")})
    result["grid"] = _with_appsheet_aliases(grid)

    design = design_writer.create_design(dict(args), grid["id"])
    design_id = str(design["id"])
    steps.append({"step": "create_design", "status": "created", "design_id": design_id})

    if args.get("auto_design", True):
        ad = auto_design(design_id)
        if not ad.get("ok"):
            result["error"] = str(ad.get("error", "Auto-design failed"))
            steps.append({"step": "auto_design", "status": "failed", "reason": result["error"]})
            result["design"] = _with_appsheet_aliases(design)
            return result
        steps.append(
            {
                "step": "auto_design",
                "status": "completed",
                "subassemblies": ad.get("subassemblies"),
                "kwp": ad.get("kwp"),
                "kwh": ad.get("kwh"),
                "kva": ad.get("kva"),
            }
        )

    design, subs = _load_design_context(design_id)
    design = _with_appsheet_aliases(design)
    result["design"] = design
    energy_specs = _energy_specs(design, subs)
    result["energy_specs"] = energy_specs
    result["output"] = {
        "energy_specs": energy_specs,
        "design_id": design_id,
        "design_name": design.get("name", ""),
        "grid_name": grid_name,
        "design_parameters": _design_parameters(design),
    }

    if args.get("wait_for_bom", True):
        bom_res = generate_bom(design_id)
        if not bom_res.get("ok"):
            result["error"] = str(bom_res.get("error", "BOM generation failed"))
            steps.append({"step": "generate_bom", "status": "failed", "reason": result["error"]})
            return result
        steps.append(
            {
                "step": "generate_bom",
                "status": "completed",
                "items": bom_res.get("items"),
                "total_cost_ngn": bom_res.get("total_cost_ngn"),
            }
        )
        shaped = _load_shaped_bom(design_id)
        result["bom"] = shaped
        cost_summary = compute_bom_cost_summary(shaped)
        result["cost_summary"] = cost_summary
        result["output"]["cost_summary"] = cost_summary
    else:
        steps.append({"step": "skip_bom", "status": "skipped", "reason": "wait_for_bom=False"})

    result["success"] = True
    return result


def _trigger_bom_sync(design_id: str, grid_name: str) -> Dict[str, Any]:
    design = Repository("designs").get(design_id)
    if not design:
        return {"success": False, "error": f"Design {design_id} not found", "backend": "internal"}

    bom_res = generate_bom(design_id)
    if not bom_res.get("ok"):
        return {
            "success": False,
            "error": str(bom_res.get("error", "BOM generation failed")),
            "design_id": design_id,
            "backend": "internal",
        }

    design, subs = _load_design_context(design_id)
    design = _with_appsheet_aliases(design)
    shaped = _load_shaped_bom(design_id)
    cost_summary = compute_bom_cost_summary(shaped)
    energy_specs = _energy_specs(design, subs)

    return {
        "success": True,
        "backend": "internal",
        "design": design,
        "bom": shaped,
        "cost_summary": cost_summary,
        "energy_specs": energy_specs,
        "output": {
            "design_parameters": _design_parameters(design),
            "energy_specs": energy_specs,
            "cost_summary": cost_summary,
            "design_id": design_id,
            "design_name": design.get("name", ""),
            "grid_name": grid_name,
        },
    }


def _update_design_sync(design_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    mapped: Dict[str, Any] = {}
    disallowed = []
    for key, value in updates.items():
        col = _UPDATE_COLUMN_ALIASES.get(key)
        if col is None:
            disallowed.append(key)
        else:
            mapped[col] = value
    if disallowed:
        raise ValueError(f"Cannot update columns: {set(disallowed)}")
    if not mapped:
        raise ValueError(
            "No columns to update — allowed: "
            + ", ".join(sorted(set(_UPDATE_COLUMN_ALIASES.values())))
        )

    updated = Repository("designs").update(design_id, mapped)
    return {"success": True, "backend": "internal", "updated": updated}


def _get_design_bom_sync(design_id: str) -> Dict[str, Any]:
    shaped = _load_shaped_bom(design_id)
    return {
        "success": True,
        "backend": "internal",
        "bom_items": shaped,
        "count": len(shaped),
    }


def _find_grid_sync(grid_name: str) -> Dict[str, Any]:
    grid = design_writer.find_grid_by_name(grid_name)
    if not grid:
        return {"success": False, "error": "Grid not found", "backend": "internal"}
    return {"success": True, "backend": "internal", "grid": _with_appsheet_aliases(grid)}


def _list_design_options_sync() -> Dict[str, Any]:
    """Valid technology choices and form defaults for interactive design creation.

    Technology enums come from gd_unit_rental_prices (the same source the
    AppSheet form's dropdowns used); each entry carries the subassembly classes
    of its engineering component so callers can tell inverters, batteries,
    MPPTs and panels apart.
    """
    rentals = Repository("unit_rental_prices").list(active_only=False)
    components = Repository("components").list(active_only=True)
    subassemblies = Repository("subassemblies").list(active_only=True)

    comp_by_name = {c.get("name"): c for c in components}
    classes_by_comp: Dict[str, set] = {}
    for sub in subassemblies:
        for comp_id in str(sub.get("main_component") or "").split(","):
            comp_id = comp_id.strip()
            if comp_id and sub.get("assembly_class"):
                classes_by_comp.setdefault(comp_id, set()).add(sub["assembly_class"])

    technology_options = []
    for rental in rentals:
        comp = comp_by_name.get(rental.get("engineering_item_name")) or {}
        technology_options.append(
            {
                "type_name": rental.get("item"),
                "engineering_item_name": rental.get("engineering_item_name"),
                "component_type": comp.get("component_type", ""),
                "assembly_classes": sorted(classes_by_comp.get(str(comp.get("id")), [])),
                "unit_monthly_rental_usd": rental.get("unit_monthly_rental"),
            }
        )

    return {
        "success": True,
        "backend": "internal",
        "technology_options": technology_options,
        "spd_type_options": design_writer.SPD_OPTIONS,
        "regulation_constraint_options": design_writer.REGULATION_OPTIONS,
        "form_defaults": dict(design_writer.FORM_DEFAULTS),
    }


# ── Async API (event-loop safe) ──────────────────────────────────────────────


async def design_and_bom(args: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(_design_and_bom_sync, args)


async def trigger_bom(design_id: str, grid_name: str = "") -> Dict[str, Any]:
    return await asyncio.to_thread(_trigger_bom_sync, design_id, grid_name)


async def update_design(design_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(_update_design_sync, design_id, updates)


async def get_design_bom(design_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_get_design_bom_sync, design_id)


async def find_grid(grid_name: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_find_grid_sync, grid_name)


async def list_design_options() -> Dict[str, Any]:
    return await asyncio.to_thread(_list_design_options_sync)
