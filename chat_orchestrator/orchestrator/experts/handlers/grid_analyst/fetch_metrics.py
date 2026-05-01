"""Fetch metrics step handler for grid analysis.

This handler fetches metrics data from Grafana for grid analysis.
It collects battery, solar, and alert data for the specified time range.
"""

from typing import Any, Dict

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step("fetch_month_metrics")
async def fetch_month_metrics(context: StepContext) -> StepResult:
    """Fetch the last month of metrics from Grafana.

    Uses MCP grafana_query tool to get performance data for:
    - Battery state of charge
    - Solar/PV power
    - System alerts

    Args:
        context: Step execution context

    Returns:
        StepResult with fetched metrics data
    """
    # Extract inputs
    grid_ref = context.get_input("grid", {})
    grid_name = grid_ref.get("grid_name")
    time_range = context.get_input("time_range", {})

    if not grid_name:
        return StepResult.failure("No grid specified in packet inputs")

    if not context.mcp_executor:
        return StepResult.failure("MCP executor not available")

    LOGGER.info(f"Fetching metrics for grid: {grid_name}")

    results: Dict[str, Any] = {
        "battery_metrics": None,
        "solar_metrics": None,
        "alerts": None,
        "errors": [],
    }

    # Extract time range parameters
    start_date = time_range.get("start_date")
    end_date = time_range.get("end_date")

    # Fetch battery metrics
    try:
        battery_result = await context.mcp_executor.call_tool(
            "grafana_query",
            {
                "grid": grid_name,
                "metric": "battery_soc",
                "start": str(start_date) if start_date else None,
                "end": str(end_date) if end_date else None,
            },
        )
        results["battery_metrics"] = battery_result
    except Exception as e:
        LOGGER.warning(f"Failed to fetch battery metrics: {e}")
        results["errors"].append(f"battery_metrics: {str(e)}")

    # Fetch solar metrics
    try:
        solar_result = await context.mcp_executor.call_tool(
            "grafana_query",
            {
                "grid": grid_name,
                "metric": "pv_power",
                "start": str(start_date) if start_date else None,
                "end": str(end_date) if end_date else None,
            },
        )
        results["solar_metrics"] = solar_result
    except Exception as e:
        LOGGER.warning(f"Failed to fetch solar metrics: {e}")
        results["errors"].append(f"solar_metrics: {str(e)}")

    # Fetch alerts
    try:
        alerts_result = await context.mcp_executor.call_tool(
            "grafana_query",
            {
                "grid": grid_name,
                "metric": "alerts",
                "start": str(start_date) if start_date else None,
                "end": str(end_date) if end_date else None,
            },
        )
        results["alerts"] = alerts_result
    except Exception as e:
        LOGGER.warning(f"Failed to fetch alerts: {e}")
        results["errors"].append(f"alerts: {str(e)}")

    # Check if we got any data
    has_data = any([results["battery_metrics"], results["solar_metrics"]])

    if not has_data and results["errors"]:
        # All fetches failed
        return StepResult.failure(f"Failed to fetch metrics: {'; '.join(results['errors'])}")

    return StepResult(
        data={
            "battery_metrics": results["battery_metrics"],
            "solar_metrics": results["solar_metrics"],
            "alerts": results["alerts"],
            "fetch_errors": results["errors"],
            "grid_name": grid_name,
        },
        state_updates={
            "metrics_fetched": True,
            "alerts_fetched": results["alerts"] is not None,
        },
        progress_message=f"Fetched metrics for {grid_name}",
    )


