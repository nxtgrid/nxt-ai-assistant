"""Tests for mock mode (Phase 5 of
docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md).

Three things are covered:

- `TestExecuteFunctionStepMockMode`: `WorkflowExecutor._execute_function_step`'s
  core Task 5.2 logic -- a mutating, mock-mode-enabled step returns
  `contract.mock`'s result instead of calling the real handler; a
  non-mutating step, or one with mock mode disabled, always calls the real
  handler; `ParsedStep.mock` overrides `StepContext.dry_run` per step; a
  mutating step with no `MockSpec` hard-fails rather than running for real
  or fabricating an unspecified result.
- `TestMockSpecIsDefensivelyCopied`: a `MockSpec` is a single shared,
  mutable, module-level-registered instance -- one run's result must not be
  able to corrupt it for the next.
- `TestRunLogMockMarking`: R6 (Task 5.5) end to end through
  `WorkflowExecutor._execute_one_step` -- a mocked top-level `kind:"function"`
  step's run-log entry is prefixed, unmistakably, regardless of what the
  MockSpec's own data/message contain.

Every handler registered here is synthetic (`zzz_test_*`), never a real
production handler -- see test_soft_failures.py/test_step_tool_schema.py's
`_cleanup_registry` fixture, mirrored exactly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import MockSpec, StepContract
from orchestrator.experts.step_registry import get_step_registry
from orchestrator.experts.workflow_executor import (
    ExecutionSummary,
    ParsedStep,
    WorkflowExecutor,
)


@pytest.fixture
def _cleanup_registry():
    """Mirrors test_soft_failures.py/test_step_tool_schema.py exactly."""
    registered: list[str] = []
    registry = get_step_registry()

    def _register(name, handler=None, contract=None):
        handler = handler or (lambda ctx: None)
        registry.register(name, handler, contract=contract)
        registered.append(name)

    yield _register

    for name in registered:
        registry.unregister(name)


@pytest.fixture
def mock_packet_service():
    mock = MagicMock()
    mock.update_state = AsyncMock(return_value={})
    mock.find_similar_completed = AsyncMock(return_value=[])
    mock.complete_step = AsyncMock(return_value={"packet_id": "packet-1"})
    mock.fail_packet = AsyncMock(return_value={"packet_id": "packet-1", "status": "failed"})
    mock.set_awaiting_input = AsyncMock(
        return_value={"packet_id": "packet-1", "status": "awaiting_input"}
    )
    return mock


@pytest.fixture
def executor(mock_packet_service):
    return WorkflowExecutor(None, mock_packet_service, None)


def _context(dry_run: bool = False, packet_state=None) -> StepContext:
    return StepContext(
        packet_id="packet-1",
        packet_type="skill_run",
        packet_goal="Do the thing",
        packet_inputs={},
        packet_state=packet_state or {},
        current_step="step_1",
        steps_completed=[],
        session_id="session-1",
        dry_run=dry_run,
    )


class TestExecuteFunctionStepMockMode:
    @pytest.mark.asyncio
    async def test_non_mutating_step_always_runs_for_real(self, executor, _cleanup_registry):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success(data={"real": True})

        _cleanup_registry("zzz_test_read", handler, contract=StepContract(mutates=False))
        step = ParsedStep(index=0, step_type="function", name="zzz_test_read", description="x")

        result = await executor._execute_function_step(step, _context(dry_run=True), {})

        assert was_called is True
        assert result.was_mocked is False
        assert result.data == {"real": True}

    @pytest.mark.asyncio
    async def test_contractless_step_always_runs_for_real(self, executor, _cleanup_registry):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        _cleanup_registry("zzz_test_no_contract", handler, contract=None)
        step = ParsedStep(
            index=0, step_type="function", name="zzz_test_no_contract", description="x"
        )

        result = await executor._execute_function_step(step, _context(dry_run=True), {})

        assert was_called is True
        assert result.was_mocked is False

    @pytest.mark.asyncio
    async def test_mutating_step_runs_for_real_when_dry_run_is_false(
        self, executor, _cleanup_registry
    ):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success(data={"real": True})

        contract = StepContract(mutates=True, mock=MockSpec())
        _cleanup_registry("zzz_test_write", handler, contract=contract)
        step = ParsedStep(index=0, step_type="function", name="zzz_test_write", description="x")

        result = await executor._execute_function_step(step, _context(dry_run=False), {})

        assert was_called is True
        assert result.was_mocked is False

    @pytest.mark.asyncio
    async def test_mutating_step_is_mocked_when_dry_run_is_true(self, executor, _cleanup_registry):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        mock = MockSpec(
            state_updates={"document_id": "MOCK-doc-1"},
            data={"document_id": "MOCK-doc-1"},
            message="Would have copied the template.",
        )
        contract = StepContract(mutates=True, produces_state=("document_id",), mock=mock)
        _cleanup_registry("zzz_test_write", handler, contract=contract)
        step = ParsedStep(index=0, step_type="function", name="zzz_test_write", description="x")

        result = await executor._execute_function_step(step, _context(dry_run=True), {})

        assert was_called is False
        assert result.was_mocked is True
        assert result.state_updates == {"document_id": "MOCK-doc-1"}
        assert result.data == {"document_id": "MOCK-doc-1"}
        assert result.progress_message == "[MOCKED] Would have copied the template."

    @pytest.mark.asyncio
    async def test_mocked_result_gets_a_generated_message_when_mockspec_has_none(
        self, executor, _cleanup_registry
    ):
        contract = StepContract(mutates=True, mock=MockSpec())  # no .message
        _cleanup_registry("zzz_test_write", lambda ctx: None, contract=contract)
        step = ParsedStep(index=0, step_type="function", name="zzz_test_write", description="x")

        result = await executor._execute_function_step(step, _context(dry_run=True), {})

        assert result.progress_message.startswith("[MOCKED]")
        assert "zzz_test_write" in result.progress_message

    @pytest.mark.asyncio
    async def test_step_level_true_override_mocks_despite_context_baseline_false(
        self, executor, _cleanup_registry
    ):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        contract = StepContract(mutates=True, mock=MockSpec())
        _cleanup_registry("zzz_test_write", handler, contract=contract)
        step = ParsedStep(
            index=0, step_type="function", name="zzz_test_write", description="x", mock=True
        )

        result = await executor._execute_function_step(step, _context(dry_run=False), {})

        assert was_called is False
        assert result.was_mocked is True

    @pytest.mark.asyncio
    async def test_step_level_false_override_runs_real_despite_context_baseline_true(
        self, executor, _cleanup_registry
    ):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        contract = StepContract(mutates=True, mock=MockSpec())
        _cleanup_registry("zzz_test_write", handler, contract=contract)
        step = ParsedStep(
            index=0, step_type="function", name="zzz_test_write", description="x", mock=False
        )

        result = await executor._execute_function_step(step, _context(dry_run=True), {})

        assert was_called is True
        assert result.was_mocked is False

    @pytest.mark.asyncio
    async def test_mutating_step_with_no_mockspec_hard_fails(self, executor, _cleanup_registry):
        """Not a soft failure: a soft failure leaves `error` unset, so
        _execute_one_step's `if result.error:` halt check would never trip
        and the run would silently continue having mocked nothing. This
        must halt -- see WorkflowExecutor._mock_step_result's docstring."""
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        contract = StepContract(mutates=True, mock=None)  # no MockSpec registered
        _cleanup_registry("zzz_test_write", handler, contract=contract)
        step = ParsedStep(index=0, step_type="function", name="zzz_test_write", description="x")

        result = await executor._execute_function_step(step, _context(dry_run=True), {})

        assert was_called is False
        assert result.was_mocked is False
        assert result.error is not None
        assert "zzz_test_write" in result.error
        assert result.is_success is False


