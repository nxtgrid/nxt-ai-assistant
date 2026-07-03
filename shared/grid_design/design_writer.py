"""Create grids and designs programmatically.

Accepts the full parameter set the AppSheet design form offered and applies the
same initial values the form applied when a field was left blank (the AppSheet
REST API filled initial values for omitted columns on Adds), so designs created
through the API/MCP match designs the old UI produced. Used by the grid_design
MCP server and the anansi_app UI.
"""

from __future__ import annotations

from typing import Any

from shared.grid_design.db import Repository
from shared.grid_design.ids import new_id

# API request field -> gd_designs column
_DESIGN_FIELD_MAP = {
    "design_name": "name",
    "inverter_type": "inverter_type",
    "battery_type": "battery_type",
    "mppt_type": "mppt_type",
    "pv_type": "pv_type",
    "pv_inverter_type": "pv_inverter_type",
    "max_connections": "max_connections",
    "initial_residential_connections": "initial_residential_connections",
    "initial_business_connections": "initial_business_connections",
    "initial_3phase_connections": "initial_3_phase_connections",
    "num_poc_teams": "number_of_poc_teams_to_install_meters",
    "anchor_load_kw": "anchor_load_kw",
    "force_3phase": "force_3_phase",
    "wp_per_conn_override": "wp_per_conn_override",
    "regulation_constraint": "constrain_design_to_known_regulation",
    "pue_hours_per_day": "pue_hours_per_day",
    "daily_generation_potential_kwh_kwp": "daily_generation_potential_kwh_kwp",
    "target_tariff_usd": "target_tariff_usd",
    "max_distance_to_center_of_consumption_m": "max_distance_to_center_of_consumption",
    "avg_distance_to_pv_combiner_m": "avg_distance_to_pv_combiner",
    "distance_to_feeder_pillar_m": "distance_to_feeder_pillar",
    "spd_type": "spd_type",
    "avg_service_drop_length_m": "average_service_drop_length_m",
    "target_kwp": "target_kwp",
    "target_kwh": "target_kwh",
    "auto_design": "auto_design",
    "created_by": "created_by",
}

REGULATION_OPTIONS = ["None", "Nigeria - DARES"]
SPD_OPTIONS = [
    "Keep default T1+T2 Type SPD (Any lightning probability)",
    "Use T2 type as T1+T2 Type due to Low (<=16 strikes per sq km per yr) lightning probability",
]

# AppSheet form initial values (Designs table). daily_generation_potential_kwh_kwp
# is deliberately NOT defaulted: the sizing engine reads the Design Rules value
# unless the caller explicitly overrides it (see auto_designer).
FORM_DEFAULTS: dict[str, Any] = {
    "initial_3phase_connections": 0,
    "avg_service_drop_length_m": 25,
    "num_poc_teams": 1,
    "anchor_load_kw": 0,
    "force_3phase": False,
    "regulation_constraint": "Nigeria - DARES",
    "pue_hours_per_day": 3,
    "target_tariff_usd": 0.45,
    "spd_type": SPD_OPTIONS[0],
    "avg_distance_to_pv_combiner_m": 40,
    "distance_to_feeder_pillar_m": 7,
    "auto_design": True,
}


def find_grid_by_name(name: str) -> dict | None:
    rows: list[dict] = Repository("grids").list(active_only=True, filters={"name": name}, limit=1)
    return rows[0] if rows else None


def create_grid(name: str, community: str | None = None) -> dict:
    grid = {"id": new_id(), "name": name, "active": True}
    if community:
        grid["community"] = community
    created: dict[str, Any] = Repository("grids").insert(grid)
    return created


def create_design(payload: dict[str, Any], grid_id: str) -> dict:
    """Insert a gd_designs row from an API payload; returns the created row.

    Applies the same connection-distribution defaults as the AppSheet flow
    (residential defaults to 90% of max, business fills the remainder after
    3-phase) plus the form initial values for omitted fields.
    """
    resolved = {k: v for k, v in payload.items() if v is not None}
    for field, default in FORM_DEFAULTS.items():
        resolved.setdefault(field, default)

    max_conns = int(resolved.get("max_connections") or 0)
    three_phase = int(resolved.get("initial_3phase_connections") or 0)
    residential = resolved.get("initial_residential_connections")
    business = resolved.get("initial_business_connections")
    if residential is None:
        residential = int(max_conns * 0.9)
    if business is None:
        # Clamp: small max_connections or oversized residential/3-phase inputs
        # must not produce a negative business count.
        business = max(0, max_conns - residential - three_phase)

    resolved["initial_residential_connections"] = residential
    resolved["initial_business_connections"] = business
    resolved["initial_3phase_connections"] = three_phase

    row: dict[str, Any] = {"id": new_id(), "grid": grid_id, "active": True}
    for field, col in _DESIGN_FIELD_MAP.items():
        val = resolved.get(field)
        if val is not None:
            row[col] = val
    created: dict[str, Any] = Repository("designs").insert(row)
    return created
