"""Generate BOM step handler for Light Preliminary Package.

Triggers BOM generation in AppSheet after the design has been updated with
real cable distances from site layout. Returns BOM items, cost summary,
and updated energy specs.
"""

import json

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step("generate_site_bom")
async def generate_site_bom(context: StepContext) -> StepResult:
    """Trigger BOM generation and fetch results.

    Calls the trigger_bom MCP tool which:
    1. Triggers BOM generation action in AppSheet
    2. Waits for completion
    3. Fetches design + BOM items
    4. Computes cost summary

    Requires:
    - design_id in state (from generate_powerplant_design)
    - mcp_executor available for tool calls
    """
    # Idempotency guard: BOM already generated (handles recovery re-entry)
    if context.get_state("bom_generated"):
        LOGGER.info("generate_site_bom: already done, skipping")
        return StepResult(
            data={
                "bom_generated": True,
                # Re-expose cached values so downstream steps (populate_lpp_cells) can
                # read them via get_previous_result() on recovery re-entry.
                "cost_summary": context.get_state("cost_summary") or {},
                "energy_specs": {
                    "total_kwp": context.get_state("total_kwp"),
                    "total_kwh": context.get_state("total_kwh"),
                    "total_kva": context.get_state("total_kva"),
                    "num_subsystems": context.get_state("num_subsystems"),
                    "num_inverters": context.get_state("num_inverters"),
                    "num_batteries": context.get_state("num_batteries"),
                    "num_panels": context.get_state("num_panels"),
                },
                "bom_items": [],
                "bom_item_count": 0,
            },
            state_updates={},
            progress_message="BOM already generated.",
        )

    design_id = context.get_state("design_id")
    if not design_id:
        return StepResult.failure("No design_id in state — run generate_powerplant_design first")

    if not context.mcp_executor:
        return StepResult.failure("MCP executor not available")

    site_name = context.get_input("site_name") or context.get_state("site_name") or "site"

    await context.send_progress_to_user(
        f"⏳ Generating BOM for {site_name}...\nThis may take a while."
    )

    try:
        result_str = await context.mcp_executor.call_tool(
            "grid_design_trigger_bom",
            {"design_id": design_id, "grid_name": site_name},
        )

        try:
            result = json.loads(result_str) if isinstance(result_str, str) else result_str
        except json.JSONDecodeError as e:
            LOGGER.error(f"Failed to parse trigger_bom response: {e}")
            return StepResult.failure("BOM generation returned invalid response")

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            LOGGER.error(f"trigger_bom failed: {error}")
            return StepResult.failure(f"BOM generation failed: {error}")

        # Extract results
        cost_summary = result.get("cost_summary", {})
        energy_specs = result.get("energy_specs", {})
        design_parameters = result.get("output", {}).get("design_parameters", {})
        bom_items = result.get("bom", [])

        LOGGER.info(
            f"BOM generated for {site_name}: "
            f"{len(bom_items)} items, "
            f"${cost_summary.get('total_cost', 0):,.2f} total"
        )

        # Update state with final energy specs (may have changed after distance update)
        state_updates = {
            "bom_generated": True,
            "cost_summary": cost_summary,
            "total_kwp": energy_specs.get("total_kwp", ""),
            "total_kwh": energy_specs.get("total_kwh", ""),
            "total_kva": energy_specs.get("total_kva", ""),
            "num_subsystems": energy_specs.get("num_subsystems", ""),
            "num_inverters": energy_specs.get("num_inverters", ""),
            "num_batteries": energy_specs.get("num_batteries", ""),
            "num_panels": energy_specs.get("num_panels", ""),
            # Update editable params with post-recalc values
            "editable_total_kwp": energy_specs.get("total_kwp", ""),
            "editable_total_kwh": energy_specs.get("total_kwh", ""),
        }

        return StepResult(
            data={
                "design_id": design_id,
                "cost_summary": cost_summary,
                "energy_specs": energy_specs,
                "design_parameters": design_parameters,
                "bom_item_count": len(bom_items),
                "bom_items": bom_items,
            },
            state_updates=state_updates,
            progress_message=(
                f"BOM generated for {site_name}: "
                f"{energy_specs.get('total_kwp', '?')} kWp, "
                f"{energy_specs.get('total_kwh', '?')} kWh, "
                f"${cost_summary.get('total_cost', 0):,.2f} total, "
                f"{len(bom_items)} items"
            ),
        )

    except Exception as e:
        LOGGER.exception(f"Error generating BOM: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))
