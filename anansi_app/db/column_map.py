"""Single source of truth mapping the AppSheet Google-Sheet tables to the new
``gd_*`` Postgres tables in the shared Supabase chat DB.

Used by both ``scripts/gen_schema.py`` (emits ``db/schema.sql``) and
``scripts/import_from_xlsx.py`` (loads data), so the schema and the importer can
never drift.

Conventions
-----------
* ``bare`` is the table name without the ``gd_`` prefix (the prefix is applied by
  the generator and by ``grid_app.lib.db.Repository``).
* ``refs``/``numeric``/``boolean`` are keyed by the *normalised* (snake_case)
  column name — i.e. the output of ``normalize.to_snake_case`` after
  ``header_overrides`` are applied.
* ``header_overrides`` are keyed by the *exact original* spreadsheet header, for
  the handful of headers the generic normaliser gets wrong.
* FK relationships are documented (and indexed) but NOT enforced with hard
  constraints in v1 — historical AppSheet data contains dangling refs, and hard
  FKs would block the migration. Tighten later once data is cleaned.
"""

from __future__ import annotations

# Tabs in the workbook that are scratch/helper/archive — never imported.
SPURIOUS_TABS = {
    "Schema Info",
    "Component Translations EN-FR",
    "Checks on Components",
    "ARCHIVEGrid Alerts",
    "component source double check a",
    "Sheet23",
}

# Columns that are virtual in AppSheet (computed, not present in the sheet) but
# that the compute engines need persisted. Added to the schema, populated by a
# recompute step. Keyed by bare table -> {col: sql_type}.
VIRTUAL_COLUMNS = {
    "components": {
        # Sourced from the BoS Purchases ledger via the cost-projection engine
        # (see grid_app/services/cost_projection.py). ddp_cost = weighted average
        # actual landed cost; projected_cost = time-decay inflation projection.
        "ddp_cost": "numeric",
        "projected_cost": "numeric",
        # Projection metadata (provenance for the just-in-time recompute).
        "cost_confidence": "text",
        "num_purchases": "numeric",
        "cost_projected_at": "timestamptz",
    },
}