class TestMockSpecIsDefensivelyCopied:
    @pytest.mark.asyncio
    async def test_mutating_the_returned_result_does_not_corrupt_the_shared_mockspec(
        self, executor, _cleanup_registry
    ):
        mock = MockSpec(state_updates={"document_id": "MOCK-doc-1"}, data={"a": 1})
        contract = StepContract(mutates=True, mock=mock)
        _cleanup_registry("zzz_test_write", lambda ctx: None, contract=contract)
        step = ParsedStep(index=0, step_type="function", name="zzz_test_write", description="x")

        result1 = await executor._execute_function_step(step, _context(dry_run=True), {})
        result1.state_updates["document_id"] = "TAMPERED"
        result1.data["a"] = "TAMPERED"

        result2 = await executor._execute_function_step(step, _context(dry_run=True), {})

        assert result2.state_updates == {"document_id": "MOCK-doc-1"}
        assert result2.data == {"a": 1}
        assert mock.state_updates == {"document_id": "MOCK-doc-1"}
        assert mock.data == {"a": 1}


class TestRunLogMockMarking:
    """R6 (Task 5.5), end to end through _execute_one_step for a top-level
    kind:"function" step."""

    @pytest.mark.asyncio
    async def test_mocked_step_run_log_entry_is_prefixed(self, executor, mock_packet_service):
        registry = get_step_registry()
        mock = MockSpec(data={"document_id": "MOCK-doc-1"})
        contract = StepContract(mutates=True, mock=mock)
        registry.register("zzz_test_write", lambda ctx: None, contract=contract)
        try:
            step = ParsedStep(
                index=0, step_type="function", name="zzz_test_write", description="x"
            )
            packet = {
                "packet_id": "packet-1",
                "packet_type": "skill_run",
                "packet_inputs": {},
                "packet_state": {},
                "steps_completed": [],
            }
            context = _context(dry_run=True)
            summary = ExecutionSummary(packet_id="packet-1", packet_type="skill_run")

            await executor._execute_one_step(
                step, [step], _MockExpertConfig(), packet, context, {}, summary, None, None
            )

            assert summary.step_records[-1].result_summary.startswith("[MOCKED]")
        finally:
            registry.unregister("zzz_test_write")

    @pytest.mark.asyncio
    async def test_real_step_run_log_entry_is_not_prefixed(self, executor, mock_packet_service):
        registry = get_step_registry()

        async def handler(ctx):
            return StepResult.success(data={"document_id": "doc-1"})

        contract = StepContract(mutates=True, mock=MockSpec())
        registry.register("zzz_test_write", handler, contract=contract)
        try:
            step = ParsedStep(
                index=0, step_type="function", name="zzz_test_write", description="x"
            )
            packet = {
                "packet_id": "packet-1",
                "packet_type": "skill_run",
                "packet_inputs": {},
                "packet_state": {},
                "steps_completed": [],
            }
            context = _context(dry_run=False)
            summary = ExecutionSummary(packet_id="packet-1", packet_type="skill_run")

            await executor._execute_one_step(
                step, [step], _MockExpertConfig(), packet, context, {}, summary, None, None
            )

            assert not summary.step_records[-1].result_summary.startswith("[MOCKED]")
        finally:
            registry.unregister("zzz_test_write")


class _MockExpertConfig:
    system_instructions = "You are a helpful expert."
    display_name = "Test Expert"

    def get_workflow(self, packet_type):
        return []
