"""Completeness + spot-check tests for package_generator step contracts.

Task 2 of Phase C attaches a `StepContract` to every `@register_step(...)` call
site under `orchestrator/experts/handlers/package_generator/`. This module:

1. Asserts every package_generator step name now has a non-None contract
   (a completeness check specific to this expert; a general lint across ALL
   experts is a later task).
2. Spot-checks a handful of contracts' consumes_state/produces_state/
   consumes_results against what the handler source actually does, for the
   contracts we are most confident about after a manual code audit.

Importing `orchestrator.experts.handlers.package_generator` triggers the
`@register_step` decorators (registration is an import-time side effect) —
this mirrors the pattern `orchestrator/experts/handlers/__init__.py` uses to
register every expert's handlers.
"""

import orchestrator.experts.handlers.package_generator  # noqa: F401  (registration side effect)
from orchestrator.experts.step_registry import get_step_contract

# Every step name registered under package_generator/ (one per @register_step
# call site — confirmed via `grep -rln "@register_step(" package_generator/`).
PACKAGE_GENERATOR_STEP_NAMES = [
    "copy_lpp_template",
    "create_site_folder",
    "dump_lpp_values",
    "fetch_geo_hazard",
    "fetch_solar_potential",
    "generate_site_bom",
    "generate_powerplant_design",
    "generate_distribution_layout",
    "generate_distribution_map",
    "generate_qgis_project",
    "generate_site_layout",
    "populate_bom_tab",
    "populate_lpp_cells",
    "resolve_community_site",
    "resolve_sites",
    "send_lpp_map_to_telegram",
    "update_design_distances",
]

# Snapshot every contract ONCE at module-import (collection) time, right after
# the registration-triggering import above, rather than calling
# `get_step_contract()` fresh inside every test method.
#
# Why this matters: `get_step_registry()` (which `get_step_contract()` reads
# from) is a process-wide singleton, and package_generator's `@register_step`
# decorators only run the FIRST time its submodules are imported per process
# -- later `import` statements are no-ops against `sys.modules`, so they can
# never re-populate a registry that's been cleared. `tests/experts/
# test_parameter_confirmation.py::TestRegisterStepWithoutSchema.setup_method`
# calls `get_step_registry().clear()` with no teardown; if that runs before
# this module's tests execute, any test here that calls `get_step_contract()`
# fresh would see `None` for every name -- a false failure with no real code
# defect behind it, entirely dependent on test collection/execution order
# (see the identical issue documented in `test_contract_lint.py`, which
# shares this pattern and was fixed the same way).
#
# pytest imports (collects) all test modules before running any test
# function's body, so this module-level snapshot is captured before any other
# module's `setup_method`/test body -- including the `.clear()` call above --
# can interfere. Do not replace these with fresh `get_step_contract()` calls
# inside test methods.
_CONTRACTS = {name: get_step_contract(name) for name in PACKAGE_GENERATOR_STEP_NAMES}


class TestPackageGeneratorContractCompleteness:
    """Every package_generator step now has a non-None StepContract."""

    def test_all_step_names_have_contracts(self):
        missing = [name for name, contract in _CONTRACTS.items() if contract is None]
        assert not missing, f"Steps missing a StepContract: {missing}"

    def test_expected_step_count(self):
        # Guards against silently dropping a step name from the list above if
        # a future edit renames/removes a @register_step call site.
        assert len(PACKAGE_GENERATOR_STEP_NAMES) == 17


