"""User-safe recovery helpers for bounded orchestration runs."""

from __future__ import annotations

from enum import Enum


class ExecutionLimitReason(str, Enum):
    """Reasons a turn stopped before completing all requested work."""

    TOOL_BUDGET = "tool_budget"
    OUTPUT_LIMIT = "output_limit"
    RECURSION_FALLBACK = "recursion_fallback"


_REASON_TEXT = {
    ExecutionLimitReason.TOOL_BUDGET: (
        "This task reached its processing limit before all requested work could finish."
    ),
    ExecutionLimitReason.OUTPUT_LIMIT: (
        "This task reached the response-size limit before it could finish safely."
    ),
    ExecutionLimitReason.RECURSION_FALLBACK: (
        "This task stopped because the workflow reached its safety limit."
    ),
}


def graph_recursion_limit(max_tool_rounds: int) -> int:
    """Return enough graph steps for the soft tool budget to terminate first."""
    return max(50, 2 * max_tool_rounds + 30)


def format_execution_limit_response(
    reason: ExecutionLimitReason,
    summary: str | None,
) -> str:
    """Format a persisted continuation message without claiming unknown work."""
    completed = summary or "- No additional action was confirmed before processing stopped."
    return (
        f"⚠️ {_REASON_TEXT[reason]}\n\n"
        f"**Completed**\n{completed}\n\n"
        "**Remaining**\n"
        "- The remaining requested work was not completed in this run.\n\n"
        "Reply `continue` or repeat this request to continue from this summary."
    )
