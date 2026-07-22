"""Equipment Diagnostics MCP Server.

Production equipment monitoring, historical analysis, chart generation,
and automated follow-up scheduling.

Provides platform-agnostic interface (currently VRM, extensible to Deye, etc.).
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, ServerCapabilities, TextContent, Tool

load_dotenv()

_ORG_NAME = os.getenv("ORGANIZATION_NAME", "the operator")
STAFF_ORG_ID: int = int(os.getenv("STAFF_ORG_ID", "2"))

# Import Supabase client for direct DB queries
from supabase import Client, create_client  # type: ignore[attr-defined]

from shared.auth import get_auth_service
from shared.utils.date_utils import to_local_time
from shared.utils.logging import get_logger
from shared.utils.response_formatters import compose_error_response, compose_json_response

from .analyzers.grid_outage_analyzer import GridOutageAnalyzer, GridOutageEvent
from .charts.chart_builder import ChartBuilder
from .platforms.vrm_platform import VRMPlatform
from .tool_schemas import TOOL_SCHEMAS

logger = get_logger("equipment-diagnostics-server")

# Startup message
print("Starting Equipment Diagnostics MCP Server...", file=sys.stderr)

# Initialize MCP server
server = Server("equipment-diagnostics-server")

# Platform instance (VRM by default, extensible to others)
platform: Optional[VRMPlatform] = None

# Supabase client for direct DB queries
db_client: Optional[Client] = None

# Analyzers and chart builder
outage_analyzer = GridOutageAnalyzer()
chart_builder: Optional[ChartBuilder] = None

# Human-readable VE.Bus / battery state mapping (VRM "bst" attribute)
BATTERY_STATE_NAMES = {
    0: "Off",
    1: "Low power",
    2: "Fault",
    3: "Bulk charging",
    4: "Absorption charging",
    5: "Float charging",
    6: "Storage mode",
    7: "Equalize charging",
    8: "Passthru",
    9: "Inverting",
    10: "Power assist",
    11: "Power supply",
    252: "External control",
}

# Configuration
DEFAULT_TIME_RANGE = os.getenv("EQUIPMENT_DIAGNOSTICS_DEFAULT_TIME_RANGE", "last_24h")
OUTAGE_THRESHOLD_W = float(os.getenv("EQUIPMENT_DIAGNOSTICS_OUTAGE_THRESHOLD_W", "100"))
CHART_WIDTH = int(os.getenv("EQUIPMENT_DIAGNOSTICS_CHART_WIDTH", "600"))
CHART_HEIGHT = int(os.getenv("EQUIPMENT_DIAGNOSTICS_CHART_HEIGHT", "400"))

# Time range mappings
TIME_RANGES = {
    "last_hour": timedelta(hours=1),
    "last_6h": timedelta(hours=6),
    "last_24h": timedelta(hours=24),
    "last_7d": timedelta(days=7),
    "last_30d": timedelta(days=30),
    "last_90d": timedelta(days=90),
}

# Default timezone for grids
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "UTC")


def _format_local_timestamp(
    utc_dt: Optional[datetime], tz_name: str = DEFAULT_TIMEZONE
) -> Optional[str]:
    """
    Format a UTC datetime as an ISO string in local time.

    Args:
        utc_dt: A datetime object in UTC
        tz_name: IANA timezone name

    Returns:
        ISO formatted string in local time, or None if input is None
    """
    local_dt = to_local_time(utc_dt, tz_name)
    if local_dt is None:
        return None
    return local_dt.isoformat()


async def _get_grid_timezone(grid_name: str) -> str:
    """Get timezone for a grid from auth database."""
    try:
        auth_service = get_auth_service()
        return await auth_service.get_grid_timezone(grid_name)
    except Exception as e:
        logger.warning(f"Failed to get timezone for grid {grid_name}: {e}")
        return DEFAULT_TIMEZONE


async def get_platform() -> VRMPlatform:
    """Get or initialize the platform instance."""
    global platform
    if platform is None:
        platform = VRMPlatform()
        await platform.initialize()
    return platform


def get_db_client() -> Client:
    """Get or initialize the Supabase client."""
    global db_client
    if db_client is None:
        db_url = os.getenv("SUPABASE_URL") or os.getenv("CHAT_DB_URL")
        db_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("CHAT_DB_SERVICE_KEY")
        if not db_url or not db_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        db_client = create_client(db_url, db_key)
    return db_client


def get_chart_builder() -> ChartBuilder:
    """Get or initialize the chart builder."""
    global chart_builder
    if chart_builder is None:
        chart_builder = ChartBuilder(width=CHART_WIDTH, height=CHART_HEIGHT)
    return chart_builder


def parse_time_range(
    time_range: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> tuple[datetime, datetime]:
    """Parse time range into start and end datetimes."""
    now = datetime.utcnow()

    if time_range == "custom" and start_time and end_time:
        return datetime.fromisoformat(start_time), datetime.fromisoformat(end_time)

    delta = TIME_RANGES.get(time_range, TIME_RANGES["last_24h"])
    return now - delta, now


@server.list_tools()
async def handle_list_tools() -> List[Tool]:
    """List available equipment diagnostics tools."""
    # Fresh Tool objects per call — see tool_schemas module docstring.
    tools = [Tool(**schema) for schema in TOOL_SCHEMAS]

    logger.info(f"Equipment diagnostics server: {len(tools)} tools available")
    return tools


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: Dict[str, Any]
) -> List[TextContent | ImageContent]:
    """Handle tool calls."""
    try:
        plat = await get_platform()

        # Route to appropriate handler
        if name == "get_equipment_status":
            return await _handle_get_equipment_status(plat, arguments)

        elif name == "get_site_info":
            return await _handle_get_site_info(plat, arguments)

        elif name == "get_equipment_details":
            return await _handle_get_equipment_details(plat, arguments)

        elif name == "get_historical_power_data":
            return await _handle_get_historical_power_data(plat, arguments)

        elif name == "get_historical_mppt_performance":
            return await _handle_get_historical_mppt_performance(arguments)

        elif name == "analyze_grid_outage":
            return await _handle_analyze_grid_outage(plat, arguments)

        elif name == "generate_power_chart":
            return await _handle_generate_power_chart(plat, arguments)

        elif name == "schedule_equipment_check":
            return await _handle_schedule_equipment_check(plat, arguments)

        elif name == "get_batch_downtime_summary":
            return await _handle_get_batch_downtime_summary(plat, arguments)

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        logger.error(f"Error in tool {name}: {e}")
        return list(compose_error_response(e))


async def _check_grid_org_access(grid_name: str, organization_id: int) -> Optional[str]:
    """Verify the grid belongs to the given org and return the canonical name, or None if denied.

    Returns the matched grid name (may differ from input due to fuzzy matching), or None if the
    grid doesn't exist within the org.
    """
    auth_service = get_auth_service()
    pool = await auth_service._get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name FROM grids
            WHERE organization_id = $1
              AND is_hidden_from_reporting IS NOT TRUE
              AND deleted_at IS NULL
            ORDER BY name
            """,
            organization_id,
        )
    org_names = [row["name"] for row in rows]
    from shared.utils.grid_matcher import find_best_grid_match

    matched_name, _, _ = find_best_grid_match(grid_name, org_names)
    return matched_name


