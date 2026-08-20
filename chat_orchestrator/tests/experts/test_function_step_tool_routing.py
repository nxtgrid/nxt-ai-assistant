"""Tests for Phase 4 routing (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md):
`WorkflowExecutor._execute_skill_step_tool_call` deciding between a real
step handler (`_execute_declared_function_step_call`) and the pre-existing
`context.mcp_executor` path, per Task 4.2/4.4.

Every handler these tests register is a synthetic in-test function, never a
real production handler (`copy_lpp_template` and friends make real Google
Drive/Sheets API calls) -- registered and unregistered per-test via
`_cleanup_registry`, exactly like `test_soft_failures.py` and
`test_step_tool_schema.py`.

Four things are covered:

- `TestRoutingByName`: a call only reaches a real handler when
  `is_declared_function_step` would also have declared it (contract-bearing,
  permission-cleared, and mutating steps additionally gated by
  `allow_write`) -- anything else, including a totally unknown name, falls
  through to `context.mcp_executor` and fails cleanly through ITS existing
  never-raise contract, never a crash.
- `TestArgumentInjection`: caller-supplied arguments reach the handler via
  the two real seams it reads from (`context.set_parameter_override` for
  `params`, `context.packet_state` for `consumes_state`), and an
  unrecognized argument is silently ignored, not rejected.
- `TestPreconditionGating`: `_soft_failure_before_running_step` (Phase 3)
  runs before the handler and can prevent it from running at all --
  guard-already-satisfied and unmet-prerequisite alike.
- `TestOutcomesMergeIntoContext`: every outcome (success, hard failure,
  handler exception) is merged into `context` via `apply_result` and
  reported back as a `ToolCallResult`, never raised to the caller.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import ParamSpec, StepContract
from orchestrator.experts.step_registry import get_step_registry
from orchestrator.experts.workflow_executor import WorkflowExecutor
from orchestrator.models.schemas import FunctionCall


@pytest.fixture
def mock_packet_service():
    mock = MagicMock()
    mock.update_state = AsyncMock(return_value={})
    # validate_step_prerequisites' Tier 2 lookup -- only actually called
    # when packet_inputs/packet_state carries a site_name/key_entity, but
    # configured unconditionally so any test that does isn't surprised by
    # an unawaitable MagicMock.
    mock.find_similar_completed = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def mock_mcp_executor():
    mock = MagicMock()
    mock.call_tool = AsyncMock(return_value={"ok": True})
    return mock


@pytest.fixture
def step_context(mock_mcp_executor):
    return StepContext(
        packet_id="packet-1",
        packet_type="skill_run",
        packet_goal="Do the thing",
        packet_inputs={},
        packet_state={},
        current_step="step_1",
        steps_completed=[],
        session_id="session-1",
        user_context=MagicMock(is_staff=True),
        mcp_executor=mock_mcp_executor,
    )


@pytest.fixture
def executor(mock_packet_service):
    return WorkflowExecutor(None, mock_packet_service, None)


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


class TestRoutingByName:
    @pytest.mark.asyncio
    async def test_contract_bearing_step_does_not_call_mcp_executor(
        self, executor, step_context, _cleanup_registry
    ):
        async def handler(ctx):
            return StepResult.success(data={"done": True})

        _cleanup_registry("zzz_test_step", handler, contract=StepContract())
        call = FunctionCall(name="zzz_test_step", arguments={})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        step_context.mcp_executor.call_tool.assert_not_called()
        assert result.success is True
        assert result.output == {"done": True}

    @pytest.mark.asyncio
    async def test_unregistered_name_falls_through_to_mcp_and_fails_cleanly(
        self, executor, step_context
    ):
        """Task 4.4's 'unknown name' case: never raises, comes back as an
        ordinary failed ToolCallResult via the pre-existing MCP path."""
        step_context.mcp_executor.call_tool = AsyncMock(side_effect=RuntimeError("unknown tool"))
        call = FunctionCall(name="totally_unregistered_tool", arguments={})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert result.success is False
        assert "unknown tool" in result.error

    @pytest.mark.asyncio
    async def test_no_contract_step_falls_through_to_mcp_not_run_directly(
        self, executor, step_context, _cleanup_registry
    ):
        """A registered handler with NO contract is routed to MCP, not run
        -- Task 4.3's 'no contract => not offered' applies to routing, not
        only to declaration."""
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        _cleanup_registry("zzz_test_no_contract", handler, contract=None)
        call = FunctionCall(name="zzz_test_no_contract", arguments={})

        await executor._execute_skill_step_tool_call(call, step_context)

        assert was_called is False
        step_context.mcp_executor.call_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_mutating_step_without_allow_write_falls_through_to_mcp(
        self, executor, step_context, _cleanup_registry
    ):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        _cleanup_registry("zzz_test_mutator", handler, contract=StepContract(mutates=True))
        call = FunctionCall(name="zzz_test_mutator", arguments={})

        await executor._execute_skill_step_tool_call(call, step_context, allow_write=False)

        assert was_called is False
        step_context.mcp_executor.call_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_mutating_step_with_allow_write_routes_to_the_real_handler(
        self, executor, step_context, _cleanup_registry
    ):
        async def handler(ctx):
            return StepResult.success(data={"sent": True})

        _cleanup_registry("zzz_test_mutator", handler, contract=StepContract(mutates=True))
        call = FunctionCall(name="zzz_test_mutator", arguments={})

        result = await executor._execute_skill_step_tool_call(
            call, step_context, allow_write=True
        )

        step_context.mcp_executor.call_tool.assert_not_called()
        assert result.success is True

    @pytest.mark.asyncio
    async def test_permission_gated_step_falls_through_to_mcp_even_with_allow_write(
        self, executor, step_context, _cleanup_registry
    ):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        _cleanup_registry(
            "zzz_test_gated", handler, contract=StepContract(required_permission="staff_only")
        )
        call = FunctionCall(name="zzz_test_gated", arguments={})

        await executor._execute_skill_step_tool_call(call, step_context, allow_write=True)

        assert was_called is False
        step_context.mcp_executor.call_tool.assert_called_once()


class TestArgumentInjection:
    @pytest.mark.asyncio
    async def test_param_argument_becomes_a_parameter_override(
        self, executor, step_context, _cleanup_registry
    ):
        seen = {}

        async def handler(ctx):
            seen["value"] = ctx.get_parameter_value("editable_total_kwp")
            return StepResult.success()

        contract = StepContract(params=(ParamSpec(name="editable_total_kwp"),))
        _cleanup_registry("zzz_test_param_step", handler, contract=contract)
        call = FunctionCall(name="zzz_test_param_step", arguments={"editable_total_kwp": 42})

        await executor._execute_skill_step_tool_call(call, step_context)

        assert seen["value"] == 42

    @pytest.mark.asyncio
    async def test_consumes_state_argument_is_written_to_packet_state(
        self, executor, step_context, _cleanup_registry
    ):
        seen = {}

        async def handler(ctx):
            seen["value"] = ctx.get_state("site_name")
            return StepResult.success()

        contract = StepContract(consumes_state=("site_name",))
        _cleanup_registry("zzz_test_state_step", handler, contract=contract)
        call = FunctionCall(name="zzz_test_state_step", arguments={"site_name": "ExampleSite"})

        await executor._execute_skill_step_tool_call(call, step_context)

        assert seen["value"] == "ExampleSite"

    @pytest.mark.asyncio
    async def test_unrecognized_argument_is_ignored_not_raised(
        self, executor, step_context, _cleanup_registry
    ):
        async def handler(ctx):
            return StepResult.success()

        _cleanup_registry("zzz_test_step", handler, contract=StepContract())
        call = FunctionCall(name="zzz_test_step", arguments={"nonsense_key": "whatever"})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert result.success is True  # did not raise; handler still ran

    @pytest.mark.asyncio
    async def test_precondition_value_supplied_directly_satisfies_the_check(
        self, executor, step_context, _cleanup_registry
    ):
        """A caller that already knows a normally producer-supplied value
        (e.g. document_id) can hand it over directly and skip the
        precondition failure -- see _execute_declared_function_step_call's
        docstring, point 1. document_id is deliberately NOT declared as a
        tool argument by step_tool_schema (it has a producer), but nothing
        stops a caller from supplying it anyway."""
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        contract = StepContract(consumes_state=("document_id",))
        _cleanup_registry("zzz_test_consumer", handler, contract=contract)
        call = FunctionCall(name="zzz_test_consumer", arguments={"document_id": "doc-123"})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert was_called is True
        assert result.success is True
        assert step_context.get_state("document_id") == "doc-123"


class TestPreconditionGating:
    @pytest.mark.asyncio
    async def test_guard_already_satisfied_returns_soft_failure_without_running_handler(
        self, executor, step_context, _cleanup_registry
    ):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success()

        contract = StepContract(guard_keys=("already_done",))
        _cleanup_registry("zzz_test_guarded", handler, contract=contract)
        step_context.packet_state["already_done"] = True
        call = FunctionCall(name="zzz_test_guarded", arguments={})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert was_called is False
        assert result.success is False
        assert "already" in result.error.lower()

    @pytest.mark.asyncio
    async def test_unmet_prerequisite_returns_soft_failure_naming_the_producer(
        self, executor, step_context, _cleanup_registry
    ):
        consumer_called = False

        async def producer(ctx):
            return StepResult.success()

        async def consumer(ctx):
            nonlocal consumer_called
            consumer_called = True
            return StepResult.success()

        _cleanup_registry(
            "zzz_test_producer", producer, contract=StepContract(produces_state=("zzz_key",))
        )
        _cleanup_registry(
            "zzz_test_consumer2", consumer, contract=StepContract(consumes_state=("zzz_key",))
        )
        call = FunctionCall(name="zzz_test_consumer2", arguments={})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert consumer_called is False
        assert result.success is False
        assert "zzz_test_producer" in result.error

    @pytest.mark.asyncio
    async def test_satisfied_prerequisites_let_the_handler_run(
        self, executor, step_context, _cleanup_registry
    ):
        was_called = False

        async def handler(ctx):
            nonlocal was_called
            was_called = True
            return StepResult.success(data={"ok": True})

        contract = StepContract(consumes_state=("zzz_key",))
        _cleanup_registry("zzz_test_consumer3", handler, contract=contract)
        step_context.packet_state["zzz_key"] = "already-there"
        call = FunctionCall(name="zzz_test_consumer3", arguments={})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert was_called is True
        assert result.success is True


class TestOutcomesMergeIntoContext:
    @pytest.mark.asyncio
    async def test_successful_call_data_is_visible_via_get_previous_result(
        self, executor, step_context, _cleanup_registry
    ):
        async def handler(ctx):
            return StepResult(data={"document_id": "doc-1"})

        _cleanup_registry("zzz_test_step", handler, contract=StepContract())
        call = FunctionCall(name="zzz_test_step", arguments={})

        await executor._execute_skill_step_tool_call(call, step_context)

        assert step_context.get_previous_result("zzz_test_step") == {"document_id": "doc-1"}

    @pytest.mark.asyncio
    async def test_successful_call_state_updates_are_visible_via_get_state(
        self, executor, step_context, _cleanup_registry
    ):
        async def handler(ctx):
            return StepResult(state_updates={"template_copied": True})

        _cleanup_registry("zzz_test_step", handler, contract=StepContract())
        call = FunctionCall(name="zzz_test_step", arguments={})

        await executor._execute_skill_step_tool_call(call, step_context)

        assert step_context.get_state("template_copied") is True

    @pytest.mark.asyncio
    async def test_handler_exception_never_raises_becomes_a_failed_tool_call_result(
        self, executor, step_context, _cleanup_registry
    ):
        async def handler(ctx):
            raise RuntimeError("boom")

        _cleanup_registry("zzz_test_step", handler, contract=StepContract())
        call = FunctionCall(name="zzz_test_step", arguments={})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert result.success is False
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_handler_returned_failure_is_surfaced_as_error(
        self, executor, step_context, _cleanup_registry
    ):
        async def handler(ctx):
            return StepResult.failure("site not found")

        _cleanup_registry("zzz_test_step", handler, contract=StepContract())
        call = FunctionCall(name="zzz_test_step", arguments={})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert result.success is False
        assert result.error == "site not found"

    @pytest.mark.asyncio
    async def test_failed_call_does_not_pollute_accumulated_results(
        self, executor, step_context, _cleanup_registry
    ):
        async def handler(ctx):
            return StepResult.failure("nope")

        _cleanup_registry("zzz_test_step", handler, contract=StepContract())
        call = FunctionCall(name="zzz_test_step", arguments={})

        await executor._execute_skill_step_tool_call(call, step_context)

        assert step_context.get_previous_result("zzz_test_step") is None

    @pytest.mark.asyncio
    async def test_tool_call_id_is_preserved_on_every_outcome(
        self, executor, step_context, _cleanup_registry
    ):
        async def handler(ctx):
            return StepResult.success()

        _cleanup_registry("zzz_test_step", handler, contract=StepContract())
        call = FunctionCall(name="zzz_test_step", arguments={}, tool_call_id="call-42")

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert result.tool_call_id == "call-42"
