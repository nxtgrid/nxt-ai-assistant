"""Generate design step handler for Light Preliminary Package.

This handler calls the grid_design MCP server to create a design in AppSheet
(without BOM). BOM is triggered separately after site layout provides real
cable distances.
"""

import json
from datetime import datetime, timezone

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


def _get_requester_name(context: StepContext) -> str:
    """Extract requester name from email or return default.

    Uses the part before @ from the email address.
    """
    email: str | None = context.effective_email
    if email and "@" in email:
        # Extract name part and capitalize
        name_part = email.split("@")[0]
        # Handle firstname.lastname format
        if "." in name_part:
            parts = name_part.split(".")
            return " ".join(p.capitalize() for p in parts)
        return name_part.capitalize()
    return "Staff"


@register_step("generate_powerplant_design")
async def generate_powerplant_design(context: StepContext) -> StepResult:
    """Generate design and BOM in AppSheet using site submission data.

    Maps pd_site_submissions fields to design_and_bom inputs:
    - grid_name <- site_name
    - design_name <- "{site_name} LPP {datetime} by {requester}"
    - max_connections <- served_building_count
    - residential/business split <- let AppSheet calculate 90/10 default

    Requires:
    - site_id and site_name in state (from generate_distribution_map)
    - mcp_executor available for tool calls
    """
    # Idempotency guard: design already created (handles recovery re-entry)
    if context.get_state("design_generated"):
        design_id = context.get_state("design_id")
        LOGGER.info(f"generate_powerplant_design: already done (design_id={design_id}), skipping")
        return StepResult(
            data={"design_id": design_id, "design_generated": True},
            state_updates={},
            progress_message="Design already created.",
        )

    # Use get_parameter_value() to respect user overrides from confirmation flow
    site_id = context.get_parameter_value("site_id") or context.get_state("site_id")
    site_name = context.get_parameter_value("site_name") or context.get_state("site_name")

    if not site_id or not site_name:
        return StepResult.failure("No site data in state - run generate_distribution_map first")

    if not context.mcp_executor:
        return StepResult.failure("MCP executor not available")

    # Get statistics from previous step (generate_distribution_map stores these)
    prev_result = context.get_previous_result("generate_distribution_map")
    statistics = prev_result.get("statistics", {}) if prev_result else {}

    # Debug: Log what we received from generate_distribution_map
    LOGGER.info(
        f"generate_powerplant_design received from generate_distribution_map: "
        f"prev_result keys={list(prev_result.keys()) if prev_result else 'None'}, "
        f"statistics={statistics}"
    )
    LOGGER.debug(f"Full accumulated_results keys: {list(context.accumulated_results.keys())}")

    # served_buildings = total connections (buildings within pole coverage)
    served_buildings = statistics.get("served_buildings", 0)
    default_max_connections = served_buildings

    # Check for user override of max_connections
    max_connections = context.get_parameter_value("max_connections")
    if max_connections is None:
        max_connections = default_max_connections
    else:
        LOGGER.info(
            f"Using user-specified max_connections: {max_connections} (default was {default_max_connections})"
        )

    if served_buildings == 0:
        LOGGER.error(
            f"No served buildings! prev_result={prev_result}, "
            f"accumulated_results={context.accumulated_results}"
        )
        return StepResult.failure(f"No served buildings found for site '{site_name}'")

    # Check for user-specified energy targets (from parameter confirmation)
    target_kwp = context.get_parameter_value("editable_total_kwp")
    target_kwh = context.get_parameter_value("editable_total_kwh")

    # Build design_and_bom arguments
    # Don't pass residential/business - let AppSheet use 90/10 default split
    # Create design name with date/time and requester for tracking
    requester_name = _get_requester_name(context)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    design_name = f"{site_name} LPP {timestamp} by {requester_name}"

    design_args = {
        "grid_name": site_name,
        "design_name": design_name,
        "max_connections": max_connections,
        "wait_for_completion": True,
    }

    # Only pass targets if user specified them (non-empty)
    if target_kwp:
        design_args["target_kwp"] = float(target_kwp)
    if target_kwh:
        design_args["target_kwh"] = float(target_kwh)

    # Calculate average service drop length from layout statistics
    drop_cable_total_m = statistics.get("drop_cable_length_m", 0)
    if drop_cable_total_m and served_buildings > 0:
        design_args["avg_service_drop_length_m"] = drop_cable_total_m / served_buildings

    LOGGER.info(
        f"Calling design_and_bom for {site_name}: {max_connections} connections "
        f"({served_buildings} served), design: {design_name}"
    )

    # Skip BOM — it will be triggered separately after site layout
    # provides real cable distances (feeder pillar, PV combiner)
    design_args["wait_for_bom"] = False

    # Send immediate progress message to user before long operation
    await context.send_progress_to_user(
        f"⏳ Generating design for {site_name}...\nThis may take a while."
    )

    try:
        result_str = await context.mcp_executor.call_tool(
            "grid_design_design_and_bom",  # server_toolname format
            design_args,
        )

        # call_tool returns JSON string from MCP server - parse it
        try:
            result = json.loads(result_str) if isinstance(result_str, str) else result_str
        except json.JSONDecodeError as e:
            LOGGER.error(f"Failed to parse design_and_bom response: {e}")
            return StepResult.failure("Design generation returned invalid response")

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            LOGGER.error(f"design_and_bom failed: {error}")
            return StepResult.failure(f"Design generation failed: {error}")

        # Extract key outputs
        design_data = result.get("design", {})
        cost_summary = result.get("cost_summary", {})
        energy_specs = result.get("energy_specs", {})

        # Extract output data which contains design_parameters and energy_specs
        output_data = result.get("output", {})
        if not energy_specs and output_data:
            energy_specs = output_data.get("energy_specs", {})

        # Extract design parameters (equipment types, constraints, etc.)
        design_parameters = output_data.get("design_parameters", {})

        # Log what we received for debugging
        LOGGER.info(
            f"Design result for {site_name}: "
            f"energy_specs={energy_specs}, cost_summary keys={list(cost_summary.keys())}"
        )

        # Build state updates — design only, no BOM yet
        design_id = design_data.get("Id")
        state_updates = {
            "design_generated": True,
            "design_id": design_id,
            "design_name": design_data.get("Name"),
            # Energy specs from autopopulated design
            "total_kwp": energy_specs.get("total_kwp", ""),
            "total_kwh": energy_specs.get("total_kwh", ""),
            "total_kva": energy_specs.get("total_kva", ""),
            "num_subsystems": energy_specs.get("num_subsystems", ""),
            "num_inverters": energy_specs.get("num_inverters", ""),
            "num_batteries": energy_specs.get("num_batteries", ""),
            "num_panels": energy_specs.get("num_panels", ""),
            # Editable parameters for confirmation flow
            "editable_total_kwp": energy_specs.get("total_kwp", ""),
            "editable_total_kwh": energy_specs.get("total_kwh", ""),
        }

        return StepResult(
            data={
                "design_id": design_id,
                "design_name": design_data.get("Name"),
                "energy_specs": energy_specs,
                "design_parameters": design_parameters,
            },
            state_updates=state_updates,
            progress_message=(
                f"Design created for {site_name}: "
                f"{energy_specs.get('total_kwp', '?')} kWp, "
                f"{energy_specs.get('total_kwh', '?')} kWh "
                f"(BOM will be generated after site layout)"
            ),
        )

    except Exception as e:
        LOGGER.exception(f"Error calling design_and_bom: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))
