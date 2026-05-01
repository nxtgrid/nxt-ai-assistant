"""Fetch solar potential step handler for Light Preliminary Package.

This handler fetches solar generation potential (kWh/kWp) from Global Solar Atlas
for the site location determined by generate_distribution_map.
"""

import json
from typing import Any, Dict

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step("fetch_solar_potential")
async def fetch_solar_potential(context: StepContext) -> StepResult:
    """Fetch solar potential data from Global Solar Atlas via MCP tool.

    Uses the coordinates from generate_distribution_map step to query the solar
    potential API and returns daily/yearly kWh/kWp values.

    Requires:
    - generate_distribution_map must have run first (provides lat/lon coordinates)

    Args:
        context: Step execution context

    Returns:
        StepResult with solar potential data including:
        - daily_kwh_per_kwp: Average daily output
        - yearly_kwh_per_kwp: Annual output
        - optimal_tilt_deg: Optimal panel tilt angle
        - ghi_kwh_m2: Global Horizontal Irradiation
    """
    # Idempotency guard: solar potential already fetched (handles recovery re-entry)
    if context.get_state("solar_potential_fetched"):
        LOGGER.info("fetch_solar_potential: already done, skipping")
        return StepResult(
            data={
                "solar_potential_fetched": True,
                "daily_kwh_per_kwp": context.get_state("gsa_daily_potential"),
                "yearly_kwh_per_kwp": context.get_state("gsa_yearly_potential"),
            },
            state_updates={},
            progress_message="Solar potential already fetched.",
        )

    # Get coordinates from map generation step
    map_result = context.get_previous_result("generate_distribution_map")

    if not map_result:
        return StepResult.failure("No map data available - run generate_distribution_map first")

    center = map_result.get("center", {})
    lat = center.get("lat")
    lon = center.get("lon")

    if lat is None or lon is None:
        LOGGER.warning(f"No coordinates in map result center: {center}")
        return StepResult.failure("No coordinates available from map generation")

    LOGGER.info(f"Fetching solar potential for ({lat}, {lon})")

    # Send progress message before API call
    await context.send_progress_to_user(
        f"Fetching solar potential data for location ({lat:.4f}, {lon:.4f})..."
    )

    # Call MCP tool
    try:
        result_str = await context.mcp_executor.call_tool(
            "solar_get_solar_potential",
            {"latitude": lat, "longitude": lon},
        )

        # Check if the response is an error message (not JSON)
        if isinstance(result_str, str) and result_str.startswith("Error:"):
            LOGGER.error(f"Solar API returned error: {result_str}")
            # Extract user-friendly error message
            error_msg = result_str.replace("Error: ", "").replace(
                "Failed to fetch solar potential: ", ""
            )
            return StepResult.failure(f"Solar data unavailable: {error_msg}")

        result: Dict[str, Any] = (
            json.loads(result_str) if isinstance(result_str, str) else result_str
        )
    except json.JSONDecodeError as e:
        LOGGER.error(
            f"Failed to parse solar API response: {e}, raw response: {result_str[:200] if result_str else 'empty'}"
        )
        return StepResult.failure("Invalid response from solar API")
    except Exception as e:
        LOGGER.error(f"Solar API call failed: {e}")
        return StepResult.failure(f"Failed to fetch solar potential: {e}")

    # Check for success
    if not result.get("success"):
        error = result.get("error", "Unknown error")
        LOGGER.error(f"Solar API returned error: {error}")
        return StepResult.failure(f"Solar API error: {error}")

    # Extract key values
    daily_kwh = result.get("daily_kwh_per_kwp")
    yearly_kwh = result.get("yearly_kwh_per_kwp")
    optimal_tilt = result.get("optimal_tilt_deg")
    ghi = result.get("ghi_kwh_m2")

    if daily_kwh is None or yearly_kwh is None:
        LOGGER.error(f"Missing solar potential values in result: {result}")
        return StepResult.failure("Solar API returned incomplete data")

    LOGGER.info(
        f"Solar potential fetched: {daily_kwh} kWh/kWp/day, "
        f"{yearly_kwh} kWh/kWp/year, optimal tilt {optimal_tilt}°"
    )

    return StepResult(
        data={
            "daily_kwh_per_kwp": daily_kwh,
            "yearly_kwh_per_kwp": yearly_kwh,
            "optimal_tilt_deg": optimal_tilt,
            "ghi_kwh_m2": ghi,
            "gti_kwh_m2": result.get("gti_kwh_m2"),
            "dni_kwh_m2": result.get("dni_kwh_m2"),
            "avg_temp_c": result.get("avg_temp_c"),
            "elevation_m": result.get("elevation_m"),
            "location": {"lat": lat, "lon": lon},
        },
        state_updates={
            "solar_potential_fetched": True,
            "gsa_daily_potential": daily_kwh,
            "gsa_yearly_potential": yearly_kwh,
        },
        progress_message=f"Solar potential: {daily_kwh:.2f} kWh/kWp/day",
    )
