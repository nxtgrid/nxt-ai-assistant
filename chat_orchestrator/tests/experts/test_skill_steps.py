"""Tests for user-designed skill steps: tool access and {{var}} output
binding on LLM steps (Phase 2 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md).

Exercises WorkflowExecutor._execute_llm_step directly (mirrors
test_workflow_executor.py's style) rather than a full workflow run, since
these are unit-level concerns: what tools_payload a step gets, what a
declared write does or doesn't extract, and that a step with
is_skill_step=False (every step parsed from a Google Doc today) is
completely unaffected by any of this.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.clients.gemini import GeminiTurnResult
from orchestrator.experts import workflow_executor as wf_module
from orchestrator.experts.step_context import StepContext
from orchestrator.experts.workflow_executor import (
    ExecutionSummary,
    ParsedStep,
    SkillStepVariableError,
    WorkflowExecutor,
)
from orchestrator.models.schemas import FunctionCall


def _turn(
    text: str = "done",
    tool_calls: list | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> GeminiTurnResult:
    """Build the real turn object generate_messages returns (matches
    test_workflow_executor.py's _turn helper -- kept local here since a
    tool_calls param is central to these tests and adding it there would
    be an unrelated change to that file)."""
    return GeminiTurnResult(
        text=text,
        tool_calls=tool_calls or [],
        finish_reason="STOP",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
    mock.call_tool = AsyncMock(return_value={"tickets": ["TKT-1", "TKT-2"]})
    return mock


@pytest.fixture
def step_context(mock_mcp_executor):
    ctx = StepContext(
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
    return ctx


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
    """Stub permissions_service.get_available_tools without a real service."""
    fake_service = MagicMock()
    fake_service.get_available_tools = AsyncMock(return_value=tools)
    monkeypatch.setattr(wf_module, "get_permissions_service", lambda: fake_service)


def _patch_max_tool_rounds(monkeypatch, rounds: int):
    fake_settings = MagicMock()
    fake_settings.max_tool_rounds = rounds
    monkeypatch.setattr(wf_module, "get_settings", lambda: fake_settings)


class TestNonSkillStepUnaffected:
    """The backward-compatibility gate: is_skill_step=False (every existing
    Google-Doc expert-workflow step) must behave exactly as before Phase 2,
    even when its description happens to contain {{...}}-looking text."""

    @pytest.mark.asyncio
    async def test_curly_braces_in_description_are_passed_through_verbatim(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet
    ):
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="respond",
            description="Respond using the template {{example}} verbatim.",
            is_skill_step=False,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        result = await executor._execute_llm_step(
            step, _MockExpertConfig(), base_skill_packet, step_context, {}
        )

        assert result == "done"
        prompt = mock_gemini.generate_messages.call_args.args[0][0].content
        assert "{{example}}" in prompt  # never rendered/validated

    @pytest.mark.asyncio
    async def test_no_tools_offered_for_non_skill_step(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        _patch_available_tools(monkeypatch, [{"name": "get_grid_status"}])
        step = ParsedStep(
            index=0, step_type="llm", name="respond", description="Just respond.", is_skill_step=False
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        await executor._execute_llm_step(step, _MockExpertConfig(), base_skill_packet, step_context, {})

        assert mock_gemini.generate_messages.call_args.kwargs["tools_payload"] is None


class TestSkillStepToolAccess:
    @pytest.mark.asyncio
    async def test_skill_step_actually_calls_the_tool_and_returns_data(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        """'A skill step instructed to list open tickets actually calls the
        tool and returns real data' -- the plan's Phase 2 acceptance
        criterion, verbatim."""
        _patch_available_tools(monkeypatch, [{"name": "get_open_tickets"}])
        _patch_max_tool_rounds(monkeypatch, 5)

        call = FunctionCall(name="get_open_tickets", arguments={"grid": "ExampleGrid"})
        mock_gemini.generate_messages = AsyncMock(
            side_effect=[
                _turn(text="", tool_calls=[call]),
                _turn(text="Found 2 tickets: TKT-1, TKT-2"),
            ]
        )
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="find_tickets",
            description="List all open tickets for ExampleGrid",
            is_skill_step=True,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)
        step_context.mcp_executor.call_tool = AsyncMock(
            return_value={"tickets": ["TKT-1", "TKT-2"]}
        )

        result = await executor._execute_llm_step(
            step, _MockExpertConfig(), base_skill_packet, step_context, {}
        )

        step_context.mcp_executor.call_tool.assert_awaited_once_with(
            "get_open_tickets", {"grid": "ExampleGrid"}
        )
        assert result == "Found 2 tickets: TKT-1, TKT-2"
        assert mock_gemini.generate_messages.call_count == 2

    @pytest.mark.asyncio
    async def test_write_tool_absent_from_payload_by_default(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        """'A step with allow_write: false cannot invoke a write tool --
        assert the tool is absent from the payload, not merely that it
        wasn't called.' Verbatim acceptance criterion: assert on the
        payload sent to the LLM, not on call_tool's call count."""
        _patch_available_tools(
            monkeypatch, [{"name": "get_open_tickets"}, {"name": "update_ticket_status"}]
        )
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="find_tickets",
            description="List open tickets.",
            is_skill_step=True,
            allow_write=False,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        await executor._execute_llm_step(step, _MockExpertConfig(), base_skill_packet, step_context, {})

        sent_tools = mock_gemini.generate_messages.call_args.kwargs["tools_payload"]
        sent_names = {t["name"] for t in sent_tools}
        assert sent_names == {"get_open_tickets"}
        assert "update_ticket_status" not in sent_names

    @pytest.mark.asyncio
    async def test_allow_write_true_includes_write_tools(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        _patch_available_tools(
            monkeypatch, [{"name": "get_open_tickets"}, {"name": "update_ticket_status"}]
        )
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="close_ticket",
            description="Close the resolved ticket.",
            is_skill_step=True,
            allow_write=True,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        await executor._execute_llm_step(step, _MockExpertConfig(), base_skill_packet, step_context, {})

        sent_tools = mock_gemini.generate_messages.call_args.kwargs["tools_payload"]
        sent_names = {t["name"] for t in sent_tools}
        assert sent_names == {"get_open_tickets", "update_ticket_status"}

    @pytest.mark.asyncio
    async def test_no_available_tools_sends_none_not_empty_list(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        # An empty resolved-tools list must behave like "no tools" to the
        # LLM client (falsy), not like an explicit empty declaration.
        _patch_available_tools(monkeypatch, [])
        step = ParsedStep(
            index=0, step_type="llm", name="respond", description="Just respond.", is_skill_step=True
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        await executor._execute_llm_step(step, _MockExpertConfig(), base_skill_packet, step_context, {})

        assert not mock_gemini.generate_messages.call_args.kwargs["tools_payload"]

    @pytest.mark.asyncio
    async def test_tool_round_loop_is_bounded_by_max_tool_rounds(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        """A model that never stops requesting tools must not hang the run."""
        _patch_available_tools(monkeypatch, [{"name": "get_thing"}])
        _patch_max_tool_rounds(monkeypatch, 2)

        always_wants_tool = _turn(
            text="", tool_calls=[FunctionCall(name="get_thing", arguments={})]
        )
        mock_gemini.generate_messages = AsyncMock(return_value=always_wants_tool)
        step = ParsedStep(
            index=0, step_type="llm", name="loop", description="Keep going.", is_skill_step=True
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        await executor._execute_llm_step(step, _MockExpertConfig(), base_skill_packet, step_context, {})

        # 1 initial call + 2 rounds (max_tool_rounds=2) = 3 total calls.
        assert mock_gemini.generate_messages.call_count == 3

    @pytest.mark.asyncio
    async def test_failed_tool_call_is_fed_back_as_error_not_raised(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        _patch_available_tools(monkeypatch, [{"name": "get_thing"}])
        _patch_max_tool_rounds(monkeypatch, 3)
        step_context.mcp_executor.call_tool = AsyncMock(side_effect=RuntimeError("tool exploded"))

        call = FunctionCall(name="get_thing", arguments={})
        mock_gemini.generate_messages = AsyncMock(
            side_effect=[_turn(text="", tool_calls=[call]), _turn(text="handled the error")]
        )
        step = ParsedStep(
            index=0, step_type="llm", name="resilient", description="Try the thing.", is_skill_step=True
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        result = await executor._execute_llm_step(
            step, _MockExpertConfig(), base_skill_packet, step_context, {}
        )

        assert result == "handled the error"
        # The failed-tool round's message pair reached the second call.
        second_call_messages = mock_gemini.generate_messages.call_args_list[1].args[0]
        tool_result_msg = next(m for m in second_call_messages if m.tool_result is not None)
        assert tool_result_msg.tool_result.success is False
        assert "tool exploded" in tool_result_msg.tool_result.error

    @pytest.mark.asyncio
    async def test_tokens_accumulate_across_every_round_not_just_the_last(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet, monkeypatch
    ):
        _patch_available_tools(monkeypatch, [{"name": "get_thing"}])
        _patch_max_tool_rounds(monkeypatch, 3)

        call = FunctionCall(name="get_thing", arguments={})
        mock_gemini.generate_messages = AsyncMock(
            side_effect=[
                _turn(text="", tool_calls=[call], input_tokens=100, output_tokens=10),
                _turn(text="final", input_tokens=50, output_tokens=20),
            ]
        )
        step = ParsedStep(
            index=0, step_type="llm", name="find", description="Find it.", is_skill_step=True
        )
        summary = ExecutionSummary(packet_id="packet-1", packet_type="skill_run")
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        await executor._execute_llm_step(
            step, _MockExpertConfig(), base_skill_packet, step_context, {}, execution_summary=summary
        )

        assert summary.total_input_tokens == 150
        assert summary.total_output_tokens == 30
        assert summary.llm_rounds == 2


class TestSkillStepVariableBinding:
    @pytest.mark.asyncio
    async def test_write_extracts_result_line_and_persists_to_packet_state(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet
    ):
        mock_gemini.generate_messages = AsyncMock(
            return_value=_turn(text="I found the count.\n\nRESULT: 42")
        )
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="count_tickets",
            description="Count tickets -> {{ticket_count}}",
            is_skill_step=True,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        result = await executor._execute_llm_step(
            step, _MockExpertConfig(), base_skill_packet, step_context, {}
        )

        mock_packet_service.update_state.assert_awaited_once_with(
            "packet-1", {"ticket_count": "42"}, "session-1"
        )
        # The internal RESULT line doesn't leak into the displayed response.
        assert "RESULT:" not in result
        assert "I found the count." in result

    @pytest.mark.asyncio
    async def test_read_resolves_against_packet_state_from_an_earlier_step(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet
    ):
        """'A -> {{x}} followed by a step reading {{x}} passes the value
        through' -- verbatim acceptance criterion. Simulates step 1 already
        having run by seeding packet_state, matching how a real run's
        packet_service.update_state call from step 1 would have landed it
        there before step 2 executes."""
        base_skill_packet["packet_state"] = {"ticket_count": "42"}
        mock_gemini.generate_messages = AsyncMock(return_value=_turn(text="Evaluating 42 tickets."))
        step = ParsedStep(
            index=1,
            step_type="llm",
            name="evaluate",
            description="Evaluate all {{ticket_count}} tickets for closure.",
            is_skill_step=True,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        await executor._execute_llm_step(step, _MockExpertConfig(), base_skill_packet, step_context, {})

        prompt = mock_gemini.generate_messages.call_args.args[0][0].content
        assert "Evaluate all 42 tickets for closure." in prompt
        assert "{{ticket_count}}" not in prompt

    @pytest.mark.asyncio
    async def test_read_of_undeclared_variable_raises_and_never_calls_llm(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet
    ):
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="broken",
            description="Use {{never_written}} here.",
            is_skill_step=True,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        with pytest.raises(SkillStepVariableError, match="never_written"):
            await executor._execute_llm_step(
                step, _MockExpertConfig(), base_skill_packet, step_context, {}
            )

        # Fails before ever spending a token -- the LLM is never called.
        mock_gemini.generate_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_declared_write_with_no_result_line_raises(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet
    ):
        mock_gemini.generate_messages = AsyncMock(
            return_value=_turn(text="I looked but found nothing conclusive.")
        )
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="count_tickets",
            description="Count tickets -> {{ticket_count}}",
            is_skill_step=True,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        with pytest.raises(SkillStepVariableError, match="ticket_count"):
            await executor._execute_llm_step(
                step, _MockExpertConfig(), base_skill_packet, step_context, {}
            )

        # Never writes an empty/missing value to packet_state.
        mock_packet_service.update_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_step_with_no_write_clause_does_not_call_update_state(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet
    ):
        mock_gemini.generate_messages = AsyncMock(return_value=_turn(text="Just a response."))
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="respond",
            description="Just say hello.",
            is_skill_step=True,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        result = await executor._execute_llm_step(
            step, _MockExpertConfig(), base_skill_packet, step_context, {}
        )

        assert result == "Just a response."
        mock_packet_service.update_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_include_in_skill_step_instruction_raises(
        self, mock_gemini, mock_packet_service, step_context, base_skill_packet
    ):
        # Skill steps don't support {{> partials.x}} in Phase 2 -- see
        # WorkflowExecutor._reject_skill_step_partial.
        step = ParsedStep(
            index=0,
            step_type="llm",
            name="broken",
            description="Do this: {{> partials.something}}",
            is_skill_step=True,
        )
        executor = WorkflowExecutor(mock_gemini, mock_packet_service, None)

        with pytest.raises(SkillStepVariableError):
            await executor._execute_llm_step(
                step, _MockExpertConfig(), base_skill_packet, step_context, {}
            )