async def _handle_get_equipment_status(
    plat: VRMPlatform, arguments: Dict[str, Any]
) -> List[TextContent]:
    """Handle get_equipment_status tool call."""
    grid_name = arguments.get("grid_name")
    metrics = arguments.get("metrics", ["inverter", "battery", "grid", "pv", "alarms"])
    organization_id = arguments.get("organization_id")

    # Fail closed when org identity is unresolved (unauthenticated caller).
    if organization_id is None:
        return [TextContent(type="text", text=f"Grid not found: {grid_name}")]

    if int(organization_id) != STAFF_ORG_ID:
        canonical = await _check_grid_org_access(grid_name, int(organization_id))
        if not canonical:
            return [TextContent(type="text", text=f"Grid not found: {grid_name}")]
        grid_name = canonical

    site_id, is_managed = await plat.get_site_id_for_grid(grid_name)
    if not is_managed:
        return [
            TextContent(
                type="text",
                text=f"Equipment data not available for '{grid_name}' "
                f"(generation not managed by {_ORG_NAME}).",
            )
        ]
    if not site_id:
        return [TextContent(type="text", text=f"Grid not found: {grid_name}")]

    status = await plat.get_equipment_status(site_id, metrics)
    status.grid_name = grid_name

    # Get grid timezone for timestamp formatting
    grid_tz = await _get_grid_timezone(grid_name)

    # Convert to dict for JSON response
    result = {
        "grid_name": status.grid_name,
        "site_id": status.site_id,
        "timezone": grid_tz,
        "timestamp": _format_local_timestamp(status.timestamp, grid_tz),
        "is_online": status.is_online,
    }

    if status.inverter:
        result["inverter"] = {
            "l1_power_w": status.inverter.l1_power_w,
            "l2_power_w": status.inverter.l2_power_w,
            "l3_power_w": status.inverter.l3_power_w,
            "total_power_w": status.inverter.total_power_w,
        }

    if status.battery:
        result["battery"] = {
            "soc_percent": status.battery.soc_percent,
            "voltage_v": status.battery.voltage_v,
            "current_a": status.battery.current_a,
            "power_w": status.battery.power_w,
            "charging": status.battery.charging,
        }

    if status.grid:
        result["grid"] = {
            "connected": status.grid.connected,
            "l1_power_w": status.grid.l1_power_w,
            "l2_power_w": status.grid.l2_power_w,
            "l3_power_w": status.grid.l3_power_w,
            "total_power_w": status.grid.total_power_w,
        }

    if status.pv:
        result["pv"] = {
            "total_power_w": status.pv.total_power_w,
        }

    if status.alarms is not None:
        result["alarms"] = [
            {
                "code": a.code,
                "description": a.description,
                "device": a.device,
                "severity": a.severity,
            }
            for a in status.alarms
        ]

    return list(compose_json_response(result))


