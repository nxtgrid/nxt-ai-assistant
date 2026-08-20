"""Tests for the run-scoped state carrier a skill step's tool-call loop uses
(Phase 2 of docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md).

`StepContext.apply_result` is the reusable merge primitive: it plays the
same role for a skill run's in-loop tool calls that `WorkflowExecutor.
_execute_one_step`'s existing `context.packet_state.update(result.state_updates)`
/ `accumulated_results[step.name] = result.data` pattern already plays for a
top-level function step -- reusing `StepContext.packet_state` /
`accumulated_results` and `StepResult.state_updates` / `data`, not a
parallel mechanism (see that method's docstring and this plan's Phase 2).

Two layers are covered:
- `TestApplyResult`: the primitive itself, directly -- including Task 2.3's
  literal acceptance shape ("two chained handler calls, the second reading a
  key the first produced").
- `TestExecuteSkillStepToolCall` / `TestStateAcrossToolLoopRounds`: the
  primitive actually wired into `_execute_skill_step_tool_call` and the
  round loop that calls it (`_call_llm_step_with_tools`), proving Task 2.2
  ("state survives across rounds... not just within one round") against the
  real loop rather than only against the primitive in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.clients.gemini import GeminiTurnResult
from orchestrator.experts import workflow_executor as wf_module
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.workflow_executor import ParsedStep, WorkflowExecutor
from orchestrator.models.schemas import FunctionCall


def _turn(
    text: str = "done",
    tool_calls: list | None = None,
) -> GeminiTurnResult:
    """Matches test_skill_steps.py's _turn helper -- kept local per that
    file's own note that a tool_calls-carrying helper belongs alongside the
    tests that need it, not in test_workflow_executor.py."""
    return GeminiTurnResult(
        text=text,
        tool_calls=tool_calls or [],
        finish_reason="STOP",
        input_tokens=0,
        output_tokens=0,
        raw_response={},
    )


class _MockExpertConfig:
    system_instructions = "You are a helpful expert."
    display_name = "Test Expert"

    def get_workflow(self, packet_type):
        return []


@pytest.fixture
def mock_gemini():
    mock = MagicMock()
    mock.generate_messages = AsyncMock(return_value=_turn())
    mock.model_name = "gemini-2.5-flash"
    return mock


@pytest.fixture
def mock_packet_service():
    mock = MagicMock()
    mock.update_state = AsyncMock(return_value={})
    return mock


@pytest.fixture
def mock_mcp_executor():
    mock = MagicMock()
    mock.call_tool = AsyncMock(return_value={"tickets": ["TKT-1"]})
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
def base_skill_packet():
    return {
        "packet_id": "packet-1",
        "packet_type": "skill_run",
        "packet_goal": "Run the skill",
        "steps_completed": [],
        "packet_inputs": {},
        "packet_state": {},
    }


def _patch_available_tools(monkeypatch, tools: list[dict]):
    fake_service = MagicMock()
    fake_service.get_available_tools = AsyncMock(return_value=tools)
    monkeypatch.setattr(wf_module, "get_permissions_service", lambda: fake_service)


def _patch_max_tool_rounds(monkeypatch, rounds: int):
    fake_settings = MagicMock()
    fake_settings.max_tool_rounds = rounds
    fake_settings.skill_max_tool_rounds = rounds
    monkeypatch.setattr(wf_module, "get_settings", lambda: fake_settings)


class TestApplyResult:
    """StepContext.apply_result -- the merge primitive, in isolation."""

    def test_merges_state_updates_into_packet_state(self, step_context):
        step_context.apply_result(
            "copy_lpp_template", StepResult(state_updates={"document_id": "doc-1"})
        )
        assert step_context.packet_state["document_id"] == "doc-1"
        assert step_context.get_state("document_id") == "doc-1"

    def test_merges_data_into_accumulated_results_under_the_given_name(self, step_context):
        step_context.apply_result(
            "fetch_grafana_kpis", StepResult(data={"cuf": 0.42})
        )
        assert step_context.accumulated_results["fetch_grafana_kpis"] == {"cuf": 0.42}
        assert step_context.get_previous_result("fetch_grafana_kpis") == {"cuf": 0.42}

    def test_bare_step_result_is_a_safe_no_op(self, step_context):
        """A StepResult with neither state_updates nor data (e.g. a read
        that produced nothing reusable) must not touch either container."""
        step_context.packet_state["existing"] = "untouched"
        step_context.accumulated_results["other_step"] = {"kept": True}

        step_context.apply_result("noop_step", StepResult())

        assert step_context.packet_state == {"existing": "untouched"}
        assert step_context.accumulated_results == {"other_step": {"kept": True}}
        assert "noop_step" not in step_context.accumulated_results

    def test_does_not_persist_to_the_database(self, step_context):
        """Unlike _execute_one_step's merge, apply_result is in-memory only
        -- DB persistence of packet_state stays owned once-per-outer-step by
        _execute_one_step's own packet_service.update_state call. There is
        no packet_service reachable from a bare StepContext at all, so this
        is really just confirming apply_result never tries to reach for
        one."""
        step_context.apply_result("some_step", StepResult(state_updates={"k": "v"}))
        assert step_context.get_state("k") == "v"  # merged in memory; no DB call made or possible

    def test_two_chained_calls_second_reads_what_first_produced_via_state(self, step_context):
        """Task 2.3, verbatim: two chained handler calls, the second reading
        a key the first produced -- via packet_state/consumes_state, the
        path a real StepContract precondition check reads."""
        step_context.apply_result(
            "copy_lpp_template", StepResult(state_updates={"document_id": "MOCK-doc-1"})
        )

        # "Second call" reads document_id exactly like a real handler would
        # via context.get_state -- e.g. populate_lpp_cells's
        # consumes_state=("document_id",) precondition.
        document_id = step_context.get_state("document_id")

        assert document_id == "MOCK-doc-1"

    def test_two_chained_calls_second_reads_what_first_produced_via_results(self, step_context):
        """Same acceptance shape, via accumulated_results/consumes_results --
        the path a contract's consumes_results precondition reads."""
        step_context.apply_result(
            "generate_distribution_map", StepResult(data={"map_url": "https://example/map.png"})
        )

        previous = step_context.get_previous_result("generate_distribution_map")

        assert previous == {"map_url": "https://example/map.png"}

    def test_second_call_can_itself_read_and_extend_state(self, step_context):
        """A fuller chain: call A produces a key, call B reads it AND
        produces its own key, proving apply_result composes across more
        than one hop, not just two fixed calls."""
        step_context.apply_result(
            "copy_lpp_template", StepResult(state_updates={"document_id": "MOCK-doc-1"})
        )

        document_id = step_context.get_state("document_id")
        assert document_id == "MOCK-doc-1"
        step_context.apply_result(
            "populate_lpp_cells",
            StepResult(state_updates={"cells_populated": True}, data={"document_id": document_id}),
        )

        assert step_context.get_state("cells_populated") is True
        assert step_context.get_state("document_id") == "MOCK-doc-1"  # first call's key untouched
        assert step_context.get_previous_result("populate_lpp_cells") == {
            "document_id": "MOCK-doc-1"
        }


