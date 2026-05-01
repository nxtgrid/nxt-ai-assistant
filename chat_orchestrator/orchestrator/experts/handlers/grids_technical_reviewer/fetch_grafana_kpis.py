"""Fetch Grafana KPIs step handler for GTR Expert.

This handler fetches main KPIs from the Grids KPI dashboard with
manual input fallback for panels that fail or are unavailable.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Conditional import for listing Grafana tools (for pattern matching)
try:
    from mcp_servers.server_registry import list_tools as registry_list_tools

    REGISTRY_AVAILABLE = True
except ImportError:
    REGISTRY_AVAILABLE = False
    registry_list_tools = None  # type: ignore[assignment, misc]

# KPI tool mapping from Grids KPI dashboard (df7gn304ulce8b)
# Use exact tool names, or "pattern:PREFIX" for dynamic panel titles
# Note: Pattern matching searches Grafana tools list (without "grafana_" prefix)
# but returns full tool name with "grafana_" prefix for calling
KPI_TOOL_MAPPING = {
    "service_uptime": "grafana_service_daily_uptime_gridname",  # Returns FS/HPS hours
    "financial_cuf": "grafana_financial_cuf_90d_gridname",
    "downtime_days": "grafana_grid_down_for_how_many_days",
    # Dynamic panel title - pattern matches without "grafana_" prefix
    # Panel title: "Issues (divide by X weeks)" -> tool: issues_divide_by_X_weeks
    "tickets_total": "pattern:issues",
}

# Cache for resolved tool patterns (cleared each run)
_resolved_tool_cache: Dict[str, Optional[str]] = {}


async def resolve_tool_pattern(pattern_prefix: str) -> Optional[str]:
    """Resolve a tool pattern to an actual tool name by listing available Grafana tools.

    Some Grafana panels have dynamic titles (e.g., "Issues (divide by 4.4 weeks)")
    which change the generated tool name. This function finds tools matching a prefix.

    Args:
        pattern_prefix: The prefix to match (e.g., "grafana_issues_divide_by_")

    Returns:
        The actual tool name if found, None otherwise
    """
    # Check cache first
    if pattern_prefix in _resolved_tool_cache:
        return _resolved_tool_cache[pattern_prefix]

    if not REGISTRY_AVAILABLE or not registry_list_tools:
        LOGGER.warning("Registry not available for pattern matching - mcp_servers not imported")
        _resolved_tool_cache[pattern_prefix] = None
        return None

    try:
        # List all Grafana tools
        grafana_tools = await registry_list_tools("grafana")
        LOGGER.info(f"Found {len(grafana_tools)} Grafana tools for pattern matching")

        # Log all tool names for debugging (at debug level to avoid noise)
        all_tool_names = [str(t.get("name", "")) for t in grafana_tools]
        LOGGER.debug(f"Available Grafana tools: {all_tool_names}")

        # Find tools matching the prefix
        matching_tools: List[str] = []
        for tool in grafana_tools:
            tool_name = str(tool.get("name", ""))
            if tool_name.startswith(pattern_prefix):
                matching_tools.append(tool_name)

        if matching_tools:
            # Use the first match (there should typically be only one)
            matched_name = matching_tools[0]
            # Add grafana_ prefix since tool calls use full name (grafana_toolname)
            resolved = (
                f"grafana_{matched_name}"
                if not matched_name.startswith("grafana_")
                else matched_name
            )
            if len(matching_tools) > 1:
                LOGGER.warning(
                    f"Multiple tools match pattern '{pattern_prefix}': {matching_tools}. Using: {resolved}"
                )
            else:
                LOGGER.info(f"Resolved pattern '{pattern_prefix}' to tool: {resolved}")
            _resolved_tool_cache[pattern_prefix] = resolved
            return resolved
        else:
            # Log tools containing "issues" to help diagnose naming issues
            issues_tools = [n for n in all_tool_names if "issues" in n.lower()]
            if issues_tools:
                LOGGER.warning(
                    f"No tools match pattern '{pattern_prefix}', but found issues-related tools: {issues_tools}"
                )
            else:
                LOGGER.warning(
                    f"No tools match pattern '{pattern_prefix}' and no issues-related tools found. "
                    f"Total Grafana tools: {len(grafana_tools)}"
                )
            _resolved_tool_cache[pattern_prefix] = None
            return None

    except Exception as e:
        LOGGER.error(f"Error resolving tool pattern '{pattern_prefix}': {e}", exc_info=True)
        _resolved_tool_cache[pattern_prefix] = None
        return None


async def get_tool_name(kpi_name: str, tool_spec: str) -> Optional[str]:
    """Get the actual tool name from a tool spec (exact name or pattern).

    Args:
        kpi_name: Name of the KPI (for logging)
        tool_spec: Either an exact tool name or "pattern:PREFIX"

    Returns:
        The resolved tool name, or None if pattern couldn't be resolved
    """
    if tool_spec.startswith("pattern:"):
        prefix = tool_spec[8:]  # Remove "pattern:" prefix
        resolved = await resolve_tool_pattern(prefix)
        if resolved:
            LOGGER.debug(f"KPI '{kpi_name}': resolved pattern to '{resolved}'")
        return resolved
    return tool_spec


# Human-readable KPI names for prompts
KPI_DISPLAY_NAMES = {
    "service_uptime": "FS/HPS Hours",
    "financial_cuf": "Financial CUF (90d)",
    "downtime_days": "Technical Downtime (days)",
    "tickets_total": "Total Tickets",
}

# Target values for KPI commentary (used by LLM for comparison)
KPI_TARGETS = {
    "fs_hours": {"value": 12.0, "unit": "hours", "direction": "above"},
    "hps_hours": {"value": 22.0, "unit": "hours", "direction": "above"},
    "financial_cuf": {"value": 55.0, "unit": "%", "direction": "above"},
    "downtime_days": {"value": 0, "unit": "days", "direction": "below"},
}

# KPIs where "No data" from Grafana means a known default value (not a failure).
# For example, downtime_days with no data means the grid had zero downtime days.
# tickets_total with no data means zero tickets.
KPI_NO_DATA_DEFAULTS: Dict[str, float] = {
    "downtime_days": 0.0,
    "tickets_total": 0.0,
}


def get_previous_month_time_range() -> Dict[str, str]:
    """Get Grafana time range for the previous calendar month.

    Returns:
        Dict with 'from' and 'to' keys for Grafana time range
    """
    now = datetime.now()

    # Get first day of current month
    if now.month == 1:
        prev_month = 12
        prev_year = now.year - 1
    else:
        prev_month = now.month - 1
        prev_year = now.year

    # Calculate start and end of previous month as ISO dates
    # Grafana needs explicit start/end dates for correct range
    first_day = datetime(prev_year, prev_month, 1)

    # Calculate last day of previous month
    if prev_month == 12:
        last_day = datetime(prev_year, 12, 31, 23, 59, 59)
    else:
        # Get first day of current review month, subtract 1 day
        next_month_first = datetime(
            prev_year if prev_month < 12 else prev_year + 1,
            prev_month + 1 if prev_month < 12 else 1,
            1,
        )
        last_day = next_month_first - timedelta(seconds=1)

    LOGGER.info(f"Previous month time range: {first_day.isoformat()} to {last_day.isoformat()}")

    return {
        "from": first_day.strftime("%Y-%m-%dT00:00:00Z"),
        "to": last_day.strftime("%Y-%m-%dT23:59:59Z"),
    }


def get_comparison_month_time_range() -> Dict[str, str]:
    """Get Grafana time range for 2 months ago (the month before the review month).

    This is the comparison month used for month-over-month commentary.

    Returns:
        Dict with 'from' and 'to' keys for Grafana time range
    """
    now = datetime.now()

    # Get 2 months back
    if now.month <= 2:
        comp_month = now.month + 10  # e.g., month 1 -> 11, month 2 -> 12
        comp_year = now.year - 1
    else:
        comp_month = now.month - 2
        comp_year = now.year

    first_day = datetime(comp_year, comp_month, 1)

    # Calculate last day of comparison month
    if comp_month == 12:
        last_day = datetime(comp_year, 12, 31, 23, 59, 59)
    else:
        next_month_first = datetime(
            comp_year if comp_month < 12 else comp_year + 1,
            comp_month + 1 if comp_month < 12 else 1,
            1,
        )
        last_day = next_month_first - timedelta(seconds=1)

    LOGGER.info(f"Comparison month time range: {first_day.isoformat()} to {last_day.isoformat()}")

    return {
        "from": first_day.strftime("%Y-%m-%dT00:00:00Z"),
        "to": last_day.strftime("%Y-%m-%dT23:59:59Z"),
    }


def get_comparison_month_label() -> str:
    """Get human-readable label for the comparison month (2 months ago).

    Returns:
        Label like "November 2025"
    """
    now = datetime.now()
    if now.month <= 2:
        comp_month = now.month + 10
        comp_year = now.year - 1
    else:
        comp_month = now.month - 2
        comp_year = now.year

    return datetime(comp_year, comp_month, 1).strftime("%B %Y")


def _build_commentary_context(
    kpi_data: Dict[str, Dict[str, Any]],
    prev_kpi_data: Dict[str, Dict[str, Any]],
    comparison_label: str,
    targets: Dict[str, Dict[str, Any]],
) -> str:
    """Build a compact commentary context string for the LLM.

    Includes target values, previous month data, and strict instructions
    to prevent hallucination.

    Args:
        kpi_data: Current month KPI data (cleaned, no raw_result)
        prev_kpi_data: Previous month KPI data (cleaned)
        comparison_label: Human-readable label for comparison month
        targets: KPI target definitions

    Returns:
        Formatted context string for inclusion in LLM prompt
    """
    lines = [
        "CRITICAL: ONLY use numbers from the kpi_data and cuf_sub_values above.",
        "Do NOT invent Revenue, ARPU, Battery Health, System Losses, or any other metrics not provided.",
        "",
        "ONLY generate commentary for these 4 KPIs: fs_hours, hps_hours, financial_cuf, downtime_days.",
        "You may reference cuf_sub_values (uncurtailed_loss, battery_usage, etc.) to EXPLAIN the financial_cuf value.",
        "",
        "Targets: FS Hours ≥12h, HPS Hours ≥22h, Financial CUF ≥55%, Downtime ≤0 days",
        "",
    ]

    # Add previous month data per grid
    if prev_kpi_data:
        lines.append(f"Previous Month ({comparison_label}):")
        for grid_key, grid_vals in prev_kpi_data.items():
            prev_fs = _extract_clean_value(grid_vals, "fs_hours")
            prev_hps = _extract_clean_value(grid_vals, "hps_hours")
            prev_cuf = _extract_clean_value(grid_vals, "financial_cuf")
            prev_dt = _extract_clean_value(grid_vals, "downtime_days")
            lines.append(
                f"  {grid_key}: FS={prev_fs}, HPS={prev_hps}, CUF={prev_cuf}, Downtime={prev_dt}"
            )
        lines.append("")

    lines.extend(
        [
            "Return ONLY a JSON block:",
            "```json",
            '{"kpi_commentary": {"GridName": {"fs_hours": "...", "hps_hours": "...", "financial_cuf": "...", "downtime_days": "..."}}}',
            "```",
            "",
            "For each KPI: state the value, compare to target, compare to previous month, explain likely causes.",
            "Do not add extra categories like 'Financials', 'System Health', or 'System Losses'.",
        ]
    )

    return "\n".join(lines)


def _extract_clean_value(grid_data: Dict[str, Any], key: str) -> str:
    """Extract a displayable value from cleaned KPI data.

    Args:
        grid_data: Grid KPI data dict
        key: KPI key to look for

    Returns:
        String representation of value or "N/A"
    """
    entry = grid_data.get(key)
    if entry is None:
        return "N/A"
    if isinstance(entry, dict):
        val = entry.get("value")
        if val is None:
            return "N/A"
        try:
            return f"{float(val):.1f}"
        except (ValueError, TypeError):
            return str(val)
    try:
        return f"{float(entry):.1f}"
    except (ValueError, TypeError):
        return str(entry)


def format_missing_kpi_prompt(missing_kpis: Dict[str, List[str]]) -> str:
    """Format a user-friendly prompt for missing KPIs.

    Args:
        missing_kpis: Dict mapping grid name to list of missing KPI names

    Returns:
        Formatted prompt string
    """
    lines = ["Some KPIs couldn't be fetched from Grafana for the following grids:\n"]

    for grid_name, kpis in missing_kpis.items():
        lines.append(f"\n**{grid_name}:**")
        for kpi in kpis:
            display_name = KPI_DISPLAY_NAMES.get(kpi, kpi)
            lines.append(f"- {display_name}")

    lines.append("\n\nPlease provide these values manually.")
    lines.append("Format: `GridName: KPI1=value, KPI2=value`")
    lines.append("\nExample: `ExampleGrid: FS Hours=11.5, Financial CUF=52.4%`")

    return "\n".join(lines)


def parse_manual_kpi_input(
    user_input: str,
    missing_kpis: Dict[str, List[str]],
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Parse user's manual KPI input.

    Args:
        user_input: User's response with manual KPI values
        missing_kpis: Dict of missing KPIs to validate against

    Returns:
        Tuple of (parsed values dict, list of still-missing items)
    """

    parsed: Dict[str, Dict[str, Any]] = {}
    still_missing: List[str] = []

    # Pattern to match "GridName: KPI=value, KPI=value"
    grid_pattern = re.compile(r"([A-Za-z][A-Za-z0-9\s]+):\s*(.+?)(?=\n[A-Za-z]|\Z)", re.DOTALL)
    kpi_pattern = re.compile(r"([^=,]+)\s*=\s*([^,]+)")

    for match in grid_pattern.finditer(user_input):
        grid_name = match.group(1).strip()
        kpi_text = match.group(2).strip()

        grid_lower = grid_name.lower()
        parsed[grid_lower] = {}

        for kpi_match in kpi_pattern.finditer(kpi_text):
            kpi_name = kpi_match.group(1).strip().lower()
            kpi_value = kpi_match.group(2).strip()

            # Try to convert to number
            try:
                # Remove % sign if present
                clean_value = kpi_value.replace("%", "").strip()
                parsed[grid_lower][kpi_name] = float(clean_value)
            except ValueError:
                parsed[grid_lower][kpi_name] = kpi_value

    # Check what's still missing
    for grid_name, kpis in missing_kpis.items():
        grid_lower = grid_name.lower()
        if grid_lower not in parsed:
            still_missing.append(f"{grid_name}: all KPIs")
        else:
            for kpi in kpis:
                kpi_lower = kpi.lower()
                display_name = KPI_DISPLAY_NAMES.get(kpi, kpi).lower()
                if kpi_lower not in parsed[grid_lower] and display_name not in parsed[grid_lower]:
                    still_missing.append(f"{grid_name}: {KPI_DISPLAY_NAMES.get(kpi, kpi)}")

    return parsed, still_missing


