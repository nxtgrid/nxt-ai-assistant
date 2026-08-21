"""Completeness + spot-check tests for grid_analyst step contracts.

Task 10.2-10.5 of docs/superpowers/plans/2026-08-20-expert-steps-as-skill-
tools.md attaches a `StepContract` to every `@register_step(...)` call site
under `orchestrator/experts/handlers/grid_analyst/`. This module:

1. Asserts every grid_analyst step name now has a non-None contract (a
   completeness check specific to this expert; the general cross-expert lint
   lives in test_contract_lint.py, extended in the same phase to cover
   grid_analyst).
2. Spot-checks Task 10.3's mutation classification (create_analysis_doc/
   create_kpi_doc are the two real mutations; the other 5 read/compute only)
   and Task 10.4's finding that analyze_failures_loop needs no special
   handling -- its "loop" is entirely internal to the handler's own body,
   the same shape as several already-contracted GTR/LPP steps that call an
   MCP tool once per item.
3. Spot-checks the get_input-vs-get_state design decision this expert's
   contracts made differently from GTR/LPP's: every caller-suppliable value
   here is modeled as a `param` (ParamSpec), never `consumes_state` --
   because these handlers read exclusively via `context.get_input(...)`,
   never falling back to `context.get_state(...)` the way LPP/GTR's
   get_input-then-get_state handlers do, and `validate_step_prerequisites`'
   `consumes_state` check never consults `packet_inputs` (only `params`
   does) -- declaring these as consumes_state would create a real, silent
   precondition-check bug: a value legitimately supplied via packet_inputs
   would be reported "missing".

Importing `orchestrator.experts.handlers.grid_analyst` triggers the
`@register_step` decorators (registration is an import-time side effect) --
mirrors the pattern `orchestrator/experts/handlers/__init__.py` uses to
register every expert's handlers.
"""

import orchestrator.experts.handlers.grid_analyst  # noqa: F401  (registration side effect)
from orchestrator.experts.step_registry import get_step_contract, get_step_registry

# Every step name registered under grid_analyst/ (one per @register_step call
# site -- confirmed via `grep -n "@register_step(" analyze_failures.py
# create_report.py fetch_metrics.py`, and matches that package's own
# __init__.py __all__, corrected in this same phase to list all 7).
GRID_ANALYST_STEP_NAMES = [
    "fetch_month_metrics",
    "fetch_multi_grid_metrics",
    "analyze_failures_loop",
    "categorize_issues",
    "create_analysis_doc",
    "create_kpi_doc",
    "calculate_kpi_values",
]

# Snapshot every contract ONCE at module-import (collection) time -- see
# test_package_generator_contracts.py/test_contract_lint.py for why this
# matters (a process-wide registry singleton + a same-process `.clear()` with
# no teardown elsewhere in the suite). Do not replace with fresh
# `get_step_contract()` calls inside test methods.
_CONTRACTS = {name: get_step_contract(name) for name in GRID_ANALYST_STEP_NAMES}

MUTATING_STEPS = {"create_analysis_doc", "create_kpi_doc"}
NON_MUTATING_STEPS = set(GRID_ANALYST_STEP_NAMES) - MUTATING_STEPS


class TestGridAnalystContractCompleteness:
    """Every grid_analyst step now has a non-None StepContract."""

    def test_every_step_has_a_contract(self):
        missing = [name for name, contract in _CONTRACTS.items() if contract is None]
        assert not missing, f"grid_analyst steps missing a StepContract: {missing}"

    def test_known_step_count_matches_the_registry(self):
        registry = get_step_registry()
        for name in GRID_ANALYST_STEP_NAMES:
            assert registry.has_handler(name), f"{name} is not a registered handler"


class TestMutationClassification:
    """Task 10.3: create_analysis_doc/create_kpi_doc are the two real
    mutations (each creates a Google Doc via google_docs_create); the other
    5 steps read Grafana/MCP data or compute in memory only."""

    def test_the_two_doc_creation_steps_mutate(self):
        for name in MUTATING_STEPS:
            assert _CONTRACTS[name].mutates is True, name
            assert _CONTRACTS[name].mutation_kind == "external_write", name

    def test_the_two_doc_creation_steps_have_a_mock(self):
        for name in MUTATING_STEPS:
            assert _CONTRACTS[name].mock is not None, name

    def test_every_other_step_does_not_mutate(self):
        still_mutating = [name for name in NON_MUTATING_STEPS if _CONTRACTS[name].mutates]
        assert not still_mutating, f"Expected mutates=False: {still_mutating}"


class TestAnalyzeFailuresLoopIsAnOrdinaryStep:
    """Task 10.4: the one structural unknown the plan flagged for this phase
    -- confirmed NOT to need special handling. The 'loop' analyze_failures_loop
    refers to is entirely internal to its own handler body (iterates a list
    of alerts, calling an MCP tool once per alert); the step model sees one
    ordinary step returning one ordinary StepResult, same as e.g. GTR's
    fetch_cuf_sub_values."""

    def test_has_a_normal_contract_like_any_other_step(self):
        contract = _CONTRACTS["analyze_failures_loop"]
        assert contract is not None
        assert contract.mutates is False

    def test_side_effects_documents_the_finding(self):
        contract = _CONTRACTS["analyze_failures_loop"]
        assert "structural unknown" in contract.side_effects.lower()


class TestGetInputValuesAreModeledAsParams:
    """This expert's design decision: every caller-suppliable value is a
    `param`, never `consumes_state` -- see this module's docstring for why
    (validate_step_prerequisites never checks packet_inputs for
    consumes_state, only for params)."""

    def test_no_step_declares_a_hard_consumes_state_key(self):
        with_consumes_state = [
            name for name in GRID_ANALYST_STEP_NAMES if _CONTRACTS[name].consumes_state
        ]
        assert not with_consumes_state, (
            f"Expected consumes_state=() for every grid_analyst step: {with_consumes_state}"
        )

    def test_fetch_month_metrics_declares_grid_as_a_required_param(self):
        contract = _CONTRACTS["fetch_month_metrics"]
        param_names = {p.name: p for p in contract.params}
        assert "grid" in param_names
        assert param_names["grid"].required is True

    def test_fetch_multi_grid_metrics_declares_a_raw_request_fallback_param(self):
        contract = _CONTRACTS["fetch_multi_grid_metrics"]
        param_names = {p.name for p in contract.params}
        assert {"grids", "time_range", "raw_request"} <= param_names
