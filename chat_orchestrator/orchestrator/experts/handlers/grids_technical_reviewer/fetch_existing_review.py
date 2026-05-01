"""Fetch existing review step handler for GTR Expert.

This handler reads existing review content from the sheet for chat mode.
Only runs when chat_mode=True is set by check_existing_review.
"""

from typing import Any, Dict

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


def format_review_for_chat(review_content: Dict[str, Any]) -> str:
    """Format review content as readable text for LLM context.

    Args:
        review_content: Dict with kpis, pending_issues, month_label

    Returns:
        Formatted string for LLM context
    """
    lines = []
    month_label = review_content.get("month_label", "Review")

    lines.append(f"## {month_label} Review Summary\n")

    # Format KPIs
    kpis = review_content.get("kpis", {})
    if kpis:
        lines.append("### KPI Values and Commentary\n")
        for kpi_name, kpi_data in kpis.items():
            value = kpi_data.get("value", "N/A")
            commentary = kpi_data.get("commentary", "")
            lines.append(f"**{kpi_name}**: {value}")
            if commentary:
                lines.append(f"  - Commentary: {commentary}")
        lines.append("")

    # Format pending issues
    pending = review_content.get("pending_issues", [])
    if pending:
        lines.append("### Pending Issues\n")
        for i, issue in enumerate(pending, 1):
            lines.append(f"{i}. {issue}")
        lines.append("")

    return "\n".join(lines)


@register_step("fetch_existing_review")
async def fetch_existing_review(context: StepContext) -> StepResult:
    """Fetch and format existing review content for chat mode.

    This step only runs in chat mode. It takes the review content
    already loaded by check_existing_review and formats it for
    the LLM context.

    In non-chat mode (normal generation flow), this step is skipped
    as it has no work to do.

    Args:
        context: Step execution context

    Returns:
        StepResult with formatted review content for chat
    """
    chat_mode = context.get_state("chat_mode", False)

    # Skip if not in chat mode
    if not chat_mode:
        LOGGER.debug("Not in chat mode - skipping fetch_existing_review")
        return StepResult(
            data={"skipped": True},
            progress_message="Skipped (not in chat mode)",
        )

    # Get the review content loaded by check_existing_review
    existing_review_content = context.get_state("existing_review_content", {})
    month_label = context.get_state("month_label", "")

    if not existing_review_content:
        return StepResult.failure(
            "No existing review content found. Chat mode requires existing review data."
        )

    # Format review content for each grid
    formatted_reviews: Dict[str, str] = {}
    combined_context = []

    for grid_name, content in existing_review_content.items():
        formatted = format_review_for_chat(content)
        formatted_reviews[grid_name] = formatted
        combined_context.append(f"# {grid_name}\n\n{formatted}")

    # Create combined chat context for the LLM
    full_context = "\n---\n\n".join(combined_context)

    LOGGER.info(f"Formatted {len(formatted_reviews)} existing review(s) for chat mode")

    return StepResult(
        data={
            "formatted_reviews": formatted_reviews,
            "chat_context": full_context,
        },
        state_updates={
            "formatted_reviews": formatted_reviews,
            "chat_context": full_context,
        },
        progress_message=f"Loaded {month_label} review content for chat",
    )