async def _handle_get_site_info(plat: VRMPlatform, arguments: Dict[str, Any]) -> List[TextContent]:
    """Handle get_site_info tool call."""
    grid_name = arguments.get("grid_name")

    site_id, is_managed = await plat.get_site_id_for_grid(grid_name)
    if not is_managed:
        return [
            TextContent(
                type="text",
                text=f"Equipment data not available for '{grid_name}' "
                f"(generation not managed by {_ORG_NAME}).",
            )
        ]
    if not site_id:
        return [TextContent(type="text", text=f"Grid not found: {grid_name}")]

    result = await plat.get_site_info(site_id)
    return list(compose_json_response(result))


async def _handle_get_equipment_details(
    plat: VRMPlatform, arguments: Dict[str, Any]
) -> List[TextContent]:
    """Handle get_equipment_details tool call."""
    grid_name = arguments.get("grid_name")

    site_id, is_managed = await plat.get_site_id_for_grid(grid_name)
    if not is_managed:
        return [
            TextContent(
                type="text",
                text=f"Equipment data not available for '{grid_name}' "
                f"(generation not managed by {_ORG_NAME}).",
            )
        ]
    if not site_id:
        return [TextContent(type="text", text=f"Grid not found: {grid_name}")]

    result = await plat.get_equipment_details(site_id)
    return list(compose_json_response(result))


async def _handle_get_historical_power_data(
    plat: VRMPlatform, arguments: Dict[str, Any]
) -> List[TextContent]:
    """Handle get_historical_power_data tool call."""
    grid_name = arguments.get("grid_name")
    time_range = arguments.get("time_range", DEFAULT_TIME_RANGE)
    start_time = arguments.get("start_time")
    end_time = arguments.get("end_time")
    metrics = arguments.get("metrics", ["grid_power", "grid_consumption", "battery_soc"])
    analysis = arguments.get("analysis", [])

    site_id, is_managed = await plat.get_site_id_for_grid(grid_name)
    if not is_managed:
        return [
            TextContent(
                type="text",
                text=f"Equipment data not available for '{grid_name}' "
                f"(generation not managed by {_ORG_NAME}).",
            )
        ]
    if not site_id:
        return [TextContent(type="text", text=f"Grid not found: {grid_name}")]

    # Get grid timezone for timestamp formatting
    grid_tz = await _get_grid_timezone(grid_name)

    # Parse time range
    start_dt, end_dt = parse_time_range(time_range, start_time, end_time)

    # Get historical data
    data_points = await plat.get_historical_power(site_id, start_dt, end_dt, metrics)

    result: Dict[str, Any] = {
        "grid_name": grid_name,
        "timezone": grid_tz,
        "time_range": {
            "start": _format_local_timestamp(start_dt, grid_tz),
            "end": _format_local_timestamp(end_dt, grid_tz),
        },
        "data_points_count": len(data_points),
    }

    # Perform requested analysis
    if "outages" in analysis:
        # Filter for grid_consumption only - outages in mini-grids are detected
        # by output consumption dropping to zero
        inverter_points = [p for p in data_points if p.metric == "grid_consumption"]
        outage_result = outage_analyzer.detect_outages(inverter_points, start_dt, end_dt)

        # Enrich each outage with battery SOC, alarms, and cause classification
        if outage_result.outages:
            try:
                await _enrich_outages(plat, outage_result.outages, site_id, grid_tz)
            except Exception as e:
                logger.warning(f"Outage enrichment failed (non-fatal): {e}")

        result["analysis"] = result.get("analysis", {})
        result["analysis"]["outages"] = outage_result.to_dict()

    if "peak_load" in analysis:
        peak = outage_analyzer.calculate_peak_load(data_points)
        result["analysis"] = result.get("analysis", {})
        result["analysis"]["peak_load"] = peak

    if "summary_stats" in analysis:
        stats = outage_analyzer.calculate_summary_stats(data_points)
        result["analysis"] = result.get("analysis", {})
        result["analysis"]["summary_stats"] = stats

    return list(compose_json_response(result))