class TestPackageGeneratorContractSpotChecks:
    """Spot-check specific contracts against a manual read of the handler source."""

    def test_generate_powerplant_design_contract(self):
        contract = _CONTRACTS["generate_powerplant_design"]
        assert contract is not None
        assert "generate_distribution_map" in contract.consumes_results
        assert "design_id" in contract.produces_state
        assert "design_generated" in contract.produces_state
        assert "design_generated" in contract.guard_keys
        # OPTIONAL_DESIGN_PARAMS forwarding — spot-check a couple of names.
        param_names = {p.name for p in contract.params}
        assert "wp_per_conn_override" in param_names
        assert "regulation_constraint" in param_names

    def test_generate_site_bom_contract(self):
        contract = _CONTRACTS["generate_site_bom"]
        assert contract is not None
        assert "design_id" in contract.consumes_state
        assert "bom_generated" in contract.produces_state
        assert "bom_generated" in contract.guard_keys

    def test_create_site_folder_contract(self):
        contract = _CONTRACTS["create_site_folder"]
        assert contract is not None
        # site_name is a hard requirement (`if not site_name: return
        # StepResult.failure(...)`); site_folder_id is only an idempotency
        # guard read (`existing_folder_id = get_state(...); if
        # existing_folder_id: ... skip`) -- the step creates the folder fine
        # without it, so it lives in optional_consumes_state instead.
        assert contract.consumes_state == ("site_name",)
        assert contract.optional_consumes_state == ("site_folder_id",)
        assert contract.produces_state == ("site_folder_id",)
        assert contract.guard_keys == ("site_folder_id",)

    def test_update_design_distances_contract(self):
        contract = _CONTRACTS["update_design_distances"]
        assert contract is not None
        assert "design_id" in contract.consumes_state
        # Both distances are read with a documented graceful-skip fallback
        # (`if avg_pv_combiner is None and feeder_pillar is None: return
        # StepResult(data={"skipped": True, ...})` -- not a failure), so
        # they're optional, not hard-required.
        assert "avg_pv_combiner_distance_m" in contract.optional_consumes_state
        assert "feeder_pillar_distance_m" in contract.optional_consumes_state
        assert contract.produces_state == ("design_distances_updated",)
        assert contract.guard_keys == ("design_distances_updated",)

    def test_send_lpp_map_to_telegram_has_no_state_writes(self):
        # This handler never returns state_updates on any path — verified by
        # reading every StepResult(...) return in send_map_to_telegram.py.
        contract = _CONTRACTS["send_lpp_map_to_telegram"]
        assert contract is not None
        assert contract.produces_state == ()
        assert "generate_distribution_map" in contract.consumes_results

    def test_resolve_community_site_compound_guard(self):
        contract = _CONTRACTS["resolve_community_site"]
        assert contract is not None
        assert set(contract.guard_keys) == {"geo_source", "footprint_count"}
        assert "footprint_count" in contract.produces_state

    def test_generate_distribution_layout_community_route_dependency(self):
        # generate_distribution_layout calls site_geo_source.load_site_row_data(),
        # which — only on the community route (geo_source == "community") — reads
        # community_boundary_drive_id/community_buildings_drive_id/community_state
        # from packet state (the drive_id keys are the cross-execution Drive-download
        # fallback used when get_previous_result("resolve_community_site") is empty,
        # e.g. run_single_step re-running this step in a later execution) and depends
        # on resolve_community_site having run first. It also reads
        # surveyed_buildings_geojson unconditionally on both routes. Each of these
        # four has a genuine in-body fallback (see generate_distribution_layout.py's
        # contract comments) and none is produced by THIS step's own produces_state
        # (resolve_community_site produces the drive_id keys), so all four live in
        # optional_consumes_state (Phase D) rather than consumes_state -- otherwise
        # `validate_step_prerequisites` could never report this step satisfied on
        # the community route.
        contract = _CONTRACTS["generate_distribution_layout"]
        assert contract is not None
        assert "resolve_community_site" in contract.consumes_results
        assert "community_boundary_drive_id" in contract.optional_consumes_state
        assert "community_buildings_drive_id" in contract.optional_consumes_state
        assert "community_state" in contract.optional_consumes_state
        assert "surveyed_buildings_geojson" in contract.optional_consumes_state
        assert "community_boundary_geojson" not in contract.optional_consumes_state
        assert "community_buildings_geojson" not in contract.optional_consumes_state
        assert "community_boundary_drive_id" not in contract.consumes_state
        assert "community_buildings_drive_id" not in contract.consumes_state
        assert "community_state" not in contract.consumes_state
        assert "surveyed_buildings_geojson" not in contract.consumes_state

    def test_generate_distribution_map_community_route_dependency(self):
        # Same cross-module dependency as generate_distribution_layout above:
        # generate_distribution_map also calls site_geo_source.load_site_row_data()
        # on the community route. Same Phase D reclassification applies.
        contract = _CONTRACTS["generate_distribution_map"]
        assert contract is not None
        assert "resolve_community_site" in contract.consumes_results
        assert "community_boundary_drive_id" in contract.optional_consumes_state
        assert "community_buildings_drive_id" in contract.optional_consumes_state
        assert "community_state" in contract.optional_consumes_state
        assert "surveyed_buildings_geojson" in contract.optional_consumes_state
        assert "community_boundary_geojson" not in contract.optional_consumes_state
        assert "community_buildings_geojson" not in contract.optional_consumes_state
        assert "community_boundary_drive_id" not in contract.consumes_state
        assert "community_buildings_drive_id" not in contract.consumes_state
        assert "community_state" not in contract.consumes_state
        assert "surveyed_buildings_geojson" not in contract.consumes_state
