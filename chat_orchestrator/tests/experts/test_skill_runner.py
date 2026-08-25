"""Tests for orchestrator.experts.skill_runner (Phase 5 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md): the bridge from
a saved skill to a runnable expert workflow.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.experts.skill_runner import (
    SKILL_EXPERT_PREFIX,
    SKILL_PACKET_TYPE,
    _ResponseBuffer,
    _resolve_skill_system_instructions,
    build_parsed_steps,
    is_skill_expert_id,
    run_skill_packet,
)
from orchestrator.experts.workflow_executor import ParsedStep, StepExecutionRecord
from orchestrator.models.schemas import UserContext


class TestIsSkillExpertId:
    def test_recognizes_the_skill_prefix(self):
        assert is_skill_expert_id("skill:11111111-1111-1111-1111-111111111111") is True

    def test_rejects_a_real_expert_id(self):
        assert is_skill_expert_id("grid_analyst") is False

    def test_rejects_none(self):
        assert is_skill_expert_id(None) is False

    def test_rejects_empty_string(self):
        assert is_skill_expert_id("") is False


class TestBuildParsedSteps:
    def _steps(self) -> List[Dict[str, Any]]:
        return [
            {"index": 0, "name": "find", "instruction": "List open tickets. -> {{tickets}}"},
            {"index": 1, "name": "summarize", "instruction": "Summarize {{tickets}}."},
        ]

    def test_every_step_is_an_llm_step_flagged_as_a_skill_step(self):
        steps = build_parsed_steps(self._steps())

        assert all(s.step_type == "llm" for s in steps)
        assert all(s.is_skill_step for s in steps)

    def test_preserves_order_by_index(self):
        unordered = [
            {"index": 1, "name": "second", "instruction": "b"},
            {"index": 0, "name": "first", "instruction": "a"},
        ]

        steps = build_parsed_steps(unordered)

        assert [s.name for s in steps] == ["first", "second"]

    def test_last_step_forced_response_step_even_if_not_flagged(self):
        steps = build_parsed_steps(self._steps())

        assert steps[-1].is_response_step is True

    def test_earlier_step_response_flag_defaults_false(self):
        steps = build_parsed_steps(self._steps())

        assert steps[0].is_response_step is False

    def test_explicit_response_step_flag_is_preserved(self):
        raw = [
            {"index": 0, "name": "a", "instruction": "x", "is_response_step": True},
            {"index": 1, "name": "b", "instruction": "y"},
        ]

        steps = build_parsed_steps(raw)

        assert steps[0].is_response_step is True  # explicit, not just the last step
        assert steps[1].is_response_step is True  # last step, forced

    def test_allow_write_defaults_false(self):
        steps = build_parsed_steps(self._steps())

        assert all(s.allow_write is False for s in steps)

    def test_allow_write_preserved_when_set(self):
        raw = [{"index": 0, "name": "a", "instruction": "x", "allow_write": True}]

        steps = build_parsed_steps(raw)

        assert steps[0].allow_write is True

    def test_falls_back_to_step_n_name_when_unnamed(self):
        raw = [{"index": 0, "instruction": "x"}]

        steps = build_parsed_steps(raw)

        assert steps[0].name == "step_1"

    def test_empty_steps_returns_empty_list(self):
        assert build_parsed_steps([]) == []

    # Phase 5 (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md):
    # mock field reading, mirroring the allow_write tests above.

    def test_mock_defaults_none_for_llm_steps(self):
        steps = build_parsed_steps(self._steps())

        assert all(s.mock is None for s in steps)

    def test_mock_true_preserved_for_llm_steps(self):
        raw = [{"index": 0, "name": "a", "instruction": "x", "mock": True}]

        steps = build_parsed_steps(raw)

        assert steps[0].mock is True

    def test_mock_false_preserved_for_llm_steps(self):
        """False is distinct from absent (None) -- an explicit opt-out must
        survive, not collapse to the same 'no override' value absence gets."""
        raw = [{"index": 0, "name": "a", "instruction": "x", "mock": False}]

        steps = build_parsed_steps(raw)

        assert steps[0].mock is False

    def test_mock_defaults_none_for_function_steps(self):
        raw = [{"index": 0, "kind": "function", "handler": "copy_lpp_template"}]

        steps = build_parsed_steps(raw)

        assert steps[0].mock is None

    def test_mock_true_preserved_for_function_steps(self):
        raw = [
            {"index": 0, "kind": "function", "handler": "copy_lpp_template", "mock": True}
        ]

        steps = build_parsed_steps(raw)

        assert steps[0].step_type == "function"
        assert steps[0].mock is True


class TestResponseBuffer:
    @pytest.mark.asyncio
    async def test_non_response_step_is_buffered_not_sent(self):
        buffer = _ResponseBuffer("tok", "-100", None)
        step = ParsedStep(index=0, step_type="llm", name="a", description="d")
        record = StepExecutionRecord(
            step_name="a", step_type="llm", description="d", result_summary="did a thing"
        )

        with patch(
            "orchestrator.experts.skill_runner.send_telegram_message", new_callable=AsyncMock
        ) as mock_send:
            await buffer.on_step_complete(step, record, None)

        mock_send.assert_not_awaited()
        assert buffer.messages_sent == 0

    @pytest.mark.asyncio
    async def test_response_step_sends_full_text_not_summary(self):
        buffer = _ResponseBuffer("tok", "-100", "42")
        step = ParsedStep(
            index=0, step_type="llm", name="a", description="d", is_response_step=True
        )
        record = StepExecutionRecord(
            step_name="a", step_type="llm", description="d", result_summary="short label"
        )

        with patch(
            "orchestrator.experts.skill_runner.send_telegram_message", new_callable=AsyncMock
        ) as mock_send:
            await buffer.on_step_complete(step, record, "The full response text.")

        mock_send.assert_awaited_once_with("tok", "-100", "The full response text.", topic_id="42")
        assert buffer.messages_sent == 1

    @pytest.mark.asyncio
    async def test_response_step_prefixes_buffered_summaries_since_last_send(self):
        buffer = _ResponseBuffer("tok", "-100", None)
        buffered_step = ParsedStep(index=0, step_type="llm", name="a", description="d")
        buffered_record = StepExecutionRecord(
            step_name="a", step_type="llm", description="d", result_summary="did step a"
        )
        response_step = ParsedStep(
            index=1, step_type="llm", name="b", description="d", is_response_step=True
        )
        response_record = StepExecutionRecord(step_name="b", step_type="llm", description="d")

        with patch(
            "orchestrator.experts.skill_runner.send_telegram_message", new_callable=AsyncMock
        ) as mock_send:
            await buffer.on_step_complete(buffered_step, buffered_record, None)
            await buffer.on_step_complete(response_step, response_record, "Final text.")

        sent_text = mock_send.call_args.args[2]
        assert "did step a" in sent_text
        assert "Final text." in sent_text
        assert sent_text.index("did step a") < sent_text.index("Final text.")

    @pytest.mark.asyncio
    async def test_buffer_clears_after_a_response_step_sends(self):
        buffer = _ResponseBuffer("tok", "-100", None)
        step = ParsedStep(
            index=0, step_type="llm", name="a", description="d", is_response_step=True
        )
        record = StepExecutionRecord(step_name="a", step_type="llm", description="d")

        with patch(
            "orchestrator.experts.skill_runner.send_telegram_message", new_callable=AsyncMock
        ) as mock_send:
            await buffer.on_step_complete(step, record, "first")
            await buffer.on_step_complete(step, record, "second")

        first_text = mock_send.call_args_list[0].args[2]
        second_text = mock_send.call_args_list[1].args[2]
        assert first_text == "first"
        assert second_text == "second"  # not re-prefixed with "first"'s summary

    @pytest.mark.asyncio
    async def test_missing_bot_token_skips_send_without_raising(self):
        buffer = _ResponseBuffer("", "-100", None)
        step = ParsedStep(
            index=0, step_type="llm", name="a", description="d", is_response_step=True
        )
        record = StepExecutionRecord(step_name="a", step_type="llm", description="d")

        with patch(
            "orchestrator.experts.skill_runner.send_telegram_message", new_callable=AsyncMock
        ) as mock_send:
            await buffer.on_step_complete(step, record, "text")

        mock_send.assert_not_awaited()
        assert buffer.messages_sent == 0

    # R6 (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md,
    # Task 5.5): the chat response surface must say "mocked" too.

    @pytest.mark.asyncio
    async def test_dry_run_prefixes_the_delivered_message(self):
        buffer = _ResponseBuffer("tok", "-100", None, dry_run=True)
        step = ParsedStep(
            index=0, step_type="llm", name="a", description="d", is_response_step=True
        )
        record = StepExecutionRecord(step_name="a", step_type="llm", description="d")

        with patch(
            "orchestrator.experts.skill_runner.send_telegram_message", new_callable=AsyncMock
        ) as mock_send:
            await buffer.on_step_complete(step, record, "Sent the map.")

        sent_text = mock_send.call_args.args[2]
        assert "MOCK RUN" in sent_text
        assert "Sent the map." in sent_text

    @pytest.mark.asyncio
    async def test_dry_run_false_leaves_the_message_unprefixed(self):
        buffer = _ResponseBuffer("tok", "-100", None, dry_run=False)
        step = ParsedStep(
            index=0, step_type="llm", name="a", description="d", is_response_step=True
        )
        record = StepExecutionRecord(step_name="a", step_type="llm", description="d")

        with patch(
            "orchestrator.experts.skill_runner.send_telegram_message", new_callable=AsyncMock
        ) as mock_send:
            await buffer.on_step_complete(step, record, "Sent the map.")

        sent_text = mock_send.call_args.args[2]
        assert sent_text == "Sent the map."

    @pytest.mark.asyncio
    async def test_dry_run_defaults_false_for_existing_callers(self):
        """Back-compat: every construction site before Phase 5 passes no
        dry_run kwarg at all."""
        buffer = _ResponseBuffer("tok", "-100", None)
        step = ParsedStep(
            index=0, step_type="llm", name="a", description="d", is_response_step=True
        )
        record = StepExecutionRecord(step_name="a", step_type="llm", description="d")

        with patch(
            "orchestrator.experts.skill_runner.send_telegram_message", new_callable=AsyncMock
        ) as mock_send:
            await buffer.on_step_complete(step, record, "text")

        assert mock_send.call_args.args[2] == "text"


class TestRunSkillPacket:
    def _skill(self, **overrides) -> Dict[str, Any]:
        base = {
            "id": "11111111-1111-1111-1111-111111111111",
            "slug": "find-tickets",
            "title": "Find Tickets",
            "summary": "Finds open tickets.",
            "steps": [{"index": 0, "name": "find", "instruction": "List tickets."}],
            "inputs": [],
            "staff_only": True,
            "status": "active",
            "created_by": "creator@example.com",
        }
        base.update(overrides)
        return base

    def _state(self, **overrides) -> Dict[str, Any]:
        base = {
            "session_id": "session_abc",
            "metadata": {
                "scheduled_execution": True,
                "skill_id": "11111111-1111-1111-1111-111111111111",
            },
            "user_context": UserContext(
                user_id="scheduled",
                user_email="ops@example.com",
                source="telegram",
                chat_id="-100999",
                topic_id="42",
                organization_ids=["7"],
            ),
            "tool_executor": MagicMock(),
        }
        base.update(overrides)
        return base

    def _packet_service(self, packet: Dict[str, Any]) -> MagicMock:
        service = MagicMock()
        service.create_packet = AsyncMock(return_value=packet)
        service.start_packet = AsyncMock(return_value=packet)
        service.fail_packet = AsyncMock(return_value={**packet, "packet_status": "failed"})
        return service

    @pytest.mark.asyncio
    async def test_skill_not_found_returns_error_without_creating_a_packet(self):
        mock_supabase = MagicMock()
        mock_supabase.get_skill = AsyncMock(return_value=None)
        packet_service = self._packet_service({})

        with patch(
            "orchestrator.experts.skill_runner.get_supabase_client", return_value=mock_supabase
        ):
            result = await run_skill_packet(
                self._state(),
                f"{SKILL_EXPERT_PREFIX}11111111-1111-1111-1111-111111111111",
                packet_service,
            )

        assert result["expert_executed"] is False
        assert "not found" in result["expert_error"]
        packet_service.create_packet.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inactive_skill_returns_error_without_creating_a_packet(self):
        mock_supabase = MagicMock()
        mock_supabase.get_skill = AsyncMock(return_value=self._skill(status="unusable"))
        packet_service = self._packet_service({})

        with patch(
            "orchestrator.experts.skill_runner.get_supabase_client", return_value=mock_supabase
        ):
            result = await run_skill_packet(
                self._state(),
                f"{SKILL_EXPERT_PREFIX}11111111-1111-1111-1111-111111111111",
                packet_service,
            )

        assert result["expert_executed"] is False
        assert "unusable" in result["expert_error"]
        packet_service.create_packet.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_steps_returns_error_without_creating_a_packet(self):
        mock_supabase = MagicMock()
        mock_supabase.get_skill = AsyncMock(return_value=self._skill(steps=[]))
        packet_service = self._packet_service({})

        with patch(
            "orchestrator.experts.skill_runner.get_supabase_client", return_value=mock_supabase
        ):
            result = await run_skill_packet(
                self._state(),
                f"{SKILL_EXPERT_PREFIX}11111111-1111-1111-1111-111111111111",
                packet_service,
            )

        assert result["expert_executed"] is False
        assert "no steps" in result["expert_error"].lower()
        packet_service.create_packet.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_happy_path_creates_and_executes_the_packet(self):
        mock_supabase = MagicMock()
        mock_supabase.get_skill = AsyncMock(return_value=self._skill())
        packet = {
            "id": "uuid-1",
            "packet_id": "skill_run_20260101_abc",
            "packet_type": SKILL_PACKET_TYPE,
            "packet_goal": "Finds open tickets.",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "find",
            "organization_id": 7,
        }
        packet_service = self._packet_service(packet)

        mock_executor = MagicMock()
        mock_executor.execute_workflow = AsyncMock(
            return_value=("Found 3 tickets.", {"accumulated_results": {}})
        )

        with (
            patch(
                "orchestrator.experts.skill_runner.get_supabase_client", return_value=mock_supabase
            ),
            patch(
                "orchestrator.experts.skill_runner.create_chat_llm_client", return_value=MagicMock()
            ),
            patch("orchestrator.experts.skill_runner.WorkflowExecutor", return_value=mock_executor),
            # This suite mocks no knowledge-store/JIT-provider network calls;
            # without this, _resolve_skill_system_instructions would try a
            # real (fake-URL) Supabase call on every test that reaches this
            # point. Its own behavior (empty/non-empty/grid-preference) is
            # covered directly by TestResolveSkillSystemInstructions below.
            patch(
                "orchestrator.experts.skill_runner._resolve_skill_system_instructions",
                new=AsyncMock(return_value=""),
            ),
        ):
            result = await run_skill_packet(
                self._state(),
                f"{SKILL_EXPERT_PREFIX}11111111-1111-1111-1111-111111111111",
                packet_service,
            )

        packet_service.create_packet.assert_awaited_once()
        create_kwargs = packet_service.create_packet.call_args.kwargs
        assert create_kwargs["packet_type"] == SKILL_PACKET_TYPE
        assert (
            create_kwargs["assigned_expert"]
            == f"{SKILL_EXPERT_PREFIX}11111111-1111-1111-1111-111111111111"
        )
        assert create_kwargs["requested_by_email"] == "creator@example.com"

        # pre_parsed_steps must have been passed through, preserving the
        # bridge's whole reason for existing.
        execute_kwargs = mock_executor.execute_workflow.call_args.kwargs
        assert execute_kwargs["pre_parsed_steps"][0].name == "find"
        assert execute_kwargs["pre_parsed_steps"][0].is_skill_step is True
        assert execute_kwargs["on_step_complete"] is not None

        assert result["expert_executed"] is True
        assert result["final_response"] == "Found 3 tickets."
        assert result["active_work_packet"] == packet

    # Task 5.1/5.5 (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-
    # tools.md): dry_run read from metadata, threaded to StepContext.dry_run,
    # and marked on the persisted packet's own title.

    @pytest.mark.asyncio
    async def test_dry_run_metadata_sets_context_dry_run_and_prefixes_packet_title(self):
        mock_supabase = MagicMock()
        mock_supabase.get_skill = AsyncMock(return_value=self._skill())
        packet = {
            "id": "uuid-1",
            "packet_id": "skill_run_20260101_abc",
            "packet_type": SKILL_PACKET_TYPE,
            "packet_goal": "Finds open tickets.",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "find",
            "organization_id": 7,
        }
        packet_service = self._packet_service(packet)

        mock_executor = MagicMock()
        mock_executor.execute_workflow = AsyncMock(
            return_value=("Found 3 tickets.", {"accumulated_results": {}})
        )

        state = self._state()
        state["metadata"] = {**state["metadata"], "dry_run": True}

        with (
            patch(
                "orchestrator.experts.skill_runner.get_supabase_client", return_value=mock_supabase
            ),
            patch(
                "orchestrator.experts.skill_runner.create_chat_llm_client", return_value=MagicMock()
            ),
            patch("orchestrator.experts.skill_runner.WorkflowExecutor", return_value=mock_executor),
            # This suite mocks no knowledge-store/JIT-provider network calls;
            # without this, _resolve_skill_system_instructions would try a
            # real (fake-URL) Supabase call on every test that reaches this
            # point. Its own behavior (empty/non-empty/grid-preference) is
            # covered directly by TestResolveSkillSystemInstructions below.
            patch(
                "orchestrator.experts.skill_runner._resolve_skill_system_instructions",
                new=AsyncMock(return_value=""),
            ),
        ):
            await run_skill_packet(
                state,
                f"{SKILL_EXPERT_PREFIX}11111111-1111-1111-1111-111111111111",
                packet_service,
            )

        create_kwargs = packet_service.create_packet.call_args.kwargs
        assert create_kwargs["packet_title"].startswith("[MOCK RUN] ")

        execute_kwargs = mock_executor.execute_workflow.call_args.kwargs
        assert execute_kwargs["context"].dry_run is True

    @pytest.mark.asyncio
    async def test_dry_run_absent_from_metadata_defaults_false(self):
        """Back-compat: every scheduled/triggered run today sets no dry_run
        key at all -- must behave exactly as before Phase 5."""
        mock_supabase = MagicMock()
        mock_supabase.get_skill = AsyncMock(return_value=self._skill())
        packet = {
            "id": "uuid-1",
            "packet_id": "skill_run_20260101_abc",
            "packet_type": SKILL_PACKET_TYPE,
            "packet_goal": "Finds open tickets.",
            "packet_inputs": {},
            "packet_state": {},
            "steps_completed": [],
            "current_step": "find",
            "organization_id": 7,
        }
        packet_service = self._packet_service(packet)

        mock_executor = MagicMock()
        mock_executor.execute_workflow = AsyncMock(
            return_value=("Found 3 tickets.", {"accumulated_results": {}})
        )

        with (
            patch(
                "orchestrator.experts.skill_runner.get_supabase_client", return_value=mock_supabase
            ),
            patch(
                "orchestrator.experts.skill_runner.create_chat_llm_client", return_value=MagicMock()
            ),
            patch("orchestrator.experts.skill_runner.WorkflowExecutor", return_value=mock_executor),
            # This suite mocks no knowledge-store/JIT-provider network calls;
            # without this, _resolve_skill_system_instructions would try a
            # real (fake-URL) Supabase call on every test that reaches this
            # point. Its own behavior (empty/non-empty/grid-preference) is
            # covered directly by TestResolveSkillSystemInstructions below.
            patch(
                "orchestrator.experts.skill_runner._resolve_skill_system_instructions",
                new=AsyncMock(return_value=""),
            ),
        ):
            await run_skill_packet(
                self._state(),
                f"{SKILL_EXPERT_PREFIX}11111111-1111-1111-1111-111111111111",
                packet_service,
            )

        create_kwargs = packet_service.create_packet.call_args.kwargs
        assert not create_kwargs["packet_title"].startswith("[MOCK RUN] ")

        execute_kwargs = mock_executor.execute_workflow.call_args.kwargs
        assert execute_kwargs["context"].dry_run is False

    @pytest.mark.asyncio
    async def test_workflow_exception_fails_the_packet(self):
        mock_supabase = MagicMock()
        mock_supabase.get_skill = AsyncMock(return_value=self._skill())
        packet = {
            "packet_id": "skill_run_1",
            "packet_type": SKILL_PACKET_TYPE,
            "packet_goal": "Finds open tickets.",
        }
        packet_service = self._packet_service(packet)

        mock_executor = MagicMock()
        mock_executor.execute_workflow = AsyncMock(side_effect=RuntimeError("LLM exploded"))

        with (
            patch(
                "orchestrator.experts.skill_runner.get_supabase_client", return_value=mock_supabase
            ),
            patch(
                "orchestrator.experts.skill_runner.create_chat_llm_client", return_value=MagicMock()
            ),
            patch("orchestrator.experts.skill_runner.WorkflowExecutor", return_value=mock_executor),
            # This suite mocks no knowledge-store/JIT-provider network calls;
            # without this, _resolve_skill_system_instructions would try a
            # real (fake-URL) Supabase call on every test that reaches this
            # point. Its own behavior (empty/non-empty/grid-preference) is
            # covered directly by TestResolveSkillSystemInstructions below.
            patch(
                "orchestrator.experts.skill_runner._resolve_skill_system_instructions",
                new=AsyncMock(return_value=""),
            ),
        ):
            result = await run_skill_packet(
                self._state(),
                f"{SKILL_EXPERT_PREFIX}11111111-1111-1111-1111-111111111111",
                packet_service,
            )

        packet_service.fail_packet.assert_awaited_once()
        assert result["expert_executed"] is False
        assert "LLM exploded" in result["expert_error"]


class TestResolveSkillSystemInstructions:
    @pytest.mark.asyncio
    async def test_combines_inline_and_jit_text(self):
        user_context = UserContext(
            user_id="u", user_email="ops@example.com", source="telegram",
            organization_ids=["7"],
        )
        with patch(
            "orchestrator.experts.skill_runner.compose_knowledge_text",
            return_value=("# Technical Knowledge\n\nInline body.", ["comms"]),
        ), patch(
            "orchestrator.experts.skill_runner.resolve_jit_context_for",
            new=AsyncMock(return_value=("# Live Context\n\nLive body.", ["entity-graph"])),
        ), patch(
            "orchestrator.experts.skill_runner.resolve_scope_grid_from_user_context",
            new=AsyncMock(return_value=None),
        ):
            result = await _resolve_skill_system_instructions(
                "11111111-1111-1111-1111-111111111111", user_context, {}
            )

        assert "Inline body." in result
        assert "Live body." in result

    @pytest.mark.asyncio
    async def test_empty_when_nothing_pinned(self):
        with patch(
            "orchestrator.experts.skill_runner.compose_knowledge_text",
            return_value=(None, []),
        ), patch(
            "orchestrator.experts.skill_runner.resolve_jit_context_for",
            new=AsyncMock(return_value=("", [])),
        ), patch(
            "orchestrator.experts.skill_runner.resolve_scope_grid_from_user_context",
            new=AsyncMock(return_value=None),
        ):
            result = await _resolve_skill_system_instructions("id", None, {})

        assert result == ""

    @pytest.mark.asyncio
    async def test_prefers_an_explicit_grid_over_the_channel_resolver(self):
        with patch(
            "orchestrator.experts.skill_runner.compose_knowledge_text", return_value=(None, [])
        ) as mock_compose, patch(
            "orchestrator.experts.skill_runner.resolve_jit_context_for",
            new=AsyncMock(return_value=("", [])),
        ) as mock_jit, patch(
            "orchestrator.experts.skill_runner.resolve_scope_grid_from_user_context",
            new=AsyncMock(return_value="should-not-be-used"),
        ) as mock_channel_grid:
            await _resolve_skill_system_instructions(
                "id", None, {"grid": {"grid_name": "grid-a"}}
            )

        mock_channel_grid.assert_not_awaited()
        assert mock_compose.call_args.args[2].grid == "grid-a"
        assert mock_jit.call_args.kwargs["grid"] == "grid-a"