class TestExecuteSkillStepToolCall:
    """_execute_skill_step_tool_call actually wired to apply_result."""

    @pytest.mark.asyncio
    async def test_successful_dict_output_is_recorded_under_the_call_name(
        self, mock_gemini, mock_packet_service, step_context
    ):
        step_context.mcp_executor.call_tool = AsyncMock(
            return_value={"tickets": ["TKT-1", "TKT-2"]}
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)
        call = FunctionCall(name="get_open_tickets", arguments={})

        await executor._execute_skill_step_tool_call(call, step_context)

        assert step_context.get_previous_result("get_open_tickets") == {
            "tickets": ["TKT-1", "TKT-2"]
        }

    @pytest.mark.asyncio
    async def test_non_dict_output_is_wrapped_before_recording(
        self, mock_gemini, mock_packet_service, step_context
    ):
        """MCP tool output isn't guaranteed to be a dict (StepResult.data
        is) -- confirm a scalar/list output still gets recorded rather than
        raising or being silently dropped."""
        step_context.mcp_executor.call_tool = AsyncMock(return_value=["TKT-1", "TKT-2"])
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)
        call = FunctionCall(name="list_ticket_ids", arguments={})

        await executor._execute_skill_step_tool_call(call, step_context)

        assert step_context.get_previous_result("list_ticket_ids") == {
            "value": ["TKT-1", "TKT-2"]
        }

    @pytest.mark.asyncio
    async def test_failed_call_does_not_pollute_accumulated_results(
        self, mock_gemini, mock_packet_service, step_context
    ):
        step_context.mcp_executor.call_tool = AsyncMock(side_effect=RuntimeError("tool exploded"))
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)
        call = FunctionCall(name="get_thing", arguments={})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert result.success is False
        assert step_context.get_previous_result("get_thing") is None
        assert step_context.accumulated_results == {}

    @pytest.mark.asyncio
    async def test_no_mcp_executor_does_not_apply_any_result(
        self, mock_gemini, mock_packet_service, step_context
    ):
        step_context.mcp_executor = None
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)
        call = FunctionCall(name="get_thing", arguments={})

        result = await executor._execute_skill_step_tool_call(call, step_context)

        assert result.success is False
        assert step_context.accumulated_results == {}


