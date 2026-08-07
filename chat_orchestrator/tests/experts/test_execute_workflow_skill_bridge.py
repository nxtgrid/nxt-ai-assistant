"""Tests for execute_workflow's pre_parsed_steps / on_step_complete params
(Phase 5 of docs/superpowers/plans/2026-08-06-user-designed-skills.md).

Nothing before this phase could turn a saved skill into a runnable
workflow: execute_workflow always derived its ParsedStep list from
expert_config.get_workflow(packet_type) + parse_workflow()'s plain-doc-text
parser, which has no notion of a skill step's is_skill_step/allow_write/
is_response_step flags -- going through it would silently drop them. These
two params are the bridge: pre_parsed_steps lets a caller (skill_runner.py)
hand execute_workflow an already-built ParsedStep list directly, and
on_step_complete is the run-mode delivery hook fired once per step that
reaches a terminal outcome.

Uses the same StepRegistry-based function-step pattern as
TestExecuteOneStepSignal in test_workflow_executor.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.clients.gemini import GeminiTurnResult
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import get_step_registry
from orchestrator.experts.workflow_executor import (
    ParsedStep,
    StepExecutionRecord,
    StepStatus,
    WorkflowExecutor,
)


def _turn(text: str = "ok", input_tokens: int = 0, output_tokens: int = 0) -> GeminiTurnResult:
    """Build the real turn object generate_messages returns -- mirrors
    test_workflow_executor.py's own _turn helper."""
    return GeminiTurnResult(
        text=text,
        tool_calls=[],
        finish_reason="STOP",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        raw_response={},
    )


@dataclass
class _MockExpertConfig:
    """get_workflow returns [] (never None -- the real ExpertConfig always
    returns a list, defaulting to ["[llm] execute - ..."] when unconfigured;
    parse_workflow has no None guard, so a mock returning None here would
    crash on a path real ExpertConfig can never take). If pre_parsed_steps
    were NOT actually bypassing get_workflow, the executor's "no workflow
    defined" fallback would kick in and produce a step named "execute", not
    any of this test's real step names -- that mismatch is what proves the
    bypass in test_pre_parsed_steps_bypasses_get_workflow.
    """

    expert_id: str = "skill_runner_test"
    display_name: str = "Skill Runner Test"
    system_instructions: str = ""
    tools: List[str] = field(default_factory=list)

    def get_workflow(self, packet_type: str) -> List[str]:
        return []


def _packet_service() -> MagicMock:
    mock = MagicMock()
    mock.complete_step = AsyncMock(return_value={"packet_id": "test_123"})
    mock.update_state = AsyncMock(return_value={})
    mock.fail_packet = AsyncMock(return_value={"packet_id": "test_123", "status": "failed"})
    mock.record_token_usage = AsyncMock(return_value=None)
    return mock


def _context() -> StepContext:
    return StepContext(
        packet_id="test_123",
        packet_type="skill_run",
        packet_goal="Run a skill",
        packet_inputs={},
        packet_state={},
        current_step="",
        steps_completed=[],
        session_id="session_abc",
        user_email="test@example.com",
    )


def _packet() -> Dict[str, Any]:
    return {
        "id": "uuid-123",
        "packet_id": "test_123",
        "packet_type": "skill_run",
        "packet_goal": "Run a skill",
        "packet_inputs": {},
        "packet_state": {},
        "steps_completed": [],
    }


class TestPreParsedSteps:
    @pytest.mark.asyncio
    async def test_pre_parsed_steps_bypasses_get_workflow(self):
        registry = get_step_registry()

        async def ok_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"ran": True})

        registry.register("my_skill_step", ok_handler)
        try:
            executor = WorkflowExecutor(None, _packet_service(), None)
            steps = [
                ParsedStep(index=0, step_type="function", name="my_skill_step", description="d")
            ]

            response, state = await executor.execute_workflow(
                _MockExpertConfig(), _packet(), _context(), pre_parsed_steps=steps
            )

            assert state["accumulated_results"]["my_skill_step"] == {"ran": True}
        finally:
            registry.unregister("my_skill_step")

    @pytest.mark.asyncio
    async def test_no_pre_parsed_steps_falls_back_to_get_workflow_as_before(self):
        # Regression guard: omitting pre_parsed_steps must preserve the
        # exact pre-Phase-5 behavior (get_workflow() returns None -> the
        # "no workflow defined" fallback step named "execute").
        mock_gemini = MagicMock()
        mock_gemini.generate_messages = AsyncMock(return_value=_turn("ok"))
        executor = WorkflowExecutor(mock_gemini, _packet_service(), None)

        response, state = await executor.execute_workflow(
            _MockExpertConfig(), _packet(), _context()
        )

        assert "execute" in state["accumulated_results"] or response == "ok"


