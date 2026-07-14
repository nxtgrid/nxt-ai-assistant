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
import functools
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from shared.grid_design import artifact_log, design_writer
from shared.grid_design.auto_designer import (
    _build_ds_row,
    auto_design,
    normalize_design_family,
    subassembly_supports_design_family,
)
from shared.grid_design.bom_generator import generate_bom
from shared.grid_design.data import load, num
from shared.grid_design.db import Repository
from shared.grid_design.ids import new_id
from shared.utils.google_auth import get_drive_credentials
from shared.utils.grid_matcher import find_best_grid_match
from shared.utils.logging import get_logger

logger = get_logger(__name__)

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

_TECHNOLOGY_FAMILY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "victron": {
        "inverter_type": "Quattro 15kVA",
        "battery_type": "Pylontech UP5000",
        "mppt_type": "Victron 250/85 MPPT",
        "pv_type": "JA455W Panel",
        "pv_inverter_type": None,
    },
    "deye": {
        "inverter_type": "Deye SUN-30K",
        "battery_type": "Deye LV Bat 5kWh",
        # Kept for compatibility with existing design forms; Deye auto-design
        # selects the family-compatible solar-array subassembly by design_types.
        "mppt_type": "Victron 250/85 MPPT",
        "pv_type": "JA455W Panel",
        "pv_inverter_type": None,
    },
}

_TECHNOLOGY_SITE_LAYOUT_TYPE = {"victron": "victron", "deye": "ess"}


def _pop_technology_family(updates: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str]]:
    remaining = dict(updates or {})
    raw_family = None
    for key in ("technology_family", "design_family", "technology_type"):
        if key in remaining:
            raw_family = remaining.pop(key)
            break
    family = normalize_design_family(raw_family) if raw_family else None
    if family:
        defaults = dict(_TECHNOLOGY_FAMILY_DEFAULTS[family])
        defaults.update(remaining)
        return defaults, family
    return remaining, None


def _auto_design_with_family(design_id: str, family: Optional[str]):
    if family:
        return auto_design(design_id, technology_family=family)
    return auto_design(design_id)


def _rollback_updates(
    design_id: str, original: Optional[dict], changed_columns: Dict[str, Any]
) -> None:
    if not original or not changed_columns:
        return
    rollback = {column: original.get(column) for column in changed_columns}
    Repository("designs").update(design_id, rollback)


def _snapshot_design_subassemblies(design_id: str) -> List[dict]:
    return Repository("design_subassemblies").list(active_only=True, filters={"design": design_id})


def _restore_design_subassemblies(design_id: str, original_rows: Optional[List[dict]]) -> None:
    if original_rows is None:
        return
    repo = Repository("design_subassemblies")
    for row in repo.list(active_only=True, filters={"design": design_id}):
        repo.soft_delete(row["id"])
    if original_rows:
        repo.upsert([{**row, "active": True} for row in original_rows])


def _apply_technology_family_defaults(args: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str]]:
    family_raw = args.get("technology_family")
    if not family_raw:
        return dict(args), None
    family = normalize_design_family(family_raw)
    merged = dict(_TECHNOLOGY_FAMILY_DEFAULTS[family])
    merged.update(args)
    merged["technology_family"] = family
    return merged, family


