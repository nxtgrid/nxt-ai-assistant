"""Generic CRUD surface over every `gd_*` table (Phase E, tasks 1-2).

`Repository` (shared/grid_design/db.py) is a thin, table-name-agnostic CRUD
wrapper with no access control of its own — anyone who can call it can read or
write any row in any `gd_*` table. The fine-grained tools in
`internal_engine.py` hand-roll grid access checks per endpoint; this module
instead builds a single **registry** (`GD_TABLE_REGISTRY`) that classifies
every table once, and generic tools (`gd_describe_tables`, `gd_list_rows`,
`gd_get_row`, `gd_upsert_row`, `gd_delete_row`) that enforce access from that
classification.

Three scopes:
- ``"grid"``: the table is anchored to a single grid (directly or via a
  parent row). Every read/write must resolve the row's grid and pass it
  through `gd_auth.assert_grid_access`.
- ``"catalog"``: global catalogue/reference tables (components, subassembly
  templates, pricing, procedures...) with no per-grid owner. A write here
  affects every grid that references it, so these are staff-only.
- ``"denied"``: identity/permission tables (`organizations`, `users`) that
  generic CRUD must never touch, regardless of caller.

`gd_upsert_row`/`gd_delete_row` never hard-delete (soft delete only, via
`Repository.soft_delete` — there is no hard-delete path anywhere in this
engine) and never allow a caller to write `created_by`/`updated_by` directly;
those are stamped server-side from the caller's `user_email` per
`TablePolicy.audit_columns`.

This module intentionally does NOT wire these tools into
`grid_design_mcp_server.py`'s `types.Tool` list / `_handle_internal_tool`
dispatch — that MCP-tool-definition wiring (including JSON-string parsing of
`values` per the Gemini inputSchema constraints) is a separate follow-up task.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from servers.grid_design_server import gd_auth
from servers.grid_design_server.internal_engine import _would_create_cycle

from shared.auth.auth_service import STAFF_ORG_ID
from shared.grid_design.db import Repository
from shared.grid_design.ids import new_id


@dataclass(frozen=True)
class TablePolicy:
    """Access classification for one `gd_*` table.

    ``table`` is the bare name Repository expects (no ``gd_`` prefix).
    ``grid_anchor`` is a tuple of column names to walk, starting from the
    row itself, to reach a grid: e.g. ``("design", "grid")`` means "read
    this row's `design` column to get a design id, fetch that design row,
    then its `grid` column is the grid id." An empty tuple means either the
    row itself IS the grid (the `grids` table) or the table isn't
    grid-scoped at all (`catalog`/`denied`).

    ``audit_columns`` names the subset of `created_by`/`updated_by` columns
    this table actually has (per `anansi_app/db/schema.sql`). Populated only
    for tables that carry those columns; `gd_upsert_row` stamps them from the
    caller's `user_email` and never lets a caller set them directly via
    `values`.
    """

    table: str
    scope: str  # "grid" | "catalog" | "denied"
    grid_anchor: tuple[str, ...] = ()
    writable_columns: tuple[str, ...] = field(default_factory=tuple)
    audit_columns: tuple[str, ...] = ()
    description: str = ""


# Column name -> table to fetch when walking a `grid_anchor` hop. "grid" is
# always the terminal hop (its target, `grids`, carries the `name` column
# `_resolve_grid_for_row` ultimately returns).
_GRID_HOP_TABLES: dict[str, str] = {
    "design": "designs",
    "job": "jobs",
    "grid": "grids",
}

# Sentinel anchor segment: try `design` first, fall back to `job`. Used by
# `bom_items`, which has both `design` and `job` FK columns and only one is
# ever set on a given row.
_DESIGN_OR_JOB = "design_or_job"


GD_TABLE_REGISTRY: dict[str, TablePolicy] = {
    # ── Denied: identity/permission surfaces, never touched by generic CRUD ──
    "organizations": TablePolicy(
        table="organizations",
        scope="denied",
        description="Tenant organizations. Identity/permission surface — not exposed to generic CRUD.",
    ),
    "users": TablePolicy(
        table="users",
        scope="denied",
        description="App users. Identity/permission surface — not exposed to generic CRUD.",
    ),
    # ── Grid-scoped: single-hop anchors ──────────────────────────────────────
    "grids": TablePolicy(
        table="grids",
        scope="grid",
        grid_anchor=(),
        writable_columns=("name", "community", "stage"),
        description="A grid/site. The anchor for every grid-scoped table below; a row's own `name` IS the grid name.",
    ),
    "grid_coords": TablePolicy(
        table="grid_coords",
        scope="grid",
        grid_anchor=("grid",),
        writable_columns=("grid", "coordinate"),
        description="A boundary/location coordinate belonging to one grid.",
    ),
    "designs": TablePolicy(
        table="designs",
        scope="grid",
        grid_anchor=("grid",),
        writable_columns=(
            "grid",
            "name",
            "inverter_type",
            "battery_type",
            "mppt_type",
            "pv_type",
            "pv_inverter_type",
            "max_connections",
            "initial_residential_connections",
            "initial_business_connections",
            "initial_3_phase_connections",
            "average_service_drop_length_m",
            "number_of_poc_teams_to_install_meters",
            "anchor_load_kw",
            "force_3_phase",
            "wp_per_conn_override",
            "constrain_design_to_known_regulation",
            "pue_hours_per_day",
            "daily_generation_potential_kwh_kwp",
            "phases",
            "target_kwp",
            "target_kwh",
            "target_tariff_usd",
            "auto_design",
            "recalculate_bom_on_design_change",
            "max_distance_to_center_of_consumption",
            "pv_area_sqm",
            "avg_distance_to_pv_combiner",
            "distance_to_feeder_pillar",
            "spd_type",
            "usd_to_ngn",
            "xrate_updated_at",
            "bom_generated_at",
            "design_checks",
            "kwp",
            "kva",
            "kwh",
            "bom_cost_estimate",
            "works_cost_estimate",
            "monthly_rental_estimate",
            "monthly_saleable_kwh_at_expected_cuf",
            "monthly_revenue_at_expected_cuf_and_tariff",
            # NOTE: "artifacts" (jsonb) is deliberately NOT writable here. It's
            # Phase B's versioned artifact-history column, exclusively managed
            # by shared/grid_design/artifact_log.py::append_design_artifact
            # (prepend-with-cap semantics) — a generic upsert overwriting it
            # would silently destroy history.
        ),
        audit_columns=("created_by",),
        description="A power-plant design for one grid, sized by the auto-designer and priced by the BOM generator.",
    ),
    "jobs": TablePolicy(
        table="jobs",
        scope="grid",
        grid_anchor=("grid",),
        writable_columns=(
            "type",
            "grid",
            "jira_reference",
            "organization",
            "technician",
            "start_date",
            "expected_days_to_complete",
            "cost_to_developer",
            "cost_to_nxt",
            "status",
        ),
        audit_columns=("created_by",),
        description="A field-ops job (install/maintenance) scheduled against one grid.",
    ),
    # ── Grid-scoped: multi-hop anchors ───────────────────────────────────────
    "design_subassemblies": TablePolicy(
        table="design_subassemblies",
        scope="grid",
        grid_anchor=("design", "grid"),
        writable_columns=(
            "design",
            "subassembly",
            "qty",
            "subassembly_image",
            "named",
            "class",
            "spec1_name",
            "spec1_value",
            "spec1_unit",
            "spec2_name",
            "spec2_value",
            "spec2_unit",
            "spec3_name",
            "spec3_value",
            "spec3_unit",
            "kwp",
            "kwh",
            "kva",
            "comment",
            "manually_edited",
        ),
        description="A subassembly instance (qty + specs) attached to one design; anchored to its grid via the parent design.",
    ),
    "bom_items": TablePolicy(
        table="bom_items",
        scope="grid",
        grid_anchor=(_DESIGN_OR_JOB, "grid"),
        writable_columns=(
            "item",
            "qty",
            "qty_with_contingency",
            "design",
            "job",
            "subassembly",
            "design_types",
            "unit_cost_ngn",
            "total_cost_ngn",
            "monthly_rental_usd",
            "source",
            "who_procures",
            "verified_in_shipment",
            "engg_comment",
            "received_count",
            "returned_count",
            "technician_comment",
        ),
        description="A priced BOM line item, attached to either a design or a job (never both); anchored to its grid via whichever parent is set.",
    ),
    "job_procedures": TablePolicy(
        table="job_procedures",
        scope="grid",
        grid_anchor=("job", "grid"),
        writable_columns=("job", "procedure", "qty", "sequence_in_job", "status", "comment"),
        description="A procedure instance scheduled within one job; anchored to its grid via the parent job.",
    ),
    "job_steps": TablePolicy(
        table="job_steps",
        scope="grid",
        grid_anchor=("job", "grid"),
        writable_columns=(
            "name",
            "step_reference",
            "job",
            "job_procedure",
            "sequence",
            "conditional_on",
            "go_conditions",
            "proof",
            "status",
            "comment",
            "approved",
        ),
        audit_columns=("created_by", "updated_by"),
        description="A single field step within a job (with proof/approval state); anchored to its grid via the parent job.",
    ),
    "job_subassemblies": TablePolicy(
        table="job_subassemblies",
        scope="grid",
        grid_anchor=("job", "grid"),
        writable_columns=("job", "subassembly", "qty", "comment"),
        description="A subassembly consumed by one field-ops job; anchored to its grid via the parent job.",
    ),
    # ── Catalog: staff-only, global blast radius ─────────────────────────────
    "components": TablePolicy(
        table="components",
        scope="catalog",
        writable_columns=(
            "name",
            "name_in_french",
            "counting_unit",
            "component_type",
            "source",
            "who_procures",
            "unit_cost_usd",
            "unit_cost_ngn",
            "spec1_name",
            "spec1_value",
            "spec1_unit",
            "spec2_name",
            "spec2_value",
            "spec2_unit",
            "spec3_name",
            "spec3_value",
            "spec3_unit",
            "contingency_pct",
            "notes",
            "do_not_miss",
            "ddp_cost",
            "projected_cost",
            "cost_confidence",
            "num_purchases",
            "cost_projected_at",
        ),
        description="A catalogue component (BOM line-item template) shared across every grid's designs. Staff-only: edits affect every design referencing it.",
    ),
    "subassemblies": TablePolicy(
        table="subassemblies",
        scope="catalog",
        writable_columns=(
            "main_component",
            "assembly_class",
            "assembly_type",
            "assembly_reference_image",
            "description",
            "design_types",
            "components_active",
            "spec1_name",
            "spec1_value",
            "spec1_unit",
            "spec2_name",
            "spec2_value",
            "spec2_unit",
            "spec3_name",
            "spec3_value",
            "spec3_unit",
            "unit_rental_usd_per_month",
        ),
        audit_columns=("created_by", "updated_by"),
        description="A catalogue subassembly template (e.g. an inverter or battery bank) shared across every grid's designs. Staff-only: edits affect every design referencing it.",
    ),
    "subassembly_components": TablePolicy(
        table="subassembly_components",
        scope="catalog",
        writable_columns=(
            "subassembly",
            "component_subassembly",
            "component",
            "qty",
            "component_type",
            "unit",
            "spec1_name",
            "spec1_value",
            "spec1_unit",
        ),
        audit_columns=("created_by", "updated_by"),
        description="A child component or nested subassembly within a catalogue subassembly template. Staff-only: edits affect every design referencing the parent template.",
    ),
    "design_rules": TablePolicy(
        table="design_rules",
        scope="catalog",
        writable_columns=("name", "description", "value", "implemented"),
        audit_columns=("created_by",),
        description="A named auto-design rule/constant used by the sizing engine. Staff-only: global to all designs.",
    ),
    "unit_rental_prices": TablePolicy(
        table="unit_rental_prices",
        scope="catalog",
        writable_columns=("item", "engineering_item_name", "unit_monthly_rental"),
        description="Monthly rental pricing per technology type, used by design_and_bom's technology dropdowns. Staff-only: global pricing.",
    ),
    "wp_per_conn_lookup": TablePolicy(
        table="wp_per_conn_lookup",
        scope="catalog",
        writable_columns=("nonresidential_threshold", "wp_per_conn", "kwh_per_kwp"),
        description="Watts-per-connection sizing lookup table used by the auto-designer. Staff-only: global sizing constants.",
    ),
    "purchases": TablePolicy(
        table="purchases",
        scope="catalog",
        writable_columns=(
            "date",
            "item_description",
            "currency",
            "qty",
            "landed_unit_cost_usd",
            "total_cost",
        ),
        description="A procurement ledger entry, used to recompute component costs just-in-time. Staff-only: shared cost history.",
    ),
    "procedures": TablePolicy(
        table="procedures",
        scope="catalog",
        writable_columns=("code", "name", "qty", "comments"),
        description="A named field-ops procedure template (e.g. an install checklist). Staff-only: shared across every job.",
    ),
    "procedure_steps": TablePolicy(
        table="procedure_steps",
        scope="catalog",
        writable_columns=(
            "name",
            "type",
            "outcomes",
            "procedure",
            "detail",
            "image",
            "requires_proof",
            "step_order",
            "conditional_on",
            "go_options",
        ),
        audit_columns=("created_by",),
        description="A single step template within a procedure. Staff-only: shared across every job that uses the parent procedure.",
    ),
}


def _resolve_grid_for_row(policy: TablePolicy, row: dict[str, Any]) -> str | None:
    """Walk `policy.grid_anchor` from an already-fetched row to a grid name.

    Plain synchronous helper (Repository is synchronous) — callers running
    inside an async context must wrap this with `asyncio.to_thread`, same as
    `gd_auth.resolve_grid_name_for_design`.

    Returns None if any hop is missing or unresolvable. Callers must treat
    None as access-denied (fail-closed) rather than surfacing which hop
    failed, matching `_require_grid_access_for_design`'s "don't leak
    not-found vs access-denied" convention.
    """
    if not row:
        return None

    if not policy.grid_anchor:
        # No anchor to walk: either this IS the grids table (the row's own
        # `name` is the grid name) or it's a not-yet-grid-scoped policy. Only
        # `grids` should ever be passed in here with an empty anchor from
        # grid-scoped call sites.
        name = row.get("name")
        return name if isinstance(name, str) and name else None

    current: dict[str, Any] | None = row
    for hop in policy.grid_anchor:
        if current is None:
            return None
        if hop == _DESIGN_OR_JOB:
            if current.get("design"):
                actual_hop, id_value = "design", current["design"]
            elif current.get("job"):
                actual_hop, id_value = "job", current["job"]
            else:
                return None
        else:
            actual_hop = hop
            id_value = current.get(hop)

        if not id_value:
            return None

        target_table = _GRID_HOP_TABLES.get(actual_hop)
        if target_table is None:
            return None

        current = Repository(target_table).get(id_value)
        if not current:
            return None

    name = current.get("name")
    return name if isinstance(name, str) and name else None


def _find_grid_row_by_name(grid_name: str) -> dict[str, Any] | None:
    """Resolve a grid name to its row (id-bearing) via the Chat DB. Sync helper."""
    rows = Repository("grids").list(active_only=True, filters={"name": grid_name}, limit=1)
    return rows[0] if rows else None


def _list_rows_sync(
    table: str, active_only: bool, filters: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    return Repository(table).list(active_only=active_only, filters=filters, limit=limit)


async def gd_describe_tables() -> dict[str, Any]:
    """Dump the table registry for LLM/tool-discovery use. No auth check (metadata only)."""
    return {
        "tables": [
            {
                "table": policy.table,
                "scope": policy.scope,
                "grid_anchor": list(policy.grid_anchor),
                "writable_columns": list(policy.writable_columns),
                "description": policy.description,
            }
            for policy in GD_TABLE_REGISTRY.values()
        ]
    }


async def gd_list_rows(
    table: str,
    organization_id: int | None,
    filters: dict[str, Any] | None = None,
    limit: int = 50,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """List rows from a `gd_*` table, enforcing scope-appropriate access.

    Grid-scoped tables REQUIRE a grid filter: either `filters["grid"]` (an
    id) or `filters["grid_name"]` (resolved to an id here). Rows are also
    checked individually (defense in depth) via `assert_grid_access` and
    silently dropped on denial rather than raising, since the required
    filter should already scope the query to a single accessible grid.
    """
    policy = GD_TABLE_REGISTRY.get(table)
    if policy is None or policy.scope == "denied":
        return {
            "success": False,
            "error": f"Table '{table}' is not available for generic row access.",
        }

    query_filters = dict(filters or {})

    if policy.scope == "catalog":
        # Mirrors _require_staff_org in grid_design_mcp_server.py (not imported
        # to avoid a circular import between that module and this one).
        if organization_id != STAFF_ORG_ID:
            return {
                "success": False,
                "error": f"You don't have access to list rows in catalogue table '{table}'.",
            }
        rows = await asyncio.to_thread(
            _list_rows_sync, table, not include_inactive, query_filters, limit
        )
        return {"success": True, "rows": rows, "count": len(rows)}

    # scope == "grid": a grid filter is mandatory.
    grid_name = query_filters.pop("grid_name", None)
    grid_filter_key = "id" if table == "grids" else "grid"
    grid_id = query_filters.get(grid_filter_key)

    if grid_name and not grid_id:
        grid_row = await asyncio.to_thread(_find_grid_row_by_name, grid_name)
        if not grid_row:
            return {"success": False, "error": f"Grid '{grid_name}' not found."}
        grid_id = grid_row["id"]
        query_filters[grid_filter_key] = grid_id

    if not grid_id:
        return {
            "success": False,
            "error": (
                "A grid filter (filters['grid'] or filters['grid_name']) is required "
                f"to list rows from grid-scoped table '{table}'."
            ),
        }

    rows = await asyncio.to_thread(
        _list_rows_sync, table, not include_inactive, query_filters, limit
    )

    allowed_rows: list[dict[str, Any]] = []
    for row in rows:
        row_grid_name = await asyncio.to_thread(_resolve_grid_for_row, policy, row)
        if row_grid_name is None:
            continue
        try:
            await gd_auth.assert_grid_access(row_grid_name, organization_id)
        except gd_auth.GridAccessDenied:
            continue
        allowed_rows.append(row)

    return {"success": True, "rows": allowed_rows, "count": len(allowed_rows)}


async def gd_get_row(table: str, row_id: str, organization_id: int | None) -> dict[str, Any]:
    """Fetch a single row by id, enforcing scope-appropriate access.

    For grid-scoped tables, "row not found" and "row exists but access
    denied" return the SAME generic error shape — same existence-oracle
    concern as `_require_grid_access_for_design` in grid_design_mcp_server.py.
    """
    policy = GD_TABLE_REGISTRY.get(table)
    if policy is None or policy.scope == "denied":
        return {
            "success": False,
            "error": f"Table '{table}' is not available for generic row access.",
        }

    if policy.scope == "catalog":
        if organization_id != STAFF_ORG_ID:
            return {
                "success": False,
                "error": f"You don't have access to rows in catalogue table '{table}'.",
            }
        row = await asyncio.to_thread(Repository(table).get, row_id)
        if not row:
            return {"success": False, "error": f"Row {row_id} not found in '{table}'."}
        return {"success": True, "row": row}

    # scope == "grid"
    denial = {
        "success": False,
        "error": f"You don't have access to row {row_id} in '{table}', or it doesn't exist.",
    }
    row = await asyncio.to_thread(Repository(table).get, row_id)
    if not row:
        return denial

    row_grid_name = await asyncio.to_thread(_resolve_grid_for_row, policy, row)
    if row_grid_name is None:
        return denial

    try:
        await gd_auth.assert_grid_access(row_grid_name, organization_id)
    except gd_auth.GridAccessDenied:
        return denial

    return {"success": True, "row": row}


def _validate_and_upsert_sync(
    policy: TablePolicy,
    table: str,
    row_id: str | None,
    values: dict[str, Any],
    user_email: str | None,
    existing_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Sync core of `gd_upsert_row`: table-specific validation, audit-column
    stamping, and the actual `Repository` insert/update.

    Split out from the async wrapper so the (already-DB-bound) validation for
    `subassembly_components` — which itself walks `subassembly_components`
    rows via `_would_create_cycle` — and the final Repository call run inside
    a single `asyncio.to_thread` hop rather than several small ones. Raises
    `ValueError` on validation failure; the caller (`gd_upsert_row`) converts
    that into `{"success": False, "error": ...}`.
    """
    # The "effective" row is what the row will look like AFTER this write:
    # the existing row (empty on create) with the incoming values merged on
    # top. subassembly_components validation must run against this, not just
    # the incoming values, since an update might only touch `qty` while
    # `component`/`component_subassembly` come from the existing row.
    effective = {**(existing_row or {}), **values}

    if table == "subassembly_components":
        has_component = bool(effective.get("component"))
        has_child_subassembly = bool(effective.get("component_subassembly"))
        if has_component == has_child_subassembly:
            raise ValueError(
                "subassembly_components requires exactly one of 'component' or "
                "'component_subassembly' to be set (not both, not neither)."
            )
        if has_child_subassembly:
            parent_id = effective.get("subassembly")
            child_id = effective.get("component_subassembly")
            if not parent_id:
                raise ValueError(
                    "subassembly_components requires a 'subassembly' (parent) value "
                    "to validate the nested component_subassembly."
                )
            if _would_create_cycle(parent_id, child_id):
                raise ValueError(
                    f"Cannot nest subassembly '{child_id}' here: it would create a "
                    "circular subassembly reference."
                )

    if table == "bom_items":
        has_design = bool(effective.get("design"))
        has_job = bool(effective.get("job"))
        if has_design == has_job:
            raise ValueError(
                "bom_items requires exactly one of 'design' or 'job' to be set "
                "(not both, not neither)."
            )

    row = dict(values)

    if row_id is None:
        if "created_by" in policy.audit_columns and user_email:
            row["created_by"] = user_email
        row["id"] = new_id()
        created = Repository(table).insert(row)
        return {"success": True, "created": created}

    if "updated_by" in policy.audit_columns and user_email:
        row["updated_by"] = user_email

    if not row:
        # Nothing to change (values were empty/fully filtered and no audit
        # column applies) — treat as a successful no-op rather than issuing a
        # pointless Repository.update call.
        return {"success": True, "updated": existing_row, "no_op": True}

    updated = Repository(table).update(row_id, row)
    return {"success": True, "updated": updated}