async def _handle_get_historical_mppt_performance(arguments: Dict[str, Any]) -> List[TextContent]:
    """Handle get_historical_mppt_performance tool call."""
    grid_name = arguments.get("grid_name")
    time_range = arguments.get("time_range", DEFAULT_TIME_RANGE)
    start_time_str = arguments.get("start_time")
    end_time_str = arguments.get("end_time")
    mppt_serial_numbers = arguments.get("mppt_serial_numbers")

    # 1. Validate grid exists (by checking its timezone, a simple way to use an existing async call)
    grid_tz = await _get_grid_timezone(grid_name)
    if grid_tz == DEFAULT_TIMEZONE:  # Fallback implies it might not have been found
        logger.warning(
            f"Could not confirm timezone for {grid_name}, proceeding with default but grid may not exist."
        )

    # 2. Parse time range
    start_dt, end_dt = parse_time_range(time_range, start_time_str, end_time_str)

    # 3. Query Database
    try:
        db = get_db_client()

        query = (
            db.from_("mppt_estimated_actual_hourly_by_mppt")
            .select("bucket, mppt_external_reference, estimated_avg, actual_avg")
            .eq("grid_name", grid_name)
            .gte("bucket", start_dt.isoformat())
            .lte("bucket", end_dt.isoformat())
            .order("bucket", desc=False)
        )

        if mppt_serial_numbers:
            query = query.in_("mppt_external_reference", mppt_serial_numbers)

        response = await asyncio.to_thread(query.execute)

        if not response.data:
            return list(
                compose_json_response(
                    {
                        "message": "No MPPT performance data found for the specified criteria.",
                        "grid_name": grid_name,
                        "time_range": {
                            "start": _format_local_timestamp(start_dt, grid_tz),
                            "end": _format_local_timestamp(end_dt, grid_tz),
                        },
                    }
                )
            )

        # 4. Format Results
        # Group data by MPPT serial
        performance_by_mppt: Dict[str, List[Dict[str, Any]]] = {}
        for row in response.data:
            serial = row.get("mppt_external_reference")
            if not serial:
                continue
            if serial not in performance_by_mppt:
                performance_by_mppt[serial] = []

            performance_by_mppt[serial].append(
                {
                    "timestamp": _format_local_timestamp(
                        datetime.fromisoformat(row["bucket"]), grid_tz
                    ),
                    "estimated_power_w": row.get("estimated_avg"),
                    "actual_power_w": row.get("actual_avg"),
                }
            )

        result = {
            "grid_name": grid_name,
            "timezone": grid_tz,
            "time_range": {
                "start": _format_local_timestamp(start_dt, grid_tz),
                "end": _format_local_timestamp(end_dt, grid_tz),
            },
            "mppt_performance": performance_by_mppt,
        }

        return list(compose_json_response(result))

    except Exception as e:
        logger.error(f"Error getting MPPT performance data: {e}")
        return list(compose_error_response(e))


