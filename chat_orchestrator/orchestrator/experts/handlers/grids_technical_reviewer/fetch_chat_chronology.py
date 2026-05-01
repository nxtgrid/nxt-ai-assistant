"""Fetch chat chronology for grids under review (previous month).

Calls the customer_get_grid_chat_chronology MCP tool to gather
customer/staff communications about each grid during the review period.
The result is stored in packet state as markdown, then injected into the
GTR analysis conversation as additional context.
"""

import json
from datetime import datetime
from typing import Any, Dict

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


def _get_previous_month_days_back() -> int:
    """Calculate days_back to cover the entire previous month from today."""
    today = datetime.utcnow()
    # First day of current month
    first_of_current = today.replace(day=1)
    # First day of previous month
    if first_of_current.month == 1:
        first_of_prev = first_of_current.replace(year=first_of_current.year - 1, month=12)
    else:
        first_of_prev = first_of_current.replace(month=first_of_current.month - 1)
    return (today - first_of_prev).days


@register_step("fetch_chat_chronology")
async def fetch_chat_chronology(context: StepContext) -> StepResult:
    """Fetch chat messages related to the grids being reviewed for the previous month."""
    grids = context.get_state("resolved_grids") or []
    if not grids:
        return StepResult(
            data={"skipped": True},
            progress_message="No grids resolved — skipping chat chronology.",
        )

    executor = context.mcp_executor
    if not executor:
        LOGGER.warning("No MCP executor available — skipping chat chronology")
        return StepResult(data={"skipped": True})

    org_id = context.organization_id or context.packet_organization_id
    days_back = _get_previous_month_days_back()
    chronology_parts: list[str] = []

    for grid in grids:
        grid_name = grid.get("name", "")
        if not grid_name:
            continue

        try:
            result = await executor.execute_tool(
                server_name="customer",
                tool_name="customer_get_grid_chat_chronology",
                arguments={
                    "grid_name": grid_name,
                    "days_back": days_back,
                    "organization_id": org_id,
                },
            )

            # Parse the tool result
            if not result or not result.success:
                LOGGER.info(f"No chat chronology for {grid_name}")
                continue

            data: Dict[str, Any] = {}
            for item in result.content or []:
                if hasattr(item, "text") and item.text:
                    try:
                        data = json.loads(item.text)
                    except (json.JSONDecodeError, TypeError):
                        pass

            timeline = data.get("timeline", [])
            if not timeline:
                continue

            # Format as markdown
            lines = [f"## Recent Communications: {grid_name} (last 7 days)"]
            lines.append(f"Organization: {data.get('organization', 'Unknown')}")
            lines.append(f"Messages: {data.get('message_count', 0)}")
            lines.append("")

            for msg in timeline[:50]:  # Cap at 50 messages per grid
                ts = msg.get("timestamp", "")[:16].replace("T", " ")
                source = msg.get("source", "")
                role = msg.get("role", "")
                content = msg.get("content", "")[:300]
                role_label = "Bot" if role == "model" else source
                lines.append(f"- **[{ts}] {role_label}:** {content}")

            chronology_parts.append("\n".join(lines))

        except Exception as e:
            LOGGER.warning(f"Failed to fetch chat chronology for {grid_name}: {e}")

    chronology_md = "\n\n".join(chronology_parts) if chronology_parts else ""

    return StepResult(
        data={"chat_chronology_md": chronology_md},
        state_updates={"chat_chronology_md": chronology_md},
        progress_message=(
            f"Loaded chat chronology ({len(chronology_parts)} grid(s))."
            if chronology_parts
            else "No recent chat activity found for these grids."
        ),
    )
