"""Update AppSheet design row with real cable distances from site layout.

After site layout computes actual PV combiner and feeder pillar distances,
this step updates the design in AppSheet and waits for recalculation.
"""

import asyncio
import json

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Time to wait for AppSheet to recalculate after updating distances
APPSHEET_RECALC_WAIT_SECONDS = 60


@register_step("update_design_distances")
async def update_design_distances(context: StepContext) -> StepResult:
    """Update the AppSheet design row with real distances from site layout.

    Reads avg_pv_combiner_distance_m and feeder_pillar_distance_m from
    generate_site_layout results and updates the design row. Then waits
    60s for AppSheet to recalculate energy specs with accurate distances.

    Requires:
    - design_id in state (from generate_powerplant_design)
    - Cable distances in state (from generate_site_layout)
    - mcp_executor available for tool calls
    """
    # Idempotency guard: distances already updated (handles recovery re-entry)
    if context.get_state("design_distances_updated"):
        LOGGER.info("update_design_distances: already done, skipping")
        return StepResult(
            data={"design_distances_updated": True, "design_id": context.get_state("design_id")},
            state_updates={},
            progress_message="Design distances already updated.",
        )

    design_id = context.get_state("design_id")
    if not design_id:
        return StepResult.failure("No design_id in state — run generate_powerplant_design first")

    if not context.mcp_executor:
        return StepResult.failure("MCP executor not available")

    # Get cable distances from state (written by generate_site_layout)
    avg_pv_combiner = context.get_state("avg_pv_combiner_distance_m")
    feeder_pillar = context.get_state("feeder_pillar_distance_m")

    if avg_pv_combiner is None and feeder_pillar is None:
        LOGGER.warning("No cable distances available — skipping design update")
        return StepResult(
            data={"skipped": True, "reason": "no_cable_distances"},
            progress_message="Skipped design update: no cable distances from site layout",
        )

    # Build AppSheet column updates
    updates = {}
    if avg_pv_combiner is not None:
        updates["Avg Distance to PV Combiner (m)"] = round(float(avg_pv_combiner), 1)
    if feeder_pillar is not None:
        updates["Distance to Feeder Pillar (m)"] = round(float(feeder_pillar), 1)

    site_name = context.get_input("site_name") or context.get_state("site_name") or "site"
    LOGGER.info(f"Updating design {design_id} for {site_name} with distances: {updates}")

    await context.send_progress_to_user(
        f"Updating design distances for {site_name}...\n"
        f"PV combiner: {avg_pv_combiner}m, Feeder pillar: {feeder_pillar}m\n"
        f"Waiting {APPSHEET_RECALC_WAIT_SECONDS}s for AppSheet to recalculate."
    )

    try:
        result_str = await context.mcp_executor.call_tool(
            "grid_design_update_design",
            {"design_id": design_id, "updates": updates},
        )

        try:
            result = json.loads(result_str) if isinstance(result_str, str) else result_str
        except json.JSONDecodeError as e:
            LOGGER.error(f"Failed to parse update_design response: {e}")
            return StepResult.failure("Design update returned invalid response")

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            LOGGER.error(f"update_design failed: {error}")
            return StepResult.failure(f"Failed to update design distances: {error}")

        # Wait for AppSheet to recalculate with new distances
        LOGGER.info(f"Waiting {APPSHEET_RECALC_WAIT_SECONDS}s for AppSheet recalculation...")
        await asyncio.sleep(APPSHEET_RECALC_WAIT_SECONDS)

        return StepResult(
            data={
                "design_id": design_id,
                "updates_applied": updates,
            },
            state_updates={
                "design_distances_updated": True,
            },
            progress_message=(
                f"Design updated with real distances — "
                f"PV combiner: {avg_pv_combiner}m, "
                f"feeder pillar: {feeder_pillar}m"
            ),
        )

    except Exception as e:
        LOGGER.exception(f"Error updating design distances: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))