class TestStateAcrossToolLoopRounds:
    """Task 2.2: state survives across rounds of the tool-call loop, not
    just within one round -- exercised through the real loop
    (_call_llm_step_with_tools / _execute_llm_step), not just the
    primitive."""

    @pytest.mark.asyncio
    async def test_round_one_result_is_still_readable_after_round_two(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        """Two different tools called across two separate rounds; after the
        loop ends, BOTH results are present -- round two did not evict or
        shadow round one. Proves `context` is genuinely one shared object
        across the whole loop, not rebuilt per round."""
        _patch_available_tools(
            monkeypatch, [{"name": "fetch_grid_status"}, {"name": "fetch_grid_kpis"}]
        )
        _patch_max_tool_rounds(monkeypatch, 5)

        call_round_1 = FunctionCall(name="fetch_grid_status", arguments={})
        call_round_2 = FunctionCall(name="fetch_grid_kpis", arguments={})
        mock_gemini.generate_messages = AsyncMock(
            side_effect=[
                _turn(text="", tool_calls=[call_round_1]),
                _turn(text="", tool_calls=[call_round_2]),
                _turn(text="Grid is healthy."),
            ]
        )
        step_context.mcp_executor.call_tool = AsyncMock(
            side_effect=[{"status": "online"}, {"cuf": 0.5}]
        )
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="check_grid",
            description="Check grid status and KPIs.",
            is_skill_step=True,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        result = await executor._execute_llm_step(
            step, _MockExpertConfig(), base_skill_packet, step_context, {}
        )

        assert result == "Grid is healthy."
        # Round 1's result (fetch_grid_status) survived into and past round 2
        # -- not overwritten, not cleared.
        assert step_context.get_previous_result("fetch_grid_status") == {"status": "online"}
        assert step_context.get_previous_result("fetch_grid_kpis") == {"cuf": 0.5}

    @pytest.mark.asyncio
    async def test_same_context_object_is_used_for_every_round(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        """Structural guarantee behind Task 2.2: _call_llm_step_with_tools
        passes one fixed `context` argument into every round -- it is never
        reconstructed mid-loop. Asserted directly via identity, not just
        inferred from behaviour, so a future refactor that starts rebuilding
        context per round fails this test immediately."""
        _patch_available_tools(monkeypatch, [{"name": "get_thing"}])
        _patch_max_tool_rounds(monkeypatch, 3)
        seen_context_ids: list[int] = []

        original = WorkflowExecutor._execute_skill_step_tool_call

        async def _spy(self, call, context, allow_write=False):
            seen_context_ids.append(id(context))
            return await original(self, call, context, allow_write=allow_write)

        monkeypatch.setattr(WorkflowExecutor, "_execute_skill_step_tool_call", _spy)

        always_wants_tool = _turn(text="", tool_calls=[FunctionCall(name="get_thing", arguments={})])
        mock_gemini.generate_messages = AsyncMock(return_value=always_wants_tool)
        step = ParsedStep(
            index=0, step_type="llm", name="loop", description="Keep going.", is_skill_step=True
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        await executor._execute_llm_step(step, _MockExpertConfig(), base_skill_packet, step_context, {})

        assert len(seen_context_ids) == 3  # max_tool_rounds=3, one call per round
        assert len(set(seen_context_ids)) == 1  # every round saw the exact same object