def _translate_design_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Map a caller-supplied {alias_or_field: value} dict to {gd_designs column: value}.

    Combines the legacy `_UPDATE_COLUMN_ALIASES` (kept as-is for backward
    compatibility with the 3 distance-column aliases) with every entry of
    `design_writer._DESIGN_FIELD_MAP`, EXCEPT "created_by" — that column is
    server-managed (who created the design) and must never be user-editable
    through this whitelist, under any alias.
    """
    combined_map: Dict[str, str] = dict(_UPDATE_COLUMN_ALIASES)
    for field, column in design_writer._DESIGN_FIELD_MAP.items():
        if field == "created_by":
            continue
        combined_map[field] = column

    if not updates:
        raise ValueError(
            "No columns to update — allowed: " + ", ".join(sorted(set(combined_map.values())))
        )

    mapped: Dict[str, Any] = {}
    disallowed = []
    for key, value in updates.items():
        col = combined_map.get(key)
        if col is None:
            disallowed.append(key)
        else:
            mapped[col] = value
    if disallowed:
        raise ValueError(f"Cannot update columns: {set(disallowed)}")
    return mapped


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
    args, family = _apply_technology_family_defaults(args)
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
        ad = _auto_design_with_family(design_id, family)
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


def _has_manual_subassembly_edits(design_id: str) -> bool:
    """True if any active subassembly on this design was hand-edited.

    Re-running auto-design replaces ALL subassemblies on a design, so callers
    that want to rerun sizing must check this first (see `_update_design_sync`
    and `_run_auto_design_sync`) to avoid silently discarding manual edits.
    Kept standalone (not inlined) since a later task reuses it for
    subassembly add/remove tools.
    """
    rows = Repository("design_subassemblies").list(
        active_only=True, filters={"design": design_id, "manually_edited": True}
    )
    return bool(rows)


def _update_design_sync(
    design_id: str,
    updates: Dict[str, Any],
    *,
    rerun_auto_design: bool = False,
    regenerate_bom: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Update gd_designs columns and optionally rerun sizing / regenerate the BOM.

    If a downstream auto-design/BOM run fails after parameter updates were
    written, the changed columns are restored so the design is not left with
    Deye/Victron fields that disagree with its active subassemblies and BOM.
    """
    updates, family = _pop_technology_family(updates)
    mapped: Dict[str, Any] = {}
    updated = None
    if updates:
        mapped = _translate_design_updates(updates)
    elif not rerun_auto_design and not regenerate_bom:
        # Nothing to change and nothing else requested — mirror the old
        # "no columns to update" error rather than silently no-op'ing.
        _translate_design_updates(updates)  # raises ValueError with the allowed-list message

    if rerun_auto_design:
        if not force and _has_manual_subassembly_edits(design_id):
            message = (
                f"Design {design_id} has manually-edited subassemblies; re-running "
                "auto-design would replace ALL of them (including your edits). "
                "Pass force=true to proceed anyway."
            )
            raise ValueError(message)

    original = (
        Repository("designs").get(design_id)
        if mapped and (rerun_auto_design or regenerate_bom)
        else None
    )
    original_subassemblies = (
        _snapshot_design_subassemblies(design_id) if rerun_auto_design else None
    )
    if mapped:
        updated = Repository("designs").update(design_id, mapped)

    try:
        if rerun_auto_design:
            result = _auto_design_with_family(design_id, family)
            if not result.get("ok"):
                raise ValueError(str(result.get("error", "Auto-design failed")))

        if regenerate_bom:
            bom_result = generate_bom(design_id)
            if not bom_result.get("ok"):
                raise ValueError(str(bom_result.get("error", "BOM generation failed")))
    except Exception:
        _rollback_updates(design_id, original, mapped)
        _restore_design_subassemblies(design_id, original_subassemblies)
        raise

    return {
        "success": True,
        "backend": "internal",
        "updated": updated,
        "auto_design_reran": rerun_auto_design,
        "bom_regenerated": regenerate_bom,
    }


def _get_design_sync(design_id: str) -> Dict[str, Any]:
    """Current parameter values + energy specs for a design.

    Lets a caller (a future MCP tool) show the LLM the current state of a
    design before it proposes a change — e.g. before "change Wp/conn to 150,"
    the LLM calls this to see the design currently has wp_per_connection: 120.
    """
    design, subs = _load_design_context(design_id)
    if not design:
        return {"success": False, "error": f"Design {design_id} not found", "backend": "internal"}
    design = _with_appsheet_aliases(design)
    energy_specs = _energy_specs(design, subs)
    return {
        "success": True,
        "backend": "internal",
        "design": design,
        "energy_specs": energy_specs,
        "design_parameters": _design_parameters(design),
    }


def _get_design_bom_sync(design_id: str) -> Dict[str, Any]:
    shaped = _load_shaped_bom(design_id)
    return {
        "success": True,
        "backend": "internal",
        "bom_items": shaped,
        "count": len(shaped),
    }


@functools.lru_cache(maxsize=1)
def _get_readonly_drive_service():
    """Cached, read-only Drive v3 service (built once per process).

    Deliberately separate from `shared/utils/drive_upload.py`'s `_get_service()`,
    which is write-scoped (``drive`` + ``documents`` OAuth scopes). This module
    only ever checks whether a file still exists on Drive, so it uses
    `get_drive_credentials()` (read-only scopes) instead — reusing the
    write-scoped cached service here would silently grant this read-only
    code path write credentials it doesn't need.
    """
    return build("drive", "v3", credentials=get_drive_credentials())


def _list_design_artifacts_sync(design_id: str) -> Dict[str, Any]:
    """Summary of all artifact types on a design: type -> version count, latest entry."""
    design = Repository("designs").get(design_id)
    if not design:
        return {"success": False, "error": f"Design {design_id} not found", "backend": "internal"}

    artifacts: Dict[str, List[dict]] = design.get("artifacts") or {}
    return {
        "success": True,
        "backend": "internal",
        "artifact_types": {
            artifact_type: {
                "version_count": len(versions),
                "latest": versions[0] if versions else None,
            }
            for artifact_type, versions in artifacts.items()
        },
    }


