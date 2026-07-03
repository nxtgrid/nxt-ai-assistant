"""BOM generation — Python port of Apps Script `copyDesignToBOM` / `addComponentsToItems`.

Recursively flattens a Design's (or Job's) subassemblies into component line items,
applies the cable-length quantity rules, the SPD-swap rule, contingency and costing,
then replaces the active BOM Items for that parent. Costs depend on
`components.projected_cost` (sourced from the external "Avg Cost List") and rentals on
`unit_rental_prices` (the external "Sizing DB"); where those are absent the figures
fall back to 0, exactly as the sheet formulas would.
"""

from __future__ import annotations

import math
import re

from shared.grid_design.data import group_by, index_by, load, num
from shared.grid_design.db import Repository
from shared.grid_design.exchange_rate import get_usd_to_ngn
from shared.grid_design.ids import new_id

_MM2 = re.compile(r"\b(\d+(?:\.\d+)?)\s*mm2\b", re.IGNORECASE)

# The exact SPD Type string that triggers the high->low surge-protector swap.
SPD_SWAP_TRIGGER = (
    "Use T2 type as T1+T2 Type due to Low (<=16 strikes per sq km per yr) lightning probability"
)
_SPD_NAMES = {
    "hiDC": "12.5kA Iimp 40kA Imax Surge Protective Device DC T1+T2 (in Cabin)",
    "loDC": "12.5kA Iimp 40kA Imax Surge Protective Device DC T2 type used as T1+T2 (in Cabin)",
    "hiAC1": "25kA Iimp 40kA Imax Surge Protective Device Single-Phase AC T1+T2 2-Poles with L-N and N-PE modules",
    "loAC1": "25kA Iimp 40kA Imax Surge Protective Device Single-Phase AC T2 type used as T1+T2 2-Poles with L-N and N-PE modules",
    "hiAC4": "25kA Iimp 40kA Imax Surge Protective Device Three-Phase AC T1+T2 4-Poles with L-N and N-PE modules",
    "loAC4": "25kA Iimp 40kA Imax Surge Protective Device Three-Phase AC T2 type used as T1+T2 4-Poles with L-N and N-PE modules",
}


def _component_flags(comp: dict) -> dict:
    name = (comp.get("name") or "").lower()
    m = _MM2.search(name)
    mm2 = float(m.group(1)) if m else None
    buried = ("rmoured" in name) and ("non-armo" not in name)
    comp["_is_service_drop"] = "service" in name
    comp["_is_pv_farm"] = buried and mm2 is not None and mm2 < 25
    comp["_is_ac_output"] = buried and mm2 is not None and mm2 >= 25
    return comp


def _add_components(
    sa_components,
    parent_qty,
    items,
    subassembly_map,
    subcomp_by_component,
    component_map,
    design,
    inherited_types="",
):
    for sa in sa_components:
        comp_id = sa.get("component")
        comp = component_map.get(comp_id)
        if not comp:
            continue
        if comp.get("_is_pv_farm"):
            sa_qty = num(design.get("avg_distance_to_pv_combiner"))
        elif comp.get("_is_ac_output"):
            sa_qty = num(design.get("distance_to_feeder_pillar"))
        elif comp.get("_is_service_drop"):
            sa_qty = num(design.get("average_service_drop_length_m"))
        else:
            sa_qty = num(sa.get("qty"))

        sa_name = "Unknown"
        design_types = inherited_types
        if sa.get("subassembly"):
            s = subassembly_map.get(sa["subassembly"])
            if s:
                sa_name = s.get("description") or "Unknown"
                design_types = s.get("design_types") or ""
        elif sa.get("component_subassembly"):
            p = component_map.get(sa["component_subassembly"])
            if p:
                sa_name = p.get("name") or "Unknown"

        children = subcomp_by_component.get(comp_id)
        if children:
            _add_components(
                children,
                parent_qty * sa_qty,
                items,
                subassembly_map,
                subcomp_by_component,
                component_map,
                design,
                design_types,
            )
        else:
            aggregatable = (
                sa.get("source") != "Temporary Use" and sa.get("component_type") != "Tools"
            )
            final_qty = parent_qty * sa_qty if aggregatable else sa_qty
            if comp_id in items:
                ex = items[comp_id]
                if aggregatable:
                    ex["qty"] += final_qty
                ex["subassembly"] += " | " + sa_name
                if design_types:
                    ex["design_types"] += " | " + design_types
            else:
                items[comp_id] = {
                    "component": comp_id,
                    "subassembly": sa_name,
                    "qty": final_qty,
                    "design_types": design_types,
                }


