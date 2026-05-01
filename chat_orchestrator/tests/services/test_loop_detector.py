"""Tests for cross-request loop detector."""

from __future__ import annotations

import os
from unittest.mock import patch

from orchestrator.models.schemas import ConversationMessage, FunctionCall, ToolCallResult
from orchestrator.services.loop_detector import (
    LOOP_HINT_MESSAGE,
    ModelTurn,
    _extract_model_turns,
    _normalize_arguments,
    _turns_are_similar,
    detect_cross_request_loop,
)

# ---------------------------------------------------------------------------
# _normalize_arguments
# ---------------------------------------------------------------------------


class TestNormalizeArguments:
    def test_sorts_keys(self):
        result = _normalize_arguments({"z": 1, "a": 2})
        assert result == '{"a": 2, "z": 1}'

    def test_nested_dicts(self):
        result = _normalize_arguments({"b": {"z": 1, "a": 2}, "a": 3})
        assert result == '{"a": 3, "b": {"a": 2, "z": 1}}'

    def test_non_serializable_falls_back(self):
        obj = object()
        result = _normalize_arguments(obj)
        assert "object" in result

    def test_empty_dict(self):
        assert _normalize_arguments({}) == "{}"

    def test_list_args(self):
        result = _normalize_arguments({"ids": [3, 1, 2]})
        assert result == '{"ids": [3, 1, 2]}'


# ---------------------------------------------------------------------------
# _extract_model_turns
# ---------------------------------------------------------------------------


class TestExtractModelTurns:
    def test_simple_conversation(self):
        history = [
            ConversationMessage(role="user", content="Hello"),
            ConversationMessage(role="model", content="Hi there!"),
            ConversationMessage(role="user", content="How are you?"),
            ConversationMessage(role="model", content="I'm good!"),
        ]
        turns = _extract_model_turns(history)
        assert len(turns) == 2
        assert turns[0].response_text == "Hi there!"
        assert turns[1].response_text == "I'm good!"

    def test_multi_tool_turn(self):
        history = [
            ConversationMessage(role="user", content="Check meter"),
            ConversationMessage(
                role="model",
                function_call=FunctionCall(name="get_meter", arguments={"id": 1}),
            ),
            ConversationMessage(
                role="tool",
                tool_result=ToolCallResult(name="get_meter", success=True, output={"status": "ok"}),
            ),
            ConversationMessage(role="model", content="Meter is OK"),
            ConversationMessage(role="user", content="Thanks"),
        ]
        turns = _extract_model_turns(history)
        assert len(turns) == 1
        assert len(turns[0].tool_calls) == 1
        tool_name, _ = next(iter(turns[0].tool_calls))
        assert tool_name == "get_meter"
        assert turns[0].response_text == "Meter is OK"

    def test_empty_history(self):
        turns = _extract_model_turns([])
        assert turns == []

    def test_only_user_messages(self):
        history = [
            ConversationMessage(role="user", content="Hello"),
            ConversationMessage(role="user", content="Anyone there?"),
        ]
        turns = _extract_model_turns(history)
        assert turns == []

    def test_trailing_model_turn_flushed(self):
        """Model turn at end of history (no following user message) is included."""
        history = [
            ConversationMessage(role="user", content="Hello"),
            ConversationMessage(role="model", content="Hi!"),
        ]
        turns = _extract_model_turns(history)
        assert len(turns) == 1
        assert turns[0].response_text == "Hi!"


# ---------------------------------------------------------------------------
# _turns_are_similar
# ---------------------------------------------------------------------------


