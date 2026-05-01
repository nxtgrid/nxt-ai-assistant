"""Fetch CUF sub-values step handler for GTR Expert.

This handler fetches CUF loss breakdown sub-values from the Capacity
Utilization Analysis dashboard for generating meaningful commentary.

Many Grafana panels return multiple metrics (e.g., "Unutilized Solar
Potential" returns Total, Curtailment, and Uncurtailed loss). Each
sub-value config specifies a metric_name_contains filter to pick the
correct sub-metric from multi-metric panels.
"""

from typing import Any, Dict, Optional

from orchestrator.experts.handlers.grids_technical_reviewer.fetch_grafana_kpis import (
    get_previous_month_time_range,
)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Sub-value definitions from CUF dashboard (cfd9da90-2bcd-4fdb-b319-be66ffe3708b)
# Each entry maps a value tag (used in expert instructions) to:
#   - tool: the Grafana tool to call
#   - display_name: human-readable name for logging
#   - metric_name_contains: substring to match in metric name (for multi-metric panels)
#     If None, takes the first metric (single-metric panels)
SUB_VALUE_CONFIGS = {
    "uncurtailed_loss": {
        "tool": "grafana_unutilized_solar_potential",
        "display_name": "Uncurtailed Loss",
        "metric_name_contains": "uncurtail",
    },
    "battery_usage": {
        "tool": "grafana_battery_usage",
        "display_name": "Battery Usage",
        "metric_name_contains": None,
    },
    "unknown_loss_wrt_sold": {
        "tool": "grafana_unknown_distr_loss",
        "display_name": "Unknown Loss wrt Sold",
        "metric_name_contains": "sold",
    },
    "self_consumption_pct": {
        "tool": "grafana_metercount_meters_for_hoursintimeperiod_h_hps_self_consumption_estimate",
        "display_name": "Self-Consumption %Loss",
        "metric_name_contains": "loss",
    },
    "power_efficiency": {
        "tool": "grafana_power_plant_efficiency",
        "display_name": "Power Plant Efficiency",
        "metric_name_contains": None,
    },
    "kwh_per_kwp": {
        "tool": "grafana_kwh_kwp",
        "display_name": "kWh per kWp",
        # First metric is "during period" which is what we want
        "metric_name_contains": None,
    },
}


def extract_metric_value(
    result: Dict[str, Any],
    metric_name_contains: Optional[str] = None,
) -> Optional[float]:
    """Extract a metric value from a Grafana tool result.

    Prefers display_value over raw value to handle unit conversions
    (e.g., percentunit where raw 0.57 should display as 57%).

    When metric_name_contains is specified, searches all metrics for one
    whose name contains the substring (case-insensitive). This handles
    multi-metric panels like "Unutilized Solar Potential" which returns
    Total, Curtailment, and Uncurtailed loss as separate metrics.

    Args:
        result: Result from Grafana tool call
        metric_name_contains: Optional substring to match in metric name.
            If None, returns the first metric.

    Returns:
        Extracted value or None if not found
    """
    if not result.get("success"):
        return None

    data = result.get("data", {})
    metrics = []

    if data.get("type") == "single_metric":
        metrics = data.get("metrics", [])
    elif isinstance(data, dict):
        metrics = data.get("metrics", [])

    if not metrics:
        metrics = result.get("metrics", [])

    if not metrics:
        return None

    # If a name filter is specified, find the matching metric
    if metric_name_contains:
        search = metric_name_contains.lower()
        for m in metrics:
            name = str(m.get("name", "")).lower()
            if search in name:
                display_value = m.get("display_value")
                if display_value is not None:
                    return float(display_value)
                value = m.get("value")
                if value is not None:
                    return float(value)

        # Log available metric names to help debug mismatches
        available = [m.get("name", "?") for m in metrics]
        LOGGER.warning(
            f"No metric matching '{metric_name_contains}' found. Available metrics: {available}"
        )
        return None

    # No filter — return first metric, prefer display_value
    display_value = metrics[0].get("display_value")
    if display_value is not None:
        return float(display_value)
    value = metrics[0].get("value")
    return float(value) if value is not None else None


