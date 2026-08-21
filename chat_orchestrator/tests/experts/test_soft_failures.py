"""Tests for soft failures (Phase 3 of
docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md).

Two things are covered:

- `TestStepResultSoftFailure` / `TestSoftFailureCodes`: the new
  `StepResult.soft_failure()` vocabulary itself -- a contract-detectable
  misuse that must NOT halt the run, unlike `StepResult.failure()`.
- `TestSoftFailureBeforeRunningStep`: `WorkflowExecutor.
  _soft_failure_before_running_step`, the pre-flight check that converts a
  `PrereqReport` (from the ALREADY-EXISTING, already-tested
  `validate_step_prerequisites` -- see `test_workflow_executor.py`'s
  `TestValidateStepPrerequisites`, which this deliberately reuses rather
  than re-deriving "which step produces this key") into the soft-failure
  vocabulary, plus the one check `validate_step_prerequisites` doesn't do:
  `guard_keys`.

`TestSoftFailureDoesNotGateExecuteFunctionStep` locks in this phase's
architectural decision: gating is NOT inside `_execute_function_step` --
every existing in-recipe-order call (every production LPP/GTR run today)
must stay completely unaffected by this phase's existence.
"""

from __future__ import annotations

from typing import Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.experts.step_context import (
    SOFT_FAILURE_CODES,
    StepContext,
    StepResult,
)
from orchestrator.experts.step_contracts import ParamSpec, StepContract
from orchestrator.experts.step_registry import get_step_registry
from orchestrator.experts.workflow_executor import WorkflowExecutor


class TestSoftFailureCodes:
    """SOFT_FAILURE_CODES: the closed vocabulary soft_failure()'s `code` draws from."""

    def test_contains_all_five_codes(self):
        assert SOFT_FAILURE_CODES == (
            "missing_parameter",
            "invalid_parameter",
            "unmet_prerequisite",
            "guard_satisfied",
            "not_permitted",
        )

    def test_is_a_tuple(self):
        assert isinstance(SOFT_FAILURE_CODES, tuple)


class TestStepResultSoftFailure:
    """StepResult.soft_failure() construction and the must-not-halt property."""

    def test_sets_code_message_and_remediation(self):
        result = StepResult.soft_failure(
            code="unmet_prerequisite",
            message="'populate_lpp_cells' is missing: 'document_id'.",
            remediation="'document_id' is produced by calling 'copy_lpp_template' first.",
        )
        assert result.soft_failure_code == "unmet_prerequisite"
        assert result.soft_failure_message == "'populate_lpp_cells' is missing: 'document_id'."
        assert result.remediation == (
            "'document_id' is produced by calling 'copy_lpp_template' first."
        )

    def test_remediation_defaults_to_empty_string_not_none(self):
        result = StepResult.soft_failure(code="guard_satisfied", message="already done")
        assert result.remediation == ""

    def test_error_stays_none_the_must_not_halt_property(self):
        """This is THE difference from StepResult.failure(): error must stay
        None, because WorkflowExecutor._execute_one_step's `if result.error:`
        check is exactly what halts a packet's whole run."""
        result = StepResult.soft_failure(code="unmet_prerequisite", message="x")
        assert result.error is None

    def test_is_success_is_false(self):
        """A soft failure did not succeed, even though it must not halt --
        is_success and 'halts the run' are deliberately different questions
        (see error_stays_none above for the halt question)."""
        result = StepResult.soft_failure(code="unmet_prerequisite", message="x")
        assert result.is_success is False

    def test_is_soft_failure_is_true(self):
        result = StepResult.soft_failure(code="unmet_prerequisite", message="x")
        assert result.is_soft_failure is True

    def test_ordinary_success_is_not_a_soft_failure(self):
        """Regression: confirms this phase didn't change success()'s behavior."""
        result = StepResult.success(data={"ok": True})
        assert result.is_soft_failure is False
        assert result.is_success is True

    def test_ordinary_failure_is_not_a_soft_failure(self):
        """Regression: confirms failure() (the hard-failure path) is
        unaffected and distinguishable from a soft failure."""
        result = StepResult.failure("boom")
        assert result.is_soft_failure is False
        assert result.is_success is False
        assert result.error == "boom"


