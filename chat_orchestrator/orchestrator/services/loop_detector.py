"""Cross-request loop detector for conversation history.

Detects when the bot's recent responses repeat the same pattern across
multiple user requests. This catches loops where each individual request
stays within max_tool_rounds but the sequence across requests is repetitive
(e.g., user says "Option 1", bot re-calls the same tool and shows the same options).

Pure-function module with no LLM calls or external dependencies.
Gated behind LOOP_DETECTION_ENABLED env var (default: true).
"""

from __future__ import annotations

import difflib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field

from orchestrator.models.schemas import ConversationMessage
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_THRESHOLD = 2
ESCALATION_AFTER = 2  # Escalate after this many additional similar turns beyond threshold
TEXT_SIMILARITY_RATIO = 0.85

LOOP_HINT_MESSAGE = (
    "[SYSTEM NOTICE — REPETITION DETECTED]\n"
    "Your last several responses have been very similar. "
    "The user may be trying to make a selection or answer a question you asked. "
    "Please read the user's latest message carefully as a direct reply to your "
    "previous message. Do NOT repeat the same tool calls or present the same "
    "options again. Instead, interpret the user's input, act on it, and move "
    "the conversation forward. If you truly cannot understand the user's intent, "
    "ask a clarifying question in different words."
)


@dataclass(frozen=True)
class LoopDetectionResult:
    """Result of cross-request loop detection."""

    hint: str | None = None
    should_escalate: bool = False
    consecutive_similar_turns: int = 0


@dataclass(frozen=True)
class ModelTurn:
    """A model turn: the set of tool calls and text response between two user messages."""

    tool_calls: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    response_text: str = ""


def _normalize_arguments(args: object) -> str:
    """Produce a stable JSON string from tool call arguments for comparison.

    Handles nested dicts by sorting keys recursively. Non-serializable values
    fall back to their repr().
    """
    try:
        return json.dumps(args, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return repr(args)


def _extract_model_turns(history: Sequence[ConversationMessage]) -> list[ModelTurn]:
    """Group consecutive model/tool messages into turns, delimited by user messages.

    A "model turn" is everything the model produces (tool calls + text response)
    between two consecutive user messages.
    """
    turns: list[ModelTurn] = []
    current_tool_calls: set[tuple[str, str]] = set()
    current_text: str = ""

    for msg in history:
        if msg.role == "user":
            # Flush any accumulated model turn
            if current_tool_calls or current_text:
                turns.append(
                    ModelTurn(
                        tool_calls=frozenset(current_tool_calls),
                        response_text=current_text,
                    )
                )
                current_tool_calls = set()
                current_text = ""
        elif msg.role == "model":
            if msg.function_call:
                normalized = _normalize_arguments(msg.function_call.arguments)
                current_tool_calls.add((msg.function_call.name, normalized))
            if msg.content:
                current_text = msg.content
        # tool results are part of the same turn but don't add new info for comparison

    # Flush final turn if any
    if current_tool_calls or current_text:
        turns.append(
            ModelTurn(
                tool_calls=frozenset(current_tool_calls),
                response_text=current_text,
            )
        )

    return turns


def _turns_are_similar(a: ModelTurn, b: ModelTurn) -> bool:
    """Check if two model turns are similar enough to be considered repetitive.

    Strategy:
    - If both turns have tool calls, they must be identical (name + args).
      When tool calls match, return True regardless of text.
      When tool calls differ, return False regardless of text.
    - If neither turn has tool calls, compare text similarity.
    - If only one has tool calls, they are structurally different.
    """
    # Empty turns are not considered similar
    if not a.tool_calls and not a.response_text and not b.tool_calls and not b.response_text:
        return False

    both_have_tools = bool(a.tool_calls) and bool(b.tool_calls)
    neither_has_tools = not a.tool_calls and not b.tool_calls

    # Both have tool calls: compare tool calls only
    if both_have_tools:
        return a.tool_calls == b.tool_calls

    # Neither has tool calls: compare text similarity
    if neither_has_tools and a.response_text and b.response_text:
        ratio = difflib.SequenceMatcher(None, a.response_text, b.response_text).ratio()
        return ratio >= TEXT_SIMILARITY_RATIO

    # One has tools, the other doesn't: structurally different
    return False


def _count_consecutive_similar_turns(turns: list[ModelTurn]) -> int:
    """Count how many consecutive similar turns exist at the end of the list."""
    if len(turns) < 2:
        return 0

    reference = turns[-1]
    count = 1
    for i in range(len(turns) - 2, -1, -1):
        if _turns_are_similar(reference, turns[i]):
            count += 1
        else:
            break
    return count


def detect_cross_request_loop(
    conversation_history: Sequence[ConversationMessage],
    threshold: int | None = None,
) -> LoopDetectionResult:
    """Detect repetitive model behavior across consecutive requests.

    Examines the last N model turns in the conversation history. If the last
    `threshold` turns are all similar to each other, returns a result with
    the hint message. If the loop persists beyond threshold + ESCALATION_AFTER,
    also signals that escalation is needed.

    Args:
        conversation_history: Full conversation history (ConversationMessage list).
        threshold: Number of consecutive similar turns required to trigger.
            Defaults to LOOP_DETECTION_THRESHOLD env var or 2.

    Returns:
        LoopDetectionResult with hint, escalation flag, and turn count.
        Fail-open: returns empty result on any error.
    """
    try:
        # Kill switch
        if os.getenv("LOOP_DETECTION_ENABLED", "true").lower() != "true":
            return LoopDetectionResult()

        if threshold is None:
            threshold = int(os.getenv("LOOP_DETECTION_THRESHOLD", str(DEFAULT_THRESHOLD)))

        if threshold < 2:
            threshold = 2

        turns = _extract_model_turns(conversation_history)

        if len(turns) < threshold:
            return LoopDetectionResult()

        consecutive = _count_consecutive_similar_turns(turns)

        if consecutive < threshold:
            return LoopDetectionResult()

        # Loop detected — log details
        reference = turns[-1]
        tool_names = (
            sorted({name for name, _ in reference.tool_calls}) if reference.tool_calls else []
        )
        text_preview = reference.response_text[:100] if reference.response_text else ""

        should_escalate = consecutive >= threshold + ESCALATION_AFTER

        if should_escalate:
            LOGGER.warning(
                f"Cross-request loop persists after hint: {consecutive} consecutive similar turns. "
                f"Escalating. Tools: {tool_names}, text preview: {text_preview!r}"
            )
        else:
            LOGGER.warning(
                f"Cross-request loop detected: {consecutive} consecutive similar turns. "
                f"Tools: {tool_names}, text preview: {text_preview!r}"
            )

        return LoopDetectionResult(
            hint=LOOP_HINT_MESSAGE,
            should_escalate=should_escalate,
            consecutive_similar_turns=consecutive,
        )

    except Exception as e:
        LOGGER.warning(f"Loop detection error (fail-open): {e}")
        return LoopDetectionResult()


__all__ = [
    "detect_cross_request_loop",
    "LoopDetectionResult",
    "LOOP_HINT_MESSAGE",
]
