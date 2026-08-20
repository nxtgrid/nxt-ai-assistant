"""Tests for orchestrator.experts.skill_summary -- auto-generating a
skill's catalog summary from its step list (Phase 3 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 4).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.experts.skill_summary import (
    MAX_SUMMARY_CHARS,
    _truncate_at_word_boundary,
    generate_skill_summary,
)


def _steps():
    return [
        {"index": 0, "name": "find", "instruction": "List all open tickets."},
        {"index": 1, "name": "evaluate", "instruction": "Evaluate each for closure."},
    ]


class TestTruncateAtWordBoundary:
    def test_short_text_is_untouched(self):
        assert _truncate_at_word_boundary("short", 200) == "short"

    def test_long_text_backs_up_to_a_space(self):
        text = "word " * 100  # well over 200 chars
        result = _truncate_at_word_boundary(text, 50)

        assert len(result) <= 50
        assert not result.endswith("wor")  # didn't cut mid-word

    def test_strips_trailing_punctuation_after_truncation(self):
        result = _truncate_at_word_boundary("one two three, four", 15)

        assert not result.endswith(",")


class TestGenerateSkillSummary:
    @pytest.mark.asyncio
    async def test_empty_steps_returns_empty_string_without_calling_llm(self):
        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway"
        ) as mock_gateway_factory:
            result = await generate_skill_summary([])

        assert result == ""
        mock_gateway_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_the_model_generated_summary(self):
        mock_gateway = SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(text="Finds and closes stale tickets."))
        )
        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            return_value=mock_gateway,
        ):
            result = await generate_skill_summary(_steps(), title="Ticket Cleanup")

        assert result == "Finds and closes stale tickets."

    @pytest.mark.asyncio
    async def test_strips_surrounding_quotes_the_model_sometimes_adds(self):
        mock_gateway = SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(text='"A quoted summary."'))
        )
        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            return_value=mock_gateway,
        ):
            result = await generate_skill_summary(_steps())

        assert result == "A quoted summary."

    @pytest.mark.asyncio
    async def test_truncates_an_overlong_model_response(self):
        overlong = "word " * 100
        mock_gateway = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(text=overlong)))
        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            return_value=mock_gateway,
        ):
            result = await generate_skill_summary(_steps())

        assert len(result) <= MAX_SUMMARY_CHARS

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty_string_not_raise(self):
        mock_gateway = SimpleNamespace(generate=AsyncMock(side_effect=RuntimeError("api down")))
        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            return_value=mock_gateway,
        ):
            result = await generate_skill_summary(_steps())

        assert result == ""

    @pytest.mark.asyncio
    async def test_prompt_includes_every_step_instruction(self):
        mock_gateway = SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(text="summary"))
        )
        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            return_value=mock_gateway,
        ):
            await generate_skill_summary(_steps())

        sent_prompt = mock_gateway.generate.call_args.args[0][0].text
        assert "List all open tickets." in sent_prompt
        assert "Evaluate each for closure." in sent_prompt

    @pytest.mark.asyncio
    async def test_a_function_step_contributes_its_handler_name_not_none(self):
        """A [function] step has no instruction (SkillStepPayload's name and
        instruction are both Optional for it) -- the prompt should read its
        handler name, not the literal string "None"."""
        mock_gateway = SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(text="summary"))
        )
        steps = [{"index": 0, "kind": "function", "handler": "fetch_grafana_kpis"}]
        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            return_value=mock_gateway,
        ):
            await generate_skill_summary(steps)

        sent_prompt = mock_gateway.generate.call_args.args[0][0].text
        assert "fetch_grafana_kpis" in sent_prompt
        assert "None" not in sent_prompt

    @pytest.mark.asyncio
    async def test_prompt_includes_a_steps_result_preview_when_present(self):
        """The builder captures what a step's tools actually returned
        (skill_builder.py's _step_response_text) so the summary can name
        the kind of data retrieved, not just repeat the instruction verbatim."""
        mock_gateway = SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(text="summary"))
        )
        steps = [
            {
                "index": 0,
                "instruction": "List all open tickets.",
                "result_preview": "Found 12 open tickets across 4 grids.",
            }
        ]
        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            return_value=mock_gateway,
        ):
            await generate_skill_summary(steps)

        sent_prompt = mock_gateway.generate.call_args.args[0][0].text
        assert "Found 12 open tickets across 4 grids." in sent_prompt

    @pytest.mark.asyncio
    async def test_a_step_with_no_result_preview_adds_no_result_line(self):
        mock_gateway = SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(text="summary"))
        )
        steps = [{"index": 0, "instruction": "List all open tickets."}]
        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            return_value=mock_gateway,
        ):
            await generate_skill_summary(steps)

        sent_prompt = mock_gateway.generate.call_args.args[0][0].text
        assert "Result:" not in sent_prompt
