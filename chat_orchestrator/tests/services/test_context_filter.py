"""Tests for ContextFilterService."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.models.schemas import ConversationMessage, FunctionCall, ToolCallResult
from orchestrator.services.context_filter import ContextFilterResult, ContextFilterService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(role="user", content="hello", function_call=None, tool_result=None):
    return ConversationMessage(
        role=role,
        content=content,
        function_call=function_call,
        tool_result=tool_result,
    )


def _fc(name="get_grid_status"):
    return FunctionCall(name=name, args={"grid": "ExampleGrid"})


def _tr(name="get_grid_status"):
    return ToolCallResult(name=name, success=True, output="OK")


# ---------------------------------------------------------------------------
# _parse_result
# ---------------------------------------------------------------------------


class TestParseResult:
    def setup_method(self):
        self.service = ContextFilterService(api_key="fake")

    def test_valid_json(self):
        text = '{"relevant_indices": [0, 2, 4], "confidence": 0.92}'
        result = self.service._parse_result(text, num_candidates=5)
        assert result.relevant_indices == [0, 2, 4]
        assert result.confidence == 0.92

    def test_markdown_wrapped_json(self):
        text = '```json\n{"relevant_indices": [1, 3], "confidence": 0.8}\n```'
        result = self.service._parse_result(text, num_candidates=5)
        assert result.relevant_indices == [1, 3]
        assert result.confidence == 0.8

    def test_invalid_json_fail_open(self):
        text = "this is not json at all"
        result = self.service._parse_result(text, num_candidates=3)
        assert result.relevant_indices == [0, 1, 2]
        assert result.confidence == 0.0

    def test_empty_text_fail_open(self):
        result = self.service._parse_result("", num_candidates=4)
        assert result.relevant_indices == [0, 1, 2, 3]
        assert result.confidence == 0.0

    def test_out_of_range_indices_stripped(self):
        text = '{"relevant_indices": [0, 1, 99, -1], "confidence": 0.75}'
        result = self.service._parse_result(text, num_candidates=3)
        assert result.relevant_indices == [0, 1]
        assert result.confidence == 0.75

    def test_all_indices_out_of_range_fail_open(self):
        text = '{"relevant_indices": [10, 20], "confidence": 0.9}'
        result = self.service._parse_result(text, num_candidates=3)
        # All invalid → fall back to all indices
        assert result.relevant_indices == [0, 1, 2]


# ---------------------------------------------------------------------------
# _enforce_tool_pairs
# ---------------------------------------------------------------------------


class TestEnforceToolPairs:
    def setup_method(self):
        self.service = ContextFilterService(api_key="fake")

    def test_function_call_pulls_in_tool_result(self):
        messages = [
            _msg(content="What's the grid status?"),
            _msg(role="model", function_call=_fc()),
            _msg(role="tool", tool_result=_tr()),
            _msg(role="model", content="The grid is online."),
        ]
        # Only index 1 (function_call) selected → should pull in index 2
        result = ContextFilterResult(relevant_indices=[1], confidence=0.9)
        enforced = self.service._enforce_tool_pairs(result, messages)
        assert 1 in enforced.relevant_indices
        assert 2 in enforced.relevant_indices

    def test_tool_result_pulls_in_function_call(self):
        messages = [
            _msg(content="What's the grid status?"),
            _msg(role="model", function_call=_fc()),
            _msg(role="tool", tool_result=_tr()),
            _msg(role="model", content="The grid is online."),
        ]
        # Only index 2 (tool_result) selected → should pull in index 1
        result = ContextFilterResult(relevant_indices=[2], confidence=0.9)
        enforced = self.service._enforce_tool_pairs(result, messages)
        assert 1 in enforced.relevant_indices
        assert 2 in enforced.relevant_indices

    def test_non_tool_messages_untouched(self):
        messages = [
            _msg(content="Hello"),
            _msg(role="model", content="Hi there"),
            _msg(content="How are you?"),
        ]
        result = ContextFilterResult(relevant_indices=[0], confidence=0.9)
        enforced = self.service._enforce_tool_pairs(result, messages)
        assert enforced.relevant_indices == [0]

    def test_preserves_confidence(self):
        messages = [_msg(content="hi")]
        result = ContextFilterResult(relevant_indices=[0], confidence=0.77)
        enforced = self.service._enforce_tool_pairs(result, messages)
        assert enforced.confidence == 0.77

    def test_indices_sorted_after_enforcement(self):
        messages = [
            _msg(content="user msg"),
            _msg(role="model", function_call=_fc()),
            _msg(role="tool", tool_result=_tr()),
            _msg(role="model", content="response"),
        ]
        # Select tool_result (2) → should add function_call (1), result sorted
        result = ContextFilterResult(relevant_indices=[3, 2], confidence=0.8)
        enforced = self.service._enforce_tool_pairs(result, messages)
        assert enforced.relevant_indices == sorted(enforced.relevant_indices)


# ---------------------------------------------------------------------------
# filter_history (integration-level, mocked LLM)
# ---------------------------------------------------------------------------


class TestFilterHistory:
    @pytest.mark.asyncio
    async def test_no_api_key_fail_open(self):
        service = ContextFilterService(api_key="")
        messages = [_msg(), _msg(role="model", content="reply")]
        result = await service.filter_history("new message", messages)
        assert result.relevant_indices == [0, 1]
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_empty_candidates(self):
        service = ContextFilterService(api_key="fake")
        result = await service.filter_history("hello", [])
        assert result.relevant_indices == []

    @pytest.mark.asyncio
    async def test_exception_fail_open(self):
        service = ContextFilterService(api_key="fake")
        messages = [_msg(), _msg(role="model", content="reply")]
        with patch.object(service, "_call_gemini", side_effect=RuntimeError("boom")):
            result = await service.filter_history("test", messages)
        assert result.relevant_indices == [0, 1]
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_successful_filter(self):
        service = ContextFilterService(api_key="fake")
        messages = [
            _msg(content="grid status for ExampleGrid"),
            _msg(role="model", content="ExampleGrid is online"),
            _msg(content="unrelated JIRA question"),
            _msg(role="model", content="JIRA answer"),
            _msg(content="what about ExampleGrid power?"),
        ]
        mock_response = '{"relevant_indices": [0, 1], "confidence": 0.9}'
        with patch.object(
            service, "_call_gemini", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await service.filter_history("ExampleGrid power output?", messages)
        assert result.relevant_indices == [0, 1]
        assert result.confidence == 0.9

    @pytest.mark.asyncio
    async def test_tool_pairs_enforced_after_parse(self):
        """filter_history should auto-include tool_result paired with function_call."""
        service = ContextFilterService(api_key="fake")
        messages = [
            _msg(content="check grid"),
            _msg(role="model", function_call=_fc()),
            _msg(role="tool", tool_result=_tr()),
            _msg(role="model", content="Grid is online"),
        ]
        # LLM returns only index 1 (function_call) — enforcement should add 2
        mock_response = '{"relevant_indices": [1], "confidence": 0.85}'
        with patch.object(
            service, "_call_gemini", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await service.filter_history("grid status", messages)
        assert 1 in result.relevant_indices
        assert 2 in result.relevant_indices