def extract_metric_value(
    result: Dict[str, Any], kpi_name: str
) -> Optional[Union[float, Dict[str, float]]]:
    """Extract the metric value from a Grafana tool result.

    Handles multiple result types:
    - single_metric: data.metrics[0].value (or multiple metrics for FS/HPS)
    - single_metric with empty metrics but summary text (e.g., Infinity datasource)
    - chart_image with data: data field might have processed values
    - raw numeric value in various locations

    For service_uptime, this may return a dict with 'fs_hours' and 'hps_hours' keys
    if the Grafana panel returns separate metrics for each.

    Args:
        result: Result from Grafana tool call
        kpi_name: Name of the KPI for logging

    Returns:
        Extracted value (float), dict of values for multi-metric panels, or None if not found
    """
    if not result.get("success"):
        return None

    data = result.get("data", {})

    # Check for single_metric result type
    if data.get("type") == "single_metric":
        metrics = data.get("metrics", [])

        # Special handling for service_uptime: extract FS and HPS separately
        if kpi_name == "service_uptime" and len(metrics) > 1:
            # Look for FS and HPS metrics by name
            fs_value = None
            hps_value = None
            total_value = None

            for metric in metrics:
                name = str(metric.get("name", "")).lower()
                # Prefer display_value for proper unit conversion
                value = metric.get("display_value") or metric.get("value")

                if value is not None:
                    if "fs" in name or "full" in name:
                        fs_value = float(value)
                        LOGGER.info(
                            f"  service_uptime: FS hours = {fs_value} (from '{metric.get('name')}')"
                        )
                    elif "hps" in name or "high" in name or "partial" in name:
                        hps_value = float(value)
                        LOGGER.info(
                            f"  service_uptime: HPS hours = {hps_value} (from '{metric.get('name')}')"
                        )
                    elif total_value is None:
                        # Use first non-FS/HPS value as total
                        total_value = float(value)

            # Return dict if we found separate FS/HPS values
            if fs_value is not None or hps_value is not None:
                result_dict: Dict[str, float] = {}
                if fs_value is not None:
                    result_dict["fs_hours"] = fs_value
                if hps_value is not None:
                    result_dict["hps_hours"] = hps_value
                return result_dict

            # Fall back to total if available
            if total_value is not None:
                return total_value

        # Standard single metric extraction
        # Use display_value which has percentunit conversion applied (value * 100)
        if metrics:
            # Prefer display_value for proper unit conversion (e.g., percentunit)
            display_value = metrics[0].get("display_value")
            if display_value is not None:
                LOGGER.debug(f"  {kpi_name}: using display_value={display_value}")
                return float(display_value)
            # Fall back to raw value
            value = metrics[0].get("value")
            if value is not None:
                return float(value)

        # If metrics is empty, try to parse the summary string
        # Grafana's _format_metric_summary() generates text like "15.00" or "No data available"
        summary = data.get("summary", "")
        if summary and "no data" not in summary.lower():
            # Try to extract a number from the summary
            try:
                # The summary is typically just a formatted number like "15.00" or "15.00 %"
                numeric_part = summary.split()[0] if summary else ""
                # Remove common formatting characters
                numeric_part = numeric_part.replace(",", "").replace("%", "").strip()
                return float(numeric_part)
            except (ValueError, IndexError):
                LOGGER.debug(f"  {kpi_name}: Could not parse summary: {summary}")

        # "No data" from Grafana — use sensible default if defined for this KPI
        if "no data" in summary.lower() or (not metrics and not summary):
            default = KPI_NO_DATA_DEFAULTS.get(kpi_name)
            if default is not None:
                LOGGER.info(f"  {kpi_name}: No data from Grafana, using default {default}")
                return default

    # Check for metrics directly in data
    if isinstance(data, dict):
        metrics = data.get("metrics", [])
        if metrics:
            value = metrics[0].get("value")
            if value is not None:
                return float(value)

        # Check for summary dict (some panels return this instead of string)
        summary = data.get("summary")
        if isinstance(summary, dict):
            for key in ["value", "total", "count", "sum", "mean", "last"]:
                if key in summary:
                    try:
                        return float(summary[key])
                    except (TypeError, ValueError):
                        pass

    # Check for metrics directly in result
    metrics = result.get("metrics", [])
    if metrics:
        value = metrics[0].get("value")
        if value is not None:
            return float(value)

    # Log the result structure for debugging unhandled formats
    LOGGER.warning(
        f"  {kpi_name}: Unable to extract value. "
        f"result_type={result.get('result_type')}, "
        f"data.type={data.get('type') if isinstance(data, dict) else 'N/A'}, "
        f"data.metrics={data.get('metrics', []) if isinstance(data, dict) else 'N/A'}, "
        f"data.summary={data.get('summary', '') if isinstance(data, dict) else 'N/A'}"
    )

    return None