@register_step("fetch_cuf_sub_values")
async def fetch_cuf_sub_values(context: StepContext) -> StepResult:
    """Fetch CUF loss breakdown sub-values for commentary analysis.

    These sub-values help explain WHY the CUF is what it is:
    - Uncurtailed loss (non-curtailment production loss from soiling/shading)
    - Battery usage (% of capacity utilized)
    - Unknown loss wrt sold (unexplained distribution losses vs sold energy)
    - Self-consumption %loss (meter standby overhead as % of total)
    - Power plant efficiency (overall conversion efficiency)
    - kWh per kWp (generation potential indicator)

    Sub-values are optional - workflow continues even if some fail.

    In chat mode, this step is skipped as we're discussing existing review data.

    Args:
        context: Step execution context

    Returns:
        StepResult with sub-values per grid (partial data OK)
    """
    # Skip in chat mode - we're discussing existing review, not generating new data
    if context.get_state("chat_mode", False):
        LOGGER.debug("Chat mode - skipping fetch_cuf_sub_values")
        return StepResult(
            data={"skipped": True},
            progress_message="Skipped (chat mode)",
        )

    grids_to_review = context.get_state("grids_to_review", [])

    if not grids_to_review:
        return StepResult.failure("No grids to review. Run resolve_grid_sheets first.")

    mcp_executor = context.mcp_executor
    if not mcp_executor:
        LOGGER.warning("MCP executor not available - skipping sub-value fetch")
        return StepResult(
            data={"cuf_sub_values": {}},
            state_updates={"cuf_sub_values": {}},
            progress_message="Skipped CUF sub-values (no executor available)",
        )

    # Use same time range as main KPIs (explicit ISO dates)
    import asyncio
    from typing import Tuple

    time_range = get_previous_month_time_range()

    sub_values: Dict[str, Dict[str, Any]] = {}
    total_fetched = 0
    total_failed = 0

    async def _fetch_cuf_for_grid(
        grid: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any], int, int]:
        """Fetch CUF sub-values for a single grid. Returns isolated results."""
        grid_name = grid["name"]
        grid_lower = grid_name.lower()
        grid_sub_values: Dict[str, Any] = {}
        fetched = 0
        failed = 0

        LOGGER.info(f"Fetching CUF sub-values for grid: {grid_name}")

        for metric_key, config in SUB_VALUE_CONFIGS.items():
            tool_name = config["tool"]
            display_name = config["display_name"]
            name_filter = config["metric_name_contains"]

            try:
                result = await mcp_executor.call_tool(
                    tool_name,
                    {
                        "Grid": grid_name,
                        "time_from": time_range["from"],
                        "time_to": time_range["to"],
                    },
                )

                if isinstance(result, dict) and result.get("success"):
                    data = result.get("data", {})
                    all_metrics = data.get("metrics", []) if isinstance(data, dict) else []
                    if all_metrics:
                        metric_names = [
                            f"'{m.get('name')}' = {m.get('display_value', m.get('value'))}"
                            for m in all_metrics
                        ]
                        LOGGER.info(f"  {metric_key}: panel returned {metric_names}")

                    value = extract_metric_value(result, name_filter)
                    if value is not None:
                        grid_sub_values[metric_key] = {
                            "value": value,
                            "display_name": display_name,
                        }
                        fetched += 1
                        LOGGER.info(f"  {metric_key}: {value}")
                    else:
                        failed += 1
                        LOGGER.warning(f"  {metric_key}: no matching metric found")
                else:
                    failed += 1

            except Exception as e:
                LOGGER.warning(f"  {metric_key} failed for {grid_name}: {e}")
                failed += 1

        return grid_lower, grid_sub_values, fetched, failed

    # Run all grids in parallel
    grid_tasks = [_fetch_cuf_for_grid(grid) for grid in grids_to_review]
    grid_results = await asyncio.gather(*grid_tasks, return_exceptions=True)

    for result_or_exc in grid_results:
        if isinstance(result_or_exc, BaseException):
            LOGGER.error(f"CUF sub-value fetch failed with exception: {result_or_exc}")
            continue
        grid_lower, grid_sub_values, fetched, failed = result_or_exc
        sub_values[grid_lower] = grid_sub_values
        total_fetched += fetched
        total_failed += failed

    # Build summary message
    if total_fetched > 0:
        message = f"Fetched {total_fetched} CUF sub-values"
        if total_failed > 0:
            message += f" ({total_failed} unavailable)"
    else:
        message = "CUF sub-values unavailable - commentary will use main KPIs only"

    return StepResult(
        data={"cuf_sub_values": sub_values},
        state_updates={"cuf_sub_values": sub_values},
        progress_message=message,
    )