def generate_bom(parent_id: str, bom_type: str = "Design", recompute_costs: bool = True) -> dict:
    """Regenerate BOM Items for a Design (or Job). Returns a summary dict.

    By default recomputes component costs from the purchase ledger first, so the
    generated BOM is priced just-in-time off the latest purchases (and thereby
    acts as the priced audit snapshot).
    """
    parent_key = "design" if bom_type == "Design" else "job"

    if recompute_costs:
        from shared.grid_design.cost_projection import recompute_component_costs

        recompute_component_costs()

    # Catalogue tables are loaded whole (the recursive traversal touches most
    # of them); the parent design/job and its subassemblies are fetched targeted.
    components = {c["id"]: _component_flags(c) for c in load("components")}
    subassemblies = index_by(load("subassemblies"), "id")
    sub_components = [c for c in load("subassembly_components")]
    rentals = load("unit_rental_prices")

    rental_map = {
        r.get("engineering_item_name"): num(r.get("unit_monthly_rental")) for r in rentals
    }

    subcomp_by_component = group_by(
        [c for c in sub_components if c.get("active") and c.get("component_subassembly")],
        "component_subassembly",
    )
    components_by_subassembly = group_by(
        [c for c in sub_components if c.get("subassembly")], "subassembly"
    )

    parent_table = "designs" if bom_type == "Design" else "jobs"
    parent = Repository(parent_table).get(parent_id)
    if not parent:
        return {"ok": False, "error": f"{bom_type} {parent_id} not found"}

    rate = num(parent.get("usd_to_ngn")) or (get_usd_to_ngn() or 0.0)

    subs_table = "design_subassemblies" if bom_type == "Design" else "job_subassemblies"
    traverse = Repository(subs_table).list(active_only=True, filters={parent_key: parent_id})

    items: dict[str, dict] = {}
    for ts in traverse:
        comps = components_by_subassembly.get(ts.get("subassembly"), [])
        if comps:
            _add_components(
                comps,
                num(ts.get("qty")),
                items,
                subassemblies,
                subcomp_by_component,
                components,
                parent,
            )

    # SPD swap map
    spd_map = None
    if bom_type == "Design" and parent.get("spd_type") == SPD_SWAP_TRIGGER:
        by_name = {c.get("name"): cid for cid, c in components.items()}
        n = _SPD_NAMES
        if n["hiDC"] in by_name and n["loDC"] in by_name:
            spd_map = {
                by_name[n["hiDC"]]: by_name[n["loDC"]],
                by_name.get(n["hiAC1"]): by_name.get(n["loAC1"]),
                by_name.get(n["hiAC4"]): by_name.get(n["loAC4"]),
            }

    rows = []
    total_cost = 0.0
    for item in items.values():
        comp_id = item["component"]
        if spd_map and comp_id in spd_map and spd_map[comp_id]:
            comp_id = spd_map[comp_id]
        comp = components.get(comp_id)
        if not comp:
            continue
        unit_cost_ngn = round(100.0 * num(comp.get("projected_cost")) * rate) / 100.0
        qty_contingent = math.ceil(item["qty"] * (1 + num(comp.get("contingency_pct"))))
        unit_rental = rental_map.get(comp.get("name"), 0.0)
        total_cost_ngn = unit_cost_ngn * item["qty"]
        total_cost += total_cost_ngn
        rows.append(
            {
                "id": new_id(),
                "item": comp_id,
                "qty": item["qty"],
                "qty_with_contingency": qty_contingent,
                "design": parent_id if bom_type == "Design" else None,
                "job": parent_id if bom_type == "Job" else None,
                "subassembly": item["subassembly"],
                "design_types": item["design_types"],
                "unit_cost_ngn": unit_cost_ngn,
                "total_cost_ngn": total_cost_ngn,
                "monthly_rental_usd": item["qty"] * unit_rental,
                "source": comp.get("source"),
                "who_procures": comp.get("who_procures"),
                "active": True,
            }
        )

    # Replace existing active BOM items for this parent (AppSheet "Delete existing BOMs")
    repo = Repository("bom_items")
    existing = repo.list(active_only=True, filters={parent_key: parent_id})
    for ex in existing:
        repo.soft_delete(ex["id"])
    repo.upsert(rows)

    # Stamp the design with generation time + rolled-up cost
    if bom_type == "Design":
        from datetime import datetime, timezone

        Repository("designs").update(
            parent_id,
            {
                "bom_generated_at": datetime.now(timezone.utc).isoformat(),
                "bom_cost_estimate": round(total_cost, 2),
            },
        )

    return {
        "ok": True,
        "items": len(rows),
        "total_cost_ngn": round(total_cost, 2),
        "exchange_rate": rate,
    }