TABLES: list[dict] = [
    {
        "bare": "organizations",
        "tab": "Organizations",
        "label": "Organizations",
        "icon": "🏢",
        "group": "Admin",
        "header_overrides": {"Created at": "created_at", "Created by": "created_by"},
        "refs": {},
        "numeric": set(),
        "boolean": {"active"},
    },
    {
        "bare": "users",
        "tab": "Users",
        "label": "Users",
        "icon": "👤",
        "group": "Admin",
        "header_overrides": {},
        "refs": {"organization": "organizations"},
        "numeric": set(),
        "boolean": {"active"},
    },
    {
        "bare": "grids",
        "tab": "Grids",
        "label": "Grids",
        "icon": "🏘️",
        "group": "Sites",
        "header_overrides": {},
        "refs": {},
        "numeric": set(),
        "boolean": {"active"},
    },
    {
        "bare": "grid_coords",
        "tab": "Grid Coords",
        "label": "Grid Coordinates",
        "icon": "📍",
        "group": "Sites",
        "header_overrides": {},
        "refs": {"grid": "grids"},
        "numeric": set(),
        "boolean": {"active"},
    },
    {
        "bare": "components",
        "tab": "Components",
        "label": "Components",
        "icon": "🔩",
        "group": "Catalogue",
        "header_overrides": {"Do not miss": "do_not_miss"},
        "refs": {},
        "numeric": {"unit_cost_usd", "unit_cost_ngn", "contingency_pct"},
        "boolean": {"active", "do_not_miss"},
    },
    {
        "bare": "subassemblies",
        "tab": "Subassemblies",
        "label": "Subassemblies",
        "icon": "📦",
        "group": "Catalogue",
        "header_overrides": {},
        "refs": {"main_component": "components"},
        "numeric": {"unit_rental_usd_per_month"},
        "boolean": {"active", "components_active"},
    },
    {
        "bare": "subassembly_components",
        "tab": "Subassembly Components",
        "label": "Subassembly Components",
        "icon": "🧩",
        "group": "Catalogue",
        "header_overrides": {},
        "refs": {
            "subassembly": "subassemblies",
            "component_subassembly": "subassemblies",
            "component": "components",
        },
        "numeric": {"qty"},
        "boolean": {"active"},
    },
    {
        "bare": "design_rules",
        "tab": "Design Rules",
        "label": "Design Rules",
        "icon": "📐",
        "group": "Catalogue",
        "header_overrides": {},
        "refs": {},
        "numeric": set(),
        "boolean": {"active", "implemented"},
    },
    {
        "bare": "designs",
        "tab": "Designs",
        "label": "Designs",
        "icon": "⚡",
        "group": "Engineering",
        "header_overrides": {
            "Recalculate BoM on Design Change?": "recalculate_bom_on_design_change",
            "Number of PoC teams to install meters": "number_of_poc_teams_to_install_meters",
        },
        "refs": {"grid": "grids"},
        "numeric": {
            "max_connections",
            "initial_residential_connections",
            "initial_business_connections",
            "initial_3_phase_connections",
            "average_service_drop_length_m",
            "number_of_poc_teams_to_install_meters",
            "anchor_load_kw",
            "pue_hours_per_day",
            "daily_generation_potential_kwh_kwp",
            "target_kwp",
            "target_kwh",
            "target_tariff_usd",
            "max_distance_to_center_of_consumption",
            # Number in AppSheet ("Wp per conn override?" — Wp per connection);
            # was mistyped boolean until scripts/migration_gd_designs_param_types.sql
            "wp_per_conn_override",
            "pv_area_sqm",
            "avg_distance_to_pv_combiner",
            "distance_to_feeder_pillar",
            "usd_to_ngn",
            "kwp",
            "kva",
            "kwh",
            "bom_cost_estimate",
            "works_cost_estimate",
            "monthly_rental_estimate",
            "monthly_saleable_kwh_at_expected_cuf",
            "monthly_revenue_at_expected_cuf_and_tariff",
        },
        "boolean": {
            "active",
            "force_3_phase",
            # constrain_design_to_known_regulation is enum text ("None" /
            # "Nigeria - DARES"), not boolean — stays untyped (text).
            "auto_design",
            "recalculate_bom_on_design_change",
        },
        "timestamp": {"xrate_updated_at", "bom_generated_at"},
    },
    {
        "bare": "design_subassemblies",
        "tab": "Design Subassemblies",
        "label": "Design Subassemblies",
        "icon": "🔧",
        "group": "Engineering",
        "header_overrides": {},
        "refs": {"design": "designs", "subassembly": "subassemblies"},
        "numeric": {"qty", "kwp", "kwh", "kva"},
        "boolean": {"active"},
    },
    {
        "bare": "bom_items",
        "tab": "BOM Items",
        "label": "BOM Items",
        "icon": "🧾",
        "group": "Engineering",
        "header_overrides": {
            "Verified to be in items to be shipped": "verified_in_shipment",
        },
        "refs": {
            "item": "components",
            "design": "designs",
            "job": "jobs",
            "subassembly": "subassemblies",
        },
        "numeric": {
            "qty",
            "qty_with_contingency",
            "unit_cost_ngn",
            "total_cost_ngn",
            "monthly_rental_usd",
            "received_count",
            "returned_count",
        },
        "boolean": {"active", "verified_in_shipment"},
    },
    {
        "bare": "procedures",
        "tab": "Procedures",
        "label": "Procedures",
        "icon": "📋",
        "group": "Field Ops",
        "header_overrides": {},
        "refs": {},
        "numeric": {"qty"},
        "boolean": {"active"},
    },
    {
        "bare": "procedure_steps",
        "tab": "Procedure Steps",
        "label": "Procedure Steps",
        "icon": "✅",
        "group": "Field Ops",
        "header_overrides": {"Order": "step_order"},  # "order" is a SQL reserved word
        "refs": {"procedure": "procedures", "conditional_on": "procedure_steps"},
        "numeric": {"step_order"},
        "boolean": {"active", "requires_proof"},
    },
    {
        "bare": "jobs",
        "tab": "Jobs",
        "label": "Jobs",
        "icon": "🛠️",
        "group": "Field Ops",
        "header_overrides": {},
        "refs": {"grid": "grids", "organization": "organizations", "technician": "users"},
        "numeric": {"expected_days_to_complete", "cost_to_developer", "cost_to_nxt"},
        "boolean": {"active"},
    },
    {
        "bare": "job_procedures",
        "tab": "Job Procedures",
        "label": "Job Procedures",
        "icon": "🗂️",
        "group": "Field Ops",
        "header_overrides": {},
        "refs": {"job": "jobs", "procedure": "procedures"},
        "numeric": {"qty", "sequence_in_job"},
        "boolean": {"active"},
    },
    {
        "bare": "job_steps",
        "tab": "Job Steps",
        "label": "Job Steps",
        "icon": "☑️",
        "group": "Field Ops",
        "header_overrides": {},
        "refs": {
            "job": "jobs",
            "job_procedure": "job_procedures",
            "step_reference": "procedure_steps",
        },
        "numeric": {"sequence"},
        "boolean": {"active", "approved"},
    },
    {
        "bare": "job_subassemblies",
        "tab": "Job Subassemblies",
        "label": "Job Subassemblies",
        "icon": "📦",
        "group": "Field Ops",
        "header_overrides": {},
        "refs": {"job": "jobs", "subassembly": "subassemblies"},
        "numeric": {"qty"},
        "boolean": {"active"},
    },
    # ── Procurement ledger (from "NXT-3053 - BoS Purchases…" workbook) ───────────
    # The live purchase history ops enters. Drives component cost projection.
    # Imported via scripts/import_purchases.py (separate source workbook), so tab
    # is None here and manual_columns defines the schema.
    {
        "bare": "purchases",
        "tab": None,
        "label": "Purchases (BoS)",
        "icon": "🛒",
        "group": "Procurement",
        "header_overrides": {},
        "refs": {},
        "numeric": {"qty", "landed_unit_cost_usd", "total_cost"},
        "boolean": {"active"},
        "manual_columns": [
            {"name": "date", "type": "timestamptz", "ref": None},
            {"name": "item_description", "type": "text", "ref": None},
            {"name": "currency", "type": "text", "ref": None},
            {"name": "qty", "type": "numeric", "ref": None},
            {"name": "landed_unit_cost_usd", "type": "numeric", "ref": None},
            {"name": "total_cost", "type": "numeric", "ref": None},
        ],
    },
    # ── Lookup tables sourced from the external "Sizing DB" Google Sheet ──────────
    # These tabs are NOT in the exported workbook (the Sizing DB was not exported).
    # The compute engines need them, so the tables exist and are populated either
    # by exporting the Sizing DB or by manual entry in-app. `tab` is None so the
    # xlsx importer skips them; `manual_columns` drives the schema generator.
    {
        "bare": "unit_rental_prices",
        "tab": None,
        "label": "Unit Rental Prices",
        "icon": "💵",
        "group": "Catalogue",
        "header_overrides": {},
        "refs": {},
        "numeric": {"unit_monthly_rental"},
        "boolean": {"active"},
        "manual_columns": [
            {"name": "item", "type": "text", "ref": None},
            {"name": "engineering_item_name", "type": "text", "ref": None},
            {"name": "unit_monthly_rental", "type": "numeric", "ref": None},
        ],
    },
    {
        "bare": "wp_per_conn_lookup",
        "tab": None,
        "label": "Wp per Conn Lookup",
        "icon": "📈",
        "group": "Catalogue",
        "header_overrides": {},
        "refs": {},
        "numeric": {"nonresidential_threshold", "wp_per_conn", "kwh_per_kwp"},
        "boolean": {"active"},
        "manual_columns": [
            {"name": "nonresidential_threshold", "type": "numeric", "ref": None},
            {"name": "wp_per_conn", "type": "numeric", "ref": None},
            {"name": "kwh_per_kwp", "type": "numeric", "ref": None},
        ],
    },
]

# FK-safe import order (parents before children) — only tables that come from the
# exported workbook (those with a source tab).
IMPORT_ORDER = [t["bare"] for t in TABLES if t.get("tab")]

BY_BARE = {t["bare"]: t for t in TABLES}