@register_step("fetch_grafana_kpis")
async def fetch_grafana_kpis(context: StepContext) -> StepResult:
    """Fetch KPIs from Grids KPI dashboard.

    Fetches main KPIs for each grid to review. If a Grafana panel fails
    or is unavailable, flags it for manual input.

    In chat mode, this step is skipped as we're discussing existing review data.

    Args:
        context: Step execution context

    Returns:
        StepResult with KPI data per grid or prompts for manual input
    """
    # Skip in chat mode - we're discussing existing review, not generating new data
    if context.get_state("chat_mode", False):
        LOGGER.debug("Chat mode - skipping fetch_grafana_kpis")
        return StepResult(
            data={"skipped": True},
            progress_message="Skipped (chat mode)",
        )

    grids_to_review = context.get_state("grids_to_review", [])

    if not grids_to_review:
        return StepResult.failure("No grids to review. Run resolve_grid_sheets first.")

    # Check if resuming after manual input
    awaiting_manual_kpis = context.get_state("awaiting_manual_kpis")

    if awaiting_manual_kpis and context.user_input:
        user_response = context.user_input.strip().lower()

        # Check for cancel/skip commands
        if user_response in ["cancel", "skip", "abort", "quit", "exit", "stop", "no"]:
            LOGGER.info("User cancelled manual KPI input - skipping workflow")
            return StepResult(
                skip_remaining=True,
                progress_message="Cancelled. No review will be generated.",
            )

        # Note: New-request detection is handled centrally in expert_handler.py
        # This step only receives input if it passed the centralized check

        # Parse user's manual input
        stored_missing_kpis = context.get_state("missing_kpis", {})
        parsed_values, still_missing = parse_manual_kpi_input(
            context.user_input, stored_missing_kpis
        )

        if still_missing:
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "Some values are still missing:\n"
                    + "\n".join(f"- {item}" for item in still_missing)
                    + "\n\nPlease provide the remaining values."
                ),
            )

        # Merge manual values with auto-fetched
        existing_kpi_data = context.get_state("kpi_data", {})
        for grid_lower, values in parsed_values.items():
            if grid_lower not in existing_kpi_data:
                existing_kpi_data[grid_lower] = {}
            existing_kpi_data[grid_lower].update(values)

        return StepResult(
            data={"kpi_data": existing_kpi_data},
            state_updates={
                "kpi_data": existing_kpi_data,
                "missing_kpis": {},
                "awaiting_manual_kpis": False,
            },
            progress_message="Manual KPI values received - data complete",
        )

    # First run - fetch from Grafana
    import asyncio

    time_range = get_previous_month_time_range()
    LOGGER.info(f"Fetching KPIs with time range: {time_range}")

    kpi_data: Dict[str, Dict[str, Any]] = {}
    missing_kpis: Dict[str, List[str]] = {}
    grafana_disabled = False
    tool_not_found = False

    mcp_executor = context.mcp_executor
    if not mcp_executor:
        return StepResult.failure("MCP executor not available")

    # Send progress message to user
    await context.send_progress_to_user(
        f"📊 Fetching KPIs from Grafana for {len(grids_to_review)} grid(s)..."
    )

    # Clear pattern cache for fresh resolution
    _resolved_tool_cache.clear()

    # Resolve any pattern-based tool names before starting
    resolved_tool_mapping: Dict[str, Optional[str]] = {}
    for kpi_name, tool_spec in KPI_TOOL_MAPPING.items():
        resolved_tool_mapping[kpi_name] = await get_tool_name(kpi_name, tool_spec)

    # Log the resolved Grafana tools for debugging
    LOGGER.info(f"Resolved Grafana tools: {resolved_tool_mapping}")

    # --- Fetch current month KPIs for all grids in parallel ---
    async def _fetch_kpis_for_grid(
        grid: Dict[str, Any],
        fetch_time_range: Dict[str, str],
    ) -> Tuple[str, str, Dict[str, Any], List[str], bool, bool]:
        """Fetch all KPIs for a single grid. Returns isolated per-grid results."""
        grid_name = grid["name"]
        grid_lower = grid_name.lower()
        grid_data: Dict[str, Any] = {}
        grid_missing: List[str] = []
        grid_grafana_disabled = False
        grid_tool_not_found = False

        LOGGER.info(f"Fetching KPIs for grid: {grid_name}")

        for kpi_name_inner, _tool_spec in KPI_TOOL_MAPPING.items():
            tool_name = resolved_tool_mapping.get(kpi_name_inner)
            if not tool_name:
                LOGGER.warning(f"  {kpi_name_inner}: Tool not resolved (pattern match failed)")
                grid_missing.append(kpi_name_inner)
                continue

            try:
                result = await mcp_executor.call_tool(
                    tool_name,
                    {
                        "Grid": grid_name,
                        "time_from": fetch_time_range["from"],
                        "time_to": fetch_time_range["to"],
                    },
                )

                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except json.JSONDecodeError:
                        LOGGER.warning(f"  {kpi_name_inner}: Could not parse result as JSON")
                        grid_missing.append(kpi_name_inner)
                        continue

                if isinstance(result, dict) and result.get("success"):
                    result_type = result.get("result_type", "unknown")
                    data = result.get("data", {})
                    metrics = data.get("metrics", []) if isinstance(data, dict) else []
                    summary = data.get("summary", "") if isinstance(data, dict) else ""

                    LOGGER.info(
                        f"  {kpi_name_inner}: result_type={result_type}, "
                        f"num_metrics={len(metrics)}, summary='{summary[:100]}'"
                    )
                    for idx, m in enumerate(metrics):
                        LOGGER.info(
                            f"    metric[{idx}]: name='{m.get('name')}', "
                            f"value={m.get('value')}, display_value={m.get('display_value')}"
                        )

                    value = extract_metric_value(result, kpi_name_inner)
                    if value is not None:
                        if isinstance(value, dict):
                            for sub_key, sub_value in value.items():
                                grid_data[sub_key] = {
                                    "value": sub_value,
                                    "source": "grafana",
                                }
                                LOGGER.info(f"  {sub_key}: {sub_value}")
                            grid_data[kpi_name_inner] = {
                                "value": value,
                                "source": "grafana",
                                "raw_result": result,
                            }
                        else:
                            grid_data[kpi_name_inner] = {
                                "value": value,
                                "source": "grafana",
                                "raw_result": result,
                            }
                            LOGGER.info(f"  {kpi_name_inner}: {value}")
                    else:
                        LOGGER.warning(f"  {kpi_name_inner}: No value in result")
                        grid_missing.append(kpi_name_inner)
                else:
                    error = (
                        result.get("error", "Unknown error")
                        if isinstance(result, dict)
                        else str(result)
                    )
                    LOGGER.warning(f"  {kpi_name_inner}: Failed - {error}")
                    grid_missing.append(kpi_name_inner)

                    if "disabled" in error.lower():
                        grid_grafana_disabled = True
                    elif "unknown tool" in error.lower():
                        grid_tool_not_found = True

            except Exception as e:
                error_str = str(e)
                LOGGER.warning(f"  {kpi_name_inner}: Exception - {error_str}")
                grid_missing.append(kpi_name_inner)

                if "disabled" in error_str.lower():
                    grid_grafana_disabled = True
                elif "unknown tool" in error_str.lower() or "not found" in error_str.lower():
                    grid_tool_not_found = True

        return (
            grid_name,
            grid_lower,
            grid_data,
            grid_missing,
            grid_grafana_disabled,
            grid_tool_not_found,
        )

    # Run all grids in parallel
    grid_tasks = [_fetch_kpis_for_grid(grid, time_range) for grid in grids_to_review]
    grid_results = await asyncio.gather(*grid_tasks, return_exceptions=True)

    for result_or_exc in grid_results:
        if isinstance(result_or_exc, BaseException):
            LOGGER.error(f"Grid KPI fetch failed with exception: {result_or_exc}")
            continue
        (
            grid_name,
            grid_lower,
            grid_data,
            grid_missing,
            grid_grafana_disabled,
            grid_tool_not_found,
        ) = result_or_exc
        kpi_data[grid_lower] = grid_data
        if grid_missing:
            missing_kpis[grid_name] = grid_missing
        if grid_grafana_disabled:
            grafana_disabled = True
        if grid_tool_not_found:
            tool_not_found = True

    # --- Fetch comparison month KPIs in parallel ---
    prev_kpi_data: Dict[str, Dict[str, Any]] = {}
    comparison_label = get_comparison_month_label()
    try:
        comparison_time_range = get_comparison_month_time_range()
        LOGGER.info(f"Fetching comparison month KPIs ({comparison_label})")

        async def _fetch_comparison_for_grid(
            grid: Dict[str, Any],
        ) -> Tuple[str, Dict[str, Any]]:
            """Fetch comparison month KPIs for a single grid."""
            g_name = grid["name"]
            g_lower = g_name.lower()
            prev_grid_data: Dict[str, Any] = {}

            for kpi_name_inner, _tool_spec in KPI_TOOL_MAPPING.items():
                tool_name = resolved_tool_mapping.get(kpi_name_inner)
                if not tool_name:
                    continue

                try:
                    result = await mcp_executor.call_tool(
                        tool_name,
                        {
                            "Grid": g_name,
                            "time_from": comparison_time_range["from"],
                            "time_to": comparison_time_range["to"],
                        },
                    )

                    if isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except json.JSONDecodeError:
                            continue

                    if isinstance(result, dict) and result.get("success"):
                        value = extract_metric_value(result, kpi_name_inner)
                        if value is not None:
                            if isinstance(value, dict):
                                for sub_key, sub_value in value.items():
                                    prev_grid_data[sub_key] = {
                                        "value": sub_value,
                                        "source": "grafana",
                                    }
                            else:
                                prev_grid_data[kpi_name_inner] = {
                                    "value": value,
                                    "source": "grafana",
                                }
                except Exception as e:
                    LOGGER.warning(f"  Comparison month {kpi_name_inner} for {g_name}: {e}")

            return g_lower, prev_grid_data

        comp_tasks = [_fetch_comparison_for_grid(grid) for grid in grids_to_review]
        comp_results = await asyncio.gather(*comp_tasks, return_exceptions=True)

        for comp_result in comp_results:
            if isinstance(comp_result, BaseException):
                LOGGER.warning(f"Comparison KPI fetch failed: {comp_result}")
                continue
            g_lower, prev_grid_data = comp_result
            if prev_grid_data:
                prev_kpi_data[g_lower] = prev_grid_data

        LOGGER.info(f"Fetched comparison month KPIs for {len(prev_kpi_data)} grid(s)")
    except Exception as e:
        LOGGER.warning(f"Failed to fetch comparison month KPIs (non-blocking): {e}")

    # --- Clean data for LLM visibility (remove raw_result) ---
    clean_kpi_data: Dict[str, Dict[str, Any]] = {}
    for grid_key, grid_vals in kpi_data.items():
        clean_grid: Dict[str, Any] = {}
        for k, v in grid_vals.items():
            if isinstance(v, dict):
                clean_grid[k] = {
                    "value": v.get("value"),
                    "source": v.get("source"),
                }
            else:
                clean_grid[k] = v
        clean_kpi_data[grid_key] = clean_grid

    prev_clean: Dict[str, Dict[str, Any]] = {}
    for grid_key, grid_vals in prev_kpi_data.items():
        clean_grid = {}
        for k, v in grid_vals.items():
            if isinstance(v, dict):
                clean_grid[k] = {
                    "value": v.get("value"),
                    "source": v.get("source"),
                }
            else:
                clean_grid[k] = v
        prev_clean[grid_key] = clean_grid

    # Build commentary context for LLM
    commentary_context = _build_commentary_context(
        clean_kpi_data, prev_clean, comparison_label, KPI_TARGETS
    )

    # Check if we need manual input
    if missing_kpis:
        total_missing = sum(len(kpis) for kpis in missing_kpis.values())
        LOGGER.info(f"Missing {total_missing} KPI(s) from Grafana - requesting manual input")

        # Use tracked error patterns for better feedback
        # (grafana_disabled and tool_not_found are set in the loop above)

        # Build helpful error context
        error_context = ""
        if grafana_disabled:
            error_context = (
                "⚠️ **Grafana actions are disabled.**\n"
                "Enable Grafana in Settings → Grafana Dashboard Panels.\n\n"
            )
        elif tool_not_found:
            # Show resolved tool names (filter out None values)
            resolved_names = [t for t in resolved_tool_mapping.values() if t]
            error_context = (
                "⚠️ **Grafana KPI tools not found.**\n"
                "The expected Grafana panels may not be indexed or enabled.\n"
                f"Expected tools: {resolved_names}\n\n"
            )

        return StepResult(
            state_updates={
                "kpi_data": kpi_data,
                "kpi_targets": KPI_TARGETS,
                "previous_month_kpi_data": prev_kpi_data,
                "previous_month_label": comparison_label,
                "missing_kpis": missing_kpis,
                "awaiting_manual_kpis": True,
            },
            needs_user_input=True,
            user_prompt=error_context + format_missing_kpi_prompt(missing_kpis),
        )

    # All KPIs fetched successfully
    return StepResult(
        data={
            "kpi_data": clean_kpi_data,
            "kpi_targets": KPI_TARGETS,
            "previous_month_kpi_data": prev_clean,
            "previous_month_label": comparison_label,
            "commentary_context": commentary_context,
        },
        state_updates={
            "kpi_data": kpi_data,
            "kpi_targets": KPI_TARGETS,
            "previous_month_kpi_data": prev_kpi_data,
            "previous_month_label": comparison_label,
            "missing_kpis": {},
        },
        progress_message=f"Fetched all KPIs for {len(grids_to_review)} grid(s)",
    )