async def gd_upsert_row(
    table: str,
    organization_id: int | None,
    user_email: str | None,
    row_id: str | None = None,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create (``row_id=None``) or update (``row_id`` given) a row in a `gd_*` table.

    Enforces the same scope-based access control as `gd_list_rows`/`gd_get_row`,
    plus:

    - Column whitelisting: any key in `values` not in `policy.writable_columns`
      (aside from `created_by`/`updated_by`, which are silently dropped — see
      below) is a hard error listing the allowed columns, not a silent ignore.
    - Audit stamping: `created_by`/`updated_by` are stamped from `user_email`
      per `policy.audit_columns` and can NEVER be set by the caller directly —
      if a caller echoes them back in `values` (e.g. from a previously fetched
      row), they're dropped rather than rejected, since that's an innocent
      round-trip, not an attempted privilege escalation.
    - Anchor-move re-auth: updating a grid-scoped row's own anchor column
      (e.g. moving a `design_subassemblies` row to a different `design`, or a
      `designs` row to a different `grid`) is re-checked against the NEW
      target grid, not just the row's current grid — otherwise a caller could
      "move" a row it can edit into a grid it doesn't own.
    - `subassembly_components` one-of-child + cycle validation (see
      `_validate_and_upsert_sync`), for both create and update.

    Never raises to the caller: `ValueError`/`gd_auth.GridAccessDenied` are
    caught and turned into `{"success": False, "error": ...}`.
    """
    policy = GD_TABLE_REGISTRY.get(table)
    if policy is None or policy.scope == "denied":
        return {
            "success": False,
            "error": f"Table '{table}' is not available for generic row access.",
        }

    raw_values = dict(values or {})
    # Caller-supplied audit columns are silently dropped, not rejected — an
    # innocent round-trip of a previously fetched row shouldn't error, but it
    # also must never let the caller set who "wrote" a row.
    raw_values.pop("created_by", None)
    raw_values.pop("updated_by", None)

    unknown = sorted(k for k in raw_values if k not in policy.writable_columns)
    if unknown:
        return {
            "success": False,
            "error": (
                f"Unknown column(s) for table '{table}': {unknown}. "
                f"Allowed columns: {list(policy.writable_columns)}."
            ),
        }

    try:
        if policy.scope == "catalog":
            if organization_id != STAFF_ORG_ID:
                return {
                    "success": False,
                    "error": f"You don't have access to write rows in catalogue table '{table}'.",
                }
            existing_row: dict[str, Any] | None = None
            if row_id is not None:
                existing_row = await asyncio.to_thread(Repository(table).get, row_id)
                if not existing_row:
                    return {"success": False, "error": f"Row {row_id} not found in '{table}'."}
            return await asyncio.to_thread(
                _validate_and_upsert_sync,
                policy,
                table,
                row_id,
                raw_values,
                user_email,
                existing_row,
            )

        # scope == "grid"
        denial = {
            "success": False,
            "error": f"You don't have access to row {row_id} in '{table}', or it doesn't exist.",
        }

        if row_id is None:
            # CREATE: resolve the grid from the (filtered) incoming values.
            # For the `grids` table itself (empty anchor), this reads
            # `values["name"]` — the new grid's own name — since
            # `_resolve_grid_for_row` treats an empty anchor as "the row IS
            # the grid". The caller supplied this value themselves, so
            # echoing it back on denial (below) doesn't leak anything new.
            grid_name = await asyncio.to_thread(_resolve_grid_for_row, policy, raw_values)
            if grid_name is None:
                anchor_hint = policy.grid_anchor[0] if policy.grid_anchor else "name"
                return {
                    "success": False,
                    "error": (
                        "Cannot determine which grid this new row belongs to — "
                        f"include the '{anchor_hint}' field."
                    ),
                }
            await gd_auth.assert_grid_access(grid_name, organization_id)
            return await asyncio.to_thread(
                _validate_and_upsert_sync, policy, table, None, raw_values, user_email, None
            )

        # UPDATE: resolve the grid from the EXISTING row first (the incoming
        # values may not even include the anchor column, e.g. a
        # design_subassemblies qty update never touches `design`).
        existing_row = await asyncio.to_thread(Repository(table).get, row_id)
        if not existing_row:
            return denial

        existing_grid_name = await asyncio.to_thread(_resolve_grid_for_row, policy, existing_row)
        if existing_grid_name is None:
            return denial
        try:
            await gd_auth.assert_grid_access(existing_grid_name, organization_id)
        except gd_auth.GridAccessDenied:
            return denial

        # Anchor-move check: if the filtered update values would change the
        # row's own anchor column (e.g. `design`/`grid`), re-resolve the grid
        # from the MERGED (existing + incoming) row and re-check access
        # against that target too. Closes the "move a row I can edit into a
        # grid I don't own" bypass.
        merged = {**existing_row, **raw_values}
        new_grid_name = await asyncio.to_thread(_resolve_grid_for_row, policy, merged)
        if new_grid_name is None:
            return denial
        if new_grid_name != existing_grid_name:
            try:
                await gd_auth.assert_grid_access(new_grid_name, organization_id)
            except gd_auth.GridAccessDenied:
                return denial

        return await asyncio.to_thread(
            _validate_and_upsert_sync, policy, table, row_id, raw_values, user_email, existing_row
        )
    except (ValueError, gd_auth.GridAccessDenied) as e:
        return {"success": False, "error": str(e)}


async def gd_delete_row(table: str, row_id: str, organization_id: int | None) -> dict[str, Any]:
    """Soft-delete a row (`Repository.soft_delete` — sets `active=False`).

    There is no hard-delete path anywhere in this engine, and this function
    must not add one. Does not check referential integrity (e.g. whether
    other rows still reference this one) — that's a documentation-level
    warning for the LLM-facing tool description, not a runtime check here.

    Catalog-scope access is checked BEFORE any `Repository` call, matching
    `gd_list_rows`/`gd_get_row`/`gd_upsert_row`. Fetching the row first (as a
    prior version of this function did) would let a non-staff caller
    distinguish "row doesn't exist" from "row exists but I can't delete it"
    purely from which error comes back — an existence oracle over catalogue
    tables (`components`, `subassemblies`, etc.) with zero DB access needed.
    """
    policy = GD_TABLE_REGISTRY.get(table)
    if policy is None or policy.scope == "denied":
        return {
            "success": False,
            "error": f"Table '{table}' is not available for generic row access.",
        }

    if policy.scope == "catalog":
        if organization_id != STAFF_ORG_ID:
            return {
                "success": False,
                "error": f"You don't have access to delete rows in catalogue table '{table}'.",
            }
        existing_row = await asyncio.to_thread(Repository(table).get, row_id)
        if not existing_row:
            return {"success": False, "error": f"Row {row_id} not found in '{table}'."}
        await asyncio.to_thread(Repository(table).soft_delete, row_id)
        return {"success": True, "deleted_id": row_id}

    # scope == "grid"
    denial = {
        "success": False,
        "error": f"You don't have access to row {row_id} in '{table}', or it doesn't exist.",
    }
    existing_row = await asyncio.to_thread(Repository(table).get, row_id)
    if not existing_row:
        return denial

    grid_name = await asyncio.to_thread(_resolve_grid_for_row, policy, existing_row)
    if grid_name is None:
        return denial
    try:
        await gd_auth.assert_grid_access(grid_name, organization_id)
    except gd_auth.GridAccessDenied:
        return denial

    await asyncio.to_thread(Repository(table).soft_delete, row_id)
    return {"success": True, "deleted_id": row_id}
