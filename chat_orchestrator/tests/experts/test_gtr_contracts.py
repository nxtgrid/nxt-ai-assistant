"""Completeness + spot-check tests for grids_technical_reviewer (GTR) step
contracts.

Task 8.1-8.2/8.5 of docs/superpowers/plans/2026-08-20-expert-steps-as-skill-
tools.md attaches a `StepContract` to every `@register_step(...)` call site
under `orchestrator/experts/handlers/grids_technical_reviewer/`. This module:

1. Asserts every GTR step name now has a non-None contract (a completeness
   check specific to this expert; the general cross-expert lint lives in
   test_contract_lint.py, extended in the same phase to cover GTR).
2. Spot-checks `write_review_section` is the ONLY real mutation (Task 8.2),
   and that the 3 pre-existing `exposed_to_builder` fetches stay real in mock
   mode because they're non-mutating (Task 8.5) -- `WorkflowExecutor.
   _execute_function_step`'s mock-mode short-circuit only ever fires for a
   `contract.mutates=True` step, so a `mutates=False` contract IS the
   mechanism that keeps a read running for real regardless of dry_run.

Importing `orchestrator.experts.handlers.grids_technical_reviewer` triggers
the `@register_step` decorators (registration is an import-time side effect)
-- mirrors the pattern `orchestrator/experts/handlers/__init__.py` uses to
register every expert's handlers.
"""

import orchestrator.experts.handlers.grids_technical_reviewer  # noqa: F401  (registration side effect)
from orchestrator.experts.step_registry import get_step_contract, get_step_registry

# Every step name registered under grids_technical_reviewer/ (one per
# @register_step call site -- confirmed via
# `grep -rln "@register_step(" grids_technical_reviewer/`, and matches
# that package's own __init__.py __all__).
GTR_STEP_NAMES = [
    "resolve_grid_sheets",
    "check_existing_review",
    "fetch_existing_review",
    "fetch_chat_chronology",
    "gtr_analysis_conversation",
    "fetch_grafana_kpis",
    "fetch_cuf_sub_values",
    "fetch_pending_actions",
    "write_review_section",
]

# Snapshot every contract ONCE at module-import (collection) time -- see
# test_package_generator_contracts.py/test_contract_lint.py for why this
# matters (a process-wide registry singleton + a same-process `.clear()` with
# no teardown elsewhere in the suite). Do not replace with fresh
# `get_step_contract()` calls inside test methods.
_CONTRACTS = {name: get_step_contract(name) for name in GTR_STEP_NAMES}


class TestGTRContractCompleteness:
    """Every GTR step now has a non-None StepContract."""

    def test_every_gtr_step_has_a_contract(self):
        missing = [name for name, contract in _CONTRACTS.items() if contract is None]
        assert not missing, f"GTR steps missing a StepContract: {missing}"

    def test_known_step_count_matches_the_registry(self):
        registry = get_step_registry()
        for name in GTR_STEP_NAMES:
            assert registry.has_handler(name), f"{name} is not a registered handler"


class TestWriteReviewSectionIsTheOnlyMutation:
    """Task 8.2: write_review_section is the one real mutation in GTR."""

    def test_write_review_section_mutates(self):
        contract = _CONTRACTS["write_review_section"]
        assert contract.mutates is True
        assert contract.mutation_kind == "external_write"

    def test_write_review_section_has_a_mock(self):
        assert _CONTRACTS["write_review_section"].mock is not None

    def test_every_other_gtr_step_does_not_mutate(self):
        non_mutating = [
            name for name in GTR_STEP_NAMES if name != "write_review_section"
        ]
        still_mutating = [
            name for name in non_mutating if _CONTRACTS[name].mutates
        ]
        assert not still_mutating, (
            f"Expected only write_review_section to mutate; also mutating: {still_mutating}"
        )


class TestExposedFetchesStayRealInMockMode:
    """Task 8.5: the 3 pre-existing exposed fetches are read-only and need no
    mocks -- confirmed here by asserting mutates=False, which is literally
    the flag WorkflowExecutor._execute_function_step's mock-mode
    short-circuit checks (see that method's docstring): a non-mutating step
    is never short-circuited, dry_run or not."""

    EXPOSED_FETCH_NAMES = ("fetch_chat_chronology", "fetch_grafana_kpis", "fetch_pending_actions")

    def test_all_three_are_exposed_to_the_builder(self):
        exposed = set(get_step_registry().builder_exposed_handlers())
        for name in self.EXPOSED_FETCH_NAMES:
            assert name in exposed, f"{name} is not exposed_to_builder"

    def test_all_three_do_not_mutate(self):
        for name in self.EXPOSED_FETCH_NAMES:
            assert _CONTRACTS[name].mutates is False, f"{name} unexpectedly mutates"

    def test_all_three_have_no_mock_spec(self):
        # A MockSpec on a non-mutating step would be dead weight -- mock mode
        # never consults it. Confirms none was accidentally added.
        for name in self.EXPOSED_FETCH_NAMES:
            assert _CONTRACTS[name].mock is None, f"{name} has an unused MockSpec"


class TestGtrAnalysisConversationIsFlaggedAsAnLlmCandidate:
    """Task 8.3: gtr_analysis_conversation is already an LLM tool-loop --
    contracted (so it's not silently invisible to the lint/tooling), but its
    side_effects prose must say so, since nothing mechanical enforces "map
    this to an [llm] step, not kind:function" at conversion time (Phase 7's
    converter has no special case for it -- see that handler's own
    docstring)."""

    def test_side_effects_mentions_the_llm_step_mapping(self):
        contract = _CONTRACTS["gtr_analysis_conversation"]
        assert 'kind:"llm"' in contract.side_effects

    def test_does_not_mutate(self):
        assert _CONTRACTS["gtr_analysis_conversation"].mutates is False