def _get_design_artifact_sync(
    design_id: str, artifact_type: str, version: int = 0
) -> Dict[str, Any]:
    """Return one artifact version, verifying Drive availability; falls through stale entries.

    ``version`` is a 0-based index into the newest-first version list (0 =
    latest). Starting at that index, walks FORWARD (toward older entries)
    until it finds a version that is actually available on Drive, marking
    any confirmed-gone entry stale as it goes via `artifact_log.mark_artifact_stale`.
    """
    design = Repository("designs").get(design_id)
    if not design:
        return {"success": False, "error": f"Design {design_id} not found", "backend": "internal"}

    versions: List[dict] = (design.get("artifacts") or {}).get(artifact_type, [])
    if not versions:
        return {
            "success": False,
            "error": f"No artifacts of type '{artifact_type}' found for design {design_id}",
            "backend": "internal",
        }

    if version < 0 or version >= len(versions):
        return {
            "success": False,
            "error": (
                f"version {version} is out of range for artifact_type '{artifact_type}' "
                f"on design {design_id} (has {len(versions)} version(s), valid range "
                f"0-{len(versions) - 1})"
            ),
            "backend": "internal",
        }

    service = _get_readonly_drive_service()

    for index in range(version, len(versions)):
        entry = versions[index]

        if entry.get("stale"):
            continue

        drive_file_id = entry.get("drive_file_id")
        try:
            service.files().get(fileId=drive_file_id, fields="id", supportsAllDrives=True).execute()
        except HttpError as e:
            if getattr(e.resp, "status", None) == 404:
                logger.warning(
                    "get_design_artifact: drive_file_id=%s (design_id=%s, "
                    "artifact_type=%s, version=%s) is gone from Drive; marking stale",
                    drive_file_id,
                    design_id,
                    artifact_type,
                    index,
                )
                artifact_log.mark_artifact_stale(design_id, artifact_type, drive_file_id)
                continue
            logger.warning(
                "get_design_artifact: non-404 Drive error checking drive_file_id=%s "
                "(design_id=%s, artifact_type=%s, version=%s); stopping walk",
                drive_file_id,
                design_id,
                artifact_type,
                index,
                exc_info=True,
            )
            return {
                "success": False,
                "error": "Could not verify artifact availability right now, try again shortly.",
                "backend": "internal",
            }
        except Exception:
            logger.warning(
                "get_design_artifact: unexpected error checking drive_file_id=%s "
                "(design_id=%s, artifact_type=%s, version=%s); stopping walk",
                drive_file_id,
                design_id,
                artifact_type,
                index,
                exc_info=True,
            )
            return {
                "success": False,
                "error": "Could not verify artifact availability right now, try again shortly.",
                "backend": "internal",
            }

        return {
            "success": True,
            "backend": "internal",
            "artifact_type": artifact_type,
            "version_index": index,
            "entry": entry,
        }

    return {
        "success": False,
        "error": (
            f"No available versions of '{artifact_type}' found for design {design_id} "
            "(all marked stale or unreachable)."
        ),
        "backend": "internal",
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
    families_by_comp: Dict[str, set] = {}
    for sub in subassemblies:
        for comp_id in str(sub.get("main_component") or "").split(","):
            comp_id = comp_id.strip()
            if comp_id and sub.get("assembly_class"):
                classes_by_comp.setdefault(comp_id, set()).add(sub["assembly_class"])
            if comp_id:
                for family in _TECHNOLOGY_FAMILY_DEFAULTS:
                    if subassembly_supports_design_family(sub, family):
                        families_by_comp.setdefault(comp_id, set()).add(family)

    technology_options = []
    for rental in rentals:
        comp = comp_by_name.get(rental.get("engineering_item_name")) or {}
        technology_options.append(
            {
                "type_name": rental.get("item"),
                "engineering_item_name": rental.get("engineering_item_name"),
                "component_type": comp.get("component_type", ""),
                "assembly_classes": sorted(classes_by_comp.get(str(comp.get("id")), [])),
                "technology_families": sorted(families_by_comp.get(str(comp.get("id")), [])),
                "unit_monthly_rental_usd": rental.get("unit_monthly_rental"),
            }
        )

    return {
        "success": True,
        "backend": "internal",
        "technology_options": technology_options,
        "technology_families": _build_design_technology_families(technology_options, subassemblies),
        "spd_type_options": design_writer.SPD_OPTIONS,
        "regulation_constraint_options": design_writer.REGULATION_OPTIONS,
        "form_defaults": dict(design_writer.FORM_DEFAULTS),
    }


def _build_design_technology_families(
    technology_options: List[dict], subassemblies: List[dict]
) -> List[Dict[str, Any]]:
    families = []
    for family, defaults in _TECHNOLOGY_FAMILY_DEFAULTS.items():
        matching_subs = [
            sub for sub in subassemblies if subassembly_supports_design_family(sub, family)
        ]
        families.append(
            {
                "family": family,
                "display_name": "Deye" if family == "deye" else "Victron",
                "site_layout_type": _TECHNOLOGY_SITE_LAYOUT_TYPE[family],
                "default_design_parameters": defaults,
                "assembly_classes": sorted(
                    {
                        str(sub.get("assembly_class"))
                        for sub in matching_subs
                        if sub.get("assembly_class")
                    }
                ),
                "subassemblies": [
                    {
                        "id": sub.get("id"),
                        "description": sub.get("description", ""),
                        "assembly_class": sub.get("assembly_class", ""),
                        "design_types": sub.get("design_types", ""),
                    }
                    for sub in matching_subs
                ],
                "technology_options": [
                    option
                    for option in technology_options
                    if family in (option.get("technology_families") or [])
                    or option.get("type_name") in defaults.values()
                ],
            }
        )
    return families


def _list_design_technology_families_sync() -> Dict[str, Any]:
    options = _list_design_options_sync()
    return {
        "success": True,
        "backend": "internal",
        "technology_families": options["technology_families"],
    }


def _run_auto_design_sync(
    design_id: str, param_overrides: Dict[str, Any], force: bool = False
) -> Dict[str, Any]:
    """Apply parameter overrides (if any), then (re-)run auto-design sizing.

    `param_overrides` is alias/field-keyed (same whitelist as `update_design`)
    and is translated via `_translate_design_updates` before being written —
    unlike `duplicate_design`'s `param_overrides`, which are already
    API-field-keyed and go straight into `design_writer.create_design`.
    """
    if not force and _has_manual_subassembly_edits(design_id):
        message = (
            f"Design {design_id} has manually-edited subassemblies; re-running "
            "auto-design would replace ALL of them (including your edits). "
            "Pass force=true to proceed anyway."
        )
        raise ValueError(message)

    param_overrides, family = _pop_technology_family(param_overrides)
    mapped = _translate_design_updates(param_overrides) if param_overrides else {}
    original = Repository("designs").get(design_id) if mapped else None
    original_subassemblies = _snapshot_design_subassemblies(design_id)
    if mapped:
        Repository("designs").update(design_id, mapped)

    result = _auto_design_with_family(design_id, family)
    if not result.get("ok"):
        _rollback_updates(design_id, original, mapped)
        _restore_design_subassemblies(design_id, original_subassemblies)
        return {
            "success": False,
            "error": str(result.get("error", "Auto-design failed")),
            "backend": "internal",
        }

    return {**_get_design_sync(design_id), "subassemblies_created": result.get("subassemblies")}


def _change_design_technology_sync(
    design_id: str,
    technology_family: str,
    *,
    rerun_auto_design: bool = True,
    regenerate_bom: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    family = normalize_design_family(technology_family)
    result = _update_design_sync(
        design_id,
        {"technology_family": family},
        rerun_auto_design=rerun_auto_design,
        regenerate_bom=regenerate_bom,
        force=force,
    )
    return {
        **result,
        "technology_family": family,
        "site_layout_type": _TECHNOLOGY_SITE_LAYOUT_TYPE[family],
        "default_design_parameters": _TECHNOLOGY_FAMILY_DEFAULTS[family],
        "recommended_next_steps": [
            "rerun_site_layout" if family == "deye" else "review_site_layout",
            "repopulate_lpp_cells",
        ],
    }


def _duplicate_design_sync(
    source_design_id: str,
    new_design_name: str,
    param_overrides: Dict[str, Any],
    run_auto_design_flag: bool = True,
    generate_bom_flag: bool = True,
) -> Dict[str, Any]:
    """Clone a design (same grid) under a new name, with optional overrides.

    `param_overrides` here is ALREADY API-field-keyed (e.g.
    wp_per_conn_override), matching what `design_writer.design_row_to_payload`
    produces and what `create_design`'s payload accepts directly — it is NOT
    run through `_translate_design_updates` (that maps to raw column names for
    Repository.update calls, which is not this path).
    """
    source = design_writer.get_design(source_design_id)
    if not source:
        return {
            "success": False,
            "error": f"Design {source_design_id} not found",
            "backend": "internal",
        }

    grid_id = source.get("grid")
    payload = design_writer.design_row_to_payload(source)
    payload["design_name"] = new_design_name
    if param_overrides:
        payload.update(param_overrides)
    # Strip created_by unconditionally as the very last step before the create
    # call — this must run AFTER the override merge, not just before it, or a
    # caller-supplied created_by override (param_overrides is directly
    # caller-controlled) would silently reintroduce the source design's
    # creator field / spoof an arbitrary one.
    payload.pop("created_by", None)

    new_design = design_writer.create_design(payload, grid_id)
    new_design_id = str(new_design["id"])
    steps: List[Dict[str, Any]] = [
        {"step": "create_design", "status": "created", "design_id": new_design_id}
    ]

    if run_auto_design_flag:
        ad_result = auto_design(new_design_id)
        if not ad_result.get("ok"):
            result = _get_design_sync(new_design_id)
            result["success"] = False
            result["error"] = str(ad_result.get("error", "Auto-design failed"))
            result["steps"] = steps + [
                {"step": "auto_design", "status": "failed", "reason": result["error"]}
            ]
            result["source_design_id"] = source_design_id
            return result
        steps.append({"step": "auto_design", "status": "completed"})

    result = _get_design_sync(new_design_id)
    result["steps"] = steps
    result["source_design_id"] = source_design_id

    if generate_bom_flag:
        bom_result = generate_bom(new_design_id)
        if not bom_result.get("ok"):
            result["success"] = False
            result["error"] = str(bom_result.get("error", "BOM generation failed"))
            steps.append({"step": "generate_bom", "status": "failed", "reason": result["error"]})
            return result
        shaped = _load_shaped_bom(new_design_id)
        result["bom"] = shaped
        result["cost_summary"] = compute_bom_cost_summary(shaped)
        steps.append({"step": "generate_bom", "status": "completed"})

    return result


# ── Name resolution helper (shared by design- and catalogue-level tools) ────


def _resolve_name_or_raise(
    input_name: str, candidates: List[dict], name_field: str, kind_label: str
) -> dict:
    """Fuzzy-resolve `input_name` against `candidates` (rows keyed by `name_field`).

    Shared by `add_subassembly` (resolves against `gd_subassemblies.description`)
    and `add_subassembly_component` (resolves against `gd_components.name` or
    `gd_subassemblies.description`) so the fuzzy-match-plus-candidate-listing
    logic lives in exactly one place.

    Raises ValueError — with the closest few candidates listed, via a direct
    `rapidfuzz.process.extract` call — when `find_best_grid_match` can't find a
    confident match. This gives the caller usable suggestions instead of a bare
    "not found," even when the miss is due to ambiguity (two close scores)
    rather than a total mismatch.
    """
    names = [c.get(name_field) for c in candidates if c.get(name_field)]
    matched_name, _was_fuzzy, _score = find_best_grid_match(input_name, names)
    if matched_name is None:
        suggestions: List[str] = []
        try:
            from rapidfuzz import process

            top = process.extract(input_name, names, limit=3)
            suggestions = [name for name, _score, _idx in top]
        except ImportError:
            pass
        hint = f" Closest matches: {', '.join(suggestions)}." if suggestions else ""
        raise ValueError(f"No {kind_label} matching '{input_name}' found.{hint}")
    return next(c for c in candidates if c.get(name_field) == matched_name)


# ── Design-level subassembly tools (gd_design_subassemblies) ────────────────


def _list_design_subassemblies_sync(design_id: str) -> Dict[str, Any]:
    rows = Repository("design_subassemblies").list(active_only=True, filters={"design": design_id})
    return {"success": True, "backend": "internal", "subassemblies": rows, "count": len(rows)}


def _add_subassembly_sync(design_id: str, subassembly_name: str, qty: float) -> Dict[str, Any]:
    """Add a subassembly instance to a design by fuzzy-matched catalogue name.

    Builds the row via the real `auto_design`-path `_build_ds_row` (same kWp/
    kWh/kVA math sizing uses) so a manually-added subassembly's energy specs
    are computed identically to an auto-generated one. `manually_edited` is
    set explicitly since `_build_ds_row`'s dict doesn't include that column
    (it defaults to false in the DB) — without this, the manual-edit guard
    (`_has_manual_subassembly_edits`) would never see this addition.
    """
    subs = Repository("subassemblies").list(active_only=True)
    sub = _resolve_name_or_raise(subassembly_name, subs, "description", "subassembly")

    sub_components = load("subassembly_components")
    row = _build_ds_row(design_id, sub, qty, sub_components)
    row["manually_edited"] = True
    inserted = Repository("design_subassemblies").insert(row)
    return {"success": True, "backend": "internal", "subassembly_added": inserted}


def _remove_subassembly_sync(design_subassembly_row_id: str) -> Dict[str, Any]:
    """Soft-delete a design subassembly row and flag the design as hand-curated.

    Non-obvious bit: soft-deleting the target row alone leaves no trace that
    THIS design's composition was manually changed — if the removed row was
    itself never `manually_edited` (e.g. it was auto-generated), the guard
    `_has_manual_subassembly_edits` would see zero flagged active rows on this
    design and a subsequent `auto_design`/`run_auto_design` run would silently
    regenerate the full original set, undoing the removal without ever
    tripping the guard. To close that gap, every remaining active sibling row
    for the design is flagged `manually_edited=True` (any ONE flagged row is
    sufficient to trip the guard), signalling "this design's subassembly set
    has been hand-curated as a whole, don't silently regenerate it."
    """
    repo = Repository("design_subassemblies")
    row = repo.get(design_subassembly_row_id)
    if not row:
        return {
            "success": False,
            "error": f"Design subassembly {design_subassembly_row_id} not found",
            "backend": "internal",
        }
    repo.soft_delete(design_subassembly_row_id)

    design_id = row.get("design")
    for sibling in repo.list(active_only=True, filters={"design": design_id}):
        if not sibling.get("manually_edited"):
            repo.update(sibling["id"], {"manually_edited": True})

    return {
        "success": True,
        "backend": "internal",
        "removed_id": design_subassembly_row_id,
        "design_id": design_id,
    }


def _set_subassembly_qty_sync(design_subassembly_row_id: str, qty: float) -> Dict[str, Any]:
    """Update a design subassembly's qty, scaling kwp/kwh/kva proportionally.

    kwp/kwh/kva were originally computed as `old_qty * spec_sum` (see
    `_build_ds_row`); since `spec_sum` doesn't change, `new_value = old_value
    / old_qty * new_qty` is exact — no need to re-fetch the catalogue spec
    rows. Guards against a zero/falsy `old_qty` by leaving kwp/kwh/kva out of
    the update entirely (keeping whatever is currently stored) rather than
    dividing by zero.

    Note: this does NOT update the parent `gd_designs.kwp/kwh/kva` totals —
    those are separate columns only ever (re)written by `auto_design`, not
    live-summed from subassemblies. Going stale here is expected/out of scope.
    """
    repo = Repository("design_subassemblies")
    row = repo.get(design_subassembly_row_id)
    if not row:
        return {
            "success": False,
            "error": f"Design subassembly {design_subassembly_row_id} not found",
            "backend": "internal",
        }

    changes: Dict[str, Any] = {"qty": qty, "manually_edited": True}
    old_qty = num(row.get("qty"))
    if old_qty:
        scale = qty / old_qty
        for field in ("kwp", "kwh", "kva"):
            changes[field] = num(row.get(field)) * scale

    updated = repo.update(design_subassembly_row_id, changes)
    return {"success": True, "backend": "internal", "updated": updated}


# ── Catalogue-level subassembly composition tools (gd_subassembly_components) ─


def _list_subassembly_components_sync(subassembly_id: str) -> Dict[str, Any]:
    rows = Repository("subassembly_components").list(
        active_only=True, filters={"subassembly": subassembly_id}
    )
    component_ids = [r["component"] for r in rows if r.get("component")]
    child_sub_ids = [r["component_subassembly"] for r in rows if r.get("component_subassembly")]
    components = Repository("components").get_many(component_ids) if component_ids else {}
    child_subs = Repository("subassemblies").get_many(child_sub_ids) if child_sub_ids else {}

    shaped: List[dict] = []
    for r in rows:
        if r.get("component"):
            comp = components.get(r["component"], {})
            shaped.append({**r, "child_type": "component", "child_name": comp.get("name", "")})
        else:
            child = child_subs.get(r.get("component_subassembly"), {})
            shaped.append(
                {**r, "child_type": "subassembly", "child_name": child.get("description", "")}
            )
    return {"success": True, "backend": "internal", "components": shaped, "count": len(shaped)}


def _would_create_cycle(parent_id: str, proposed_child_id: str) -> bool:
    """True if nesting `proposed_child_id` under `parent_id` would close a cycle.

    Direct self-reference (`parent_id == proposed_child_id`) is checked first
    without touching the DB. Otherwise DFS forward from `proposed_child_id`
    through `component_subassembly` links (i.e. walk the proposed child's own
    descendants): if that walk ever reaches `parent_id`, then `parent_id` is
    already a descendant of `proposed_child_id`, so nesting `proposed_child_id`
    under `parent_id` would close a loop.
    """
    if parent_id == proposed_child_id:
        return True
    seen = {proposed_child_id}
    frontier = [proposed_child_id]
    while frontier:
        current = frontier.pop()
        rows = Repository("subassembly_components").list(
            active_only=True, filters={"subassembly": current}
        )
        for r in rows:
            nested = r.get("component_subassembly")
            if nested == parent_id:
                return True
            if nested and nested not in seen:
                seen.add(nested)
                frontier.append(nested)
    return False


def _add_subassembly_component_sync(
    subassembly_id: str,
    component_name: Optional[str] = None,
    child_subassembly_name: Optional[str] = None,
    qty: float = 1,
    unit: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a child (plain component OR nested subassembly) to a catalogue subassembly.

    CATALOGUE-level edit: unlike the design-level add/remove/set-qty tools
    above (which touch exactly one design's own rows), this edits the shared
    subassembly TEMPLATE — every design referencing `subassembly_id` will pick
    up this change the next time `trigger_bom`/`auto_design` runs against it.
    A future MCP tool description wrapping this function should warn callers
    of that blast radius; no warning mechanism is implemented here.
    """
    if bool(component_name) == bool(child_subassembly_name):
        raise ValueError("Exactly one of component_name or child_subassembly_name must be given.")

    component_id: Optional[str] = None
    child_subassembly_id: Optional[str] = None

    if component_name:
        components = Repository("components").list(active_only=True)
        comp = _resolve_name_or_raise(component_name, components, "name", "component")
        component_id = comp["id"]
    else:
        subs = Repository("subassemblies").list(active_only=True)
        child_sub = _resolve_name_or_raise(
            child_subassembly_name, subs, "description", "subassembly"
        )
        child_subassembly_id = child_sub["id"]
        if _would_create_cycle(subassembly_id, child_subassembly_id):
            raise ValueError(
                f"Cannot add '{child_subassembly_name}' into subassembly {subassembly_id}: "
                "it would create a circular subassembly reference."
            )

    row = {
        "id": new_id(),
        "subassembly": subassembly_id,
        "component": component_id,
        "component_subassembly": child_subassembly_id,
        "qty": qty,
        "unit": unit,
        "active": True,
    }
    inserted = Repository("subassembly_components").insert(row)
    return {"success": True, "backend": "internal", "component_added": inserted}


def _remove_subassembly_component_sync(row_id: str) -> Dict[str, Any]:
    repo = Repository("subassembly_components")
    row = repo.get(row_id)
    if not row:
        return {
            "success": False,
            "error": f"Subassembly component {row_id} not found",
            "backend": "internal",
        }
    repo.soft_delete(row_id)
    return {"success": True, "backend": "internal", "removed_id": row_id}


def _set_subassembly_component_qty_sync(row_id: str, qty: float) -> Dict[str, Any]:
    repo = Repository("subassembly_components")
    row = repo.get(row_id)
    if not row:
        return {
            "success": False,
            "error": f"Subassembly component {row_id} not found",
            "backend": "internal",
        }
    updated = repo.update(row_id, {"qty": qty})
    return {"success": True, "backend": "internal", "updated": updated}


def _duplicate_subassembly_sync(source_subassembly_id: str, new_description: str) -> Dict[str, Any]:
    """Clone a catalogue subassembly (all fields) plus its full component list."""
    source = Repository("subassemblies").get(source_subassembly_id)
    if not source:
        return {
            "success": False,
            "error": f"Subassembly {source_subassembly_id} not found",
            "backend": "internal",
        }

    new_row = {k: v for k, v in source.items() if k != "id"}
    new_row["id"] = new_id()
    new_row["description"] = new_description
    inserted = Repository("subassemblies").insert(new_row)

    child_repo = Repository("subassembly_components")
    children = child_repo.list(active_only=True, filters={"subassembly": source_subassembly_id})
    copied = 0
    for child in children:
        new_child = {k: v for k, v in child.items() if k != "id"}
        new_child["id"] = new_id()
        new_child["subassembly"] = inserted["id"]
        child_repo.insert(new_child)
        copied += 1

    return {
        "success": True,
        "backend": "internal",
        "subassembly": inserted,
        "components_copied": copied,
    }


# ── Async API (event-loop safe) ──────────────────────────────────────────────


async def design_and_bom(args: Dict[str, Any]) -> Dict[str, Any]:
    return await asyncio.to_thread(_design_and_bom_sync, args)


async def trigger_bom(design_id: str, grid_name: str = "") -> Dict[str, Any]:
    return await asyncio.to_thread(_trigger_bom_sync, design_id, grid_name)


async def update_design(
    design_id: str,
    updates: Dict[str, Any],
    *,
    rerun_auto_design: bool = False,
    regenerate_bom: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _update_design_sync,
        design_id,
        updates,
        rerun_auto_design=rerun_auto_design,
        regenerate_bom=regenerate_bom,
        force=force,
    )


async def get_design(design_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_get_design_sync, design_id)


async def get_design_bom(design_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_get_design_bom_sync, design_id)


async def list_design_artifacts(design_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_list_design_artifacts_sync, design_id)


async def get_design_artifact(
    design_id: str, artifact_type: str, version: int = 0
) -> Dict[str, Any]:
    return await asyncio.to_thread(_get_design_artifact_sync, design_id, artifact_type, version)


async def find_grid(grid_name: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_find_grid_sync, grid_name)


async def list_design_options() -> Dict[str, Any]:
    return await asyncio.to_thread(_list_design_options_sync)


async def list_design_technology_families() -> Dict[str, Any]:
    return await asyncio.to_thread(_list_design_technology_families_sync)


async def run_auto_design(
    design_id: str, param_overrides: Dict[str, Any], force: bool = False
) -> Dict[str, Any]:
    return await asyncio.to_thread(_run_auto_design_sync, design_id, param_overrides, force)


async def change_design_technology(
    design_id: str,
    technology_family: str,
    *,
    rerun_auto_design: bool = True,
    regenerate_bom: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _change_design_technology_sync,
        design_id,
        technology_family,
        rerun_auto_design=rerun_auto_design,
        regenerate_bom=regenerate_bom,
        force=force,
    )


async def duplicate_design(
    source_design_id: str,
    new_design_name: str,
    param_overrides: Dict[str, Any],
    run_auto_design_flag: bool = True,
    generate_bom_flag: bool = True,
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _duplicate_design_sync,
        source_design_id,
        new_design_name,
        param_overrides,
        run_auto_design_flag,
        generate_bom_flag,
    )


# ── Design-level subassembly tools ───────────────────────────────────────────


async def list_design_subassemblies(design_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_list_design_subassemblies_sync, design_id)


async def add_subassembly(design_id: str, subassembly_name: str, qty: float) -> Dict[str, Any]:
    return await asyncio.to_thread(_add_subassembly_sync, design_id, subassembly_name, qty)


async def remove_subassembly(design_subassembly_row_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_remove_subassembly_sync, design_subassembly_row_id)


async def set_subassembly_qty(design_subassembly_row_id: str, qty: float) -> Dict[str, Any]:
    return await asyncio.to_thread(_set_subassembly_qty_sync, design_subassembly_row_id, qty)


async def get_design_id_for_subassembly_row(row_id: str) -> Optional[str]:
    """Resolve a design_subassemblies row id to its parent design id.

    Pure data lookup (no access-control decision) — used by the MCP server's
    auth layer to find which design/grid a row-id-anchored tool call
    (remove_subassembly, set_subassembly_qty) is touching, BEFORE the write
    happens, so grid access can be checked ahead of the mutation rather than
    as a side effect of it.
    """

    def _sync() -> Optional[str]:
        row = Repository("design_subassemblies").get(row_id)
        design_id = row.get("design") if row else None
        return design_id if isinstance(design_id, str) else None

    return await asyncio.to_thread(_sync)


# ── Catalogue-level subassembly composition tools ───────────────────────────


async def list_subassembly_components(subassembly_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_list_subassembly_components_sync, subassembly_id)


async def add_subassembly_component(
    subassembly_id: str,
    component_name: Optional[str] = None,
    child_subassembly_name: Optional[str] = None,
    qty: float = 1,
    unit: Optional[str] = None,
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _add_subassembly_component_sync,
        subassembly_id,
        component_name,
        child_subassembly_name,
        qty,
        unit,
    )


async def remove_subassembly_component(row_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_remove_subassembly_component_sync, row_id)


async def set_subassembly_component_qty(row_id: str, qty: float) -> Dict[str, Any]:
    return await asyncio.to_thread(_set_subassembly_component_qty_sync, row_id, qty)


async def duplicate_subassembly(source_subassembly_id: str, new_description: str) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _duplicate_subassembly_sync, source_subassembly_id, new_description
    )