async def _enrich_outages(
    plat: VRMPlatform,
    outages: List[GridOutageEvent],
    site_id: str,
    grid_tz: str,
) -> None:
    """Enrich outages with battery SOC, alarms, and cause classification.

    Fetches SOC and alarm data for all outages in parallel to minimize latency,
    then classifies each outage cause using the outage analyzer.
    """
    logger.info(f"Enriching {len(outages)} outages for site {site_id}")

    # Build parallel tasks grouped per outage: (soc, alarms)
    soc_tasks = []
    alarm_tasks = []
    for outage in outages:
        alarm_start = outage.start_time - timedelta(minutes=10)
        alarm_end = outage.end_time + timedelta(minutes=10)
        soc_tasks.append(plat.get_battery_soc_at_time(site_id, outage.start_time))
        alarm_tasks.append(plat.get_historical_alarms(site_id, alarm_start, alarm_end))

    # Execute all API calls in parallel
    soc_results = await asyncio.gather(*soc_tasks, return_exceptions=True)
    alarm_results = await asyncio.gather(*alarm_tasks, return_exceptions=True)

    soc_count = 0
    alarm_count = 0
    cause_count = 0

    for i, outage in enumerate(outages):
        soc_raw = soc_results[i]
        if isinstance(soc_raw, BaseException):
            logger.warning(f"SOC fetch failed for outage {i}: {soc_raw}")
            battery_soc: Optional[float] = None
        else:
            battery_soc = soc_raw
            if battery_soc is not None:
                soc_count += 1

        alarm_raw = alarm_results[i]
        if isinstance(alarm_raw, BaseException):
            logger.warning(f"Alarm fetch failed for outage {i}: {alarm_raw}")
            alarms: Optional[List[Dict[str, Any]]] = None
        else:
            alarms = alarm_raw
            if alarms:
                alarm_count += 1

        # Classify outage cause (also sets battery_soc_at_outage and related_alarms)
        outage_analyzer.classify_outage_cause(
            outage,
            battery_soc=battery_soc,
            alarms=alarms,
        )
        if outage.cause_category:
            cause_count += 1

    logger.info(
        f"Enrichment complete: {soc_count}/{len(outages)} got SOC, "
        f"{alarm_count}/{len(outages)} got alarms, "
        f"{cause_count}/{len(outages)} got cause classification"
    )


async def _search_jira_tickets_during_outage(
    grid_name: str,
    outage_start: datetime,
    outage_end: datetime,
) -> List[Dict[str, Any]]:
    """Search for OPS JIRA tickets created or updated during a downtime window.

    Gives the LLM additional context about what was happening during the outage.
    """
    try:
        from servers.jira_server.jira_mcp_server import handle_call_tool as jira_handle_call_tool

        # Format dates for JIRA (YYYY-MM-DD)
        start_date = outage_start.strftime("%Y-%m-%d")
        end_date = outage_end.strftime("%Y-%m-%d")

        result = await jira_handle_call_tool(
            "jira_search_issues_with_comments",
            {
                "grid": grid_name,
                "created_after": start_date,
                "created_before": end_date,
                "max_results": 10,
            },
        )

        # Parse the JSON response from JIRA tool
        if result and len(result) > 0:
            text_content = result[0].text
            data = json.loads(text_content)
            issues = data.get("issues", [])
            # Return lightweight summaries
            return [
                {
                    "key": issue.get("key"),
                    "summary": issue.get("summary"),
                    "status": issue.get("status"),
                    "created": issue.get("created"),
                    "assignee": issue.get("assignee"),
                }
                for issue in issues
            ]
    except ImportError:
        logger.debug("JIRA server not available for ticket search")
    except Exception as e:
        logger.warning(f"JIRA ticket search failed (non-fatal): {e}")

    return []