class TestTurnsAreSimilar:
    def test_identical_tool_calls(self):
        args = _normalize_arguments({"org_id": 2, "grid": "ExampleGrid"})
        turn_a = ModelTurn(tool_calls=frozenset({("get_status", args)}))
        turn_b = ModelTurn(tool_calls=frozenset({("get_status", args)}))
        assert _turns_are_similar(turn_a, turn_b) is True

    def test_different_tool_calls(self):
        args_a = _normalize_arguments({"org_id": 2})
        args_b = _normalize_arguments({"org_id": 3})
        turn_a = ModelTurn(tool_calls=frozenset({("get_status", args_a)}))
        turn_b = ModelTurn(tool_calls=frozenset({("get_status", args_b)}))
        assert _turns_are_similar(turn_a, turn_b) is False

    def test_similar_text(self):
        text = "Here are your options:\n1. Option A\n2. Option B\nPlease select one."
        turn_a = ModelTurn(response_text=text)
        turn_b = ModelTurn(response_text=text)
        assert _turns_are_similar(turn_a, turn_b) is True

    def test_slightly_different_text_still_similar(self):
        text_a = "Here are your options:\n1. Option A (10kW)\n2. Option B (20kW)\nPlease select."
        text_b = "Here are your options:\n1. Option A (10kW)\n2. Option B (20kW)\nPlease choose."
        turn_a = ModelTurn(response_text=text_a)
        turn_b = ModelTurn(response_text=text_b)
        assert _turns_are_similar(turn_a, turn_b) is True

    def test_dissimilar_text(self):
        turn_a = ModelTurn(response_text="The meter status is online.")
        turn_b = ModelTurn(response_text="Your JIRA ticket has been created: PROJ-123")
        assert _turns_are_similar(turn_a, turn_b) is False

    def test_empty_turns_not_similar(self):
        turn_a = ModelTurn()
        turn_b = ModelTurn()
        assert _turns_are_similar(turn_a, turn_b) is False

    def test_same_tools_different_text_still_similar(self):
        """When both turns have tool calls, similarity is based on tools only."""
        args = _normalize_arguments({"org_id": 2, "grid": "ExampleGrid"})
        turn_a = ModelTurn(
            tool_calls=frozenset({("get_status", args)}),
            response_text="Grid ExampleGrid is online with 45kW output.",
        )
        turn_b = ModelTurn(
            tool_calls=frozenset({("get_status", args)}),
            response_text="Grid ExampleGrid status: online, producing 45kW.",
        )
        assert _turns_are_similar(turn_a, turn_b) is True

    def test_one_has_tools_other_has_text(self):
        args = _normalize_arguments({"id": 1})
        turn_a = ModelTurn(tool_calls=frozenset({("get_meter", args)}))
        turn_b = ModelTurn(response_text="Meter is online")
        assert _turns_are_similar(turn_a, turn_b) is False


# ---------------------------------------------------------------------------
# detect_cross_request_loop
# ---------------------------------------------------------------------------


def _build_repetitive_history(num_repeats: int = 3) -> list[ConversationMessage]:
    """Build a history where the model repeats the same tool call + response."""
    messages: list[ConversationMessage] = []
    for _ in range(num_repeats):
        messages.append(ConversationMessage(role="user", content="Option 1"))
        messages.append(
            ConversationMessage(
                role="model",
                function_call=FunctionCall(
                    name="customer_meter_information",
                    arguments={"organization_id": 2, "meter_name": "ABC-123"},
                ),
            ),
        )
        messages.append(
            ConversationMessage(
                role="tool",
                tool_result=ToolCallResult(
                    name="customer_meter_information",
                    success=True,
                    output={"meters": [{"id": 1}, {"id": 2}]},
                ),
            ),
        )
        messages.append(
            ConversationMessage(
                role="model",
                content="I found multiple meters. Please select:\n1. Meter A\n2. Meter B",
            ),
        )
    return messages