class TestSoftFailureBeforeRunningStep:
    """WorkflowExecutor._soft_failure_before_running_step."""

    @pytest.fixture
    def mock_packet_service(self):
        mock = MagicMock()
        mock.find_similar_completed = AsyncMock(return_value=[])
        return mock

    @pytest.fixture
    def executor(self, mock_packet_service):
        return WorkflowExecutor(None, mock_packet_service, None)

    @pytest.fixture(autouse=True)
    def _cleanup_registry(self):
        """Mirrors test_workflow_executor.py's TestValidateStepPrerequisites
        fixture exactly, since this method is built directly on top of
        validate_step_prerequisites and needs the same registry setup."""
        registered: list[str] = []
        registry = get_step_registry()

        def _register(name, handler=None, contract=None):
            handler = handler or (lambda ctx: None)
            registry.register(name, handler, contract=contract)
            registered.append(name)

        yield _register

        for name in registered:
            registry.unregister(name)

    def _context(self, packet_state: Dict | None = None) -> StepContext:
        return StepContext(
            packet_id="packet-1",
            packet_type="skill_run",
            packet_goal="Do the thing",
            packet_inputs={},
            packet_state=packet_state or {},
            current_step="step_1",
            steps_completed=[],
            session_id="session-1",
        )

    @pytest.mark.asyncio
    async def test_no_contract_returns_none(self, executor, _cleanup_registry):
        _cleanup_registry("plain_step")

        result = await executor._soft_failure_before_running_step(self._context(), "plain_step")

        assert result is None

    @pytest.mark.asyncio
    async def test_all_prerequisites_satisfied_returns_none(self, executor, _cleanup_registry):
        contract = StepContract(consumes_state=("document_id",))
        _cleanup_registry("populate_cells", contract=contract)

        result = await executor._soft_failure_before_running_step(
            self._context(packet_state={"document_id": "doc-1"}), "populate_cells"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_guard_key_already_true_returns_guard_satisfied(
        self, executor, _cleanup_registry
    ):
        contract = StepContract(guard_keys=("cells_populated",))
        _cleanup_registry("populate_cells", contract=contract)

        result = await executor._soft_failure_before_running_step(
            self._context(packet_state={"cells_populated": True}), "populate_cells"
        )

        assert result is not None
        assert result.soft_failure_code == "guard_satisfied"
        assert result.error is None
        assert "cells_populated" in result.soft_failure_message

    @pytest.mark.asyncio
    async def test_guard_key_explicitly_false_does_not_short_circuit(
        self, executor, _cleanup_registry
    ):
        """Truthiness, not presence: an explicit False guard key means 'not
        done yet', so this must fall through to the ordinary prerequisite
        check (which passes here -- no consumes_state declared)."""
        contract = StepContract(guard_keys=("cells_populated",))
        _cleanup_registry("populate_cells", contract=contract)

        result = await executor._soft_failure_before_running_step(
            self._context(packet_state={"cells_populated": False}), "populate_cells"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_guard_key_absent_does_not_short_circuit(self, executor, _cleanup_registry):
        contract = StepContract(guard_keys=("cells_populated",))
        _cleanup_registry("populate_cells", contract=contract)

        result = await executor._soft_failure_before_running_step(
            self._context(packet_state={}), "populate_cells"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_guard_satisfied_takes_precedence_over_unmet_prerequisite(
        self, executor, _cleanup_registry
    ):
        """Work already done outranks 'here's what's missing' -- no point
        telling the caller how to (re)do work that doesn't need doing."""
        contract = StepContract(
            guard_keys=("cells_populated",), consumes_state=("document_id",)
        )
        _cleanup_registry("populate_cells", contract=contract)

        result = await executor._soft_failure_before_running_step(
            self._context(packet_state={"cells_populated": True}), "populate_cells"
        )

        assert result.soft_failure_code == "guard_satisfied"

    @pytest.mark.asyncio
    async def test_missing_state_with_known_producer_names_it_in_remediation(
        self, executor, _cleanup_registry
    ):
        """The exact LPP scenario from the plan: populate_lpp_cells needs
        document_id, which copy_lpp_template produces."""
        producer_contract = StepContract(produces_state=("document_id",))
        consumer_contract = StepContract(consumes_state=("document_id",))
        _cleanup_registry("copy_lpp_template", contract=producer_contract)
        _cleanup_registry("populate_lpp_cells", contract=consumer_contract)

        result = await executor._soft_failure_before_running_step(
            self._context(), "populate_lpp_cells"
        )

        assert result is not None
        assert result.soft_failure_code == "unmet_prerequisite"
        assert result.error is None
        assert "document_id" in result.soft_failure_message
        assert "copy_lpp_template" in result.remediation

    @pytest.mark.asyncio
    async def test_missing_state_with_no_known_producer(self, executor, _cleanup_registry):
        contract = StepContract(consumes_state=("orphan_key",))
        _cleanup_registry("needs_orphan", contract=contract)

        result = await executor._soft_failure_before_running_step(self._context(), "needs_orphan")

        assert result.soft_failure_code == "unmet_prerequisite"
        assert "orphan_key" in result.soft_failure_message
        assert "no known producer" in result.remediation

    @pytest.mark.asyncio
    async def test_missing_results_names_the_step_to_call(self, executor, _cleanup_registry):
        contract = StepContract(consumes_results=("prior_step",))
        _cleanup_registry("needs_prior_result", contract=contract)
        _cleanup_registry("prior_step")

        result = await executor._soft_failure_before_running_step(
            self._context(), "needs_prior_result"
        )

        assert result.soft_failure_code == "unmet_prerequisite"
        assert "prior_step" in result.soft_failure_message
        assert "prior_step" in result.remediation

    @pytest.mark.asyncio
    async def test_missing_required_param_is_reported(self, executor, _cleanup_registry):
        contract = StepContract(params=(ParamSpec(name="site_name", required=True),))
        _cleanup_registry("needs_param", contract=contract)

        result = await executor._soft_failure_before_running_step(self._context(), "needs_param")

        assert result.soft_failure_code == "unmet_prerequisite"
        assert "site_name" in result.soft_failure_message
        assert "site_name" in result.remediation


class TestSoftFailureDoesNotGateExecuteFunctionStep:
    """Locks in Phase 3's architectural decision: gating lives in the NEW
    _soft_failure_before_running_step, one level above _execute_function_step
    -- mirroring how run_single_step already gates before calling
    _execute_one_step/_execute_function_step rather than inside them.
    _execute_function_step itself must stay completely unconditional, so
    every existing in-recipe-order call (every production run today, where
    the recipe already guarantees correct order) is unaffected by this
    phase's existence."""

    @pytest.fixture(autouse=True)
    def _cleanup_registry(self):
        registered: list[str] = []
        registry = get_step_registry()

        def _register(name, handler, contract=None):
            registry.register(name, handler, contract=contract)
            registered.append(name)

        yield _register

        for name in registered:
            registry.unregister(name)

    @pytest.mark.asyncio
    async def test_handler_still_runs_despite_unmet_prerequisite(self, _cleanup_registry):
        """A handler whose contract's consumes_state ISN'T satisfied still
        runs when called directly through _execute_function_step -- proving
        that method performs no automatic gating on its own."""
        contract = StepContract(consumes_state=("document_id",))
        was_called = False

        async def handler(ctx: StepContext) -> StepResult:
            nonlocal was_called
            was_called = True
            return StepResult.success(data={"ran": True})

        _cleanup_registry("populate_cells", handler, contract=contract)

        from orchestrator.experts.workflow_executor import ParsedStep

        step = ParsedStep(index=0, step_type="function", name="populate_cells", description="x")
        context = StepContext(
            packet_id="packet-1",
            packet_type="skill_run",
            packet_goal="Do the thing",
            packet_inputs={},
            packet_state={},  # document_id NOT present
            current_step="populate_cells",
            steps_completed=[],
            session_id="session-1",
        )
        executor = WorkflowExecutor(None, MagicMock(), None)

        result = await executor._execute_function_step(step, context, {})

        assert was_called is True
        assert result.data == {"ran": True}