class TestOnStepComplete:
    @pytest.mark.asyncio
    async def test_fires_once_per_completed_step_in_order(self):
        registry = get_step_registry()

        async def ok_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"step": ctx.current_step})

        registry.register("step_one", ok_handler)
        registry.register("step_two", ok_handler)
        try:
            executor = WorkflowExecutor(None, _packet_service(), None)
            steps = [
                ParsedStep(index=0, step_type="function", name="step_one", description="d"),
                ParsedStep(index=1, step_type="function", name="step_two", description="d"),
            ]
            completed: List[tuple] = []

            async def on_step_complete(
                step: ParsedStep, record: StepExecutionRecord, final_response
            ) -> None:
                completed.append((step.name, record.status, final_response))

            await executor.execute_workflow(
                _MockExpertConfig(),
                _packet(),
                _context(),
                pre_parsed_steps=steps,
                on_step_complete=on_step_complete,
            )

            # Function steps never produce a final_response (that's LLM-step
            # only -- see execute_workflow's on_step_complete docstring).
            assert completed == [
                ("step_one", StepStatus.SUCCESS, None),
                ("step_two", StepStatus.SUCCESS, None),
            ]
        finally:
            registry.unregister("step_one")
            registry.unregister("step_two")

    @pytest.mark.asyncio
    async def test_llm_step_passes_its_full_response_text(self):
        # result_summary is a short label ("Generated N chars"), never the
        # actual text -- on_step_complete's 3rd arg is what the plan's
        # Phase 5, item 8 delivery buffer actually sends for a response step.
        mock_gemini = MagicMock()
        mock_gemini.generate_messages = AsyncMock(
            return_value=_turn("This is the full LLM response text.")
        )
        executor = WorkflowExecutor(mock_gemini, _packet_service(), None)
        steps = [ParsedStep(index=0, step_type="llm", name="respond", description="d")]
        seen = []

        async def on_step_complete(step, record, final_response) -> None:
            seen.append(final_response)
            assert record.result_summary != final_response

        await executor.execute_workflow(
            _MockExpertConfig(),
            _packet(),
            _context(),
            pre_parsed_steps=steps,
            on_step_complete=on_step_complete,
        )

        assert seen == ["This is the full LLM response text."]

    @pytest.mark.asyncio
    async def test_no_callback_means_no_behavior_change(self):
        registry = get_step_registry()

        async def ok_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"ok": True})

        registry.register("step_one", ok_handler)
        try:
            executor = WorkflowExecutor(None, _packet_service(), None)
            steps = [ParsedStep(index=0, step_type="function", name="step_one", description="d")]

            # Must not raise just because on_step_complete was omitted.
            response, state = await executor.execute_workflow(
                _MockExpertConfig(), _packet(), _context(), pre_parsed_steps=steps
            )

            assert state["accumulated_results"]["step_one"] == {"ok": True}
        finally:
            registry.unregister("step_one")

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_fail_the_workflow(self):
        registry = get_step_registry()

        async def ok_handler(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"ok": True})

        registry.register("step_one", ok_handler)
        try:
            executor = WorkflowExecutor(None, _packet_service(), None)
            steps = [ParsedStep(index=0, step_type="function", name="step_one", description="d")]

            async def broken_callback(step: ParsedStep, record: StepExecutionRecord, _fr) -> None:
                raise RuntimeError("delivery backend down")

            # Should complete normally despite the callback raising.
            response, state = await executor.execute_workflow(
                _MockExpertConfig(),
                _packet(),
                _context(),
                pre_parsed_steps=steps,
                on_step_complete=broken_callback,
            )

            assert state["accumulated_results"]["step_one"] == {"ok": True}
        finally:
            registry.unregister("step_one")