@register_step("fetch_multi_grid_metrics")
async def fetch_multi_grid_metrics(context: StepContext) -> StepResult:
    """Fetch metrics for multiple grids (for KPI reports).

    Args:
        context: Step execution context

    Returns:
        StepResult with metrics for all grids
    """
    grids = context.get_input("grids", [])
    time_range = context.get_input("time_range", {})

    # If grids not parsed, try to extract from raw_request
    if not grids:
        raw_request = context.get_input("raw_request", "")
        grids = _extract_grids_from_request(raw_request)
        LOGGER.info(f"Extracted grids from raw_request: {grids}")

    # If time_range not parsed, try to extract from raw_request
    if not time_range:
        raw_request = context.get_input("raw_request", "")
        time_range = _extract_time_range_from_request(raw_request)
        LOGGER.info(f"Extracted time_range from raw_request: {time_range}")

    if not grids:
        return StepResult.failure("No grids specified in packet inputs")

    if not context.mcp_executor:
        return StepResult.failure("MCP executor not available")

    all_metrics = {}
    errors = []

    for grid_ref in grids:
        grid_name = grid_ref.get("grid_name")
        if not grid_name:
            continue

        LOGGER.info(f"Fetching metrics for grid: {grid_name}")

        try:
            result = await context.mcp_executor.call_tool(
                "grafana_query",
                {
                    "grid": grid_name,
                    "metric": "summary",
                    "start": str(time_range.get("start_date")),
                    "end": str(time_range.get("end_date")),
                },
            )
            all_metrics[grid_name] = result
        except Exception as e:
            LOGGER.warning(f"Failed to fetch metrics for {grid_name}: {e}")
            errors.append(f"{grid_name}: {str(e)}")

    if not all_metrics:
        return StepResult.failure(f"Failed to fetch metrics for any grid: {errors}")

    return StepResult(
        data={
            "grid_metrics": all_metrics,
            "grids_fetched": list(all_metrics.keys()),
            "fetch_errors": errors,
        },
        state_updates={
            "grids_processed": list(all_metrics.keys()),
        },
        progress_message=f"Fetched metrics for {len(all_metrics)} grids",
    )


def _extract_grids_from_request(raw_request: str) -> list:
    """Extract grid names from a raw request string.

    Handles formats like:
    - "/report monthly ExampleGrid"
    - "/report weekly grids GridA, GridB"
    - "/kpi ExampleGrid last 7 days"

    Args:
        raw_request: The raw user request string

    Returns:
        List of grid reference dicts: [{"grid_name": "ExampleGrid"}, ...]
    """
    import re

    if not raw_request:
        return []

    # Remove the command prefix
    text = re.sub(r"^/\w+\s*", "", raw_request, flags=re.IGNORECASE)

    # Remove common time phrases
    time_phrases = [
        r"\b(?:last|past|previous)\s+\d+\s+(?:days?|weeks?|months?)\b",
        r"\b(?:daily|weekly|monthly|yearly)\b",
        r"\b(?:this|last)\s+(?:week|month|year)\b",
        r"\b(?:today|yesterday)\b",
        r"\bfor\s+\w+\b",  # "for January"
    ]
    for phrase in time_phrases:
        text = re.sub(phrase, "", text, flags=re.IGNORECASE)

    # Remove keywords
    keywords = ["grid", "grids", "site", "sites", "report", "kpi", "analyze", "analyse"]
    for kw in keywords:
        text = re.sub(rf"\b{kw}\b", "", text, flags=re.IGNORECASE)

    # Clean up and split
    text = text.strip()
    text = re.sub(r"\s+", " ", text)  # Normalize spaces

    # Split by comma or "and"
    parts = re.split(r"[,]|\band\b", text)
    grids = []

    for part in parts:
        name = part.strip().strip(".,!?\"'")
        if name and len(name) > 1:  # Skip single chars
            grids.append({"grid_name": name})

    return grids


def _extract_time_range_from_request(raw_request: str) -> dict:
    """Extract time range from a raw request string.

    Handles formats like:
    - "last 7 days"
    - "monthly"
    - "last month"
    - "weekly"

    Args:
        raw_request: The raw user request string

    Returns:
        Time range dict with start_date and end_date
    """
    import re
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    text = raw_request.lower()

    # Check for "last N days/weeks/months"
    match = re.search(r"last\s+(\d+)\s+(days?|weeks?|months?)", text)
    if match:
        num = int(match.group(1))
        unit = match.group(2)
        if "day" in unit:
            delta = timedelta(days=num)
        elif "week" in unit:
            delta = timedelta(weeks=num)
        else:  # month
            delta = timedelta(days=num * 30)
        return {
            "start_date": (now - delta).isoformat(),
            "end_date": now.isoformat(),
            "timezone": "UTC",
        }

    # Check for period keywords
    if "daily" in text or "today" in text:
        return {
            "start_date": now.replace(hour=0, minute=0, second=0).isoformat(),
            "end_date": now.isoformat(),
            "timezone": "UTC",
        }
    if "weekly" in text or "this week" in text:
        start = now - timedelta(days=7)
        return {
            "start_date": start.isoformat(),
            "end_date": now.isoformat(),
            "timezone": "UTC",
        }
    if "monthly" in text or "this month" in text or "last month" in text:
        start = now - timedelta(days=30)
        return {
            "start_date": start.isoformat(),
            "end_date": now.isoformat(),
            "timezone": "UTC",
        }

    # Default to last 30 days
    return {
        "start_date": (now - timedelta(days=30)).isoformat(),
        "end_date": now.isoformat(),
        "timezone": "UTC",
    }