async def _handle_analyze_grid_outage(
    plat: VRMPlatform, arguments: Dict[str, Any]
) -> List[TextContent]:
    """Handle analyze_grid_outage tool call."""
    grid_name = arguments.get("grid_name")
    outage_time_str = arguments.get("outage_time")
    search_window = arguments.get("search_window_minutes", 60)

    site_id, is_managed = await plat.get_site_id_for_grid(grid_name)
    if not is_managed:
        return [
            TextContent(
                type="text",
                text=f"Equipment data not available for '{grid_name}' "
                f"(generation not managed by {_ORG_NAME}).",
            )
        ]
    if not site_id:
        return [TextContent(type="text", text=f"Grid not found: {grid_name}")]

    # Get grid timezone for timestamp formatting
    grid_tz = await _get_grid_timezone(grid_name)

    # Determine search window
    if outage_time_str:
        outage_time = datetime.fromisoformat(outage_time_str)
        start_dt = outage_time - timedelta(minutes=search_window)
        end_dt = outage_time + timedelta(minutes=search_window)
    else:
        # Search last 24 hours for most recent outage
        end_dt = datetime.utcnow()
        start_dt = end_dt - timedelta(hours=24)

    # Get grid consumption data (o1, o2, o3 = AC output consumption per phase)
    # In mini-grids, outages are detected by output consumption dropping to zero
    data_points = await plat.get_historical_power(site_id, start_dt, end_dt, ["grid_consumption"])

    # Find outages
    outage_result = outage_analyzer.detect_outages(data_points, start_dt, end_dt)

    if not outage_result.outages:
        return list(
            compose_json_response(
                {
                    "grid_name": grid_name,
                    "timezone": grid_tz,
                    "message": "No grid outages found in the search window",
                    "search_window": {
                        "start": _format_local_timestamp(start_dt, grid_tz),
                        "end": _format_local_timestamp(end_dt, grid_tz),
                    },
                }
            )
        )

    # Return the most recent (or closest to specified time) outage
    if outage_time_str:
        # Find closest to specified time
        target_time = datetime.fromisoformat(outage_time_str)
        closest = min(
            outage_result.outages,
            key=lambda o: abs((o.start_time - target_time).total_seconds()),
        )
    else:
        # Return most recent
        closest = outage_result.outages[-1]

    # Calculate peak load before outage using VRM max aggregation
    # to capture momentary peaks that averaged data would miss
    peak_window_start = closest.start_time - timedelta(minutes=30)
    try:
        peak_data_points = await plat.get_historical_power(
            site_id, peak_window_start, closest.start_time, ["grid_consumption"], aggregation="max"
        )
    except Exception as e:
        logger.warning(f"Max aggregation fetch failed, falling back to mean data: {e}")
        peak_data_points = data_points
    peak_before = outage_analyzer.calculate_peak_load(
        peak_data_points, before_time=closest.start_time, window_minutes=30
    )

    # Fetch battery SOC, battery state, alarms, and JIRA tickets in parallel
    alarm_window_start = closest.start_time - timedelta(minutes=10)
    alarm_window_end = closest.end_time + timedelta(minutes=10)

    battery_soc, battery_state, alarms, jira_tickets = await asyncio.gather(
        plat.get_battery_soc_at_time(site_id, closest.start_time),
        plat.get_battery_state_at_time(site_id, closest.start_time),
        plat.get_historical_alarms(site_id, alarm_window_start, alarm_window_end),
        _search_jira_tickets_during_outage(grid_name, closest.start_time, closest.end_time),
    )

    battery_state_name = BATTERY_STATE_NAMES.get(battery_state, f"Unknown ({battery_state})")

    # Classify the outage cause
    closest = outage_analyzer.classify_outage_cause(
        closest,
        battery_soc=battery_soc,
        alarms=alarms,
    )

    # If the outage appears ended, verify with real-time power check
    # A brief power spike during an outage can fool the detector into thinking recovery occurred
    if not closest.is_ongoing:
        try:
            current_power = await plat.get_current_inverter_power(site_id)
            if (
                current_power.total_power_w is not None
                and current_power.total_power_w < OUTAGE_THRESHOLD_W
            ):
                closest.is_ongoing = True
        except Exception as e:
            logger.warning(f"Real-time power check failed (non-fatal): {e}")

    # Build outage dict with local timestamps
    outage_dict = closest.to_dict()
    outage_dict["start_time"] = _format_local_timestamp(closest.start_time, grid_tz)
    if closest.is_ongoing:
        outage_dict["end_time"] = None
        outage_dict["duration"] = "ongoing"
        outage_dict["duration_seconds"] = int(
            (datetime.utcnow() - closest.start_time).total_seconds()
        )
    else:
        outage_dict["end_time"] = _format_local_timestamp(closest.end_time, grid_tz)

    result = {
        "grid_name": grid_name,
        "timezone": grid_tz,
        "outage": outage_dict,
        "pre_outage_conditions": {
            "peak_load_w": peak_before.get("total_power_w") if peak_before else None,
            "battery_soc_percent": battery_soc,
            "vebus_state": battery_state_name,
            "vebus_state_code": battery_state,
        },
        "peak_load_before_outage": peak_before,  # Keep for backwards compatibility
        "total_outages_in_window": len(outage_result.outages),
    }

    # Include relevant alarms for detailed diagnosis
    if closest.related_alarms:
        result["related_alarms"] = [
            {
                "timestamp": _format_local_timestamp(
                    datetime.fromisoformat(a["timestamp"]) if a.get("timestamp") else None,
                    grid_tz,
                ),
                "description": a.get("description", ""),
                "code": a.get("code", ""),
                "device": a.get("device", ""),
                "severity": a.get("severity", ""),
            }
            for a in closest.related_alarms
        ]

    # Include JIRA tickets filed during the outage for additional context
    if jira_tickets:
        result["related_jira_tickets"] = jira_tickets

    # Add summary for easy consumption
    ongoing_suffix = " The grid has NOT recovered and is still down." if closest.is_ongoing else ""

    if closest.cause_category:
        if closest.cause_category == "battery_depletion":
            result["summary"] = (
                f"Grid went down due to battery depletion (SOC: {battery_soc:.1f}%). "
                "This is expected behavior - the system is designed to protect battery longevity."
                + ongoing_suffix
            )
        elif closest.cause_category == "low_battery":
            alarm_desc = (
                closest.related_alarms[0].get("description", "Low battery")
                if closest.related_alarms
                else "Low battery"
            )
            result["summary"] = (
                f"Grid went down due to low battery alarm: {alarm_desc}. "
                f"Battery SOC was {battery_soc:.1f}% at outage time."
                if battery_soc
                else f"Grid went down due to low battery alarm: {alarm_desc}."
            ) + ongoing_suffix
        elif closest.cause_category == "high_temperature":
            alarm_desc = (
                closest.related_alarms[0].get("description", "High temperature")
                if closest.related_alarms
                else "High temperature"
            )
            result["summary"] = (
                f"Grid went down due to high temperature: {alarm_desc}. "
                "Check equipment cooling and ventilation." + ongoing_suffix
            )
        elif closest.cause_category == "vebus_error":
            alarm_desc = (
                closest.related_alarms[0].get("description", "VE.Bus error")
                if closest.related_alarms
                else "VE.Bus error"
            )
            result["summary"] = (
                f"Grid went down due to VE.Bus error: {alarm_desc}." + ongoing_suffix
            )
        elif closest.cause_category == "overload":
            result["summary"] = (
                f"Grid went down due to overload. "
                f"Peak load before outage: {peak_before.get('total_power_w', 0):.0f}W"
                + ongoing_suffix
            )
        elif closest.cause_category == "grid_fault":
            result["summary"] = (
                f"Partial grid fault affecting phases: {', '.join(closest.affected_phases)}"
                + ongoing_suffix
            )
        else:
            result["summary"] = (
                "Outage cause could not be determined from available data." + ongoing_suffix
            )

    return list(compose_json_response(result))