class TestDetectCrossRequestLoop:
    def test_no_loop_normal_conversation(self):
        history = [
            ConversationMessage(role="user", content="Hello"),
            ConversationMessage(role="model", content="Hi there!"),
            ConversationMessage(role="user", content="What's the grid status?"),
            ConversationMessage(role="model", content="Grid is online with 45kW output."),
        ]
        result = detect_cross_request_loop(history)
        assert result.hint is None
        assert result.should_escalate is False

    def test_text_loop_detected(self):
        history = [
            ConversationMessage(role="user", content="Option 1"),
            ConversationMessage(
                role="model",
                content="Please select an option:\n1. Option A\n2. Option B",
            ),
            ConversationMessage(role="user", content="I choose option 1"),
            ConversationMessage(
                role="model",
                content="Please select an option:\n1. Option A\n2. Option B",
            ),
        ]
        result = detect_cross_request_loop(history)
        assert result.hint == LOOP_HINT_MESSAGE
        assert result.should_escalate is False

    def test_tool_call_loop_detected(self):
        history = _build_repetitive_history(num_repeats=2)
        result = detect_cross_request_loop(history)
        assert result.hint == LOOP_HINT_MESSAGE
        assert result.should_escalate is False

    def test_threshold_3_requires_3_repeats(self):
        # 2 repeats should NOT trigger with threshold=3
        history = _build_repetitive_history(num_repeats=2)
        result = detect_cross_request_loop(history, threshold=3)
        assert result.hint is None

        # 3 repeats should trigger
        history = _build_repetitive_history(num_repeats=3)
        result = detect_cross_request_loop(history, threshold=3)
        assert result.hint == LOOP_HINT_MESSAGE

    def test_short_history_no_loop(self):
        history = [
            ConversationMessage(role="user", content="Hello"),
            ConversationMessage(role="model", content="Hi!"),
        ]
        result = detect_cross_request_loop(history)
        assert result.hint is None

    def test_empty_history(self):
        result = detect_cross_request_loop([])
        assert result.hint is None

    def test_disabled_via_env(self):
        history = _build_repetitive_history(num_repeats=3)
        with patch.dict(os.environ, {"LOOP_DETECTION_ENABLED": "false"}):
            result = detect_cross_request_loop(history)
            assert result.hint is None

    def test_exception_fail_open(self):
        """If an unexpected error occurs, returns empty result (fail-open)."""
        with patch(
            "orchestrator.services.loop_detector._extract_model_turns",
            side_effect=RuntimeError("boom"),
        ):
            result = detect_cross_request_loop([ConversationMessage(role="user", content="hi")])
            assert result.hint is None
            assert result.should_escalate is False

    def test_threshold_below_2_clamped(self):
        """Threshold < 2 is clamped to 2."""
        history = _build_repetitive_history(num_repeats=2)
        result = detect_cross_request_loop(history, threshold=1)
        assert result.hint == LOOP_HINT_MESSAGE

    def test_different_turns_no_false_positive(self):
        """Different tool calls across turns should not trigger."""
        history = [
            ConversationMessage(role="user", content="Check meter A"),
            ConversationMessage(
                role="model",
                function_call=FunctionCall(name="get_meter", arguments={"id": "A"}),
            ),
            ConversationMessage(
                role="tool",
                tool_result=ToolCallResult(name="get_meter", success=True, output={}),
            ),
            ConversationMessage(role="model", content="Meter A is online."),
            ConversationMessage(role="user", content="Check meter B"),
            ConversationMessage(
                role="model",
                function_call=FunctionCall(name="get_meter", arguments={"id": "B"}),
            ),
            ConversationMessage(
                role="tool",
                tool_result=ToolCallResult(name="get_meter", success=True, output={}),
            ),
            ConversationMessage(role="model", content="Meter B is offline."),
        ]
        result = detect_cross_request_loop(history)
        assert result.hint is None

    def test_escalation_after_persistent_loop(self):
        """Escalation triggers after threshold + ESCALATION_AFTER similar turns."""
        # Default threshold=2, ESCALATION_AFTER=2, so 4 repeats triggers escalation
        history = _build_repetitive_history(num_repeats=4)
        result = detect_cross_request_loop(history)
        assert result.hint == LOOP_HINT_MESSAGE
        assert result.should_escalate is True
        assert result.consecutive_similar_turns == 4

    def test_no_escalation_at_threshold(self):
        """At exactly threshold similar turns, hint fires but no escalation."""
        history = _build_repetitive_history(num_repeats=2)
        result = detect_cross_request_loop(history)
        assert result.hint == LOOP_HINT_MESSAGE
        assert result.should_escalate is False
        assert result.consecutive_similar_turns == 2

    def test_no_escalation_below_escalation_threshold(self):
        """3 similar turns (threshold=2): hint but no escalation yet."""
        history = _build_repetitive_history(num_repeats=3)
        result = detect_cross_request_loop(history)
        assert result.hint == LOOP_HINT_MESSAGE
        assert result.should_escalate is False
        assert result.consecutive_similar_turns == 3