async def _handle_generate_power_chart(
    plat: VRMPlatform, arguments: Dict[str, Any]
) -> List[TextContent | ImageContent]:
    """Handle generate_power_chart tool call."""
    grid_name = arguments.get("grid_name")
    chart_type = arguments.get("chart_type")
    time_range = arguments.get("time_range", DEFAULT_TIME_RANGE)
    highlight_events = arguments.get("highlight_events", True)

    site_id, is_managed = await plat.get_site_id_for_grid(grid_name)
    if not is_managed:
        return [
            TextContent(
                type="text",
                text=f"Equipment data not available for '{grid_name}' "
                f"(generation not managed by {_ORG_NAME}).",
            )
        ]
    if not site_id:
        return [TextContent(type="text", text=f"Grid not found: {grid_name}")]

    # Parse time range
    start_dt, end_dt = parse_time_range(time_range)

    # Determine metrics based on chart type
    # grid_consumption (o1-o3) = total load-side consumption including AC-coupled PV
    metrics_map = {
        "power_timeline": ["grid_consumption"],
        "battery_soc": ["battery_soc"],
        "grid_vs_inverter": ["grid_power", "grid_consumption"],
        "load_distribution": ["grid_consumption"],
        "outage_events": ["grid_consumption"],
    }
    metrics = metrics_map.get(chart_type, ["grid_consumption"])

    # Get historical data
    data_points = await plat.get_historical_power(site_id, start_dt, end_dt, metrics)

    if not data_points:
        return [TextContent(type="text", text=f"No data available for {grid_name} in {time_range}")]

    # Get outages if highlighting (using grid_consumption data)
    outages = None
    if highlight_events and chart_type in ["power_timeline", "outage_events"]:
        inverter_points = [p for p in data_points if p.metric == "grid_consumption"]
        outage_result = outage_analyzer.detect_outages(inverter_points, start_dt, end_dt)
        outages = outage_result.outages

    # Generate chart
    try:
        builder = get_chart_builder()
        chart_base64 = builder.generate_chart(
            chart_type=chart_type,
            data_points=data_points,
            grid_name=grid_name,
            outages=outages,
            time_range=time_range,
        )

        return [
            ImageContent(
                type="image",
                data=chart_base64,
                mimeType="image/png",
            ),
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "result_type": "chart_image",
                        "chart_type": chart_type,
                        "grid_name": grid_name,
                        "time_range": time_range,
                        "data_points": len(data_points),
                        "outages_highlighted": len(outages) if outages else 0,
                    }
                ),
            ),
        ]

    except Exception as e:
        logger.error(f"Chart generation error: {e}")
        return [TextContent(type="text", text=f"Chart generation failed: {str(e)}")]


async def _handle_schedule_equipment_check(
    plat: VRMPlatform, arguments: Dict[str, Any]
) -> List[TextContent]:
    """Handle schedule_equipment_check tool call."""
    grid_name = arguments.get("grid_name")
    delay_minutes = arguments.get("delay_minutes", 5)
    check_type = arguments.get("check_type", "full_status")
    expected_condition = arguments.get("expected_condition")
    notify_on_failure = arguments.get("notify_on_failure", True)

    # Verify grid exists
    site_id, is_managed = await plat.get_site_id_for_grid(grid_name)
    if not is_managed:
        return [
            TextContent(
                type="text",
                text=f"Equipment data not available for '{grid_name}' "
                f"(generation not managed by {_ORG_NAME}).",
            )
        ]
    if not site_id:
        return [TextContent(type="text", text=f"Grid not found: {grid_name}")]

    # Get grid timezone for timestamp formatting
    grid_tz = await _get_grid_timezone(grid_name)

    # Build the command to schedule
    # This will be picked up by the schedule server
    command = f"/equipment_status {grid_name}"
    if check_type != "full_status":
        command += f" --check={check_type}"

    # Calculate scheduled time
    scheduled_time = datetime.utcnow() + timedelta(minutes=delay_minutes)

    result = {
        "scheduled": True,
        "grid_name": grid_name,
        "timezone": grid_tz,
        "check_type": check_type,
        "delay_minutes": delay_minutes,
        "scheduled_time": _format_local_timestamp(scheduled_time, grid_tz),
        "command": command,
        "expected_condition": expected_condition,
        "notify_on_failure": notify_on_failure,
        "message": (
            f"Scheduled equipment check for {grid_name} in {delay_minutes} minutes. "
            f"Check type: {check_type}."
        ),
    }

    # Note: Actual scheduling integration would call the schedule_server
    # This returns the intent - the orchestrator can use schedule_server to create the schedule

    return list(compose_json_response(result))


async def _handle_get_batch_downtime_summary(
    plat: VRMPlatform, arguments: Dict[str, Any]
) -> List[TextContent]:
    """Handle get_batch_downtime_summary tool call."""
    grid_names = arguments.get("grid_names", [])
    hours = arguments.get("hours", 24)
    max_concurrent = arguments.get("max_concurrent", 5)

    if not grid_names:
        return [TextContent(type="text", text="No grid names provided")]

    logger.info(
        f"Fetching downtime for {len(grid_names)} grids with max_concurrent={max_concurrent}"
    )

    # Use the batch method with semaphore
    results = await plat.get_batch_downtime_summary(
        grid_names=grid_names,
        hours=hours,
        max_concurrent=max_concurrent,
        timeout_per_grid=3.0,
    )

    # Convert to dict format for JSON response
    response: Dict[str, Any] = {
        "grids_requested": len(grid_names),
        "grids_with_data": len([r for r in results.values() if r.error is None]),
        "grids_with_errors": len([r for r in results.values() if r.error is not None]),
        "hours_analyzed": hours,
        "results": {},
    }

    for grid_name, summary in results.items():
        response["results"][grid_name] = summary.to_dict()

    return list(compose_json_response(response))


async def main():
    """Main server function."""
    try:
        logger.info("Starting Equipment Diagnostics MCP Server...")
        print("Equipment Diagnostics server initialized", file=sys.stderr)

        options = InitializationOptions(
            server_name="equipment-diagnostics-server",
            server_version="1.0.0",
            capabilities=ServerCapabilities(),
        )

        async with stdio_server() as (read_stream, write_stream):
            print("Connected to stdio streams", file=sys.stderr)
            await server.run(read_stream, write_stream, options)

    except Exception as e:
        print(f"Fatal error in Equipment Diagnostics server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Equipment Diagnostics server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"Equipment Diagnostics server crashed: {e}", file=sys.stderr)
        sys.exit(1)
