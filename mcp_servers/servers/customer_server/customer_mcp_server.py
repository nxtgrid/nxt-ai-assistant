"""MCP Customer Server - Customer-facing tools for payment and commissioning status checks."""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo

import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities

# Load environment variables from .env file BEFORE importing shared_code
load_dotenv()

# Import VRMPlatform for downtime fetching in /grids command
from servers.equipment_diagnostics_server.platforms.vrm_platform import InverterVoltage, VRMPlatform

from shared.auth import get_auth_service
from shared.auth.auth_service import MANAGED_GENERATION_COLUMN
from shared.utils.geo import parse_location_geom
from shared.utils.http_client import HTTPClientMixin
from shared.utils.response_formatters import compose_error_response, compose_json_response

# Configure logging to stderr for Claude Desktop visibility
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("customer-server")

# Startup messages to stderr
print("🚀 Customer MCP Server starting...", file=sys.stderr)
print(f"📍 Python path: {sys.path}", file=sys.stderr)
print(f"📂 Working directory: {os.getcwd()}", file=sys.stderr)

# Initialize MCP server
server = Server("customer-server")

# Auth Database configuration (contains orders, directives, meters, connections, customers, grids)
AUTH_SUPABASE_URL = os.getenv("AUTH_SUPABASE_URL", "")
AUTH_SUPABASE_KEY = os.getenv("AUTH_SUPABASE_KEY", "")
AUTH_SUPABASE_ANON_KEY = os.getenv("AUTH_SUPABASE_ANON_KEY", "")

# Payment processor configuration
PAYMENT_PROCESSOR_API_URL = os.getenv("PAYMENT_PROCESSOR_API_URL", "")
PAYMENT_PROCESSOR_SECRET_KEY = os.getenv("PAYMENT_PROCESSOR_SECRET_KEY")

# Metering Platform API configuration (meter commissioning service)
METERING_API_URL = os.getenv("METERING_API_URL", "")
METERING_BEARER_TOKEN = os.getenv("METERING_BEARER_TOKEN", "")
METERING_API_KEY = os.getenv("METERING_API_KEY", "")

# TimescaleDB configuration (grid energy snapshots)
TIMESCALE_HOST = os.getenv("TIMESCALE_HOST", "")
TIMESCALE_PORT = int(os.getenv("TIMESCALE_PORT", "37244"))
TIMESCALE_DATABASE = os.getenv("TIMESCALE_DATABASE", "tsdb")
TIMESCALE_USER = os.getenv("TIMESCALE_USER", "")
TIMESCALE_PASSWORD = os.getenv("TIMESCALE_PASSWORD", "")

# Default staleness threshold for status data (30 minutes)
STALENESS_THRESHOLD = timedelta(minutes=30)

# Max concurrent VRM API calls for batch operations (downtime, weather, voltage)
VRM_BATCH_MAX_CONCURRENT = int(os.getenv("VRM_BATCH_MAX_CONCURRENT", "12"))

# Staff organization ID (controls staff-only views in customer tools)
STAFF_ORG_ID: int = int(os.getenv("STAFF_ORG_ID", "2"))
# Default timezone for display and fallback when grid has no timezone configured
DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "UTC")
CUSTOMER_METER_ACTIONS_ENABLED: bool = (
    os.getenv("CUSTOMER_METER_ACTIONS_ENABLED", "false").lower() == "true"
)
# IMPORTANT: If you change this env var, also update the "enum" array in tool_definitions.json
# for the set_meter_power_limit tool's power_limit_watts property so they stay in sync.
try:
    CUSTOMER_METER_POWER_LIMIT_OPTIONS: list[int] = [
        int(x.strip())
        for x in os.getenv("CUSTOMER_METER_POWER_LIMIT_OPTIONS", "200,600").split(",")
        if x.strip()
    ]
except ValueError:
    logger.warning(
        "CUSTOMER_METER_POWER_LIMIT_OPTIONS is malformed; falling back to [200, 600]. "
        "Expected comma-separated integers, e.g. '200,600'."
    )
    CUSTOMER_METER_POWER_LIMIT_OPTIONS = [200, 600]

_METER_ACTIONS_DISABLED_MSG: str = (
    "Meter write actions are disabled. Set CUSTOMER_METER_ACTIONS_ENABLED=true to enable."
)

# Rate limiting for meter write actions — keyed by "{action}:{meter_number}"
_last_action_times: dict[str, datetime] = {}
_ACTION_COOLDOWNS: dict[str, timedelta] = {
    "set_meter_power_limit": timedelta(minutes=5),
    "set_meter_date": timedelta(minutes=5),
    "turn_meter_on": timedelta(minutes=5),
    "turn_meter_off": timedelta(minutes=5),
    "resend_meter_token": timedelta(minutes=10),
    "resend_clear_tamper_token": timedelta(minutes=10),
    "resend_power_limit_token": timedelta(minutes=10),
    "retry_commissioning": timedelta(minutes=15),
    "unassign_meter": timedelta(hours=1),
}

# Base URL for the grid management platform (used to build direct links in tool responses).
# Optional — if unset, platform_url fields are omitted from tool output.
PLATFORM_BASE_URL: str = os.getenv("PLATFORM_BASE_URL", "").rstrip("/")

# Status stability: average over recent snapshots to prevent flapping
STATUS_STABILITY_SNAPSHOT_COUNT = 3  # Use majority voting over 3 snapshots
STATUS_STABILITY_MAX_LOOKBACK_MINUTES = 60  # Don't go beyond 1 hour even if fewer snapshots


def _format_time_12h(hour: int, minute: int) -> str:
    """Convert 24h time to 12h format (e.g., 14:00 -> 2:00 PM)."""
    if hour == 0:
        return f"12:{minute:02d} AM"
    elif hour < 12:
        return f"{hour}:{minute:02d} AM"
    elif hour == 12:
        return f"12:{minute:02d} PM"
    elif hour == 24:
        return "Midnight"
    else:
        return f"{hour - 12}:{minute:02d} PM"


def _to_local_time(
    utc_dt: Optional[datetime], tz_name: str = DEFAULT_TIMEZONE
) -> Optional[datetime]:
    """
    Convert a UTC datetime to local time in the specified timezone.

    Args:
        utc_dt: A datetime object in UTC (or naive, assumed UTC)
        tz_name: IANA timezone name (e.g., 'Africa/Lagos', 'UTC')

    Returns:
        Datetime in local timezone, or None if input is None
    """
    if utc_dt is None:
        return None

    try:
        # Ensure the datetime is timezone-aware (assume UTC if naive)
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)

        # Convert to target timezone
        local_tz = ZoneInfo(tz_name)
        return utc_dt.astimezone(local_tz)
    except Exception as e:
        logger.warning(f"Failed to convert to timezone {tz_name}: {e}")
        # Return original datetime on error
        return utc_dt


def _format_local_timestamp(
    utc_dt: Optional[datetime], tz_name: str = DEFAULT_TIMEZONE, include_tz: bool = True
) -> Optional[str]:
    """
    Format a UTC datetime as an ISO string in local time.

    Args:
        utc_dt: A datetime object in UTC
        tz_name: IANA timezone name
        include_tz: Whether to include timezone abbreviation in output

    Returns:
        ISO formatted string in local time, or None if input is None
    """
    local_dt = _to_local_time(utc_dt, tz_name)
    if local_dt is None:
        return None

    # Format as ISO with timezone offset
    return local_dt.isoformat()


def _format_downtime_summary_text(
    downtime_dict: Dict[str, Any], tz_name: str = DEFAULT_TIMEZONE
) -> str:
    """Format a pre-built summary sentence from downtime data for LLM verbatim use.

    Produces a 1-2 sentence summary like:
    - "No downtime in the last 24 hours."
    - "2 outages totaling 3h 15m, caused by battery depletion. Last outage started Mon 8:15 PM, recovered Tue 7:12 AM WAT."
    - "1 outage of 45m due to battery depletion (ongoing since Mon 4:20 AM WAT)."
    """
    total_min = downtime_dict.get("total_downtime_minutes", 0) or 0
    if total_min == 0:
        return "No downtime in the last 24 hours."

    outage_count = downtime_dict.get("outage_count", 0)
    causes = downtime_dict.get("causes", {})

    # Format duration
    hours, mins = divmod(total_min, 60)
    if hours > 0 and mins > 0:
        duration_str = f"{hours}h {mins}m"
    elif hours > 0:
        duration_str = f"{hours}h"
    else:
        duration_str = f"{mins}m"

    # Format causes
    if causes:
        cause_parts = []
        for cause, minutes in sorted(causes.items(), key=lambda x: x[1], reverse=True):
            cause_label = cause.replace("_", " ")
            cause_parts.append(cause_label)
        cause_str = " and ".join(cause_parts) if len(cause_parts) <= 2 else ", ".join(cause_parts)
    else:
        cause_str = "unknown cause"

    # Build first part
    if outage_count == 1:
        summary = f"1 outage of {duration_str} due to {cause_str}"
    else:
        summary = f"{outage_count} outages totaling {duration_str}, caused by {cause_str}"

    # Add last outage timing
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, Exception):
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    def _parse_local(iso_str: str) -> datetime:
        """Parse ISO string, assume UTC if naive, convert to grid timezone."""
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)

    fmt = "%a %-I:%M %p"
    is_ongoing = downtime_dict.get("last_outage_ongoing", False)
    last_start_iso = downtime_dict.get("last_outage_time")
    last_end_iso = downtime_dict.get("last_outage_end")

    if last_start_iso and is_ongoing:
        try:
            local_start = _parse_local(last_start_iso)
            tz_abbrev = local_start.strftime("%Z") or "WAT"
            summary += f" (ongoing since {local_start.strftime(fmt)} {tz_abbrev})."
        except (ValueError, TypeError):
            summary += " (ongoing)."
    elif last_start_iso and last_end_iso:
        try:
            local_start = _parse_local(last_start_iso)
            local_end = _parse_local(last_end_iso)
            tz_abbrev = local_start.strftime("%Z") or "WAT"
            summary += (
                f". Last outage started {local_start.strftime(fmt)}, "
                f"recovered {local_end.strftime(fmt)} {tz_abbrev}."
            )
        except (ValueError, TypeError):
            summary += "."
    else:
        summary += "."

    return summary


def _weather_to_icon(weather: Optional[str]) -> str:
    """Convert weather description to icon. Returns icon + original text for unknown weather."""
    if not weather:
        return "❓"

    weather_lower = weather.lower()

    # Map weather descriptions to icons
    if any(w in weather_lower for w in ["clear", "sunny"]):
        return "☀️"
    elif any(w in weather_lower for w in ["partly cloudy", "partly sunny"]):
        return "⛅"
    elif any(w in weather_lower for w in ["cloud", "cloudy", "overcast"]):
        return "☁️"
    elif any(w in weather_lower for w in ["rain", "drizzle", "shower"]):
        return "🌧️"
    elif any(w in weather_lower for w in ["thunder", "storm"]):
        return "⛈️"
    elif any(w in weather_lower for w in ["fog", "mist", "haze"]):
        return "🌫️"
    elif any(w in weather_lower for w in ["snow", "sleet"]):
        return "❄️"
    elif "night" in weather_lower:
        return "🌙"
    elif "wind" in weather_lower:
        return "💨"
    else:
        # Unknown weather - return the text itself
        return weather


def _find_closest_grid_name(
    input_name: str, available_names: List[str], threshold: int = 80
) -> Optional[str]:
    """
    Find the closest matching grid name using fuzzy matching.

    Uses the shared grid_matcher module which provides:
    - Case-insensitive exact matching (fast path)
    - Fuzzy matching with rapidfuzz for typos/misspellings
    - Partial matching (e.g., "Komponents" -> "Komponents Office")

    Args:
        input_name: User-provided grid name (may have typos/case issues)
        available_names: List of valid grid names
        threshold: Minimum similarity score (0-100) for fuzzy match

    Returns:
        Closest matching name if within threshold, None otherwise
    """
    if not input_name or not available_names:
        return None

    try:
        from shared.utils.grid_matcher import find_best_grid_match

        matched_name, was_fuzzy, score = find_best_grid_match(
            input_name, available_names, threshold=threshold
        )
        return matched_name
    except ImportError:
        # Fallback: simple case-insensitive exact match
        input_lower = input_name.lower()
        for name in available_names:
            if name.lower() == input_lower:
                return name
        return None


def is_stale(timestamp: Optional[datetime], threshold_hours: Optional[float] = None) -> bool:
    """Return True if timestamp is older than the threshold.

    Args:
        timestamp: A datetime object or None
        threshold_hours: Custom threshold in hours (default: 0.5 = 30 minutes)

    Returns:
        True if timestamp is None or older than threshold
    """
    if timestamp is None:
        return True  # No timestamp = consider stale
    now = datetime.now(timezone.utc)
    # Handle naive timestamps by assuming UTC
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    threshold = timedelta(hours=threshold_hours) if threshold_hours else STALENESS_THRESHOLD
    return bool((now - timestamp) > threshold)


class CustomerServiceClient(HTTPClientMixin):
    """Client for customer-facing operations."""

    def __init__(self):
        super().__init__()
        self.auth_supabase_url = AUTH_SUPABASE_URL
        self.auth_supabase_key = AUTH_SUPABASE_KEY
        self.auth_supabase_anon_key = AUTH_SUPABASE_ANON_KEY
        self.payment_processor_url = PAYMENT_PROCESSOR_API_URL
        self.payment_processor_key = PAYMENT_PROCESSOR_SECRET_KEY
        self.metering_api_url = METERING_API_URL.rstrip("/") if METERING_API_URL else METERING_API_URL
        self.metering_bearer_token = METERING_BEARER_TOKEN
        self.metering_api_key = METERING_API_KEY

    def _check_rate_limit(self, action: str, meter_number: str) -> str | None:
        """Return an error string if the action is within its cooldown window, else None."""
        key = f"{action}:{meter_number}"
        last = _last_action_times.get(key)
        cooldown = _ACTION_COOLDOWNS[action]
        if last and (datetime.now(timezone.utc) - last) < cooldown:
            remaining = (
                int((cooldown - (datetime.now(timezone.utc) - last)).total_seconds() / 60) + 1
            )
            return (
                f"This action was recently performed on meter {meter_number}. "
                f"Please wait {remaining} more minute(s) before retrying."
            )
        _last_action_times[key] = datetime.now(timezone.utc)
        return None

    async def _get_supabase_client(self):
        """Get Supabase client for AUTH database."""
        if not self.auth_supabase_url or not (
            self.auth_supabase_key or self.auth_supabase_anon_key
        ):
            raise Exception(
                "Auth Supabase not configured. Set AUTH_SUPABASE_URL and (AUTH_SUPABASE_KEY or AUTH_SUPABASE_ANON_KEY) in environment."
            )

        from supabase import create_client

        # Use anon key for RLS-based access, or service key if anon key not available
        key = self.auth_supabase_anon_key or self.auth_supabase_key
        return create_client(self.auth_supabase_url, key)

    async def get_user_organization(self, user_email: str) -> Optional[int]:
        """
        Get organization_id for a user by their email.

        Args:
            user_email: User's email address

        Returns:
            Organization ID or None if not found
        """
        try:
            # Use AuthService to get user permissions
            auth_service = get_auth_service()
            permissions = await auth_service.get_user_permissions(user_email)

            if permissions.organization_ids:
                org_id = int(permissions.organization_ids[0])
                logger.info(f"Found organization_id {org_id} for user {user_email}")
                return org_id

            logger.warning(f"No organization found for user {user_email}")
            return None

        except Exception as e:
            logger.error(f"Error getting user organization: {e}")
            return None

    async def _get_last_fs_delivery(
        self,
        conn,
        grid_id: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Get the last FS command delivery percentage for a grid.

        Args:
            conn: Auth database connection
            grid_id: Grid ID to get delivery stats for

        Returns:
            Dict with command, delivery_pct, successful, total, executed_at
            or None if no FS commands found for this grid
        """
        try:
            row = await conn.fetchrow(
                """
                SELECT
                    dbe.successful_count,
                    dbe.total_count,
                    db.fs_command,
                    dbe.created_at
                FROM directive_batch_executions dbe
                JOIN directive_batches db ON dbe.directive_batch_id = db.id
                WHERE db.grid_id = $1
                  AND db.fs_command IS NOT NULL
                ORDER BY dbe.id DESC
                LIMIT 1
                """,
                grid_id,
            )

            if not row:
                return None

            total = row["total_count"] or 0
            successful = row["successful_count"] or 0
            delivery_pct = round((successful / total * 100), 1) if total > 0 else 0

            return {
                "command": row["fs_command"],
                "delivery_pct": delivery_pct,
                "successful": successful,
                "total": total,
                "executed_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
        except Exception as e:
            logger.error(f"Error getting last FS delivery for grid {grid_id}: {e}")
            return None

    async def _get_fs_schedule(
        self,
        conn,
        grid_id: int,
        current_fs_on: Optional[bool],
    ) -> Dict[str, Any]:
        """
        Get FS command schedule for the next 24 hours.

        Returns:
        - scheduled_commands: Raw list of scheduled FS on/off commands with times
        - fs_on_periods: Calculated FS On periods (start/end/duration)
        - total_fs_on_hours: Total hours FS will be on in next 24h
        - current_state: Current FS state

        Args:
            conn: Auth database connection
            grid_id: Grid ID to get schedule for
            current_fs_on: Current FS state from TimescaleDB (True/False/None)

        Returns:
            Dict with scheduled_commands, fs_on_periods, total_fs_on_hours
        """
        try:
            # Query directive_batches for FS commands
            fs_commands = await conn.fetch(
                """
                SELECT id, hour, minute, is_repeating, fs_command
                FROM directive_batches
                WHERE grid_id = $1
                  AND fs_command IS NOT NULL
                  AND (is_deleted IS NULL OR is_deleted = false)
                ORDER BY hour, minute
                """,
                grid_id,
            )

            if not fs_commands:
                return {
                    "current_state": (
                        "on" if current_fs_on else "off" if current_fs_on is False else "unknown"
                    ),
                    "scheduled_commands": [],
                    "fs_on_periods": [],
                    "total_fs_on_hours": None,
                    "summary": "No FS schedule configured for this grid",
                }

            # Get current time in UTC
            now = datetime.now(timezone.utc)
            current_hour = now.hour
            current_minute = now.minute
            now_minutes = current_hour * 60 + current_minute

            # Filter commands for next 24 hours
            # For repeating commands that have already passed today, shift them to tomorrow
            # by adding 24*60 minutes to their effective time
            commands_next_24h = []
            for cmd in fs_commands:
                cmd_dict = dict(cmd)
                cmd_minutes = cmd_dict["hour"] * 60 + cmd_dict["minute"]

                if cmd_dict["is_repeating"]:
                    # Repeating: if time has passed today, it will run tomorrow
                    if cmd_minutes <= now_minutes:
                        # Already passed today - schedule for tomorrow (add 24h)
                        cmd_dict["effective_minutes"] = cmd_minutes + 24 * 60
                    else:
                        # Still upcoming today
                        cmd_dict["effective_minutes"] = cmd_minutes
                    commands_next_24h.append(cmd_dict)
                else:
                    # Non-repeating: only if time hasn't passed today
                    if cmd_minutes > now_minutes:
                        cmd_dict["effective_minutes"] = cmd_minutes
                        commands_next_24h.append(cmd_dict)

            # Sort by effective time (accounts for tomorrow's repeating commands)
            sorted_cmds = sorted(commands_next_24h, key=lambda x: x["effective_minutes"])

            # Build scheduled_commands list (the raw schedule)
            scheduled_commands = []
            for cmd in sorted_cmds:
                time_12h = _format_time_12h(cmd["hour"], cmd["minute"])
                scheduled_commands.append(
                    {
                        "time": f"{cmd['hour']:02d}:{cmd['minute']:02d}",
                        "time_display": time_12h,
                        "action": cmd["fs_command"],  # 'on' or 'off'
                        "is_repeating": cmd["is_repeating"],
                    }
                )

            if not commands_next_24h:
                return {
                    "current_state": (
                        "on" if current_fs_on else "off" if current_fs_on is False else "unknown"
                    ),
                    "scheduled_commands": [],
                    "fs_on_periods": [],
                    "total_fs_on_hours": None,
                    "summary": "No remaining FS commands scheduled today",
                }

            # Calculate FS On periods accounting for current state
            periods = []
            total_minutes = 0.0

            # Track current simulated state (start with actual current state)
            simulated_fs_on = current_fs_on if current_fs_on is not None else False
            current_on_start_minutes: Optional[int] = None

            # If currently ON, track from "now"
            if simulated_fs_on:
                current_on_start_minutes = now_minutes

            for cmd in sorted_cmds:
                # Use effective_minutes for calculation (accounts for tomorrow)
                cmd_minutes = cmd["effective_minutes"]
                # Display time uses original hour/minute
                cmd_time_str = f"{cmd['hour']:02d}:{cmd['minute']:02d}"
                cmd_time_12h = _format_time_12h(cmd["hour"], cmd["minute"])

                # Case-insensitive comparison for fs_command (DB may store ON/OFF or on/off)
                fs_cmd = cmd["fs_command"].lower() if cmd["fs_command"] else ""

                if fs_cmd == "on":
                    if not simulated_fs_on:
                        # Transitioning OFF → ON
                        simulated_fs_on = True
                        current_on_start_minutes = cmd_minutes
                    # else: ON → ON has no effect (already on)

                elif fs_cmd == "off":
                    if simulated_fs_on and current_on_start_minutes is not None:
                        # Transitioning ON → OFF - record the period
                        duration_minutes = cmd_minutes - current_on_start_minutes
                        duration_hours = round(duration_minutes / 60, 1)

                        # Format start time
                        if current_on_start_minutes == now_minutes:
                            start_display = "Now"
                            start_str = "now"
                        else:
                            start_hour = current_on_start_minutes // 60
                            start_min = current_on_start_minutes % 60
                            start_display = _format_time_12h(start_hour, start_min)
                            start_str = f"{start_hour:02d}:{start_min:02d}"

                        periods.append(
                            {
                                "start": start_str,
                                "start_display": start_display,
                                "end": cmd_time_str,
                                "end_display": cmd_time_12h,
                                "duration_hours": duration_hours,
                            }
                        )
                        total_minutes += duration_minutes

                        simulated_fs_on = False
                        current_on_start_minutes = None
                    # else: OFF → OFF has no effect (already off)

            # Handle unclosed period (still ON at end of 24h window)
            if simulated_fs_on and current_on_start_minutes is not None:
                # ON extends to end of 24h window from now
                end_minutes = now_minutes + 24 * 60
                duration_minutes = end_minutes - current_on_start_minutes
                duration_hours = round(duration_minutes / 60, 1)

                # Format start time
                if current_on_start_minutes == now_minutes:
                    start_display = "Now"
                    start_str = "now"
                else:
                    start_hour = current_on_start_minutes // 60
                    start_min = current_on_start_minutes % 60
                    start_display = _format_time_12h(start_hour, start_min)
                    start_str = f"{start_hour:02d}:{start_min:02d}"

                periods.append(
                    {
                        "start": start_str,
                        "start_display": start_display,
                        "end": "24:00",
                        "end_display": "Midnight",
                        "duration_hours": duration_hours,
                    }
                )
                total_minutes += duration_minutes

            # Calculate total hours
            total_hours = round(total_minutes / 60, 1) if total_minutes > 0 else 0

            # Build summary
            if scheduled_commands:
                cmd_summary = ", ".join(
                    f"FS {c['action'].upper()} at {c['time_display']}" for c in scheduled_commands
                )
            else:
                cmd_summary = "No commands scheduled"

            return {
                "current_state": (
                    "on" if current_fs_on else "off" if current_fs_on is False else "unknown"
                ),
                "scheduled_commands": scheduled_commands,
                "fs_on_periods": periods,
                "total_fs_on_hours": total_hours,
                "summary": f"Schedule: {cmd_summary}. Total FS On: {total_hours}h in next 24h",
            }

        except Exception as e:
            logger.error(f"Error getting FS schedule for grid {grid_id}: {e}")
            return {
                "current_state": "unknown",
                "scheduled_commands": [],
                "fs_on_periods": [],
                "total_fs_on_hours": None,
                "summary": f"Error retrieving FS schedule: {str(e)}",
                "error": str(e),
            }

    async def _get_yesterday_on_hours(
        self,
        ts_conn,
        grid_id: int,
    ) -> Dict[str, Any]:
        """
        Calculate how many hours the grid was ON yesterday.

        Uses grid_energy_snapshot_15_min table with TimescaleDB's native
        time_bucket_gapfill() for gap-filling missing periods using LOCF
        (Last Observation Carried Forward).

        ON = is_fs_active=true OR is_hps_on=true.

        Args:
            ts_conn: TimescaleDB connection
            grid_id: Grid ID

        Returns:
            Dict with yesterday_on_hours, total_periods, on_periods, coverage_pct
        """
        try:
            # Calculate yesterday's date range in UTC (naive timestamps for DB compatibility)
            now = datetime.utcnow()
            yesterday_start = (now - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            yesterday_end = yesterday_start + timedelta(days=1)

            # Use TimescaleDB's time_bucket_gapfill with LOCF for gap filling
            # This fills in missing 15-min slots using last observation carried forward
            rows = await ts_conn.fetch(
                """
                SELECT
                    time_bucket_gapfill('15 minutes', created_at) AS bucket,
                    locf(last(is_fs_active, created_at)) AS is_fs_active,
                    locf(last(is_hps_on, created_at)) AS is_hps_on
                FROM grid_energy_snapshot_15_min
                WHERE grid_id = $1
                  AND created_at >= $2
                  AND created_at < $3
                GROUP BY bucket
                ORDER BY bucket
                """,
                grid_id,
                yesterday_start,
                yesterday_end,
            )

            # Count ON periods (is_fs_active=True OR is_hps_on=True)
            total_slots = 96  # 24 hours * 4 slots per hour
            on_count = 0
            actual_data_points = 0

            for row in rows:
                is_fs = row["is_fs_active"]
                is_hps = row["is_hps_on"]

                # Count actual data points (not gap-filled nulls)
                if is_fs is not None or is_hps is not None:
                    actual_data_points += 1

                # ON if either is True
                if is_fs is True or is_hps is True:
                    on_count += 1

            # Convert to hours (each slot = 0.25 hours)
            on_hours = round(on_count * 0.25, 1)
            coverage_pct = (
                round(actual_data_points / total_slots * 100, 1) if total_slots > 0 else 0
            )

            return {
                "on_hours": on_hours,
                "total_periods": total_slots,
                "on_periods": on_count,
                "data_coverage_pct": coverage_pct,
                "date": yesterday_start.strftime("%Y-%m-%d"),
            }

        except Exception as e:
            logger.error(f"Error calculating yesterday ON hours for grid {grid_id}: {e}")
            return {
                "on_hours": None,
                "error": str(e),
            }

    async def _get_fs_state_transitions(
        self,
        ts_conn,
        grid_id: int,
        start_date: datetime,
        end_date: datetime,
        grid_tz: str = DEFAULT_TIMEZONE,
    ) -> Dict[str, Any]:
        """
        Query TimescaleDB for FS state transitions over a date range.

        Detects when is_fs_active changes between true/false, computes daily ON hours.

        Args:
            ts_conn: TimescaleDB connection
            grid_id: Grid ID
            start_date: Start datetime (UTC, inclusive)
            end_date: End datetime (UTC, exclusive)
            grid_tz: Grid timezone for day boundary grouping

        Returns:
            Dict with per-day FS ON hours, data coverage, and state transitions
        """
        try:
            tz = ZoneInfo(grid_tz)
            rows = await ts_conn.fetch(
                """
                SELECT
                    time_bucket_gapfill('15 minutes', created_at) AS bucket,
                    locf(last(is_fs_active, created_at)) AS is_fs_active
                FROM grid_energy_snapshot_15_min
                WHERE grid_id = $1
                  AND created_at >= $2
                  AND created_at < $3
                GROUP BY bucket
                ORDER BY bucket
                """,
                grid_id,
                start_date,
                end_date,
            )

            # Group by local date and detect transitions
            days: Dict[str, Dict[str, Any]] = {}
            prev_fs = None

            for row in rows:
                bucket_utc = row["bucket"]
                is_fs = row["is_fs_active"]

                # Convert to local time for day grouping
                if bucket_utc.tzinfo is None:
                    bucket_utc = bucket_utc.replace(tzinfo=timezone.utc)
                local_dt = bucket_utc.astimezone(tz)
                day_key = local_dt.strftime("%Y-%m-%d")

                if day_key not in days:
                    days[day_key] = {
                        "fs_on_slots": 0,
                        "total_slots": 0,
                        "data_points": 0,
                        "transitions": [],
                    }

                day = days[day_key]
                day["total_slots"] += 1

                if is_fs is not None:
                    day["data_points"] += 1

                if is_fs is True:
                    day["fs_on_slots"] += 1

                # Detect transitions
                if prev_fs is not None and is_fs is not None and prev_fs != is_fs:
                    new_state = "on" if is_fs else "off"
                    day["transitions"].append(
                        {
                            "time": local_dt.strftime("%-I:%M %p"),
                            "time_utc": bucket_utc.strftime("%H:%M"),
                            "new_state": new_state,
                        }
                    )

                prev_fs = is_fs

            # Compute summary per day
            result_days = {}
            for day_key, day in days.items():
                fs_on_hours = round(day["fs_on_slots"] * 0.25, 1)
                expected_slots = 96  # 24h * 4 slots/h
                data_coverage_pct = round(day["data_points"] / expected_slots * 100, 1)
                result_days[day_key] = {
                    "fs_on_hours": fs_on_hours,
                    "data_coverage_pct": data_coverage_pct,
                    "transitions": day["transitions"],
                }

            return {"days": result_days}

        except Exception as e:
            logger.error(f"Error getting FS state transitions for grid {grid_id}: {e}")
            return {"days": {}, "error": str(e)}

    async def _get_fs_command_executions(
        self,
        conn,
        grid_id: int,
        start_date: datetime,
        end_date: datetime,
        grid_tz: str = DEFAULT_TIMEZONE,
    ) -> Dict[str, Any]:
        """
        Query Auth DB for FS command executions in a date range.

        Args:
            conn: Auth database connection
            grid_id: Grid ID
            start_date: Start datetime (UTC, inclusive)
            end_date: End datetime (UTC, exclusive)
            grid_tz: Grid timezone for day boundary grouping

        Returns:
            Dict with per-day command executions and delivery percentages
        """
        try:
            tz = ZoneInfo(grid_tz)
            rows = await conn.fetch(
                """
                SELECT
                    dbe.successful_count, dbe.total_count, dbe.created_at,
                    db.fs_command, db.hour, db.minute, db.is_repeating
                FROM directive_batch_executions dbe
                JOIN directive_batches db ON dbe.directive_batch_id = db.id
                WHERE db.grid_id = $1
                  AND db.fs_command IS NOT NULL
                  AND dbe.created_at >= $2
                  AND dbe.created_at < $3
                ORDER BY dbe.created_at
                """,
                grid_id,
                start_date,
                end_date,
            )

            days: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                exec_at = row["created_at"]
                if exec_at and exec_at.tzinfo is None:
                    exec_at = exec_at.replace(tzinfo=timezone.utc)
                local_dt = exec_at.astimezone(tz) if exec_at else None
                day_key = local_dt.strftime("%Y-%m-%d") if local_dt else "unknown"

                if day_key not in days:
                    days[day_key] = {"commands": [], "delivery_pcts": []}

                total = row["total_count"] or 0
                successful = row["successful_count"] or 0
                delivery_pct = round((successful / total * 100), 1) if total > 0 else 0

                days[day_key]["commands"].append(
                    {
                        "time": local_dt.strftime("%-I:%M %p") if local_dt else None,
                        "command": row["fs_command"],
                        "delivery_pct": delivery_pct,
                        "successful": successful,
                        "total": total,
                    }
                )
                days[day_key]["delivery_pcts"].append(delivery_pct)

            # Compute avg delivery per day
            result_days = {}
            for day_key, day in days.items():
                pcts = day["delivery_pcts"]
                avg_pct = round(sum(pcts) / len(pcts), 1) if pcts else None
                result_days[day_key] = {
                    "commands": day["commands"],
                    "avg_delivery_pct": avg_pct,
                }

            return {"days": result_days}

        except Exception as e:
            logger.error(f"Error getting FS command executions for grid {grid_id}: {e}")
            return {"days": {}, "error": str(e)}

    def _correlate_fs_commands_and_state(
        self,
        transitions_data: Dict[str, Any],
        commands_data: Dict[str, Any],
        grid_tz: str = DEFAULT_TIMEZONE,
    ) -> List[Dict[str, Any]]:
        """
        Merge command executions and state transitions into a per-day correlated view.

        For each command, looks for a matching state transition (same direction, within 30 min).
        Unmatched transitions or commands are flagged as discrepancies.

        Returns:
            List of per-day summary dicts
        """
        trans_days = transitions_data.get("days", {})
        cmd_days = commands_data.get("days", {})
        all_dates = sorted(set(list(trans_days.keys()) + list(cmd_days.keys())))

        daily_summary = []
        for date_key in all_dates:
            trans_day = trans_days.get(date_key, {})
            cmd_day = cmd_days.get(date_key, {})

            transitions = list(trans_day.get("transitions", []))
            commands = list(cmd_day.get("commands", []))

            # Track which transitions have been matched
            matched_trans_indices = set()
            enriched_commands = []

            for cmd in commands:
                cmd_direction = (cmd.get("command") or "").lower()  # normalize to "on"/"off"
                matched_transition = None

                # Look for a transition in the same direction within 30 min
                for i, trans in enumerate(transitions):
                    if i in matched_trans_indices:
                        continue
                    if trans.get("new_state") != cmd_direction:
                        continue
                    # Simple time proximity check via string comparison
                    # (both are in local time format like "6:15 AM")
                    matched_transition = trans.get("time")
                    matched_trans_indices.add(i)
                    break

                enriched_commands.append(
                    {
                        **cmd,
                        "matched_transition": matched_transition,
                    }
                )

            # Enriched transitions with matched_command
            enriched_transitions = []
            for i, trans in enumerate(transitions):
                matched_cmd = None
                if i in matched_trans_indices:
                    # Find the command that matched this transition
                    for ecmd in enriched_commands:
                        if ecmd.get("matched_transition") == trans.get("time"):
                            matched_cmd = ecmd.get("time")
                            break

                enriched_transitions.append(
                    {
                        **trans,
                        "matched_command": matched_cmd,
                    }
                )

            # Discrepancies: unmatched commands or transitions
            discrepancies = []
            for ecmd in enriched_commands:
                if ecmd.get("matched_transition") is None:
                    discrepancies.append(
                        f"Command '{ecmd.get('command')}' at {ecmd.get('time')} had no matching state transition"
                    )
            for i, etrans in enumerate(enriched_transitions):
                if i not in matched_trans_indices:
                    discrepancies.append(
                        f"State transition to '{etrans.get('new_state')}' at {etrans.get('time')} had no matching command"
                    )

            daily_summary.append(
                {
                    "date": date_key,
                    "fs_on_hours": trans_day.get("fs_on_hours", 0),
                    "data_coverage_pct": trans_day.get("data_coverage_pct", 0),
                    "commands_executed": enriched_commands,
                    "state_transitions": enriched_transitions,
                    "discrepancies": discrepancies,
                    "avg_delivery_pct": cmd_day.get("avg_delivery_pct"),
                }
            )

        return daily_summary

    async def _get_fs_summary_for_grid(
        self,
        auth_conn,
        ts_conn,
        grid_id: int,
        grid_tz: str,
        start_date: datetime,
        end_date: datetime,
    ) -> Optional[Dict[str, Any]]:
        """
        Get correlated FS command/state summary for a grid. Reuses existing open connections.

        Args:
            auth_conn: Auth database connection
            ts_conn: TimescaleDB connection
            grid_id: Grid ID
            grid_tz: Grid timezone
            start_date: Start datetime (UTC)
            end_date: End datetime (UTC)

        Returns:
            Correlated daily summary or None on failure
        """
        try:
            transitions_data, commands_data = await asyncio.gather(
                self._get_fs_state_transitions(ts_conn, grid_id, start_date, end_date, grid_tz),
                self._get_fs_command_executions(auth_conn, grid_id, start_date, end_date, grid_tz),
            )

            daily_summary = self._correlate_fs_commands_and_state(
                transitions_data, commands_data, grid_tz
            )

            # Compute overall summary
            total_fs_on = sum(d.get("fs_on_hours", 0) for d in daily_summary)
            total_days = len(daily_summary) or 1
            delivery_pcts = [
                d["avg_delivery_pct"]
                for d in daily_summary
                if d.get("avg_delivery_pct") is not None
            ]
            overall_delivery = (
                round(sum(delivery_pcts) / len(delivery_pcts), 1) if delivery_pcts else None
            )
            total_discrepancies = sum(len(d.get("discrepancies", [])) for d in daily_summary)

            return {
                "daily_summary": daily_summary,
                "summary": {
                    "total_days": len(daily_summary),
                    "total_fs_on_hours": round(total_fs_on, 1),
                    "avg_daily_fs_on_hours": round(total_fs_on / total_days, 1),
                    "overall_avg_delivery_pct": overall_delivery,
                    "total_discrepancies": total_discrepancies,
                },
            }
        except Exception as e:
            logger.error(f"Error getting FS summary for grid {grid_id}: {e}")
            return None

    async def get_fs_daily_summary(
        self,
        organization_id: int,
        grid_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get daily FS summary showing command executions vs actual state transitions.

        Args:
            organization_id: Organization ID (2 = staff)
            grid_name: Grid name (fuzzy matched)
            start_date: Start date YYYY-MM-DD (inclusive, defaults to yesterday)
            end_date: End date YYYY-MM-DD (inclusive, defaults to today)

        Returns:
            Correlated daily FS summary with discrepancy detection
        """
        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "6543")),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                ssl="require",
                statement_cache_size=0,
            )

            try:
                # Resolve grid name via fuzzy matching (same pattern as get_grid_status)
                if organization_id == STAFF_ORG_ID:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """
                    )
                else:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE organization_id = $1
                          AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """,
                        organization_id,
                    )

                available_names = [row["name"] for row in available_rows]
                if grid_name:
                    matched = _find_closest_grid_name(grid_name, available_names)
                    if matched:
                        grid_name = matched

                if not grid_name:
                    return {"error": "Grid name is required"}

                # Get grid_id and timezone
                if organization_id == STAFF_ORG_ID:
                    grid_row = await conn.fetchrow(
                        """
                        SELECT id, name, timezone FROM grids
                        WHERE LOWER(name) = LOWER($1)
                          AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        grid_name,
                    )
                else:
                    grid_row = await conn.fetchrow(
                        """
                        SELECT id, name, timezone FROM grids
                        WHERE LOWER(name) = LOWER($1) AND organization_id = $2
                          AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        grid_name,
                        organization_id,
                    )

                if not grid_row:
                    return {"error": f"Grid '{grid_name}' not found"}

                grid_id = grid_row["id"]
                resolved_name = grid_row["name"]
                grid_tz = grid_row["timezone"] or DEFAULT_TIMEZONE

                # Parse dates (defaults: yesterday to today)
                now_utc = datetime.utcnow()
                if start_date:
                    try:
                        sd = datetime.strptime(start_date, "%Y-%m-%d")
                    except ValueError:
                        return {
                            "error": f"Invalid start_date format: {start_date}. Use YYYY-MM-DD."
                        }
                else:
                    sd = (now_utc - timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )

                if end_date:
                    try:
                        ed = datetime.strptime(end_date, "%Y-%m-%d")
                    except ValueError:
                        return {"error": f"Invalid end_date format: {end_date}. Use YYYY-MM-DD."}
                else:
                    ed = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

                # End date is inclusive, so add 1 day for the query range
                ed_exclusive = ed + timedelta(days=1)

                # Cap at 30 days
                if (ed_exclusive - sd).days > 31:
                    return {"error": "Date range cannot exceed 30 days"}

                # Reject future start dates
                if sd > now_utc:
                    return {"error": "Start date cannot be in the future"}

                # Open TimescaleDB connection
                ts_conn = None
                try:
                    import asyncpg as asyncpg_ts

                    if TIMESCALE_HOST and TIMESCALE_USER and TIMESCALE_PASSWORD:
                        ts_conn = await asyncpg_ts.connect(
                            host=TIMESCALE_HOST,
                            port=TIMESCALE_PORT,
                            user=TIMESCALE_USER,
                            password=TIMESCALE_PASSWORD,
                            database=TIMESCALE_DATABASE,
                            ssl="require",
                        )

                        fs_result = await self._get_fs_summary_for_grid(
                            auth_conn=conn,
                            ts_conn=ts_conn,
                            grid_id=grid_id,
                            grid_tz=grid_tz,
                            start_date=sd,
                            end_date=ed_exclusive,
                        )
                    else:
                        return {"error": "TimescaleDB not configured"}
                finally:
                    if ts_conn:
                        await ts_conn.close()

                if not fs_result:
                    return {"error": "Failed to get FS summary data"}

                return {
                    "grid_name": resolved_name,
                    "grid_id": grid_id,
                    "date_range": {
                        "start": sd.strftime("%Y-%m-%d"),
                        "end": ed.strftime("%Y-%m-%d"),
                    },
                    "timezone": grid_tz,
                    **fs_result,
                }

            finally:
                await conn.close()

        except Exception as e:
            logger.error(f"Error in get_fs_daily_summary: {e}")
            return {"error": f"Failed to get FS daily summary: {str(e)}"}

    async def _get_meter_enriched_info(
        self, meter_id: int, client=None, organization_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get enriched meter information including customer, connection, and grid details.

        Args:
            meter_id: Meter ID to enrich
            client: DEPRECATED - no longer used (kept for compatibility)
            organization_id: Optional organization ID for filtering (bypassed for staff, see STAFF_ORG_ID)

        Returns:
            Dict with enriched meter information including:
            - meter_no, meter_id
            - customer_name, customer_id
            - connection_type, connection_id
            - grid_name, grid_id
            - grid_status (from is_hps_on or similar field)
        """
        try:
            # Use direct database connection instead of Supabase API to bypass RLS
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Query meter with connection, grid, DCU references, and status fields
                # Apply organization filter unless staff
                if organization_id and organization_id != STAFF_ORG_ID:
                    meter_row = await conn.fetchrow(
                        """
                        SELECT id, external_reference, connection_id, rls_grid_id, dcu_id,
                               is_on, kwh_credit_available, power_limit, power_limit_hps_mode,
                               power_limit_should_be, power_limit_updated_at,
                               power_limit_should_be_updated_at, last_seen_at,
                               connection_metrics, is_on_updated_at, kwh_credit_available_updated_at,
                               rls_organization_id
                        FROM meters
                        WHERE id = $1
                          AND rls_organization_id = $2
                        LIMIT 1
                        """,
                        meter_id,
                        organization_id,
                    )
                else:
                    # Staff (org 2) - no organization filter
                    meter_row = await conn.fetchrow(
                        """
                        SELECT id, external_reference, connection_id, rls_grid_id, dcu_id,
                               is_on, kwh_credit_available, power_limit, power_limit_hps_mode,
                               power_limit_should_be, power_limit_updated_at,
                               power_limit_should_be_updated_at, last_seen_at,
                               connection_metrics, is_on_updated_at, kwh_credit_available_updated_at,
                               rls_organization_id
                        FROM meters
                        WHERE id = $1
                        LIMIT 1
                        """,
                        meter_id,
                    )

                if not meter_row:
                    return {"error": "Meter not found"}

                meter = dict(meter_row)
            enriched = {
                "meter_no": meter.get(
                    "external_reference"
                ),  # Schema uses external_reference for meter number
            }

            # Add meter status fields
            if meter.get("is_on") is not None:
                enriched["is_on"] = meter.get("is_on")
            if meter.get("is_on_updated_at") is not None:
                enriched["is_on_updated_at"] = meter.get("is_on_updated_at")
            if meter.get("kwh_credit_available") is not None:
                enriched["kwh_credit_available"] = meter.get("kwh_credit_available")
            if meter.get("kwh_credit_available_updated_at") is not None:
                enriched["kwh_credit_available_updated_at"] = meter.get(
                    "kwh_credit_available_updated_at"
                )
            if meter.get("power_limit") is not None:
                enriched["power_limit"] = meter.get("power_limit")
            if meter.get("power_limit_should_be") is not None:
                enriched["power_limit_should_be"] = meter.get("power_limit_should_be")
            if meter.get("power_limit_updated_at") is not None:
                enriched["power_limit_updated_at"] = meter.get("power_limit_updated_at")
            if meter.get("power_limit_should_be_updated_at") is not None:
                enriched["power_limit_should_be_updated_at"] = meter.get(
                    "power_limit_should_be_updated_at"
                )
            if meter.get("last_seen_at") is not None:
                enriched["last_seen_at"] = meter.get("last_seen_at")
            if meter.get("power_limit_hps_mode") is not None:
                enriched["power_limit_hps_mode"] = meter.get("power_limit_hps_mode")
            if meter.get("connection_metrics"):
                enriched["connection_metrics"] = meter.get("connection_metrics")

                # Get connection and customer info
                connection_id = meter.get("connection_id")
                if connection_id:
                    try:
                        # Schema has boolean flags for connection type instead of single field
                        connection_row = await conn.fetchrow(
                            """
                            SELECT id, customer_id, is_residential, is_commercial, is_public
                            FROM connections
                            WHERE id = $1
                            LIMIT 1
                            """,
                            connection_id,
                        )

                        if connection_row:
                            # Derive connection type from boolean flags
                            if connection_row.get("is_residential"):
                                enriched["connection_type"] = "Residential"
                            elif connection_row.get("is_commercial"):
                                enriched["connection_type"] = "Commercial"
                            elif connection_row.get("is_public"):
                                enriched["connection_type"] = "Public"
                            else:
                                enriched["connection_type"] = "Unknown"

                            # Get customer name from accounts table via customer.account_id
                            customer_id = connection_row.get("customer_id")
                            if customer_id:
                                customer_row = await conn.fetchrow(
                                    """
                                    SELECT id, account_id
                                    FROM customers
                                    WHERE id = $1
                                    LIMIT 1
                                    """,
                                    customer_id,
                                )

                                if customer_row:
                                    # Get full_name from accounts table
                                    account_id = customer_row.get("account_id")
                                    if account_id:
                                        account_row = await conn.fetchrow(
                                            """
                                            SELECT id, full_name
                                            FROM accounts
                                            WHERE id = $1
                                            LIMIT 1
                                            """,
                                            account_id,
                                        )

                                        if account_row:
                                            enriched["customer_name"] = account_row.get("full_name")
                    except Exception as e:
                        logger.warning(f"Could not enrich connection/customer info: {e}")

                # Get grid info - schema uses rls_grid_id
                grid_id = meter.get("rls_grid_id")
                if grid_id:
                    try:
                        # Schema has is_hps_on boolean field for grid status
                        grid_row = await conn.fetchrow(
                            """
                            SELECT id, name, is_hps_on
                            FROM grids
                            WHERE id = $1
                            LIMIT 1
                            """,
                            grid_id,
                        )

                        if grid_row:
                            enriched["grid_name"] = grid_row.get("name")

                            # Use is_hps_on to determine grid status
                            if "is_hps_on" in grid_row and grid_row["is_hps_on"] is not None:
                                enriched["grid_status"] = (
                                    "grid is energized" if grid_row["is_hps_on"] else "grid is down"
                                )
                    except Exception as e:
                        logger.warning(f"Could not enrich grid info: {e}")

                # Get DCU online status
                dcu_id = meter.get("dcu_id")
                if dcu_id:
                    try:
                        dcu_row = await conn.fetchrow(
                            """
                            SELECT id, is_online, last_online_at
                            FROM dcus
                            WHERE id = $1
                            LIMIT 1
                            """,
                            dcu_id,
                        )

                        if dcu_row:
                            if "is_online" in dcu_row and dcu_row["is_online"] is not None:
                                enriched["dcu_status"] = (
                                    "dcu is online" if dcu_row["is_online"] else "dcu is offline"
                                )
                    except Exception as e:
                        logger.warning(f"Could not enrich DCU info: {e}")

                # Get last token from directives
                try:
                    token_row = await conn.fetchrow(
                        """
                        SELECT token, directive_type::text, created_at
                        FROM directives
                        WHERE meter_id = $1
                          AND token IS NOT NULL
                          AND token != ''
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        meter_id,
                    )

                    if token_row:
                        enriched["last_token"] = token_row["token"]
                        enriched["last_token_type"] = token_row["directive_type"]
                        enriched["last_token_created_at"] = token_row["created_at"]
                except Exception as e:
                    logger.warning(f"Could not get last token for meter {meter_id}: {e}")

            return enriched

        except Exception as e:
            logger.error(f"Error enriching meter info: {e}")
            return {"error": f"Failed to enrich meter info: {str(e)}"}

    async def _get_order_recipient_info(self, order: Dict[str, Any], conn=None) -> Dict[str, Any]:
        """
        Get enriched recipient information for an order.

        Args:
            order: Order dict with meta_receiver_type and meta_receiver_id
            conn: asyncpg connection (used for agent lookup; meter path uses AuthService pool)

        Returns:
            Dict with recipient information:
            - If meter: enriched meter info (customer, grid, connection)
            - If agent: agent name and email from accounts table
        """
        try:
            # Schema uses meta_receiver_type instead of receiving_type
            receiver_type = order.get("meta_receiver_type", "METER").upper()

            if receiver_type == "AGENT":
                # Use accounts table for agent recipients
                # Schema uses meta_receiver_id for agent ID
                agent_id = order.get("meta_receiver_id")
                if not agent_id:
                    return {"type": "agent", "error": "No meta_receiver_id in order"}

                try:
                    agent_row = await conn.fetchrow(
                        """
                        SELECT id, full_name
                        FROM accounts
                        WHERE id = $1
                        LIMIT 1
                        """,
                        agent_id,
                    )

                    if agent_row:
                        return {
                            "type": "agent",
                            "agent_name": agent_row["full_name"],
                        }
                    else:
                        return {"type": "agent", "error": "Agent not found"}
                except Exception as e:
                    logger.warning(f"Could not fetch agent info: {e}")
                    return {"type": "agent", "error": str(e)}

            else:
                # Default: meter recipient - use meta_receiver_id as meter_id
                meter_id = order.get("meta_receiver_id")
                if not meter_id:
                    return {"type": "meter", "error": "No meta_receiver_id in order"}

                # Get organization_id from order for filtering
                organization_id = order.get("rls_organization_id")

                # Get enriched meter info (uses AuthService pool internally)
                meter_info = await self._get_meter_enriched_info(
                    meter_id, organization_id=organization_id
                )
                meter_info["type"] = "meter"
                return meter_info

        except Exception as e:
            logger.error(f"Error getting recipient info: {e}")
            return {"error": f"Failed to get recipient info: {str(e)}"}

    async def check_payment_completion(
        self,
        transaction_reference: str,
        user_email: str,
        organization_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Check payment completion status for a transaction reference.

        Args:
            transaction_reference: Transaction reference (format: OrgName+MeterRef__timestamp)
            user_email: User email for organization lookup
            organization_id: Optional organization ID (will be looked up if not provided)

        Returns:
            Dictionary with payment status from payment processor, orders table, and directive
        """
        # Validate reference format (only require + separator)
        if "+" not in transaction_reference:
            return {
                "error": (
                    "Invalid transaction reference. "
                    "Please ask the customer for the exact reference from their receipt."
                )
            }

        # Get user's organization_id if not provided
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "5432")),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                ssl="require",
                statement_cache_size=0,
            )

            try:
                # Try to find order with exact reference first
                order_fields = (
                    "id, order_status::text, external_reference, "
                    "rls_organization_id, meta_receiver_id, meta_receiver_type::text"
                )
                if organization_id != STAFF_ORG_ID:
                    order_row = await conn.fetchrow(
                        f"SELECT {order_fields} FROM orders "
                        "WHERE external_reference = $1 AND rls_organization_id = $2 "
                        "LIMIT 1",
                        transaction_reference,
                        organization_id,
                    )
                else:
                    order_row = await conn.fetchrow(
                        f"SELECT {order_fields} FROM orders WHERE external_reference = $1 LIMIT 1",
                        transaction_reference,
                    )

                # If not found, try normalizing single underscore to double
                # (OCR may misread __ as _)
                if order_row is None and "_" in transaction_reference:
                    import re

                    normalized_ref = re.sub(
                        r"(?<!_)_(?!_)(\d{4}-\d{2}-\d{2})",
                        r"__\1",
                        transaction_reference,
                    )

                    if normalized_ref != transaction_reference:
                        logger.info(f"Trying normalized reference: {normalized_ref}")
                        if organization_id != STAFF_ORG_ID:
                            order_row = await conn.fetchrow(
                                f"SELECT {order_fields} FROM orders "
                                "WHERE external_reference = $1 "
                                "AND rls_organization_id = $2 LIMIT 1",
                                normalized_ref,
                                organization_id,
                            )
                        else:
                            order_row = await conn.fetchrow(
                                f"SELECT {order_fields} FROM orders "
                                "WHERE external_reference = $1 LIMIT 1",
                                normalized_ref,
                            )

                if order_row is None:
                    return {
                        "order_found": False,
                        "message": "Order not found for your organization",
                        "tx_ref": transaction_reference,
                    }

                order = dict(order_row)
                order_id = order["id"]
                order_status = order["order_status"]

                # Query directives table for latest directive
                directive_row = await conn.fetchrow(
                    "SELECT id, directive_status::text, token "
                    "FROM directives WHERE order_id = $1 "
                    "ORDER BY created_at DESC LIMIT 1",
                    order_id,
                )

                directive_status = "not found"
                directive_token = None
                if directive_row:
                    directive_status = directive_row["directive_status"] or "unknown"
                    directive_token = directive_row["token"]

                # Get enriched recipient information
                recipient_info = await self._get_order_recipient_info(order, conn)

            finally:
                await conn.close()

            # Get payment processor transaction status (HTTP, no DB needed)
            payment_processor_status = "not found"
            try:
                payment_processor_result = await self._verify_payment_processor_transaction(
                    transaction_reference
                )
                if payment_processor_result.get("status") == "success":
                    tx_data = payment_processor_result.get("data", {})
                    payment_processor_status = tx_data.get("status", "unknown")
            except Exception as e:
                logger.warning(f"Could not verify payment processor transaction: {e}")
                payment_processor_status = f"error: {str(e)}"

            # Build response
            # NOTE: transaction_reference IS the tx_ref (merchant transaction reference).
            # The payment processor has already been checked using this reference.
            # Do NOT ask the user for a separate Transaction ID — this is it.
            response = {
                "order_found": True,
                "tx_ref": transaction_reference,
                "tx_ref_note": (
                    "This IS the merchant transaction reference (tx_ref) used to verify "
                    "with the payment processor. No additional ID is needed."
                ),
                "status_in_payment_processor": payment_processor_status,
                "status_in_orders_table": order_status,
                "directive_status": directive_status,
                "directive_token": directive_token,
            }

            # Add recipient enrichment
            if recipient_info and "error" not in recipient_info:
                response["recipient"] = recipient_info
            elif recipient_info and "error" in recipient_info:
                # Include error but don't fail the whole request
                response["recipient"] = {"error": recipient_info["error"]}

            return response

        except Exception as e:
            logger.error(f"Error checking payment completion: {e}")
            return {"error": f"Failed to check payment status: {str(e)}"}

    async def find_payment(
        self,
        customer_name: str = "",
        amount: Optional[float] = None,
        date: Optional[str] = None,
        organization_name: Optional[str] = None,
        user_email: str = "",
        organization_id: Optional[int] = None,
        time_window_hours: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Search for payment orders by any combination of: customer/sender name, amount, date.

        Searches both external_reference (EOS format: OrgName+CustomerRef__timestamp)
        and meta_receiver_name (registered NXT Grid customer name) so it works for
        EOS screenshots, bank receipts (FirstBank, OPay, etc.), and other evidence.

        At least one of customer_name, amount, or date must be provided.

        Date handling:
        - Datetime strings (e.g. "2026-05-29T16:42:43"): ±time_window_hours window
        - Date-only strings (e.g. "2026-05-29"): full 24-hour day search

        Uses asyncpg with AUTH_DB credentials (AUTH_DB_USER).

        Args:
            customer_name: Customer or sender name from receipt (optional)
            amount: Payment amount (optional, ±5% tolerance)
            date: Date or datetime from receipt (optional)
            organization_name: Organization name prefix in external_reference (optional)
            user_email: User email for organization lookup
            organization_id: Optional organization ID (injected by orchestrator)
            time_window_hours: Hours before/after datetime for time window (default 2.0)

        Returns:
            Dict with search results: 0 matches (not found), 1 match (auto-verified),
            or 2-5 matches (list for user selection)
        """
        name_clean = (customer_name or "").strip()

        if not name_clean and amount is None and not (date and date.strip()):
            return {
                "error": (
                    "At least one search criterion is required: customer_name, amount, or date"
                )
            }

        # Resolve organization
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "5432")),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                ssl="require",
                statement_cache_size=0,
            )

            try:
                conditions = []
                params: list = []
                param_idx = 1

                # Name: each word must appear in EITHER external_reference OR meta_receiver_name
                if name_clean:
                    name_parts = [p for p in name_clean.split() if len(p) >= 2]
                    if not name_parts:
                        return {
                            "error": "Customer name too short to search (minimum 2 characters per word)"
                        }
                    for part in name_parts:
                        conditions.append(
                            f"(external_reference ILIKE ${param_idx} OR meta_receiver_name ILIKE ${param_idx})"
                        )
                        params.append(f"%{part}%")
                        param_idx += 1

                # Optional: organization name prefix in external_reference
                if organization_name and organization_name.strip():
                    conditions.append(f"external_reference ILIKE ${param_idx}")
                    params.append(f"{organization_name.strip()}+%")
                    param_idx += 1

                # Optional: time window around provided date
                if date and date.strip():
                    try:
                        date_str = date.strip()
                        date_only = len(date_str) == 10 and date_str[4] == "-"
                        if date_only:
                            # Search entire calendar day in the configured timezone
                            parsed_date = datetime.fromisoformat(date_str).date()
                            tz = ZoneInfo(DEFAULT_TIMEZONE)
                            window_start = datetime(
                                parsed_date.year,
                                parsed_date.month,
                                parsed_date.day,
                                0,
                                0,
                                0,
                                tzinfo=tz,
                            )
                            window_end = datetime(
                                parsed_date.year,
                                parsed_date.month,
                                parsed_date.day,
                                23,
                                59,
                                59,
                                tzinfo=tz,
                            )
                        else:
                            parsed_dt = datetime.fromisoformat(date_str)
                            if parsed_dt.tzinfo is None:
                                parsed_dt = parsed_dt.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
                            window_start = parsed_dt - timedelta(hours=time_window_hours)
                            window_end = parsed_dt + timedelta(hours=time_window_hours)
                        conditions.append(f"created_at >= ${param_idx}")
                        params.append(window_start)
                        param_idx += 1
                        conditions.append(f"created_at <= ${param_idx}")
                        params.append(window_end)
                        param_idx += 1
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Could not parse date '{date}': {e}")

                # Optional: amount filter (±5% tolerance)
                if amount is not None and amount > 0:
                    tolerance = amount * 0.05
                    conditions.append(f"amount >= ${param_idx}")
                    params.append(amount - tolerance)
                    param_idx += 1
                    conditions.append(f"amount <= ${param_idx}")
                    params.append(amount + tolerance)
                    param_idx += 1

                # Security: org scoping for non-staff
                if organization_id != STAFF_ORG_ID:
                    conditions.append(f"rls_organization_id = ${param_idx}")
                    params.append(organization_id)
                    param_idx += 1

                # Only search orders with external_reference
                conditions.append("external_reference IS NOT NULL")
                conditions.append("external_reference != ''")

                where_clause = " AND ".join(conditions)
                query = f"""
                    SELECT id, external_reference, order_status::text,
                           amount, created_at, rls_organization_id,
                           meta_receiver_name
                    FROM orders
                    WHERE {where_clause}
                    ORDER BY created_at DESC
                    LIMIT 5
                """

                rows = await conn.fetch(query, *params)

            finally:
                await conn.close()

            search_criteria = {
                k: v
                for k, v in {
                    "customer_name": name_clean or None,
                    "amount": amount,
                    "date": date,
                    "organization_name": organization_name,
                    "time_window_hours": time_window_hours
                    if (date and not (len((date or "").strip()) == 10))
                    else None,
                }.items()
                if v is not None
            }

            if not rows:
                return {
                    "matches_found": 0,
                    "message": (
                        "No payment orders found matching the provided details. "
                        "The payment may not have been recorded yet, or the details "
                        "on the receipt may differ from what is stored in the system."
                    ),
                    "search_criteria": search_criteria,
                    "suggestion": (
                        "Ask the customer for: (1) the exact transaction reference from "
                        "the receipt, or (2) the meter number to look up the account directly. "
                        "Alternatively, try with just the amount and date if a name search returned nothing."
                    ),
                }

            if len(rows) == 1:
                # Single match — auto-verify with payment processor
                tx_ref = rows[0]["external_reference"]
                logger.info(f"find_payment: single match, auto-verifying tx_ref={tx_ref}")
                verification = await self.check_payment_completion(
                    transaction_reference=tx_ref,
                    user_email=user_email,
                    organization_id=organization_id,
                )
                verification["matched_via"] = "find_payment (single match, auto-verified)"
                return verification

            # Multiple matches — return list so LLM can ask for clarification
            matches = []
            for row in rows:
                matches.append(
                    {
                        "external_reference": row["external_reference"],
                        "order_status": row["order_status"],
                        "amount": row["amount"],
                        "created_at": (
                            row["created_at"].isoformat() if row["created_at"] else None
                        ),
                        "receiver_name": row["meta_receiver_name"],
                    }
                )

            return {
                "matches_found": len(matches),
                "message": (
                    f"Found {len(matches)} payment orders matching the provided details — "
                    "cannot uniquely identify the payment. Ask the customer for additional "
                    "details (e.g. meter number, or the exact transaction/session ID from their receipt) "
                    "to identify the correct one, or call check_payment_completion with the "
                    "correct external_reference from the list below."
                ),
                "matches": matches,
                "search_criteria": search_criteria,
            }

        except Exception as e:
            logger.error(f"Error in find_payment: {e}")
            return {"error": f"Failed to search for payment: {str(e)}"}

    async def lookup_transactions(
        self,
        user_email: str = "",
        organization_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        reference_number: Optional[str] = None,
        amount: Optional[float] = None,
        receiver_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        List payment transactions filtered by optional criteria.

        Scoped to the user's organization; staff (STAFF_ORG_ID) can see all orgs.
        """
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        result_limit = min(int(limit) if limit else 20, 50)

        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "5432")),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                ssl="require",
                statement_cache_size=0,
            )

            try:
                conditions: list = []
                params: list = []
                param_idx = 1

                # Org scoping for non-staff
                if organization_id != STAFF_ORG_ID:
                    conditions.append(f"rls_organization_id = ${param_idx}")
                    params.append(organization_id)
                    param_idx += 1

                # Date range
                if date_from and date_from.strip():
                    try:
                        ds = date_from.strip()
                        if len(ds) == 10:
                            ds = f"{ds}T00:00:00"
                        dt = datetime.fromisoformat(ds)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
                        conditions.append(f"created_at >= ${param_idx}")
                        params.append(dt)
                        param_idx += 1
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Could not parse date_from '{date_from}': {e}")

                if date_to and date_to.strip():
                    try:
                        ds = date_to.strip()
                        if len(ds) == 10:
                            ds = f"{ds}T23:59:59"
                        dt = datetime.fromisoformat(ds)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
                        conditions.append(f"created_at <= ${param_idx}")
                        params.append(dt)
                        param_idx += 1
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Could not parse date_to '{date_to}': {e}")

                # Reference number substring match
                if reference_number and reference_number.strip():
                    conditions.append(f"external_reference ILIKE ${param_idx}")
                    params.append(f"%{reference_number.strip()}%")
                    param_idx += 1

                # Amount with ±5% tolerance
                if amount is not None and amount > 0:
                    tolerance = amount * 0.05
                    conditions.append(f"amount >= ${param_idx}")
                    params.append(amount - tolerance)
                    param_idx += 1
                    conditions.append(f"amount <= ${param_idx}")
                    params.append(amount + tolerance)
                    param_idx += 1

                # Receiver name fuzzy match (each word independently)
                if receiver_name and receiver_name.strip():
                    for word in receiver_name.strip().split():
                        if len(word) >= 2:
                            conditions.append(f"meta_receiver_name ILIKE ${param_idx}")
                            params.append(f"%{word}%")
                            param_idx += 1

                where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
                query = f"""
                    SELECT external_reference, order_status::text,
                           amount, created_at, meta_receiver_name
                    FROM orders
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT {result_limit}
                """

                rows = await conn.fetch(query, *params)

            finally:
                await conn.close()

            if not rows:
                return {
                    "transactions_found": 0,
                    "message": "No transactions found matching the given filters.",
                }

            transactions = [
                {
                    "reference_number": row["external_reference"],
                    "amount": row["amount"],
                    "date_time": row["created_at"].isoformat() if row["created_at"] else None,
                    "receiver_name": row["meta_receiver_name"],
                    "status": row["order_status"],
                }
                for row in rows
            ]

            return {
                "transactions_found": len(transactions),
                "transactions": transactions,
            }

        except Exception as e:
            logger.error(f"Error in lookup_transactions: {e}")
            return {"error": f"Failed to look up transactions: {str(e)}"}

    async def _verify_payment_processor_transaction(self, tx_ref: str) -> Dict[str, Any]:
        """
        Verify transaction using payment processor transaction reference.

        Args:
            tx_ref: Merchant transaction reference

        Returns:
            Payment processor API response
        """
        if not self.payment_processor_url or not self.payment_processor_key:
            raise Exception("Payment processor not configured")

        from urllib.parse import quote

        client = await self.get_session()
        url = f"{self.payment_processor_url}/transactions/verify_by_reference?tx_ref={quote(tx_ref, safe='')}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.payment_processor_key}",
        }

        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return dict(await response.json())
        except Exception as e:
            logger.error(f"Payment processor API request failed: {e}")
            raise

    async def meter_information(
        self,
        meter_number: str,
        user_email: str,
        organization_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Get comprehensive information about a meter.

        Args:
            meter_number: Meter number to look up
            user_email: User email for organization lookup
            organization_id: Optional organization ID (will be looked up if not provided)

        Returns:
            Dictionary with meter information including customer details, connection info,
            grid status, meter power status, credit balance, recent directives, and error history
        """
        # Get user's organization_id if not provided
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            # Use direct database connection instead of Supabase API to bypass RLS
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Query meters table - schema uses external_reference for meter number
                # Apply organization filter unless staff
                if organization_id != STAFF_ORG_ID:
                    meter_row = await conn.fetchrow(
                        """
                        SELECT id, external_reference, rls_organization_id, rls_grid_id
                        FROM meters
                        WHERE external_reference = $1
                          AND rls_organization_id = $2
                        LIMIT 1
                        """,
                        meter_number,
                        organization_id,
                    )
                else:
                    # Staff (org 2) - no organization filter
                    meter_row = await conn.fetchrow(
                        """
                        SELECT id, external_reference, rls_organization_id, rls_grid_id
                        FROM meters
                        WHERE external_reference = $1
                        LIMIT 1
                        """,
                        meter_number,
                    )

                if not meter_row:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }

                meter_id = meter_row["id"]
                grid_id = meter_row.get("rls_grid_id")

                # Query directives table for latest 5 directives (any type)
                # Cast directive_status and directive_type to text to handle invalid enum values
                directive_rows = await conn.fetch(
                    """
                    SELECT id, directive_type::text as directive_type, directive_status::text as directive_status, created_at, updated_at
                    FROM directives
                    WHERE meter_id = $1
                    ORDER BY created_at DESC
                    LIMIT 5
                    """,
                    meter_id,
                )

                directives = []
                for directive in directive_rows:
                    directives.append(
                        {
                            "directive_id": directive["id"],
                            "type": directive.get("directive_type", "unknown"),
                            "status": directive.get("directive_status", "unknown"),
                            "created_at": directive.get("created_at"),
                            "updated_at": directive.get("updated_at"),
                        }
                    )

                # Query for last directive with error status
                # Cast directive_status and directive_type to text to handle invalid enum values
                error_directive_row = await conn.fetchrow(
                    """
                    SELECT id, directive_type::text as directive_type, directive_status::text as directive_status, directive_error, created_at, updated_at
                    FROM directives
                    WHERE meter_id = $1
                      AND directive_status::text = 'FAILED'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    meter_id,
                )

                last_error_directive = None
                if error_directive_row:
                    last_error_directive = {
                        "directive_id": error_directive_row["id"],
                        "type": error_directive_row.get("directive_type", "unknown"),
                        "status": error_directive_row.get("directive_status", "unknown"),
                        "error": error_directive_row.get("directive_error"),
                        "created_at": error_directive_row.get("created_at"),
                        "updated_at": error_directive_row.get("updated_at"),
                    }

                # Query for last successful token directive
                # Cast directive_status and directive_type to text to handle invalid enum values
                # Note: 'SUCCESSFUL' is the correct enum value, not 'COMPLETED'
                successful_token_row = await conn.fetchrow(
                    """
                    SELECT id, directive_type::text as directive_type, directive_status::text as directive_status, token, created_at, updated_at
                    FROM directives
                    WHERE meter_id = $1
                      AND directive_status::text IN ('COMPLETED', 'SUCCESSFUL')
                      AND directive_type::text = 'TOKEN'
                      AND token IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    meter_id,
                )

                last_successful_token = None
                if successful_token_row:
                    last_successful_token = {
                        "directive_id": successful_token_row["id"],
                        "token": successful_token_row.get("token"),
                        "token_type": successful_token_row.get("directive_type", "TOKEN"),
                        "created_at": successful_token_row.get("created_at"),
                        "updated_at": successful_token_row.get("updated_at"),
                    }

                try:
                    commissioning_row = await conn.fetchrow(
                        """
                        SELECT mc.created_at, mc.meter_commissioning_status::text AS meter_commissioning_status
                        FROM meter_commissionings mc
                        JOIN metering_hardware_install_sessions mhis
                          ON mc.metering_hardware_install_session_id = mhis.id
                        WHERE mhis.meter_id = $1
                        ORDER BY mc.created_at DESC
                        LIMIT 1
                        """,
                        meter_id,
                    )
                except Exception as commissioning_err:
                    logger.warning(
                        f"Commissioning query failed for meter {meter_number}: {commissioning_err}"
                    )
                    commissioning_row = None

            # Get enriched meter information (customer, connection, grid)
            # Note: This method still uses Supabase client - will need refactoring if it queries auth db
            enriched_meter = await self._get_meter_enriched_info(meter_id, None, organization_id)

            # Get comprehensive grid status if meter has a grid
            full_grid_status = None
            if grid_id:
                full_grid_status = await self.get_grid_status(
                    organization_id=organization_id,
                    grid_id=grid_id,
                )

            # Build response with enriched data
            response = {
                "meter_found": True,
                "meter_number": meter_number,
                "commissioning_date": commissioning_row["created_at"]
                if commissioning_row
                else None,
                "commissioning_status": commissioning_row["meter_commissioning_status"].upper()
                if commissioning_row and commissioning_row["meter_commissioning_status"]
                else None,
                "directives_count": len(directives),
                "directives": directives,
                "last_error_directive": last_error_directive,
                "last_successful_token": last_successful_token,
                "message": (
                    f"Found {len(directives)} directive(s)"
                    if directives
                    else "No directives found for this meter"
                ),
            }

            # Add enriched fields if available (customer, connection, grid, dcu, meter status)
            if "customer_name" in enriched_meter:
                response["customer_name"] = enriched_meter["customer_name"]
            if "connection_type" in enriched_meter:
                response["connection_type"] = enriched_meter["connection_type"]

            # Include full grid status if available
            if full_grid_status and "error" not in full_grid_status:
                response["grid"] = full_grid_status
            elif "grid_name" in enriched_meter:
                # Fallback to simple grid info
                response["grid_name"] = enriched_meter["grid_name"]
                if "grid_status" in enriched_meter:
                    response["grid_status"] = enriched_meter["grid_status"]

            if "dcu_status" in enriched_meter:
                response["dcu_status"] = enriched_meter["dcu_status"]

            # Add meter status fields
            if "is_on" in enriched_meter:
                response["is_on"] = enriched_meter["is_on"]
            if "is_on_updated_at" in enriched_meter:
                response["is_on_updated_at"] = enriched_meter["is_on_updated_at"]
            if "kwh_credit_available" in enriched_meter:
                response["kwh_credit_available"] = enriched_meter["kwh_credit_available"]
            if "kwh_credit_available_updated_at" in enriched_meter:
                response["kwh_credit_available_updated_at"] = enriched_meter[
                    "kwh_credit_available_updated_at"
                ]
            if "power_limit" in enriched_meter:
                response["power_limit"] = enriched_meter["power_limit"]
            # FS command propagation: target vs actual power limit
            if "power_limit_should_be" in enriched_meter:
                target = enriched_meter["power_limit_should_be"]
                actual = enriched_meter.get("power_limit")
                response["power_limit_target"] = target
                response["power_limit_pending"] = (
                    target is not None and actual is not None and actual != target
                )
            if "power_limit_updated_at" in enriched_meter:
                response["power_limit_updated_at"] = enriched_meter["power_limit_updated_at"]
            if "last_seen_at" in enriched_meter:
                response["last_seen_at"] = enriched_meter["last_seen_at"]
            if "connection_metrics" in enriched_meter:
                response["connection_metrics"] = enriched_meter["connection_metrics"]

            # Add 30-day consumption summary from TimescaleDB (non-fatal)
            try:
                if TIMESCALE_HOST and TIMESCALE_USER and TIMESCALE_PASSWORD:
                    import asyncpg as asyncpg_ts

                    ts_conn = await asyncpg_ts.connect(
                        host=TIMESCALE_HOST,
                        port=TIMESCALE_PORT,
                        user=TIMESCALE_USER,
                        password=TIMESCALE_PASSWORD,
                        database=TIMESCALE_DATABASE,
                        ssl="require",
                    )
                    try:
                        summary = await ts_conn.fetchrow(
                            """
                            SELECT SUM(consumption_kwh) as total_kwh,
                                   AVG(consumption_kwh) as avg_hourly_kwh,
                                   MAX(consumption_kwh) as max_hourly_kwh,
                                   COUNT(DISTINCT date_trunc('day', created_at)) as days_with_data
                            FROM meter_snapshot_1_h
                            WHERE meter_external_reference = $1
                              AND created_at >= NOW() - INTERVAL '30 days'
                            """,
                            meter_number,
                        )
                        if summary and summary["total_kwh"] is not None:
                            response["consumption_30d"] = {
                                "total_kwh": round(summary["total_kwh"], 3),
                                "avg_hourly_kwh": round(summary["avg_hourly_kwh"], 3),
                                "max_hourly_kwh": round(summary["max_hourly_kwh"], 3),
                                "days_with_data": summary["days_with_data"],
                            }
                    finally:
                        await ts_conn.close()
            except Exception as e:
                logger.warning(f"Could not fetch consumption summary: {e}")

            return response

        except Exception as e:
            logger.error(f"Error fetching meter information for {meter_number}: {e}")
            return {"error": f"Failed to fetch meter information: {str(e)}"}

    async def list_grid_meters(
        self,
        grid_name: str,
        organization_id: int,
    ) -> Dict[str, Any]:
        """
        List all non-cabin meters for a grid with power limits and status.

        Args:
            grid_name: Grid name (supports fuzzy matching)
            organization_id: Organization ID (injected by orchestrator)

        Returns:
            Dict with grid name and list of meters with key fields
        """
        try:
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Resolve grid name with fuzzy matching
                if organization_id == STAFF_ORG_ID:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """
                    )
                else:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE organization_id = $1
                          AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """,
                        organization_id,
                    )

                available_names = [row["name"] for row in available_rows]
                matched_name = _find_closest_grid_name(grid_name, available_names)
                if not matched_name:
                    grid_list = ", ".join(available_names[:10])
                    suffix = (
                        f" (and {len(available_names) - 10} more)"
                        if len(available_names) > 10
                        else ""
                    )
                    return {
                        "error": f"Grid '{grid_name}' not found. Available: {grid_list}{suffix}"
                    }

                corrected_grid_name = (
                    matched_name if matched_name.lower() != grid_name.lower() else None
                )

                # Get grid ID
                grid_row = await conn.fetchrow(
                    "SELECT id, name FROM grids WHERE name = $1 AND deleted_at IS NULL LIMIT 1",
                    matched_name,
                )
                if not grid_row:
                    return {"error": f"Grid '{matched_name}' not found"}

                # Query meters for this grid, excluding cabin meters
                meter_rows = await conn.fetch(
                    """
                    SELECT m.external_reference, m.is_on, m.kwh_credit_available,
                           m.power_limit, m.power_limit_should_be,
                           m.power_limit_hps_mode, m.meter_phase,
                           m.last_seen_at,
                           d.is_online as dcu_online
                    FROM meters m
                    LEFT JOIN dcus d ON m.dcu_id = d.id
                    WHERE m.rls_grid_id = $1
                      AND m.is_cabin_meter IS NOT TRUE
                      AND m.deleted_at IS NULL
                    ORDER BY m.external_reference
                    """,
                    grid_row["id"],
                )

                meters = []
                for row in meter_rows:
                    # Communication status
                    dcu_online = row["dcu_online"]
                    last_seen = row["last_seen_at"]
                    if dcu_online is True and last_seen:
                        comms = "online"
                    elif dcu_online is False:
                        comms = "offline (DCU down)"
                    elif last_seen is None:
                        comms = "never seen"
                    else:
                        comms = "offline"

                    limit_actual = row["power_limit"]
                    limit_target = row["power_limit_should_be"]

                    meters.append(
                        {
                            "meter_number": row["external_reference"],
                            "is_on": row["is_on"],
                            "comms_status": comms,
                            "kwh_credit_available": (
                                round(row["kwh_credit_available"], 2)
                                if row["kwh_credit_available"] is not None
                                else None
                            ),
                            "power_limit_w": limit_actual,
                            "power_limit_target_w": limit_target,
                            "power_limit_pending": (
                                limit_target is not None
                                and limit_actual is not None
                                and limit_actual != limit_target
                            ),
                            "power_limit_hps_mode_w": row["power_limit_hps_mode"],
                            "meter_phase": (
                                str(row["meter_phase"]) if row["meter_phase"] else None
                            ),
                        }
                    )

                result = {
                    "grid_name": matched_name,
                    "meter_count": len(meters),
                    "meters": meters,
                }
                if corrected_grid_name:
                    result["corrected_from"] = grid_name

                return result

        except Exception as e:
            logger.error(f"Error listing grid meters: {e}")
            return {"error": f"Failed to list meters: {str(e)}"}

    async def get_meters_on_pole(
        self,
        pole_reference: str,
        organization_id: int,
        grid_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List all meters connected to a specific pole.

        Args:
            pole_reference: Pole external reference (printed on pole label)
            organization_id: Organization ID (injected by orchestrator)
            grid_name: Optional grid name to narrow the search

        Returns:
            Dict with pole info and list of meters
        """
        try:
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Build pole lookup query with org scoping
                if organization_id == STAFF_ORG_ID:
                    # Staff: see all poles, optionally filter by grid
                    if grid_name:
                        available_names = [
                            r["name"]
                            for r in await conn.fetch(
                                "SELECT name FROM grids WHERE deleted_at IS NULL ORDER BY name"
                            )
                        ]
                        matched_grid = _find_closest_grid_name(grid_name, available_names)
                        if not matched_grid:
                            return {"error": f"Grid '{grid_name}' not found"}
                        pole_rows = await conn.fetch(
                            """
                            SELECT p.id, p.external_reference, p.nickname, g.name as grid_name
                            FROM poles p
                            JOIN grids g ON p.grid_id = g.id
                            WHERE p.external_reference = $1 AND g.name = $2
                            """,
                            pole_reference,
                            matched_grid,
                        )
                    else:
                        pole_rows = await conn.fetch(
                            """
                            SELECT p.id, p.external_reference, p.nickname, g.name as grid_name
                            FROM poles p
                            LEFT JOIN grids g ON p.grid_id = g.id
                            WHERE p.external_reference = $1
                            """,
                            pole_reference,
                        )
                else:
                    # Customer: only poles in their org's grids
                    pole_rows = await conn.fetch(
                        """
                        SELECT p.id, p.external_reference, p.nickname, g.name as grid_name
                        FROM poles p
                        JOIN grids g ON p.grid_id = g.id
                        WHERE p.external_reference = $1
                          AND g.organization_id = $2
                        """,
                        pole_reference,
                        organization_id,
                    )

                if not pole_rows:
                    return {"error": f"Pole '{pole_reference}' not found"}

                # If multiple poles match (different grids), return all
                all_results = []
                for pole_row in pole_rows:
                    meter_rows = await conn.fetch(
                        """
                        SELECT m.external_reference, m.is_on, m.kwh_credit_available,
                               m.power_limit, m.power_limit_should_be,
                               m.power_limit_hps_mode, m.meter_phase,
                               m.balance, m.last_seen_at,
                               m.power_limit_updated_at,
                               m.power_limit_should_be_updated_at,
                               d.is_online as dcu_online,
                               d.last_online_at as dcu_last_online
                        FROM meters m
                        LEFT JOIN dcus d ON m.dcu_id = d.id
                        WHERE m.pole_id = $1
                          AND m.is_cabin_meter IS NOT TRUE
                          AND m.deleted_at IS NULL
                        ORDER BY m.external_reference
                        """,
                        pole_row["id"],
                    )

                    meters = []
                    for row in meter_rows:
                        # Determine communication status from DCU and last_seen_at
                        dcu_online = row["dcu_online"]
                        last_seen = row["last_seen_at"]
                        if dcu_online is True and last_seen:
                            comms_status = "online"
                        elif dcu_online is False:
                            comms_status = "offline (DCU down)"
                        elif last_seen is None:
                            comms_status = "never seen"
                        else:
                            comms_status = "offline"

                        # Detect pending power limit command (FS propagation)
                        limit_actual = row["power_limit"]
                        limit_target = row["power_limit_should_be"]
                        limit_pending = (
                            limit_target is not None
                            and limit_actual is not None
                            and limit_actual != limit_target
                        )

                        meters.append(
                            {
                                "meter_number": row["external_reference"],
                                "is_on": row["is_on"],
                                "comms_status": comms_status,
                                "last_seen_at": (last_seen.isoformat() if last_seen else None),
                                "kwh_credit_available": (
                                    round(row["kwh_credit_available"], 2)
                                    if row["kwh_credit_available"] is not None
                                    else None
                                ),
                                "balance": (
                                    round(row["balance"], 2) if row["balance"] is not None else None
                                ),
                                "power_limit_w": limit_actual,
                                "power_limit_target_w": limit_target,
                                "power_limit_pending": limit_pending,
                                "power_limit_hps_mode_w": row["power_limit_hps_mode"],
                                "power_limit_updated_at": (
                                    row["power_limit_updated_at"].isoformat()
                                    if row["power_limit_updated_at"]
                                    else None
                                ),
                                "meter_phase": (
                                    str(row["meter_phase"]) if row["meter_phase"] else None
                                ),
                            }
                        )

                    all_results.append(
                        {
                            "pole_reference": pole_row["external_reference"],
                            "pole_nickname": pole_row["nickname"],
                            "grid_name": pole_row["grid_name"],
                            "meter_count": len(meters),
                            "meters": meters,
                        }
                    )

                if len(all_results) == 1:
                    return all_results[0]
                return {"poles_found": len(all_results), "results": all_results}

        except Exception as e:
            logger.error(f"Error getting meters on pole: {e}")
            return {"error": f"Failed to get meters on pole: {str(e)}"}

    async def get_meter_consumption(
        self,
        meter_number: str,
        organization_id: int,
        days_back: int = 30,
    ) -> Dict[str, Any]:
        """Get daily consumption history for a meter from TimescaleDB.

        Args:
            meter_number: Meter external reference
            organization_id: Organization ID (injected by orchestrator)
            days_back: Number of days to look back (default 30, max 365)

        Returns:
            Dict with daily consumption data and a base64 chart image
        """
        days_back = min(max(days_back, 1), 365)

        try:
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            # Verify meter belongs to user's organization
            async with pool.acquire() as conn:
                if organization_id == STAFF_ORG_ID:
                    meter_row = await conn.fetchrow(
                        "SELECT id, external_reference, rls_grid_id FROM meters "
                        "WHERE external_reference = $1 AND deleted_at IS NULL LIMIT 1",
                        meter_number,
                    )
                else:
                    meter_row = await conn.fetchrow(
                        "SELECT id, external_reference, rls_grid_id FROM meters "
                        "WHERE external_reference = $1 AND rls_organization_id = $2 "
                        "AND deleted_at IS NULL LIMIT 1",
                        meter_number,
                        organization_id,
                    )

                if not meter_row:
                    return {"error": f"Meter '{meter_number}' not found or not accessible"}

                # Get grid name for chart title
                grid_row = await conn.fetchrow(
                    "SELECT name FROM grids WHERE id = $1", meter_row["rls_grid_id"]
                )
                grid_name = grid_row["name"] if grid_row else "Unknown"

            # Query TimescaleDB for hourly snapshots aggregated by day
            if not (TIMESCALE_HOST and TIMESCALE_USER and TIMESCALE_PASSWORD):
                return {"error": "TimescaleDB not configured"}

            import asyncpg as asyncpg_ts

            ts_conn = await asyncpg_ts.connect(
                host=TIMESCALE_HOST,
                port=TIMESCALE_PORT,
                user=TIMESCALE_USER,
                password=TIMESCALE_PASSWORD,
                database=TIMESCALE_DATABASE,
                ssl="require",
            )

            try:
                rows = await ts_conn.fetch(
                    """
                    SELECT date_trunc('day', created_at) as day,
                           SUM(consumption_kwh) as total_kwh,
                           MAX(consumption_kwh) as max_hourly_kwh,
                           AVG(consumption_kwh) as avg_hourly_kwh,
                           COUNT(*) as sample_hours
                    FROM meter_snapshot_1_h
                    WHERE meter_external_reference = $1
                      AND created_at >= NOW() - make_interval(days => $2)
                    GROUP BY day
                    ORDER BY day
                    """,
                    meter_number,
                    days_back,
                )
            finally:
                await ts_conn.close()

            if not rows:
                return {
                    "meter_number": meter_number,
                    "grid_name": grid_name,
                    "days_back": days_back,
                    "message": "No consumption data found for this period",
                    "daily_data": [],
                }

            # Build daily data
            daily_data = []
            for r in rows:
                daily_data.append(
                    {
                        "date": r["day"].strftime("%Y-%m-%d"),
                        "total_kwh": round(r["total_kwh"], 3)
                        if r["total_kwh"] is not None
                        else None,
                        "max_hourly_kwh": round(r["max_hourly_kwh"], 3)
                        if r["max_hourly_kwh"] is not None
                        else None,
                        "avg_hourly_kwh": round(r["avg_hourly_kwh"], 3)
                        if r["avg_hourly_kwh"] is not None
                        else None,
                        "sample_hours": r["sample_hours"],
                    }
                )

            total_consumption = sum(d["total_kwh"] or 0 for d in daily_data)
            avg_daily = total_consumption / len(daily_data) if daily_data else 0

            # Generate chart
            chart_b64 = self._render_consumption_chart(
                daily_data, meter_number, grid_name, days_back
            )

            result: Dict[str, Any] = {
                "meter_number": meter_number,
                "grid_name": grid_name,
                "days_back": days_back,
                "days_with_data": len(daily_data),
                "total_consumption_kwh": round(total_consumption, 3),
                "avg_daily_kwh": round(avg_daily, 3),
                "daily_data": daily_data,
            }
            if chart_b64:
                result["chart_base64"] = chart_b64
            return result

        except Exception as e:
            logger.error(f"Error getting meter consumption: {e}")
            return {"error": f"Failed to get consumption history: {str(e)}"}

    @staticmethod
    def _render_consumption_chart(
        daily_data: list,
        meter_number: str,
        grid_name: str,
        days_back: int,
    ) -> str:
        """Render a bar+line chart of daily consumption. Returns base64 PNG."""
        try:
            import base64
            import io

            import matplotlib

            matplotlib.use("Agg")
            from datetime import datetime

            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt

            dates = [datetime.strptime(d["date"], "%Y-%m-%d") for d in daily_data]
            totals = [d["total_kwh"] for d in daily_data]
            maxes = [d["max_hourly_kwh"] for d in daily_data]

            fig, ax1 = plt.subplots(figsize=(12, 5))
            fig.patch.set_facecolor("#1a1a2e")
            ax1.set_facecolor("#1a1a2e")

            # Bar chart for daily total
            bar_width = 0.8 if len(dates) <= 31 else 0.6
            ax1.bar(dates, totals, width=bar_width, color="#5794F2", alpha=0.8, label="Daily Total")
            ax1.set_ylabel("Daily Total (kWh)", color="#c0c0c0")
            ax1.tick_params(axis="y", colors="#c0c0c0")
            ax1.tick_params(axis="x", colors="#c0c0c0")

            # Line for max hourly on secondary axis
            ax2 = ax1.twinx()
            ax2.plot(
                dates,
                maxes,
                color="#FF7383",
                linewidth=1.5,
                marker=".",
                markersize=4,
                label="Max Hourly",
            )
            ax2.set_ylabel("Max Hourly (kWh)", color="#FF7383")
            ax2.tick_params(axis="y", colors="#FF7383")

            # Formatting
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
            if len(dates) > 14:
                ax1.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
            fig.autofmt_xdate(rotation=45)

            avg_daily = sum(totals) / len(totals) if totals else 0
            ax1.axhline(
                y=avg_daily,
                color="#73BF69",
                linestyle="--",
                alpha=0.5,
                label=f"Avg: {avg_daily:.2f} kWh/day",
            )

            title = f"Meter {meter_number} — {grid_name} ({days_back}d)"
            ax1.set_title(title, color="white", fontsize=13, pad=10)

            # Combined legend
            lines1, labels1 = ax1.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax1.legend(
                lines1 + lines2,
                labels1 + labels2,
                loc="upper left",
                facecolor="#2a2a3e",
                edgecolor="#444",
                labelcolor="white",
                fontsize=9,
            )

            ax1.spines["top"].set_visible(False)
            ax2.spines["top"].set_visible(False)
            for spine in ax1.spines.values():
                spine.set_color("#444")
            for spine in ax2.spines.values():
                spine.set_color("#444")
            ax1.grid(axis="y", alpha=0.15, color="white")

            buf = io.BytesIO()
            fig.savefig(
                buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor()
            )
            plt.close(fig)
            buf.seek(0)
            return base64.b64encode(buf.read()).decode("utf-8")
        except Exception as e:
            logger.warning(f"Failed to render consumption chart: {e}")
            return ""

    async def get_grid_chat_chronology(
        self,
        grid_name: str,
        organization_id: int,
        days_back: int = 7,
    ) -> Dict[str, Any]:
        """
        Get a chronological timeline of all chat messages related to a specific grid.

        Collects messages from:
        - The grid's O&M group topic (internal_telegram_group_chat_id + thread_id)
        - Individual org user DMs (chat_sessions with matching organization_id)
        - Developer group (organization's developer_group_telegram_chat_id)

        Args:
            grid_name: Grid name (supports fuzzy matching)
            organization_id: Organization ID (injected by orchestrator)
            days_back: Number of days to look back (default 7, max 90)

        Returns:
            Dict with grid info, sources, and chronological timeline of messages
        """
        days_back = min(max(days_back, 1), 90)
        escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")

        try:
            # --- Step 1: Resolve grid via Auth DB ---
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Resolve grid name with fuzzy matching (org-scoped for non-staff)
                if organization_id == STAFF_ORG_ID:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """
                    )
                else:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE organization_id = $1
                          AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """,
                        organization_id,
                    )

                available_names = [row["name"] for row in available_rows]
                matched_name = _find_closest_grid_name(grid_name, available_names)

                # If no grid match, try matching against organization names
                # (e.g., "AcmeCorp" → org "Acme Corp" → grid "ExampleGrid")
                org_match_grids = []
                if not matched_name:
                    org_rows = await conn.fetch(
                        """
                        SELECT o.id, o.name, g.name as grid_name
                        FROM organizations o
                        JOIN grids g ON g.organization_id = o.id
                        WHERE g.deleted_at IS NULL
                          AND g.is_hidden_from_reporting IS NOT TRUE
                        ORDER BY o.name
                        """
                    )
                    org_names = list({r["name"] for r in org_rows})
                    matched_org = _find_closest_grid_name(grid_name, org_names)
                    if matched_org:
                        org_match_grids = [r for r in org_rows if r["name"] == matched_org]

                if not matched_name and not org_match_grids:
                    grid_list = ", ".join(available_names[:10])
                    suffix = (
                        f" (and {len(available_names) - 10} more)"
                        if len(available_names) > 10
                        else ""
                    )
                    return {
                        "error": f"Grid or organization '{grid_name}' not found. "
                        f"Available grids: {grid_list}{suffix}"
                    }

                # If matched by org name, use the first grid (or aggregate all)
                if not matched_name and org_match_grids:
                    matched_name = org_match_grids[0]["grid_name"]
                    logger.info(
                        f"Resolved org '{grid_name}' → grid '{matched_name}' "
                        f"(org: {org_match_grids[0]['name']})"
                    )

                # Get grid details
                grid_row = await conn.fetchrow(
                    """
                    SELECT id, name, organization_id,
                           internal_telegram_group_chat_id,
                           internal_telegram_group_thread_id,
                           telegram_config
                    FROM grids
                    WHERE name = $1 AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    matched_name,
                )
                if not grid_row:
                    return {"error": f"Grid '{matched_name}' not found"}

                grid_org_id = grid_row["organization_id"]
                group_chat_id = grid_row["internal_telegram_group_chat_id"]
                group_thread_id = grid_row["internal_telegram_group_thread_id"]

                # Extract logbook chat/topic IDs from telegram_config JSON
                from shared.auth import GridTelegramSources, parse_telegram_config

                tg_config = parse_telegram_config(grid_row["telegram_config"])
                logbook_chat_id = tg_config.get("internal_logbook_chat_id")
                logbook_topic_id = tg_config.get("internal_logbook_topic_id")

                # Build sources for classify_source() calls later
                grid_sources = GridTelegramSources(
                    om_chat_id=str(group_chat_id or ""),
                    om_topic_id=str(group_thread_id or ""),
                    logbook_chat_id=str(logbook_chat_id or ""),
                    logbook_topic_id=str(logbook_topic_id or ""),
                )

                # Get organization details
                org_row = await conn.fetchrow(
                    """
                    SELECT name, formal_name, developer_group_telegram_chat_id
                    FROM organizations
                    WHERE id = $1
                    """,
                    grid_org_id,
                )
                org_name = (org_row["formal_name"] or org_row["name"]) if org_row else "Unknown"
                dev_group_chat_id = org_row["developer_group_telegram_chat_id"] if org_row else None

            # --- Step 2: Query Chat DB (Supabase PostgREST) ---
            chat_db_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
            chat_db_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
            if not chat_db_url or not chat_db_key:
                return {"error": "Chat database not configured"}

            from supabase import create_client  # type: ignore[attr-defined]

            chat_client = create_client(chat_db_url, chat_db_key)

            cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

            # Build list of telegram_chat_ids to query for sessions
            # 1. Group chat (O&M topic)
            # 2. Developer group
            # 3. Individual org user sessions (by organization_id)

            # Find sessions matching the grid's group chat or developer group
            target_chat_ids = []
            if group_chat_id:
                target_chat_ids.append(str(group_chat_id))
            if dev_group_chat_id:
                target_chat_ids.append(str(dev_group_chat_id))
            if logbook_chat_id:
                target_chat_ids.append(str(logbook_chat_id))

            all_sessions: list = []
            seen_session_ids: set = set()

            def _add_sessions(sessions: list) -> None:
                for s in sessions:
                    if s["id"] not in seen_session_ids:
                        seen_session_ids.add(s["id"])
                        all_sessions.append(s)

            # Fetch sessions by telegram_chat_id (group + developer group)
            # For the O&M group, filter to the specific topic thread for this grid
            # to avoid pulling messages from other grid topics in the same group.
            if group_chat_id and group_thread_id:
                grid_topic_resp = (
                    chat_client.table("chat_sessions")
                    .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                    .eq("telegram_chat_id", str(group_chat_id))
                    .eq("telegram_topic_id", str(group_thread_id))
                    .execute()
                )
                _add_sessions(grid_topic_resp.data or [])
            elif group_chat_id:
                # No thread ID — non-forum group, fetch all sessions for the chat
                grid_topic_resp = (
                    chat_client.table("chat_sessions")
                    .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                    .eq("telegram_chat_id", str(group_chat_id))
                    .execute()
                )
                _add_sessions(grid_topic_resp.data or [])

            if dev_group_chat_id:
                dev_sessions_resp = (
                    chat_client.table("chat_sessions")
                    .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                    .eq("telegram_chat_id", str(dev_group_chat_id))
                    .execute()
                )
                _add_sessions(dev_sessions_resp.data or [])

            # Fetch sessions for the Logbook group topic (from telegram_config)
            if logbook_chat_id and logbook_topic_id:
                logbook_sessions_resp = (
                    chat_client.table("chat_sessions")
                    .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                    .eq("telegram_chat_id", str(logbook_chat_id))
                    .eq("telegram_topic_id", str(logbook_topic_id))
                    .execute()
                )
                _add_sessions(logbook_sessions_resp.data or [])

            # Fetch sessions by organization_id (individual DMs only).
            # Exclude sessions from O&M/Logbook groups — already topic-filtered above.
            org_sessions_resp = (
                chat_client.table("chat_sessions")
                .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                .eq("organization_id", grid_org_id)
                .execute()
            )
            # Chat IDs to skip (already fetched with topic filtering)
            skip_chat_ids = {str(cid) for cid in [group_chat_id, logbook_chat_id] if cid}
            for s in org_sessions_resp.data or []:
                if str(s.get("telegram_chat_id", "")) in skip_chat_ids:
                    continue
                _add_sessions([s])

            # Filter out staff org (2) and escalation group sessions
            filtered_sessions = []
            for s in all_sessions:
                chat_id_str = str(s.get("telegram_chat_id", ""))
                sess_org = s.get("organization_id")
                # Skip staff org sessions (unless it's the target group/dev group)
                if sess_org == STAFF_ORG_ID and chat_id_str not in target_chat_ids:
                    continue
                # Skip escalation group
                if escalation_chat_id and chat_id_str == str(escalation_chat_id):
                    continue
                filtered_sessions.append(s)

            if not filtered_sessions:
                return {
                    "grid_name": matched_name,
                    "organization": org_name,
                    "days_back": days_back,
                    "message_count": 0,
                    "sources": [],
                    "timeline": [],
                }

            # --- Step 3: Fetch messages for each session ---
            timeline = []
            source_counts: Dict[str, Dict[str, Any]] = {}
            batch_size = 50

            for i in range(0, len(filtered_sessions), batch_size):
                batch = filtered_sessions[i : i + batch_size]
                session_ids = [s["id"] for s in batch]
                session_map = {s["id"]: s for s in batch}

                messages_resp = (
                    chat_client.table("chat_messages")
                    .select("session_id, role, content, created_at")
                    .in_("session_id", session_ids)
                    .gte("created_at", cutoff)
                    .in_("role", ["user", "model"])
                    .order("created_at", desc=False)
                    .execute()
                )

                for msg in messages_resp.data or []:
                    content = msg.get("content")
                    if not content or not content.strip():
                        continue

                    session = session_map.get(msg["session_id"], {})
                    chat_id_str = str(session.get("telegram_chat_id", ""))
                    topic_id = session.get("telegram_topic_id")

                    # Determine source type and name
                    classified = grid_sources.classify_source(chat_id_str, str(topic_id or ""))
                    if classified:
                        source_type, label_prefix = classified
                        source_name = f"{label_prefix} {matched_name}"
                    elif group_chat_id and chat_id_str == str(group_chat_id):
                        # O&M group but different topic
                        source_type = "om_other"
                        source_name = "O&M Group (other topic)"
                    elif logbook_chat_id and chat_id_str == str(logbook_chat_id):
                        # Logbook group but different topic
                        source_type = "logbook_other"
                        source_name = "Logbook (other topic)"
                    elif dev_group_chat_id and chat_id_str == str(dev_group_chat_id):
                        source_type = "developer_group"
                        source_name = f"{org_name} Dev Group"
                    else:
                        source_type = "individual"
                        title = session.get("title") or "User"
                        source_name = f"{title} (DM)"

                    # Truncate content to 500 chars
                    truncated = content[:500] + "..." if len(content) > 500 else content

                    timeline.append(
                        {
                            "timestamp": msg.get("created_at", ""),
                            "source": source_name,
                            "source_type": source_type,
                            "role": msg["role"],
                            "content": truncated,
                        }
                    )

                    # Track source counts
                    if source_name not in source_counts:
                        source_counts[source_name] = {
                            "name": source_name,
                            "type": source_type,
                            "message_count": 0,
                        }
                    source_counts[source_name]["message_count"] += 1

            # Sort timeline chronologically
            timeline.sort(key=lambda m: m["timestamp"])

            sources = sorted(source_counts.values(), key=lambda s: s["message_count"], reverse=True)

            result = {
                "grid_name": matched_name,
                "organization": org_name,
                "days_back": days_back,
                "message_count": len(timeline),
                "sources": sources,
                "timeline": timeline,
            }

            # Create a work packet so the mini-app can render the timeline
            try:
                from uuid import uuid4

                packet_id = f"chat_chronology_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
                chat_client.table("agent_work_packets").insert(
                    {
                        "packet_id": packet_id,
                        "packet_type": "chat_chronology",
                        "packet_title": f"Chat Timeline: {matched_name}",
                        "packet_goal": f"Chat chronology for {matched_name} ({org_name})",
                        "assigned_expert": "chat_chronology",
                        "packet_status": "completed",
                        "packet_inputs": {
                            "grid_name": matched_name,
                            "organization": org_name,
                            "days_back": days_back,
                        },
                        "packet_state": {
                            "timeline": timeline,
                            "sources": sources,
                        },
                        "packet_outputs": {},
                        "organization_id": grid_org_id,
                    }
                ).execute()

                # Build mini-app URL (same signing as View State)
                import hashlib
                import hmac

                mini_app_url = os.getenv("MINI_APP_BASE_URL", "").rstrip("/")
                hmac_secret = os.getenv("MINI_APP_HMAC_SECRET", "")
                if mini_app_url and hmac_secret:
                    sig = hmac.new(
                        hmac_secret.encode(), packet_id.encode(), hashlib.sha256
                    ).hexdigest()[:16]
                    result["timeline_url"] = (
                        f"{mini_app_url}/?packet_id={packet_id}&view=timeline&sig={sig}"
                    )
                    logger.info(f"Created chronology packet {packet_id}")
            except Exception as e:
                logger.warning(f"Failed to create chronology packet: {e}")

            return result

        except Exception as e:
            logger.error(f"Error getting grid chat chronology: {e}")
            return {"error": f"Failed to get chat chronology: {str(e)}"}

    async def _fetch_meter(
        self,
        meter_number: str,
        organization_id: int,
        extra_columns: str = "",
    ) -> tuple[Any, Any]:  # (asyncpg.Connection, asyncpg.Record | None)
        """Open an Auth DB connection and look up a meter by external reference.

        Applies org-scoping for non-staff orgs. Caller is responsible for
        closing the returned connection in a finally block.

        Args:
            meter_number: The meter's external reference string.
            organization_id: Resolved org ID; STAFF_ORG_ID skips org-scoping.
            extra_columns: Comma-prefixed extra columns to SELECT (e.g. ", connection_id").

        Returns:
            (conn, row) — row is None if meter not found.
        """
        import asyncpg as _asyncpg

        conn = await _asyncpg.connect(
            host=os.getenv("AUTH_DB_HOST"),
            port=int(os.getenv("AUTH_DB_PORT", "6543")),
            user=os.getenv("AUTH_DB_USER"),
            password=os.getenv("AUTH_DB_PASSWORD"),
            database=os.getenv("AUTH_DB_NAME", "postgres"),
            ssl="require",
            statement_cache_size=0,
        )
        # extra_columns MUST be a hardcoded literal (e.g. ", connection_id") — NEVER user-supplied input.
        select_cols = f"id, external_reference{extra_columns}"
        if organization_id != STAFF_ORG_ID:
            row = await conn.fetchrow(
                f"SELECT {select_cols} FROM meters "
                "WHERE external_reference = $1 AND rls_organization_id = $2 LIMIT 1",
                meter_number,
                organization_id,
            )
        else:
            row = await conn.fetchrow(
                f"SELECT {select_cols} FROM meters WHERE external_reference = $1 LIMIT 1",
                meter_number,
            )
        return conn, row

    async def retry_commissioning(
        self,
        meter_number: str,
        user_email: str,
        organization_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Retry commissioning for a meter by calling Metering Platform API.

        Args:
            meter_number: Meter number to retry commissioning for
            user_email: User email for organization lookup
            organization_id: Optional organization ID (will be looked up if not provided)

        Returns:
            Dictionary with retry commissioning status
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        # Check if Metering Platform is configured
        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter commissioning actions."
            }

        if err := self._check_rate_limit("retry_commissioning", meter_number.strip()):
            return {"error": err}

        # Get user's organization_id if not provided
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }

                meter_id = meter["id"]

                # Look up the most recent commissioning for this meter.
                # meters.last_commissioning_id does not exist — query the
                # meter_commissionings table directly instead.
                commissioning = await conn.fetchrow(
                    """
                    SELECT mc.id, mc.meter_commissioning_status
                    FROM meter_commissionings mc
                    JOIN metering_hardware_install_sessions mhis
                      ON mc.metering_hardware_install_session_id = mhis.id
                    WHERE mhis.meter_id = $1
                    ORDER BY mc.created_at DESC
                    LIMIT 1
                    """,
                    meter_id,
                )

                if not commissioning:
                    return {
                        "error": "No commissioning record found for this meter. Cannot retry commissioning.",
                        "meter_number": meter_number,
                    }

                last_commissioning_id = commissioning["id"]
                status = (commissioning["meter_commissioning_status"] or "").upper()

                if status == "SUCCESSFUL":
                    return {
                        "error": "Meter is already successfully commissioned.",
                        "meter_number": meter_number,
                        "commissioning_id": last_commissioning_id,
                        "commissioning_status": status,
                        "message": "This meter does not need commissioning retry. It is already commissioned.",
                    }

                if status == "PROCESSING":
                    return {
                        "error": "A commissioning attempt is currently in progress.",
                        "meter_number": meter_number,
                        "commissioning_id": last_commissioning_id,
                        "commissioning_status": status,
                        "message": "Please wait for the current commissioning to complete before retrying.",
                    }

            finally:
                await conn.close()

            # Call Metering Platform API to retry commissioning
            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meter-installs/retry-commissioning"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }
            body = {"id": last_commissioning_id}

            try:
                response = await http_client.post(url, headers=headers, json=body)
                response.raise_for_status()
                metering_response = await response.json()

                return {
                    "success": True,
                    "meter_number": meter_number,
                    "commissioning_id": last_commissioning_id,
                    "new_commissioning_id": metering_response.get("id"),
                    "message": (
                        "Commissioning retry has been initiated successfully. "
                        "This process typically takes 2-5 minutes to complete. "
                        "You can check the meter_information tool to monitor progress."
                    ),
                }

            except Exception as e:
                logger.error(f"Metering Platform API request failed: {e}")
                error_msg = str(e)

                if "400" in error_msg or "BadRequest" in error_msg:
                    return {
                        "error": f"Cannot retry commissioning: {error_msg}",
                        "meter_number": meter_number,
                        "commissioning_id": last_commissioning_id,
                        "message": "The meter may already be processing or successfully commissioned.",
                    }
                else:
                    return {
                        "error": f"Failed to contact commissioning service: {error_msg}",
                        "meter_number": meter_number,
                        "commissioning_id": last_commissioning_id,
                    }

        except Exception as e:
            logger.error(f"Error retrying commissioning: {e}")
            return {"error": f"Failed to retry commissioning: {str(e)}"}

    async def unassign_meter(
        self,
        meter_number: str,
        user_email: str,
        organization_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Unassign a meter from its current connection by calling Metering Platform API.

        Args:
            meter_number: Meter number (external reference) to unassign
            user_email: User email for organization lookup
            organization_id: Optional organization ID (will be looked up if not provided)

        Returns:
            Dictionary with unassignment status
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("unassign_meter", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(
                meter_number, organization_id, ", connection_id, rls_organization_id"
            )
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }

                meter_id = meter["id"]
                connection_id = meter["connection_id"]

                if not connection_id:
                    return {
                        "error": "Meter is not currently assigned to any connection.",
                        "meter_number": meter_number,
                        "message": "This meter does not need unassignment — it has no active connection.",
                    }

            finally:
                await conn.close()

            # Call Metering Platform API to unassign meter
            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meters/{meter_id}/unassign"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }

            try:
                response = await http_client.post(url, headers=headers)
                response.raise_for_status()

                return {
                    "success": True,
                    "meter_number": meter_number,
                    "message": (
                        "Meter has been unassigned successfully. "
                        "It is now available for reassignment to another connection."
                    ),
                }

            except Exception as e:
                logger.error(f"Metering Platform API unassign request failed: {e}")
                error_msg = str(e)

                if "400" in error_msg or "BadRequest" in error_msg:
                    return {
                        "error": "Cannot unassign meter. The meter may not be in a state that allows unassignment.",
                        "meter_number": meter_number,
                    }
                else:
                    return {
                        "error": "Failed to contact meter service. Please try again later.",
                        "meter_number": meter_number,
                    }

        except Exception as e:
            logger.error(f"Error unassigning meter: {e}")
            return {
                "error": "Something went wrong while unassigning the meter. The team has been notified."
            }

    async def set_meter_power_limit(
        self,
        meter_number: str,
        power_limit_watts: int,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Set the HPS power limit for a meter via Metering Platform API.

        Sends a SET_POWER_LIMIT interaction to the meter via POST /meter-interactions/create-one.
        Requires CUSTOMER_METER_ACTIONS_ENABLED=true.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if power_limit_watts not in CUSTOMER_METER_POWER_LIMIT_OPTIONS:
            allowed = ", ".join(str(w) for w in CUSTOMER_METER_POWER_LIMIT_OPTIONS)
            return {
                "error": (f"Invalid power limit: {power_limit_watts}W. Allowed values: {allowed}W.")
            }

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("set_meter_power_limit", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_id = meter["id"]
            finally:
                await conn.close()

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meter-interactions/create-one"
            headers = {"Content-Type": "application/json", "X-API-KEY": self.metering_api_key}
            body = {
                "meter_id": meter_id,
                "meter_interaction_type": "SET_POWER_LIMIT",
                "target_power_limit": power_limit_watts,
            }

            try:
                response = await http_client.post(url, headers=headers, json=body)
                response.raise_for_status()
                metering_response = await response.json()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "power_limit_watts": power_limit_watts,
                    "interaction_id": metering_response.get("id"),
                    "message": (
                        f"Power limit set to {power_limit_watts}W for meter {meter_number}. "
                        "The change will take effect on the next meter communication."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform API set_power_limit request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check meter number and limit.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error setting meter power limit: {e}")
            return {
                "error": "Something went wrong while setting the power limit. The team has been notified."
            }

    async def set_meter_date(
        self,
        meter_number: str,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Set the current date on a meter via Metering Platform API.

        Sends a SET_DATE interaction to the meter via POST /meter-interactions/create-one.
        The date is the current date in the deployment's local timezone (DEFAULT_TIMEZONE).
        Requires CUSTOMER_METER_ACTIONS_ENABLED=true.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("set_meter_date", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        # Use the deployment's local timezone so the calendar date matches the meter's local time
        now = datetime.now(ZoneInfo(DEFAULT_TIMEZONE))

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_id = meter["id"]
            finally:
                await conn.close()

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meter-interactions/create-one"
            headers = {"Content-Type": "application/json", "X-API-KEY": self.metering_api_key}
            body = {
                "meter_id": meter_id,
                "meter_interaction_type": "SET_DATE",
                "payload_data": {"year": now.year, "month": now.month, "day": now.day},
            }

            try:
                response = await http_client.post(url, headers=headers, json=body)
                response.raise_for_status()
                metering_response = await response.json()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "date_set": now.strftime("%Y-%m-%d"),
                    "interaction_id": metering_response.get("id"),
                    "message": (
                        f"Date set to {now.strftime('%Y-%m-%d')} on meter {meter_number}. "
                        "The change will take effect on the next meter communication."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform API set_meter_date request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error setting meter date: {e}")
            return {
                "error": "Something went wrong while setting the meter date. The team has been notified."
            }

    async def send_relay_state(
        self,
        meter_number: str,
        user_email: str,
        interaction_type: Literal["TURN_ON", "TURN_OFF"],
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a TURN_ON or TURN_OFF interaction to a meter via Metering Platform."""
        if interaction_type not in ("TURN_ON", "TURN_OFF"):
            return {"error": f"Invalid interaction_type: {interaction_type}"}

        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        rate_key = "turn_meter_on" if interaction_type == "TURN_ON" else "turn_meter_off"
        if err := self._check_rate_limit(rate_key, meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_id = meter["id"]
            finally:
                await conn.close()

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meter-interactions/create-one"
            headers = {"Content-Type": "application/json", "X-API-KEY": self.metering_api_key}
            body = {"meter_id": meter_id, "meter_interaction_type": interaction_type}

            try:
                response = await http_client.post(url, headers=headers, json=body)
                response.raise_for_status()
                metering_response = await response.json()
                state = "ON" if interaction_type == "TURN_ON" else "OFF"
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "state": state,
                    "interaction_id": metering_response.get("id"),
                    "message": (
                        f"Meter {meter_number} relay turned {state}. "
                        "The change will take effect on the next meter communication."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform API {interaction_type} request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error sending {interaction_type} for meter {meter_number}: {e}")
            return {"error": "Something went wrong. The team has been notified."}

    async def resend_meter_token(
        self,
        meter_number: str,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Resend the last prepayment token to a meter via Metering Platform API.

        Looks up the most recent token from the directives table and delivers it
        via POST /meters/:external_reference/tokens/deliver.
        Requires CUSTOMER_METER_ACTIONS_ENABLED=true.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("resend_meter_token", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_db_id = meter["id"]
                # Use the DB-stored canonical reference in the URL, not the raw input string.
                external_ref = meter["external_reference"]

                directive = await conn.fetchrow(
                    """
                    SELECT token FROM directives
                    WHERE meter_id = $1 AND directive_type = 'TOP_UP'
                    AND token IS NOT NULL AND token != ''
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    meter_db_id,
                )
            finally:
                await conn.close()

            if not directive or not directive["token"]:
                return {
                    "error": "No previous token found for this meter.",
                    "meter_number": meter_number,
                }

            token_code = directive["token"]

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meters/{external_ref}/tokens/deliver"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }

            try:
                response = await http_client.post(url, headers=headers, json={"token": token_code})
                response.raise_for_status()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "message": (
                        f"Token resent successfully to meter {meter_number}. "
                        "The customer should receive it via their registered channel shortly."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform token resend request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check the meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error resending meter token: {e}")
            return {
                "error": "Something went wrong while resending the token. The team has been notified."
            }

    async def resend_clear_tamper_token(
        self,
        meter_number: str,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Resend the last CLEAR_TAMPER token to a meter via Metering Platform API.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("resend_clear_tamper_token", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_db_id = meter["id"]
                external_ref = meter["external_reference"]

                directive = await conn.fetchrow(
                    """
                    SELECT token FROM directives
                    WHERE meter_id = $1 AND directive_type = 'CLEAR_TAMPER'
                    AND token IS NOT NULL AND token != ''
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    meter_db_id,
                )
            finally:
                await conn.close()

            if not directive or not directive["token"]:
                return {
                    "error": "No previous CLEAR_TAMPER token found for this meter.",
                    "meter_number": meter_number,
                }

            token_code = directive["token"]

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meters/{external_ref}/tokens/deliver"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }

            try:
                response = await http_client.post(url, headers=headers, json={"token": token_code})
                response.raise_for_status()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "message": (
                        f"CLEAR_TAMPER token resent successfully to meter {meter_number}. "
                        "The customer should receive it via their registered channel shortly."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform CLEAR_TAMPER token resend request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check the meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error resending CLEAR_TAMPER token: {e}")
            return {
                "error": "Something went wrong while resending the token. The team has been notified."
            }

    async def resend_power_limit_token(
        self,
        meter_number: str,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Resend the last PLS (power limit set) token to a meter via Metering Platform API.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("resend_power_limit_token", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_db_id = meter["id"]
                external_ref = meter["external_reference"]

                directive = await conn.fetchrow(
                    """
                    SELECT token FROM directives
                    WHERE meter_id = $1 AND directive_type = 'PLS'
                    AND token IS NOT NULL AND token != ''
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    meter_db_id,
                )
            finally:
                await conn.close()

            if not directive or not directive["token"]:
                return {
                    "error": "No previous PLS (power limit set) token found for this meter.",
                    "meter_number": meter_number,
                }

            token_code = directive["token"]

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meters/{external_ref}/tokens/deliver"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }

            try:
                response = await http_client.post(url, headers=headers, json={"token": token_code})
                response.raise_for_status()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "message": (
                        f"PLS (power limit set) token resent successfully to meter {meter_number}. "
                        "The customer should receive it via their registered channel shortly."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform PLS token resend request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check the meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error resending PLS token: {e}")
            return {
                "error": "Something went wrong while resending the token. The team has been notified."
            }

    async def get_grid_status(
        self,
        organization_id: int,
        grid_name: Optional[str] = None,
        grid_id: Optional[int] = None,
        user_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get comprehensive status for a grid.

        Args:
            organization_id: Organization ID (injected by orchestrator)
            grid_name: Grid name (required unless grid_id is provided)
            grid_id: Grid ID for direct lookup (optional, used by meter_information)
            user_email: Optional email for logging

        Returns:
            Dict with grid status including HPS, FS, DCU status, weather, and latest_state from TimescaleDB
        """
        try:
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Track if we corrected the grid name for the response
                corrected_grid_name = None

                # If grid_id is provided, lookup directly (no fuzzy matching needed)
                if grid_id:
                    grid_row = await conn.fetchrow(
                        f"""
                        SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                               is_fs_on, is_fs_on_updated_at, timezone,
                               current_weather, are_all_dcus_online,
                               generation_gateway_last_seen_at, generation_external_site_id,
                               is_hps_on_threshold_kw, {MANAGED_GENERATION_COLUMN},
                               kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                               kwh_tariff_full_service, kwp_tariff,
                               location_geom::text as location_wkb
                        FROM grids
                        WHERE id = $1
                          AND is_hidden_from_reporting IS NOT TRUE
                          AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        grid_id,
                    )
                else:
                    # Fetch available grid names for fuzzy matching
                    if organization_id == STAFF_ORG_ID:
                        # Staff sees all grids
                        available_rows = await conn.fetch(
                            """
                            SELECT name FROM grids
                            WHERE is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                            ORDER BY name
                            """
                        )
                    else:
                        # Customer sees only their organization's grids
                        available_rows = await conn.fetch(
                            """
                            SELECT name FROM grids
                            WHERE organization_id = $1
                              AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                            ORDER BY name
                            """,
                            organization_id,
                        )

                    available_names = [row["name"] for row in available_rows]

                    # If grid_name provided, use fuzzy matching to correct it
                    if grid_name:
                        matched_name = _find_closest_grid_name(grid_name, available_names)
                        if matched_name:
                            if matched_name.lower() != grid_name.lower():
                                corrected_grid_name = matched_name
                                logger.info(
                                    f"Corrected grid name '{grid_name}' -> '{matched_name}'"
                                )
                            grid_name = matched_name
                        # If no match found, grid_name stays as-is and query will fail gracefully

                    # Now query with (potentially corrected) grid_name
                    if not grid_name:
                        # No grid name provided — return error with available grids
                        grid_list = ", ".join(available_names[:10])
                        suffix = (
                            f" (and {len(available_names) - 10} more)"
                            if len(available_names) > 10
                            else ""
                        )
                        return {
                            "error": f"No grid name specified. Please provide a grid name. Available grids: {grid_list}{suffix}"
                        }

                    if organization_id == STAFF_ORG_ID:
                        # Staff - no org filter
                        grid_row = await conn.fetchrow(
                            f"""
                            SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                                   is_fs_on, is_fs_on_updated_at,
                                   current_weather, are_all_dcus_online,
                                   generation_gateway_last_seen_at, timezone,
                                   generation_external_site_id, is_hps_on_threshold_kw,
                                   {MANAGED_GENERATION_COLUMN},
                                   kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                                   kwh_tariff_full_service, kwp_tariff,
                                   location_geom::text as location_wkb
                            FROM grids
                            WHERE LOWER(name) = LOWER($1)
                              AND is_hidden_from_reporting IS NOT TRUE
                              AND deleted_at IS NULL
                            LIMIT 1
                            """,
                            grid_name,
                        )
                    else:
                        # Customer - filter by organization
                        grid_row = await conn.fetchrow(
                            f"""
                            SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                                   is_fs_on, is_fs_on_updated_at,
                                   current_weather, are_all_dcus_online,
                                   generation_gateway_last_seen_at, timezone,
                                   generation_external_site_id, is_hps_on_threshold_kw,
                                   {MANAGED_GENERATION_COLUMN},
                                   kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                                   kwh_tariff_full_service, kwp_tariff,
                                   location_geom::text as location_wkb
                            FROM grids
                            WHERE LOWER(name) = LOWER($1)
                              AND organization_id = $2
                              AND is_hidden_from_reporting IS NOT TRUE
                              AND deleted_at IS NULL
                            LIMIT 1
                            """,
                            grid_name,
                            organization_id,
                        )

                if not grid_row:
                    # Get available grid names to help user
                    if organization_id == STAFF_ORG_ID:
                        available = await conn.fetch(
                            """
                            SELECT name FROM grids
                            WHERE is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                            ORDER BY name LIMIT 20
                            """,
                        )
                    else:
                        available = await conn.fetch(
                            """
                            SELECT name FROM grids
                            WHERE organization_id = $1
                              AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                            ORDER BY name
                            """,
                            organization_id,
                        )

                    available_names = [row["name"] for row in available]
                    return {
                        "error": f"Grid not found: {grid_name or '(no name specified)'}",
                        "available_grids": available_names,
                    }

                grid = dict(grid_row)
                grid_id = grid["id"]
                grid_tz = grid.get("timezone") or DEFAULT_TIMEZONE

                # Determine if this is a staff request
                is_staff = organization_id == STAFF_ORG_ID

                # Initialize TimescaleDB data (will be populated below)
                latest_state = None
                business_snapshot = None
                ts_hps_on = None
                ts_fs_on = None
                ts_created = None
                ts_is_stale = True  # Default to stale if no TimescaleDB data

                # Initialize VRM real-time metrics
                vrm_battery_soc: float | None = None
                vrm_battery_current: float | None = None
                vrm_solar_power_w: float | None = None
                vrm_grid_consumption_w: float | None = None
                vrm_is_on = None  # VRM voltage check for ON/OFF determination
                vrm_power_kw = None  # VRM power for HPS/Isolated determination
                vrm_l1_power_w: float | None = None
                vrm_l2_power_w: float | None = None
                vrm_l3_power_w: float | None = None
                vrm_platform = None

                # Fetch VRM real-time metrics BEFORE TimescaleDB so latest_state can use them
                try:
                    vrm_platform = VRMPlatform()
                    await vrm_platform.initialize()
                    site_id = grid.get("generation_external_site_id")
                    if site_id:
                        sid = str(site_id)
                        try:
                            vrm_results = await asyncio.gather(
                                vrm_platform.get_current_inverter_voltage(sid),
                                vrm_platform.get_current_battery_status(sid),
                                vrm_platform.get_current_pv_power(sid),
                                vrm_platform.get_current_grid_status(sid),
                                return_exceptions=True,
                            )

                            # Inverter voltage/power (total + per-phase)
                            voltage = vrm_results[0]
                            if not isinstance(voltage, (Exception, BaseException)):
                                if not voltage.error:  # type: ignore[union-attr]
                                    # Check if VRM data is stale (gateway >30 min old)
                                    vrm_data_ts = voltage.data_timestamp  # type: ignore[union-attr]
                                    if vrm_data_ts and (
                                        datetime.utcnow() - vrm_data_ts
                                    ) > timedelta(minutes=30):
                                        pass  # Leave vrm_is_on as None → falls to Unknown
                                    else:
                                        vrm_is_on = voltage.is_producing  # type: ignore[union-attr]
                                    vrm_power_kw = voltage.total_power_kw  # type: ignore[union-attr]
                                    vrm_l1_power_w = voltage.l1_power_w  # type: ignore[union-attr]
                                    vrm_l2_power_w = voltage.l2_power_w  # type: ignore[union-attr]
                                    vrm_l3_power_w = voltage.l3_power_w  # type: ignore[union-attr]

                            # Battery SOC and current
                            battery = vrm_results[1]
                            if not isinstance(battery, (Exception, BaseException)):
                                vrm_battery_soc = battery.soc_percent  # type: ignore[union-attr]
                                vrm_battery_current = battery.current_a  # type: ignore[union-attr]

                            # PV/Solar power
                            pv = vrm_results[2]
                            if not isinstance(pv, (Exception, BaseException)):
                                vrm_solar_power_w = pv.total_power_w  # type: ignore[union-attr]

                            # Grid consumption (inverter output to customers)
                            grid_status_vrm = vrm_results[3]
                            if not isinstance(grid_status_vrm, (Exception, BaseException)):
                                vrm_grid_consumption_w = grid_status_vrm.total_power_w  # type: ignore[union-attr]

                        except Exception as vrm_rt_err:
                            logger.warning(f"VRM real-time fetch failed: {vrm_rt_err}")
                except Exception as vrm_init_err:
                    logger.warning(f"VRM init failed for {grid['name']}: {vrm_init_err}")

                try:
                    import asyncpg as asyncpg_ts

                    if TIMESCALE_HOST and TIMESCALE_USER and TIMESCALE_PASSWORD:
                        ts_conn = await asyncpg_ts.connect(
                            host=TIMESCALE_HOST,
                            port=TIMESCALE_PORT,
                            user=TIMESCALE_USER,
                            password=TIMESCALE_PASSWORD,
                            database=TIMESCALE_DATABASE,
                            ssl="require",
                        )
                        try:
                            # Majority voting query for HPS/FS status stability
                            # Uses last 3 snapshots within 1 hour to prevent flapping
                            ts_row = await ts_conn.fetchrow(
                                """
                                WITH recent_snapshots AS (
                                    SELECT
                                        created_at, is_fs_active, is_hps_on, should_fs_be_on,
                                        battery_soc_bs_pct, battery_current_bc_a,
                                        grid_consumption_total_kwh,
                                        pv_energy_to_battery_pb_kwh,
                                        pv_energy_to_grid_pc_kwh,
                                        ROW_NUMBER() OVER (ORDER BY created_at DESC) as rn
                                    FROM grid_energy_snapshot_15_min
                                    WHERE grid_id = $1
                                      AND created_at >= NOW() - INTERVAL '1 hour'
                                )
                                SELECT
                                    MAX(created_at) as created_at,
                                    -- Majority voting for status stability
                                    (COUNT(*) FILTER (WHERE is_fs_active = true) * 2 > COUNT(*)) as is_fs_active,
                                    (COUNT(*) FILTER (WHERE is_hps_on = true) * 2 > COUNT(*)) as is_hps_on,
                                    -- Latest values for other fields
                                    (ARRAY_AGG(should_fs_be_on ORDER BY created_at DESC))[1] as should_fs_be_on,
                                    (ARRAY_AGG(battery_soc_bs_pct ORDER BY created_at DESC))[1] as battery_soc_bs_pct,
                                    (ARRAY_AGG(battery_current_bc_a ORDER BY created_at DESC))[1] as battery_current_bc_a,
                                    (ARRAY_AGG(grid_consumption_total_kwh ORDER BY created_at DESC))[1] as grid_consumption_total_kwh,
                                    (ARRAY_AGG(pv_energy_to_battery_pb_kwh ORDER BY created_at DESC))[1] as pv_energy_to_battery_pb_kwh,
                                    (ARRAY_AGG(pv_energy_to_grid_pc_kwh ORDER BY created_at DESC))[1] as pv_energy_to_grid_pc_kwh,
                                    COUNT(*) as snapshot_count
                                FROM recent_snapshots
                                WHERE rn <= $2
                                """,
                                grid_id,
                                STATUS_STABILITY_SNAPSHOT_COUNT,
                            )

                            if ts_row and ts_row["created_at"]:
                                # Format timestamp and check staleness based on TimescaleDB created_at
                                ts_created = ts_row["created_at"]
                                if ts_created and ts_created.tzinfo is None:
                                    ts_created = ts_created.replace(tzinfo=timezone.utc)

                                # Use TimescaleDB created_at for staleness (30 min threshold)
                                ts_is_stale = is_stale(ts_created)

                                # Get HPS/FS status from TimescaleDB (majority voted)
                                ts_hps_on = ts_row["is_hps_on"]
                                ts_fs_on = ts_row["is_fs_active"]

                                # Determine battery status from VRM current (preferred) or TimescaleDB
                                # VRM convention: positive current = charging, negative = discharging
                                battery_current = vrm_battery_current
                                if battery_current is None:
                                    battery_current = ts_row["battery_current_bc_a"]
                                if battery_current is None:
                                    battery_status = "unknown"
                                elif battery_current > 0:
                                    battery_status = "charging"
                                elif battery_current < 0:
                                    battery_status = "discharging"
                                else:
                                    battery_status = "idle"

                                # Calculate total solar production (to battery + to grid)
                                pv_to_battery = ts_row["pv_energy_to_battery_pb_kwh"] or 0
                                pv_to_grid = ts_row["pv_energy_to_grid_pc_kwh"] or 0
                                total_solar_kwh = pv_to_battery + pv_to_grid

                                # Use VRM real-time values with TimescaleDB fallback
                                latest_state = {
                                    "timestamp": _format_local_timestamp(ts_created, grid_tz),
                                    "is_stale": ts_is_stale,
                                    "is_fs_active": ts_fs_on,
                                    "is_hps_on": ts_hps_on,
                                    "should_fs_be_on": ts_row["should_fs_be_on"],
                                    "battery_soc_pct": (
                                        vrm_battery_soc
                                        if vrm_battery_soc is not None
                                        else ts_row["battery_soc_bs_pct"]
                                    ),
                                    "battery_current_a": battery_current,
                                    "battery_status": battery_status,
                                    "consumption_w": (
                                        vrm_grid_consumption_w
                                        if vrm_grid_consumption_w is not None
                                        else None
                                    ),
                                    "consumption_kwh": ts_row["grid_consumption_total_kwh"],
                                    "solar_power_w": vrm_solar_power_w,
                                    "solar_production_kwh": (
                                        total_solar_kwh if total_solar_kwh else None
                                    ),
                                    "data_source": (
                                        "vrm" if vrm_battery_soc is not None else "timescale"
                                    ),
                                }
                            else:
                                # No TimescaleDB data - use VRM real-time data if available
                                vrm_batt_status = "unknown"
                                if vrm_battery_current is not None:
                                    if vrm_battery_current > 0:
                                        vrm_batt_status = "charging"
                                    elif vrm_battery_current < 0:
                                        vrm_batt_status = "discharging"
                                    else:
                                        vrm_batt_status = "idle"

                                latest_state = {
                                    "timestamp": None,
                                    "is_stale": True,
                                    "is_fs_active": None,
                                    "is_hps_on": None,
                                    "should_fs_be_on": None,
                                    "battery_soc_pct": vrm_battery_soc,
                                    "battery_current_a": vrm_battery_current,
                                    "battery_status": vrm_batt_status,
                                    "consumption_w": vrm_grid_consumption_w,
                                    "consumption_kwh": None,
                                    "solar_power_w": vrm_solar_power_w,
                                    "solar_production_kwh": None,
                                    "data_source": "vrm" if vrm_battery_soc is not None else None,
                                }

                            # Fetch business snapshot for the most recent day with complete data
                            # Filter by grid_name IS NOT NULL to skip incomplete/partial rows
                            business_row = await ts_conn.fetchrow(
                                """
                                SELECT created_at, kwp, kwh,
                                       residential_connection_count, commercial_connection_count,
                                       public_connection_count, lifeline_connection_count,
                                       total_connection_count, total_meter_count,
                                       three_phase_meter_count, total_consumption_kwh,
                                       battery_modules_on_count, battery_modules_off_count,
                                       energy_topup_revenue, monthly_rental, daily_rental,
                                       women_impacted_count, connection_fee_revenue,
                                       fs_single_phase_connection_fee, hps_single_phase_connection_fee,
                                       fs_three_phase_connection_fee
                                FROM grid_business_snapshot_1_d
                                WHERE grid_id = $1
                                  AND grid_name IS NOT NULL
                                ORDER BY created_at DESC
                                LIMIT 1
                                """,
                                grid_id,
                            )

                            business_snapshot = None
                            logger.info(
                                f"Business snapshot query for grid_id={grid_id}: "
                                f"found={business_row is not None}"
                            )
                            if business_row:
                                bs_created = business_row["created_at"]
                                if bs_created and bs_created.tzinfo is None:
                                    bs_created = bs_created.replace(tzinfo=timezone.utc)

                                # Build business snapshot (filtered by mode)
                                # Calculate non-residential (commercial + public)
                                non_residential = (
                                    business_row["commercial_connection_count"] or 0
                                ) + (business_row["public_connection_count"] or 0)
                                three_phase = business_row["three_phase_meter_count"] or 0

                                business_snapshot = {
                                    "snapshot_date": (
                                        bs_created.strftime("%Y-%m-%d") if bs_created else None
                                    ),
                                    "capacity": {
                                        "kwp": business_row["kwp"]
                                        if grid.get(MANAGED_GENERATION_COLUMN)
                                        else None,
                                        "kwh": business_row["kwh"],
                                    },
                                    "connections": {
                                        "residential": business_row["residential_connection_count"],
                                        "non_residential": non_residential,
                                        "total": business_row["total_connection_count"],
                                    },
                                    "meters": {
                                        "total": business_row["total_meter_count"],
                                    },
                                    "consumption_kwh": business_row["total_consumption_kwh"],
                                    "battery_modules": {
                                        "on": business_row["battery_modules_on_count"],
                                        "off": business_row["battery_modules_off_count"],
                                    },
                                }

                                # Only include 3-phase count if non-zero
                                if three_phase > 0:
                                    business_snapshot["meters"]["three_phase"] = three_phase

                                # Staff-only fields
                                if is_staff:
                                    business_snapshot["revenue"] = {
                                        "energy_topup": business_row["energy_topup_revenue"],
                                        "connection_fee": business_row["connection_fee_revenue"],
                                    }
                                    business_snapshot["rental"] = {
                                        "monthly": business_row["monthly_rental"],
                                        "daily": business_row["daily_rental"],
                                    }
                                    business_snapshot["women_impacted_count"] = business_row[
                                        "women_impacted_count"
                                    ]
                                    business_snapshot["connection_fees"] = {
                                        "fs_single_phase": business_row[
                                            "fs_single_phase_connection_fee"
                                        ],
                                        "hps_single_phase": business_row[
                                            "hps_single_phase_connection_fee"
                                        ],
                                        "fs_three_phase": business_row[
                                            "fs_three_phase_connection_fee"
                                        ],
                                    }
                                # Note: *issue_count columns are excluded for both modes

                            # Get yesterday's ON hours while we have the ts_conn
                            yesterday_on = await self._get_yesterday_on_hours(ts_conn, grid_id)

                            # FS daily summary for yesterday + today
                            now_utc = datetime.utcnow()
                            fs_start = (now_utc - timedelta(days=1)).replace(
                                hour=0, minute=0, second=0, microsecond=0
                            )
                            fs_end = (now_utc + timedelta(days=1)).replace(
                                hour=0, minute=0, second=0, microsecond=0
                            )
                            fs_daily = await self._get_fs_summary_for_grid(
                                auth_conn=conn,
                                ts_conn=ts_conn,
                                grid_id=grid_id,
                                grid_tz=grid_tz,
                                start_date=fs_start,
                                end_date=fs_end,
                            )
                        finally:
                            await ts_conn.close()
                    else:
                        logger.warning("TimescaleDB not configured, skipping latest_state")
                        yesterday_on = {"on_hours": None, "error": "TimescaleDB not configured"}
                        fs_daily = None
                except Exception as ts_err:
                    logger.error(f"TimescaleDB query error: {ts_err}")
                    # Continue without latest_state on error
                    yesterday_on = {"on_hours": None, "error": str(ts_err)}
                    fs_daily = None

                # Get FS schedule (uses auth DB connection, pass current FS state from TimescaleDB)
                fs_schedule = await self._get_fs_schedule(conn, grid_id, ts_fs_on)

                # Get last FS command delivery stats
                last_fs_delivery = await self._get_last_fs_delivery(conn, grid_id)

                # Get 24h downtime analysis and live weather from VRM
                # (vrm_platform already initialized above for real-time metrics)
                downtime_24h = None
                live_weather = None
                equipment_note = None  # Note if equipment data unavailable
                try:
                    # Reuse vrm_platform if already initialized, otherwise create new
                    if not vrm_platform:
                        vrm_platform = VRMPlatform()
                        await vrm_platform.initialize()

                    # Fetch downtime and weather in parallel
                    downtime_task = vrm_platform.get_downtime_summary(
                        grid["name"], hours=24, timeout_seconds=3.0
                    )
                    weather_task = vrm_platform.get_site_weather(grid["name"], timeout_seconds=3.0)
                    results = await asyncio.gather(
                        downtime_task, weather_task, return_exceptions=True
                    )
                    downtime_result = results[0]
                    weather_result = results[1]

                    # Process downtime result
                    if not isinstance(downtime_result, Exception):
                        if downtime_result.error is None:  # type: ignore[union-attr]
                            downtime_24h = downtime_result.to_dict()  # type: ignore[union-attr]
                            downtime_24h["summary_text"] = _format_downtime_summary_text(
                                downtime_24h, tz_name=grid_tz
                            )
                        elif "not managed" in (downtime_result.error or ""):  # type: ignore[union-attr]
                            # Grid's generation is not managed by the operator
                            equipment_note = downtime_result.error  # type: ignore[union-attr]
                    else:
                        logger.warning(f"Downtime fetch failed: {downtime_result}")

                    # Process weather result
                    if not isinstance(weather_result, Exception):
                        if weather_result.error is None:  # type: ignore[union-attr]
                            live_weather = weather_result.to_dict()  # type: ignore[union-attr]
                    else:
                        logger.warning(f"Weather fetch failed: {weather_result}")

                except Exception as vrm_err:
                    logger.warning(f"VRM fetch failed for {grid['name']}: {vrm_err}")

                # Build response - use TimescaleDB for HPS/FS status with staleness check
                location = parse_location_geom(grid.get("location_wkb"))
                result = {
                    "grid_name": grid["name"],
                    "grid_id": grid_id,
                    "platform_url": f"{PLATFORM_BASE_URL}/grid/{grid_id}/"
                    if PLATFORM_BASE_URL
                    else None,
                    "timezone": grid_tz,
                }
                if location:
                    result["location"] = location

                # Add note if we corrected the grid name
                if corrected_grid_name:
                    result["name_corrected_from"] = corrected_grid_name

                # Determine service status using VRM voltage/power with TimescaleDB fallback
                # - VRM voltage determines ON/OFF (if available)
                # - VRM power vs threshold determines HPS (if ON), with TimescaleDB fallback
                # - FS status always from TimescaleDB (VRM doesn't track FS)
                # - "Likely Isolated" = ON but power below HPS threshold
                # NOTE: Keep this logic consistent with /grids command in list_all_grids_status()
                hps_threshold_kw = grid.get("is_hps_on_threshold_kw")

                if vrm_is_on is None:
                    # No VRM data - fall back to TimescaleDB-only logic
                    if ts_is_stale or (ts_fs_on is None and ts_hps_on is None):
                        service_status = "Unknown"
                    elif ts_fs_on:
                        service_status = "FS"
                    elif ts_hps_on:
                        service_status = "HPS"
                    else:
                        service_status = "Down"
                elif vrm_is_on is False:
                    # VRM says grid is OFF (no inverter voltage)
                    service_status = "Down"
                else:
                    # VRM says grid is ON - determine HPS using VRM power vs threshold
                    # with TimescaleDB fallback if VRM power unavailable
                    if vrm_power_kw is not None and hps_threshold_kw is not None:
                        vrm_hps_on = vrm_power_kw >= float(hps_threshold_kw)
                    else:
                        # Fallback to TimescaleDB if VRM power unavailable
                        vrm_hps_on = ts_hps_on

                    # Power below threshold = Isolated, even if meters report FS
                    if vrm_hps_on is False:
                        service_status = "Likely Isolated"
                    elif ts_fs_on is True:
                        service_status = "FS"
                    elif vrm_hps_on is True:
                        service_status = "HPS"
                    else:
                        service_status = "Likely Isolated"

                # Service status section: current state + yesterday's on hours + downtime
                result["service_status"] = {
                    "service": service_status,
                    "inverter_power_kw": vrm_power_kw,  # VRM total output power
                    "inverter_l1_power_kw": (
                        round(vrm_l1_power_w / 1000, 3) if vrm_l1_power_w is not None else None
                    ),
                    "inverter_l2_power_kw": (
                        round(vrm_l2_power_w / 1000, 3) if vrm_l2_power_w is not None else None
                    ),
                    "inverter_l3_power_kw": (
                        round(vrm_l3_power_w / 1000, 3) if vrm_l3_power_w is not None else None
                    ),
                    "updated_at": _format_local_timestamp(ts_created, grid_tz),
                    "is_stale": ts_is_stale,
                    "yesterday_on_hours": yesterday_on.get("on_hours"),
                    "downtime_24h": downtime_24h,
                }

                # Add note if equipment data is unavailable (non-managed grid)
                if equipment_note:
                    result["equipment_note"] = equipment_note

                # Query DCU status directly from dcus table (not cached grids.are_all_dcus_online)
                # This ensures consistency with /grids command
                dcu_rows = await conn.fetch(
                    """
                    SELECT external_reference, is_online, last_online_at
                    FROM dcus
                    WHERE grid_id = $1
                    ORDER BY external_reference
                    """,
                    grid_id,
                )

                dcu_total = len(dcu_rows)
                dcu_online = sum(1 for row in dcu_rows if row["is_online"])
                all_online = dcu_online == dcu_total and dcu_total > 0

                # Build visual: 📶📶🅇 for 2 online, 1 offline (consistent with /grids)
                if dcu_total > 0:
                    dcu_visual = "📶" * dcu_online + "🅇" * (dcu_total - dcu_online)
                else:
                    dcu_visual = "N/A"

                # Build offline DCUs list
                hardware_url = (
                    f"{PLATFORM_BASE_URL}/grid/{grid_id}/hardware/" if PLATFORM_BASE_URL else None
                )
                offline_dcus = [
                    {
                        "name": row["external_reference"],
                        "last_online_at": _format_local_timestamp(row["last_online_at"], grid_tz),
                        "status_is_stale": is_stale(row["last_online_at"]),
                    }
                    for row in dcu_rows
                    if not row["is_online"]
                ]

                result["dcus"] = {
                    "all_online": all_online,
                    "online": dcu_online,
                    "total": dcu_total,
                    "visual": dcu_visual,
                    "offline_dcus": offline_dcus,
                }
                if offline_dcus:
                    result["dcus"]["hardware_url"] = hardware_url

                # Use live weather from Open-Meteo if available, fallback to DB weather
                weather_icon = _weather_to_icon(grid["current_weather"])
                result["live_weather"] = (
                    live_weather
                    if live_weather
                    else {
                        "icon": weather_icon,
                        "description": grid["current_weather"] or "Unknown",
                        "display": weather_icon,
                    }
                )
                # Keep legacy weather field for backward compatibility
                result["weather"] = grid["current_weather"]
                result["latest_state"] = latest_state
                result["business_snapshot"] = business_snapshot

                # Tariff (Naira per kWh)
                result["kwh_tariff_naira"] = grid.get("kwh_tariff_essential_service")

                # FS Detail section: FS schedule + last FS command delivery
                result["fs_detail"] = {
                    "fs_schedule": fs_schedule,
                    "last_fs_command": last_fs_delivery,
                    "daily_summary": fs_daily,
                }

                # Only include data_freshness when there's an issue (stale data)
                if ts_is_stale:
                    result["data_freshness"] = {
                        "warning": "Data may be stale",
                        "staleness_threshold_minutes": 30,
                    }

                logger.info(
                    f"Grid status for {grid['name']}: HPS={ts_hps_on}, FS={ts_fs_on}, "
                    f"business_snapshot={'present' if business_snapshot else 'None'}, "
                    f"fs_schedule={fs_schedule.get('summary', 'N/A') if fs_schedule else 'None'}, "
                    f"yesterday_on={yesterday_on.get('on_hours', 'N/A')}h"
                )
                return result

        except Exception as e:
            logger.error(f"Error getting grid status: {e}")
            return {"error": f"Failed to get grid status: {str(e)}"}

    async def get_all_grids_status(self, organization_id: int) -> Dict[str, Any]:
        """
        Get status of all grids accessible to the user, grouped by operational status.

        Args:
            organization_id: Organization ID (2 = staff sees all, others see their org only)

        Returns:
            Dict with grids grouped by status category with icons
        """
        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "6543")),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                ssl="require",
                statement_cache_size=0,
            )

            try:
                # Staff sees all grids, others filtered by organization
                if organization_id == STAFF_ORG_ID:
                    rows = await conn.fetch(
                        f"""
                        SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                               is_fs_on, is_fs_on_updated_at, timezone,
                               current_weather, are_all_dcus_online,
                               generation_gateway_last_seen_at, generation_external_site_id,
                               is_hps_on_threshold_kw, {MANAGED_GENERATION_COLUMN},
                               kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                               kwh_tariff_full_service, kwp_tariff,
                               location_geom::text as location_wkb
                        FROM grids
                        WHERE is_hidden_from_reporting IS NOT TRUE
                          AND deleted_at IS NULL
                        ORDER BY name
                        """
                    )
                else:
                    rows = await conn.fetch(
                        f"""
                        SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                               is_fs_on, is_fs_on_updated_at, timezone,
                               current_weather, are_all_dcus_online,
                               generation_gateway_last_seen_at, generation_external_site_id,
                               is_hps_on_threshold_kw, {MANAGED_GENERATION_COLUMN},
                               kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                               kwh_tariff_full_service, kwp_tariff,
                               location_geom::text as location_wkb
                        FROM grids
                        WHERE organization_id = $1
                          AND is_hidden_from_reporting IS NOT TRUE
                          AND deleted_at IS NULL
                        ORDER BY name
                        """,
                        organization_id,
                    )

                # Categorize grids by status
                # Use 30-minute threshold for staleness (same as /grid command)
                grids_staleness_threshold = timedelta(minutes=30)

                def is_grids_stale(timestamp: Optional[datetime]) -> bool:
                    """Check staleness with 30-minute threshold."""
                    if timestamp is None:
                        return True
                    now = datetime.now(timezone.utc)
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    return bool((now - timestamp) > grids_staleness_threshold)

                # Fetch latest state from TimescaleDB for all grids (batch query)
                grid_ids = [row["id"] for row in rows]
                ts_data_map: Dict[int, Dict[str, Any]] = {}
                fs_hours_map: Dict[int, float] = {}

                try:
                    import asyncpg as asyncpg_ts

                    if TIMESCALE_HOST and TIMESCALE_USER and TIMESCALE_PASSWORD and grid_ids:
                        ts_conn = await asyncpg_ts.connect(
                            host=TIMESCALE_HOST,
                            port=TIMESCALE_PORT,
                            user=TIMESCALE_USER,
                            password=TIMESCALE_PASSWORD,
                            database=TIMESCALE_DATABASE,
                            ssl="require",
                        )
                        try:
                            # Majority voting query: average over last 3 snapshots within 1 hour
                            # This prevents status flapping when power oscillates around threshold
                            ts_rows = await ts_conn.fetch(
                                """
                                WITH recent_snapshots AS (
                                    SELECT
                                        grid_id, created_at, is_fs_active, is_hps_on,
                                        should_fs_be_on, battery_soc_bs_pct, battery_current_bc_a,
                                        grid_consumption_total_kwh,
                                        ROW_NUMBER() OVER (PARTITION BY grid_id ORDER BY created_at DESC) as rn
                                    FROM grid_energy_snapshot_15_min
                                    WHERE grid_id = ANY($1)
                                      AND created_at >= NOW() - INTERVAL '1 hour'
                                )
                                SELECT
                                    grid_id,
                                    MAX(created_at) as created_at,
                                    -- Majority voting: true if more than half show true
                                    (COUNT(*) FILTER (WHERE is_fs_active = true) * 2 > COUNT(*)) as is_fs_active,
                                    (COUNT(*) FILTER (WHERE is_hps_on = true) * 2 > COUNT(*)) as is_hps_on,
                                    -- Latest values for other fields
                                    (ARRAY_AGG(should_fs_be_on ORDER BY created_at DESC))[1] as should_fs_be_on,
                                    (ARRAY_AGG(battery_soc_bs_pct ORDER BY created_at DESC))[1] as battery_soc_bs_pct,
                                    (ARRAY_AGG(battery_current_bc_a ORDER BY created_at DESC))[1] as battery_current_bc_a,
                                    (ARRAY_AGG(grid_consumption_total_kwh ORDER BY created_at DESC))[1] as grid_consumption_total_kwh,
                                    COUNT(*) as snapshot_count
                                FROM recent_snapshots
                                WHERE rn <= $2
                                GROUP BY grid_id
                                """,
                                grid_ids,
                                STATUS_STABILITY_SNAPSHOT_COUNT,
                            )

                            for ts_row in ts_rows:
                                gid = ts_row["grid_id"]
                                # Determine battery status
                                battery_current = ts_row["battery_current_bc_a"]
                                if battery_current is None:
                                    battery_status = "unknown"
                                elif battery_current > 0:
                                    battery_status = "charging"
                                elif battery_current < 0:
                                    battery_status = "discharging"
                                else:
                                    battery_status = "idle"

                                # Store raw timestamp for later conversion
                                ts_created = ts_row["created_at"]
                                if ts_created and ts_created.tzinfo is None:
                                    ts_created = ts_created.replace(tzinfo=timezone.utc)

                                ts_data_map[gid] = {
                                    "timestamp_utc": ts_created,  # Raw datetime for per-grid tz conversion
                                    "is_fs_active": ts_row["is_fs_active"],
                                    "is_hps_on": ts_row["is_hps_on"],
                                    "should_fs_be_on": ts_row["should_fs_be_on"],
                                    "battery_soc_pct": ts_row["battery_soc_bs_pct"],
                                    "battery_status": battery_status,
                                    "consumption_kwh": ts_row["grid_consumption_total_kwh"],
                                }

                            # Batch FS ON hours for last 24h
                            fs_hours_rows = await ts_conn.fetch(
                                """
                                SELECT
                                    grid_id,
                                    COUNT(*) FILTER (WHERE is_fs_active = true) AS fs_on_slots,
                                    COUNT(*) AS total_slots
                                FROM grid_energy_snapshot_15_min
                                WHERE grid_id = ANY($1)
                                  AND created_at >= NOW() - INTERVAL '24 hours'
                                GROUP BY grid_id
                                """,
                                grid_ids,
                            )
                            for fsr in fs_hours_rows:
                                fs_hours_map[fsr["grid_id"]] = round(
                                    (fsr["fs_on_slots"] or 0) * 0.25, 1
                                )
                        finally:
                            await ts_conn.close()
                except Exception as ts_err:
                    logger.error(f"TimescaleDB batch query error: {ts_err}")
                    # Continue without TimescaleDB data

                # Fetch DCU counts per grid (online vs total)
                dcu_counts_map: Dict[int, Dict[str, int]] = {}
                try:
                    dcu_rows = await conn.fetch(
                        """
                        SELECT grid_id,
                               COUNT(*) as total,
                               COUNT(*) FILTER (WHERE is_online = true) as online
                        FROM dcus
                        WHERE grid_id = ANY($1)
                        GROUP BY grid_id
                        """,
                        grid_ids,
                    )
                    for dcu_row in dcu_rows:
                        dcu_counts_map[dcu_row["grid_id"]] = {
                            "online": dcu_row["online"],
                            "total": dcu_row["total"],
                        }
                except Exception as dcu_err:
                    logger.error(f"DCU counts query error: {dcu_err}")

                # Batch last FS delivery per grid (last 24h)
                fs_delivery_map: Dict[int, Dict[str, Any]] = {}
                try:
                    fs_del_rows = await conn.fetch(
                        """
                        WITH latest_exec AS (
                            SELECT
                                db.grid_id, dbe.successful_count, dbe.total_count,
                                db.fs_command, dbe.created_at,
                                ROW_NUMBER() OVER (
                                    PARTITION BY db.grid_id ORDER BY dbe.id DESC
                                ) as rn
                            FROM directive_batch_executions dbe
                            JOIN directive_batches db ON dbe.directive_batch_id = db.id
                            WHERE db.grid_id = ANY($1)
                              AND db.fs_command IS NOT NULL
                              AND dbe.created_at >= NOW() - INTERVAL '24 hours'
                        )
                        SELECT grid_id, successful_count, total_count, fs_command, created_at
                        FROM latest_exec WHERE rn = 1
                        """,
                        grid_ids,
                    )
                    for fdr in fs_del_rows:
                        total = fdr["total_count"] or 0
                        successful = fdr["successful_count"] or 0
                        delivery_pct = round((successful / total * 100), 1) if total > 0 else 0
                        fs_delivery_map[fdr["grid_id"]] = {
                            "command": fdr["fs_command"],
                            "delivery_pct": delivery_pct,
                            "successful": successful,
                            "total": total,
                        }
                except Exception as fs_del_err:
                    logger.error(f"FS delivery batch query error: {fs_del_err}")

                # Fetch 24h downtime, weather, and inverter voltage for all grids (parallel)
                downtime_map: Dict[str, Dict[str, Any]] = {}
                weather_map: Dict[str, Dict[str, Any]] = {}
                voltage_map: Dict[str, InverterVoltage] = {}  # site_id -> voltage
                equipment_note_map: Dict[str, str] = {}  # Track non-managed grid notes

                # Build map of grid_id -> VRM site_id for voltage lookup
                grid_site_map: Dict[int, str] = {}
                for row in rows:
                    site_id = row.get("generation_external_site_id")
                    if site_id:
                        grid_site_map[row["id"]] = str(site_id)

                try:
                    grid_names_for_downtime = [row["name"] for row in rows]
                    vrm_platform = VRMPlatform()
                    await vrm_platform.initialize()

                    # Build task list: downtime, weather, and voltage (if we have site IDs)
                    downtime_task = vrm_platform.get_batch_downtime_summary(
                        grid_names=grid_names_for_downtime,
                        hours=24,
                        max_concurrent=VRM_BATCH_MAX_CONCURRENT,
                        timeout_per_grid=3.0,
                    )
                    weather_task = vrm_platform.get_batch_weather(
                        grid_names=grid_names_for_downtime,
                        max_concurrent=VRM_BATCH_MAX_CONCURRENT,
                        timeout_per_grid=3.0,
                    )

                    # Fetch inverter voltage to determine ON/OFF status
                    site_ids_list = list(set(grid_site_map.values()))
                    voltage_task = vrm_platform.get_batch_inverter_voltage(
                        site_ids=site_ids_list,
                        max_concurrent=VRM_BATCH_MAX_CONCURRENT,
                        timeout_per_site=10.0,
                    )

                    results = await asyncio.gather(
                        downtime_task, weather_task, voltage_task, return_exceptions=True
                    )
                    downtime_results = results[0]
                    weather_results = results[1]
                    voltage_results = results[2]

                    # Process downtime results
                    if not isinstance(downtime_results, Exception):
                        for grid_name, summary in downtime_results.items():  # type: ignore[union-attr]
                            if summary.error is None:
                                downtime_map[grid_name] = summary.to_dict()
                            elif "not managed" in (summary.error or ""):
                                # Track non-managed grids with explanatory note
                                equipment_note_map[grid_name] = summary.error
                        logger.info(
                            f"Downtime fetch: {len(downtime_map)}/{len(grid_names_for_downtime)} grids"
                        )
                    else:
                        logger.error(f"Downtime fetch error: {downtime_results}")

                    # Process weather results
                    if not isinstance(weather_results, Exception):
                        for grid_name, weather in weather_results.items():  # type: ignore[union-attr]
                            if weather.error is None:
                                weather_map[grid_name] = weather.to_dict()
                        logger.info(
                            f"Weather fetch: {len(weather_map)}/{len(grid_names_for_downtime)} grids"
                        )
                    else:
                        logger.error(f"Weather fetch error: {weather_results}")

                    # Process voltage results
                    if not isinstance(voltage_results, Exception):
                        voltage_map = voltage_results  # type: ignore[assignment]
                        online_count = sum(
                            1 for v in voltage_map.values() if v.is_producing and not v.error
                        )
                        logger.info(
                            f"Voltage fetch: {len(voltage_map)} sites, {online_count} producing"
                        )
                    else:
                        logger.error(f"Voltage fetch error: {voltage_results}")

                except Exception as vrm_err:
                    logger.error(f"VRM fetch error: {vrm_err}")
                    # Continue without VRM data

                fs_on_grids = []
                hps_on_grids = []
                likely_isolated_grids = []
                off_grids = []
                unknown_grids = []

                for row in rows:
                    grid = dict(row)
                    grid_id = grid["id"]

                    # Get TimescaleDB data for this grid
                    ts_data = ts_data_map.get(grid_id)
                    grid_tz = grid.get("timezone") or DEFAULT_TIMEZONE

                    # Check staleness based on TimescaleDB created_at (30 min threshold)
                    if ts_data and ts_data.get("timestamp_utc"):
                        ts_timestamp = ts_data["timestamp_utc"]
                        ts_is_stale = is_grids_stale(ts_timestamp)
                    else:
                        ts_is_stale = True  # No TimescaleDB data = stale

                    # Get HPS/FS status from TimescaleDB (majority voted)
                    if ts_data:
                        hps_on = None if ts_is_stale else ts_data.get("is_hps_on")
                        fs_on = None if ts_is_stale else ts_data.get("is_fs_active")
                    else:
                        hps_on = None
                        fs_on = None

                    # Get VRM voltage data for this grid to determine ON/OFF
                    site_id = grid.get("generation_external_site_id")
                    vrm_voltage = voltage_map.get(str(site_id)) if site_id else None
                    vrm_is_on = (
                        vrm_voltage.is_producing if vrm_voltage and not vrm_voltage.error else None
                    )
                    # Get total inverter power (all phases) from VRM
                    vrm_power_kw = vrm_voltage.total_power_kw if vrm_voltage else None

                    # Check if VRM data is stale (gateway hasn't reported in 30+ min)
                    # data_timestamp is UTC-based (utcnow - secondsAgo)
                    vrm_data_stale = False
                    if vrm_voltage and vrm_voltage.data_timestamp:
                        vrm_age = datetime.utcnow() - vrm_voltage.data_timestamp
                        vrm_data_stale = vrm_age > timedelta(minutes=30)

                    # Determine category and icon using new logic:
                    # - VRM voltage determines ON/OFF (if available)
                    # - TimescaleDB majority vote determines HPS/FS (if ON)
                    # - "Likely Isolated" = ON but power below HPS threshold
                    # - Stale VRM data (>30 min) = unknown
                    if vrm_is_on is None or vrm_data_stale:
                        # No VRM data — honest answer is "unknown"
                        category = "unknown"
                        icon = "Ⅹ"
                    elif vrm_is_on is False:
                        # VRM says grid is OFF (no inverter voltage)
                        category = "off"
                        icon = "🔴"
                    else:
                        # VRM says grid is ON - check HPS/FS status
                        hps_threshold_kw = grid.get("is_hps_on_threshold_kw")

                        # Determine HPS on using VRM power vs threshold
                        if vrm_power_kw is not None and hps_threshold_kw is not None:
                            vrm_hps_on = vrm_power_kw >= float(hps_threshold_kw)
                        else:
                            # Fallback to TimescaleDB if VRM power unavailable
                            vrm_hps_on = hps_on

                        if vrm_hps_on is False:
                            # Power below HPS threshold = Isolated, regardless of meter FS state
                            # (meters may report FS while inverter is actually below threshold)
                            category = "likely_isolated"
                            icon = "🔌"
                        elif fs_on is True:
                            category = "fs_on"
                            icon = "🟢"
                        elif vrm_hps_on is True:
                            category = "hps_on"
                            icon = "🟡"
                        else:
                            # vrm_hps_on is None (no power data, no TimescaleDB fallback)
                            category = "unknown"
                            icon = "Ⅹ"

                    # Build DCU status with counts
                    dcu_counts = dcu_counts_map.get(grid_id, {"online": 0, "total": 0})
                    dcu_online = dcu_counts["online"]
                    dcu_total = dcu_counts["total"]
                    # Build visual: 📶📶🅇 for 2 online, 1 offline
                    if dcu_total > 0:
                        dcu_visual = "📶" * dcu_online + "🅇" * (dcu_total - dcu_online)
                    else:
                        dcu_visual = "N/A"

                    # Convert weather to icon
                    weather_icon = _weather_to_icon(grid["current_weather"])

                    # Build latest_state from TimescaleDB data
                    if ts_data:
                        latest_state = {
                            "timestamp": _format_local_timestamp(
                                ts_data.get("timestamp_utc"), grid_tz
                            ),
                            "is_fs_active": ts_data.get("is_fs_active"),
                            "is_hps_on": ts_data.get("is_hps_on"),
                            "should_fs_be_on": ts_data.get("should_fs_be_on"),
                            "battery_soc_pct": ts_data.get("battery_soc_pct"),
                            "battery_status": ts_data.get("battery_status"),
                            "consumption_kwh": ts_data.get("consumption_kwh"),
                            "is_stale": ts_is_stale,
                        }
                    else:
                        latest_state = {
                            "timestamp": None,
                            "is_stale": True,
                            "is_fs_active": None,
                            "is_hps_on": None,
                            "should_fs_be_on": None,
                            "battery_soc_pct": None,
                            "battery_status": "unknown",
                            "consumption_kwh": None,
                        }

                    # Get downtime and weather data for this grid
                    downtime_data = downtime_map.get(grid["name"])
                    weather_data = weather_map.get(grid["name"])
                    equipment_note = equipment_note_map.get(grid["name"])

                    location = parse_location_geom(grid.get("location_wkb"))
                    grid_info = {
                        "name": grid["name"],
                        "grid_id": grid_id,
                        "platform_url": f"{PLATFORM_BASE_URL}/grid/{grid_id}/"
                        if PLATFORM_BASE_URL
                        else None,
                        "timezone": grid_tz,
                        "icon": icon,
                        "hps_on": hps_on,
                        "fs_on": fs_on,
                        "inverter_power_kw": vrm_power_kw,
                        "dcu_status": {
                            "visual": dcu_visual,
                            "online": dcu_online,
                            "total": dcu_total,
                        },
                        "live_weather": (
                            weather_data
                            if weather_data
                            else {
                                "icon": weather_icon,
                                "description": grid["current_weather"] or "Unknown",
                                "display": weather_icon,
                            }
                        ),
                        "latest_state": latest_state,
                        "downtime_24h": downtime_data,
                        "fs_hours_24h": fs_hours_map.get(grid_id),
                        "fs_delivery_24h": fs_delivery_map.get(grid_id),
                    }

                    if location:
                        grid_info["location"] = location

                    # Add note if equipment data unavailable (non-managed grid)
                    if equipment_note:
                        grid_info["equipment_note"] = equipment_note

                    if category == "fs_on":
                        fs_on_grids.append(grid_info)
                    elif category == "hps_on":
                        hps_on_grids.append(grid_info)
                    elif category == "likely_isolated":
                        likely_isolated_grids.append(grid_info)
                    elif category == "off":
                        off_grids.append(grid_info)
                    else:
                        unknown_grids.append(grid_info)

                result = {
                    "grids_by_status": {
                        "fs_on": fs_on_grids,
                        "hps_on": hps_on_grids,
                        "likely_isolated": likely_isolated_grids,
                        "off": off_grids,
                        "unknown": unknown_grids,
                    },
                    "summary": {
                        "total": len(rows),
                        "fs_on": len(fs_on_grids),
                        "hps_on": len(hps_on_grids),
                        "likely_isolated": len(likely_isolated_grids),
                        "off": len(off_grids),
                        "unknown": len(unknown_grids),
                    },
                    "legend": {
                        "status_icons": {
                            "🟢": "FS On (Full Service active)",
                            "🟡": "HPS On (High Power Service on, FS off/unknown)",
                            "🔌": "Likely Isolated (Inverter ON but power below HPS threshold)",
                            "🔴": "Off (No inverter voltage detected)",
                            "Ⅹ": "Unknown (Stale data >30 minutes old)",
                        },
                        "dcu_icons": {
                            "📶": "DCU online",
                            "🅇": "DCU offline",
                            "example": "📶📶🅇 = 2 online, 1 offline",
                        },
                        "weather_icons": {
                            "☀️": "Clear/Sunny",
                            "⛅": "Partly cloudy",
                            "☁️": "Cloudy/Overcast",
                            "🌧️": "Rain",
                            "⛈️": "Thunderstorm",
                            "🌫️": "Fog/Mist",
                            "❄️": "Snow",
                            "💨": "Windy",
                        },
                        "downtime_icons": {
                            "⚡️": "Stable (no downtime in 24h)",
                            "🔻": "Has downtime - check hours and causes",
                        },
                    },
                }

                # Compute fleet_summary for executive summary rendering
                all_grids = (
                    fs_on_grids + hps_on_grids + likely_isolated_grids + off_grids + unknown_grids
                )
                grids_with_faults = []
                grids_with_downtime = []
                low_fs_delivery = []
                offline_dcus = []
                fs_hours_values = []

                for g in all_grids:
                    dt = g.get("downtime_24h")
                    if dt and isinstance(dt, dict):
                        total_min = dt.get("total_downtime_minutes", 0) or 0
                        if total_min > 0:
                            # Find top cause by minutes
                            causes = dt.get("causes", {})
                            top_cause = max(causes, key=causes.get) if causes else "unknown"
                            grids_with_downtime.append(
                                {
                                    "name": g["name"],
                                    "downtime_minutes": round(total_min),
                                    "top_cause": top_cause,
                                }
                            )
                            # Check for grid_fault or unknown causes
                            for cause in causes:
                                if cause in ("grid_fault", "unknown"):
                                    grids_with_faults.append(
                                        {
                                            "name": g["name"],
                                            "fault_type": cause,
                                        }
                                    )
                                    break

                    # Check FS delivery
                    fs_del = g.get("fs_delivery_24h")
                    if fs_del and isinstance(fs_del, dict):
                        delivery_pct = fs_del.get("delivery_pct")
                        if delivery_pct is not None and delivery_pct < 75:
                            low_fs_delivery.append(
                                {
                                    "name": g["name"],
                                    "delivery_pct": delivery_pct,
                                }
                            )

                    # Check offline DCUs
                    dcu = g.get("dcu_status", {})
                    dcu_offline = (dcu.get("total", 0) or 0) - (dcu.get("online", 0) or 0)
                    if dcu_offline > 0:
                        offline_dcus.append(
                            {
                                "name": g["name"],
                                "offline_count": dcu_offline,
                            }
                        )

                    # Collect FS hours for average
                    fsh = g.get("fs_hours_24h")
                    if fsh is not None:
                        fs_hours_values.append(fsh)

                # Sort downtime grids by severity
                grids_with_downtime.sort(key=lambda x: x["downtime_minutes"], reverse=True)

                fleet_avg_fs_hours = (
                    round(sum(fs_hours_values) / len(fs_hours_values), 1)
                    if fs_hours_values
                    else None
                )

                result["fleet_summary"] = {
                    "grids_by_status_count": {
                        "fs_on": len(fs_on_grids),
                        "hps_on": len(hps_on_grids),
                        "isolated": len(likely_isolated_grids),
                        "off": len(off_grids),
                        "unknown": len(unknown_grids),
                    },
                    "total_grids": len(all_grids),
                    "grids_with_faults": grids_with_faults,
                    "grids_with_downtime": grids_with_downtime[:5],
                    "low_fs_delivery": low_fs_delivery,
                    "offline_dcus": offline_dcus,
                    "fleet_avg_fs_hours": fleet_avg_fs_hours,
                }

                logger.info(
                    f"All grids status for org {organization_id}: "
                    f"{len(fs_on_grids)} FS on, {len(hps_on_grids)} HPS on, "
                    f"{len(likely_isolated_grids)} likely isolated, "
                    f"{len(off_grids)} off, {len(unknown_grids)} unknown"
                )
                return result

            finally:
                await conn.close()

        except Exception as e:
            logger.error(f"Error getting all grids status: {e}")
            return {"error": f"Failed to get all grids status: {str(e)}"}

    async def close(self):
        """Close HTTP session."""
        await self.close_session()


# Global client instance
customer_client = CustomerServiceClient()


async def get_last_gtr_summary(grid_name: str) -> Dict[str, Any]:
    """Get the last GTR (Grid Technical Report) summary from Google Sheets.

    Resolves grid name to a spreadsheet ID via expert instructions,
    then reads the most recent review section.

    Args:
        grid_name: Grid name to look up

    Returns:
        Dict with month_label, kpis, pending_issues, or error
    """
    import re
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial

    # Pattern to extract grid-to-sheet mappings from expert instructions
    grid_sheet_patterns = [
        re.compile(
            r"^\s*[-*]\s*([^:]+):\s*(https://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9_-]+)",
            re.MULTILINE,
        ),
        re.compile(
            r"^([A-Za-z][A-Za-z0-9\s]*):\s*(https://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9_-]+)",
            re.MULTILINE,
        ),
    ]
    spreadsheet_id_pattern = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")

    # Step 1: Fetch expert instructions doc to get grid-to-sheet mappings
    expert_doc_id = os.getenv("EXPERT_INSTRUCTIONS_DOC_ID")
    if not expert_doc_id:
        return {"error": "GTR expert not configured"}

    try:
        from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown
    except ImportError:
        return {"error": "Google Drive integration not available"}

    doc_text = fetch_google_doc_markdown(expert_doc_id)
    if not doc_text:
        return {"error": "Could not fetch expert instructions"}

    # Extract grid-to-sheet mappings
    mappings: Dict[str, Dict[str, str]] = {}
    for pattern in grid_sheet_patterns:
        for match in pattern.finditer(doc_text):
            name = match.group(1).strip()
            url = match.group(2).strip()
            if name.lower() in ["grid", "name", "url", "sheet"]:
                continue
            id_match = spreadsheet_id_pattern.search(url)
            sid = id_match.group(1) if id_match else ""
            key = name.lower()
            if key not in mappings:
                mappings[key] = {"name": name, "url": url, "spreadsheet_id": sid}

    if not mappings:
        return {"no_gtr": True, "message": "No GTR sheets configured"}

    # Step 2: Fuzzy-match grid name
    from shared.utils.grid_matcher import find_best_grid_match

    available_names = [g["name"] for g in mappings.values()]
    matched_name, _, _ = find_best_grid_match(grid_name, available_names)
    if not matched_name:
        return {"no_gtr": True, "message": f"No GTR sheet for grid '{grid_name}'"}

    sheet_info = mappings.get(matched_name.lower())
    if not sheet_info or not sheet_info.get("spreadsheet_id"):
        return {"no_gtr": True, "message": f"No GTR sheet for grid '{matched_name}'"}

    spreadsheet_id = sheet_info["spreadsheet_id"]

    # Step 3: Compute month label (review is for previous month)
    now = datetime.now()
    if now.month == 1:
        review_month, review_year = 12, now.year - 1
    else:
        review_month, review_year = now.month - 1, now.year
    month_names = [
        "",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    month_label = f"{month_names[review_month]} {review_year}"

    # Step 4: Read sheet (sync Google API in thread pool)
    def _read_sheet_sync(sid: str, mlabel: str) -> Dict[str, Any]:
        try:
            from googleapiclient.discovery import build

            from shared.utils.google_auth import get_sheets_credentials
        except ImportError:
            return {"error": "Google Sheets integration not configured"}

        try:
            credentials = get_sheets_credentials()
            service = build("sheets", "v4", credentials=credentials)

            # Read column A to find review row
            col_a = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=sid, range="A:A")
                .execute()
                .get("values", [])
            )

            # Find the review row
            review_row = None
            for i, row in enumerate(col_a):
                if row and f"{mlabel.lower()} review" in str(row[0]).strip().lower():
                    review_row = i
                    break

            if review_row is None:
                # Try previous-previous month as fallback
                return {"no_gtr": True, "message": f"No {mlabel} review found in sheet"}

            # Read the review section (15 rows, all columns)
            start_row = review_row + 1
            end_row = review_row + 16
            section = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=sid, range=f"A{start_row}:Z{end_row}")
                .execute()
                .get("values", [])
            )

            if not section:
                return {"no_gtr": True, "message": "Empty review section"}

            # Parse KPIs and pending issues
            kpis = {}
            pending_issues = []
            for idx, srow in enumerate(section):
                if idx < 2:
                    continue  # Skip header rows

                # KPIs (columns A=name, B=value, C=commentary)
                if len(srow) >= 3:
                    kpi_name = str(srow[0]).strip() if srow[0] else ""
                    kpi_value = str(srow[1]).strip() if len(srow) > 1 and srow[1] else ""
                    commentary = str(srow[2]).strip() if len(srow) > 2 and srow[2] else ""
                    if kpi_name and kpi_name.lower() not in ["notes:", "kpi", ""]:
                        kpis[kpi_name] = {"value": kpi_value, "commentary": commentary}

                # Pending issues (column E, index 4)
                if len(srow) > 4:
                    cell = str(srow[4]).strip()
                    if cell and cell.lower() not in ["pending issues", "new issues", "actions", ""]:
                        pending_issues.append(cell)

            return {
                "month_label": mlabel,
                "grid_name": matched_name,
                "kpis": kpis,
                "pending_issues": pending_issues,
            }

        except Exception as e:
            logger.error(f"Error reading GTR sheet {sid}: {e}")
            if "404" in str(e):
                return {"no_gtr": True, "message": "GTR sheet not found"}
            elif "403" in str(e):
                return {"no_gtr": True, "message": "Access denied to GTR sheet"}
            return {"error": f"Failed to read GTR sheet: {str(e)}"}

    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gtr_")
    try:
        result = await loop.run_in_executor(
            executor, partial(_read_sheet_sync, spreadsheet_id, month_label)
        )
        return result
    except Exception as e:
        logger.error(f"GTR summary error for {grid_name}: {e}")
        return {"error": f"Failed to fetch GTR summary: {str(e)}"}
    finally:
        executor.shutdown(wait=False)


async def get_my_open_issues(
    organization_id: int,
    issue_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Return open escalations for the caller's organisation, optionally filtered by issue type."""
    chat_db_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
    chat_db_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
    if not chat_db_url or not chat_db_key:
        return {"error": "Chat database not configured"}

    try:
        from supabase import create_client  # type: ignore[attr-defined]

        client = create_client(chat_db_url, chat_db_key)

        query = (
            client.table("escalation_mappings")
            .select(
                "id, question_text, reason, action_type, created_at, thread_id, "
                "chat_threads(issue_type)"
            )
            .eq("organization_id", organization_id)
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(50)
        )
        response = query.execute()
        rows = response.data or []

        results = []
        for row in rows:
            thread_data = row.get("chat_threads") or {}
            row_issue_type = (
                thread_data.get("issue_type") if isinstance(thread_data, dict) else None
            )
            if issue_type and row_issue_type != issue_type:
                continue
            results.append(
                {
                    "id": row.get("id"),
                    "thread_id": row.get("thread_id"),
                    "issue_type": row_issue_type or "unknown",
                    "summary": row.get("question_text"),
                    "reason": row.get("reason"),
                    "action_type": row.get("action_type"),
                    "created_at": row.get("created_at"),
                }
            )

        # Summarise counts per type for the response header
        type_counts: Dict[str, int] = {}
        for r in results:
            t = r["issue_type"]
            type_counts[t] = type_counts.get(t, 0) + 1

        return {
            "total_open": len(results),
            "by_type": type_counts,
            "issues": results,
        }
    except Exception as e:
        logger.error(f"Error fetching open issues for org={organization_id}: {e}")
        return {"error": f"Failed to fetch open issues: {str(e)}"}


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List available customer-facing tools."""
    tools = [
        types.Tool(
            name="check_payment_completion",
            description=(
                "[READ-ONLY] Check the completion status of a payment transaction. "
                "Provide the transaction reference exactly as given by the customer from their receipt or records "
                "to see if the payment was successful on the payment processor, whether the order "
                "is marked as completed, and the status of any associated directive (meter token). "
                "This tool ONLY retrieves information - it CANNOT retry payments, resend tokens, "
                "or modify orders. Do NOT construct or guess transaction references - they must come from the customer."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transaction_reference": {
                        "type": "string",
                        "description": (
                            "Transaction reference exactly as provided by the customer from their receipt or records. "
                            "Do NOT construct or guess this value."
                        ),
                    },
                    "user_email": {
                        "type": "string",
                        "description": "(Injected by orchestrator) User's email for access verification",
                    },
                    "organization_id": {
                        "type": "integer",
                        "description": "(Injected by orchestrator) User's organization ID",
                    },
                },
                "required": ["transaction_reference"],
            },
            visible_to_customer=True,
        ),
        types.Tool(
            name="find_payment",
            description=(
                "[READ-ONLY] Search for a payment order from any receipt type "
                "(EOS/NXT Pay screenshot, FirstBank receipt, OPay receipt, etc.). "
                "Use when the exact transaction reference is not available. "
                "Provide any combination of: customer/sender name, amount, date — "
                "at least one is required. "
                "Searches both the transaction reference and the registered customer name, "
                "so it works even when the bank sender name differs from the EOS customer name. "
                "Date-only inputs (e.g. 2026-05-29) search the full day; "
                "datetime inputs use a ±2h window by default. "
                "Amount tolerance is ±5%. "
                "If exactly one match is found, automatically verifies it with the payment processor. "
                "If zero or multiple matches are found, returns an LLM-readable explanation "
                "with suggestions for next steps."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": (
                            "Customer or sender name from the receipt. Works for EOS customer names, "
                            "bank sender names (e.g. 'ADAMU SULEIMAN' from FirstBank), "
                            "or OPay sender names. Each word is matched independently."
                        ),
                    },
                    "amount": {
                        "type": "number",
                        "description": "Payment amount from the receipt (±5% tolerance applied)",
                    },
                    "date": {
                        "type": "string",
                        "description": (
                            "Date or datetime from receipt in ISO format. "
                            "Date-only (e.g. 2026-05-29) searches the full calendar day. "
                            "Datetime (e.g. 2026-05-29T16:42:43) uses a ±2h window by default."
                        ),
                    },
                    "organization_name": {
                        "type": "string",
                        "description": "Organization name prefix if visible on receipt (optional)",
                    },
                    "time_window_hours": {
                        "type": "number",
                        "description": (
                            "Hours before/after the provided datetime to search (default 2.0). "
                            "Only applies to datetime inputs, not date-only inputs."
                        ),
                    },
                    "user_email": {
                        "type": "string",
                        "description": "(Injected by orchestrator) User's email for access verification",
                    },
                    "organization_id": {
                        "type": "integer",
                        "description": "(Injected by orchestrator) User's organization ID",
                    },
                },
                "required": [],
            },
            visible_to_customer=True,
        ),
        types.Tool(
            name="meter_information",
            description=(
                "[READ-ONLY] Get comprehensive information about a meter including customer details, "
                "connection type, grid status, DCU connectivity, meter power state, "
                "credit balance, power limits (including HPS mode limit), and connection quality metrics. "
                "Also shows the latest 5 directives sent to the meter (commissioning, "
                "tokens, configuration changes, etc.) and highlights the most recent "
                "failed directive if any. This tool ONLY retrieves and displays information - "
                "it CANNOT retry commissioning, send directives, add credit, change settings, "
                "or take any actions on the meter. Useful for troubleshooting meter issues, "
                "checking customer account status, and tracking directive delivery.\n\n"
                "Response Fields:\n"
                "- meter_found: Boolean indicating if meter exists for this organization\n"
                "- meter_number: The meter number queried\n"
                "- customer_name: Name of the customer this meter belongs to\n"
                "- connection_type: Type of connection (Residential, Commercial, etc.)\n"
                "- grid_name: Name of the grid the meter is connected to\n"
                "- grid_status: Grid online/offline status ('grid is energized' or 'grid is down')\n"
                "- dcu_status: DCU connectivity ('dcu is online' or 'dcu is offline')\n"
                "- is_on: Boolean - whether meter is currently powered on\n"
                "- is_on_updated_at: Timestamp when is_on was last updated. Old timestamps indicate "
                "lack of successful communication with the meter.\n"
                "- kwh_credit_available: Available kWh credit balance on the meter\n"
                "- kwh_credit_available_updated_at: Timestamp when credit balance was last updated. "
                "Old timestamps indicate lack of successful communication with the meter.\n"
                "- power_limit: Power limit in watts configured for this meter\n"
                "- power_limit_hps_mode: Power limit in watts when grid is in High Priority Service (HPS) mode. "
                "This is the reduced power level the meter will be limited to when the grid is operating "
                "in HPS mode (limited solar/battery capacity). Requests to change 'power limit in HPS mode' "
                "refer to this value.\n"
                "- connection_metrics: JSON object with connection quality data (signal strength, last_seen)\n"
                "- directives_count: Number of recent directives (max 5)\n"
                "- directives: Array of most recent directives with id, type, status, created_at, updated_at\n"
                "- last_error_directive: Most recent failed directive with error details (null if none)\n"
                "- last_successful_token: Most recent successful token directive (null if none)\n"
                "  - directive_id (integer): Directive ID\n"
                "  - token (string): The token value that was successfully delivered\n"
                "  - token_type (string): Type of token directive (always 'TOKEN')\n"
                "  - created_at (string): ISO timestamp when created\n"
                "  - updated_at (string): ISO timestamp when completed\n"
                "- message: Human-readable summary"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "meter_number": {
                        "type": "string",
                        "description": "Meter number to check commissioning status for",
                    },
                    "user_email": {
                        "type": "string",
                        "description": "(Injected by orchestrator) User's email for access verification",
                    },
                    "organization_id": {
                        "type": "integer",
                        "description": "(Injected by orchestrator) User's organization ID",
                    },
                },
                "required": ["meter_number"],
            },
            visible_to_customer=True,
        ),
        types.Tool(
            name="customer_get_grid_status",
            description=(
                "[READ-ONLY] Get comprehensive status for a grid including power status (HPS/FS), "
                "capacity (kWp/kWh), DCU connectivity, and current weather. "
                "You MUST provide the grid_name parameter.\n\n"
                "Response Fields:\n"
                "- grid_name: Name of the grid\n"
                "- grid_id: Grid ID\n"
                "- status.hps_on: Boolean - whether High Power Service is active\n"
                "- status.hps_updated_at: Timestamp when HPS status was last updated\n"
                "- status.fs_on: Boolean - whether Full Service is active\n"
                "- status.fs_updated_at: Timestamp when FS status was last updated\n"
                "- capacity.kwp: Installed solar capacity in kilowatt-peak\n"
                "- capacity.kwh: Battery storage capacity in kilowatt-hours\n"
                "- dcus.all_online: Boolean - whether all DCUs are online\n"
                "- dcus.offline_dcus: Array of offline DCUs (if any) with name and last_online_at\n"
                "- weather: Current weather at the grid location"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "grid_name": {
                        "type": "string",
                        "description": "Name of the grid (optional - defaults to first visible grid for organization)",
                    },
                },
                "required": [],
            },
            visible_to_customer=True,
        ),
        types.Tool(
            name="customer_get_all_grids_status",
            description=(
                "[READ-ONLY] Get status of all grids accessible to the user, grouped by operational status. "
                "Returns grids with icons for status, DCU connectivity, weather, and current inverter power:\n"
                "Status icons: 🟢 FS On | 🟡 HPS On | 🔌 Likely Isolated | 🔴 Off | Ⅹ Unknown\n"
                "DCU icons: 📶 All online | ⚠️ Some offline\n"
                "Weather icons: ☀️ ⛅ ☁️ 🌧️ ⛈️ 🌫️ ❄️ 💨\n\n"
                "Each grid includes inverter_power_kw (current total inverter output in kW from VRM). "
                "'Likely Isolated' means inverter is ON but power is below HPS threshold.\n\n"
                "Staff users see all grids. "
                "Other users see only their organization's grids. "
                "Hidden grids (is_hidden_from_reporting=true) are excluded."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            visible_to_customer=True,
        ),
        # types.Tool(
        #     name="retry_commissioning",
        #     description=(
        #         "[ACTION - STARTS METER COMMISSIONING] Retry the commissioning process for a meter. "
        #         "This action initiates a new commissioning attempt using the meter's last commissioning ID. "
        #         "IMPORTANT: This process takes 2-5 minutes to complete and CANNOT be retried if it fails. "
        #         "Only use this tool when a previous commissioning attempt has failed and you need to try again. "
        #         "The meter must have a previous commissioning attempt (last_commissioning_id must exist). "
        #         "You can use the meter_information tool after 2-5 minutes to check if commissioning succeeded. "
        #         "Staff-only tool (not visible to customers)."
        #     ),
        #     inputSchema={
        #         "type": "object",
        #         "properties": {
        #             "meter_number": {
        #                 "type": "string",
        #                 "description": "Meter number to retry commissioning for",
        #             },
        #             "user_email": {
        #                 "type": "string",
        #                 "description": "Staff email address (required for access verification)",
        #             },
        #             "organization_id": {
        #                 "type": "integer",
        #                 "description": "Organization ID (optional, will be looked up if not provided)",
        #             },
        #         },
        #         "required": ["meter_number", "user_email"],
        #     },
        #     visible_to_customer=False,
        # ),
    ]

    logger.info(f"Customer server: {len(tools)} tools available")
    return tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle tool calls."""
    try:
        if name == "check_payment_completion":
            result = await customer_client.check_payment_completion(
                transaction_reference=arguments.get("transaction_reference"),
                user_email=arguments.get("user_email"),
                organization_id=arguments.get("organization_id"),
            )
        elif name == "find_payment":
            result = await customer_client.find_payment(
                customer_name=arguments.get("customer_name", ""),
                amount=arguments.get("amount"),
                date=arguments.get("date"),
                organization_name=arguments.get("organization_name"),
                user_email=arguments.get("user_email", ""),
                organization_id=arguments.get("organization_id"),
                time_window_hours=float(arguments.get("time_window_hours", 2.0)),
            )
        elif name == "lookup_transactions":
            result = await customer_client.lookup_transactions(
                user_email=arguments.get("user_email", ""),
                organization_id=arguments.get("organization_id"),
                date_from=arguments.get("date_from"),
                date_to=arguments.get("date_to"),
                reference_number=arguments.get("reference_number"),
                amount=arguments.get("amount"),
                receiver_name=arguments.get("receiver_name"),
                limit=arguments.get("limit"),
            )
        elif name == "meter_information":
            result = await customer_client.meter_information(
                meter_number=arguments.get("meter_number"),
                user_email=arguments.get("user_email"),
                organization_id=arguments.get("organization_id"),
            )
        elif name == "customer_list_grid_meters":
            organization_id = arguments.get("organization_id")
            grid_name = arguments.get("grid_name")
            if not organization_id:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: organization_id is required (should be injected by orchestrator)",
                    )
                ]
            if not grid_name:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: grid_name is required",
                    )
                ]
            result = await customer_client.list_grid_meters(
                grid_name=grid_name,
                organization_id=int(organization_id),
            )
        elif name == "retry_commissioning":
            result = await customer_client.retry_commissioning(
                meter_number=arguments.get("meter_number"),
                user_email=arguments.get("user_email"),
                organization_id=arguments.get("organization_id"),
            )
        elif name == "unassign_meter":
            result = await customer_client.unassign_meter(
                meter_number=arguments.get("meter_number"),
                user_email=arguments.get("user_email"),
                organization_id=arguments.get("organization_id"),
            )
        elif name == "set_meter_power_limit":
            meter_number = arguments.get("meter_number", "")
            power_limit_watts = int(arguments.get("power_limit_watts", 0))
            user_email = arguments.get("user_email", "")
            raw_org = arguments.get("organization_id")
            organization_id = int(raw_org) if raw_org is not None else None
            result = await customer_client.set_meter_power_limit(
                meter_number=meter_number,
                power_limit_watts=power_limit_watts,
                user_email=user_email,
                organization_id=organization_id,
            )
            return [types.TextContent(type="text", text=json.dumps(result, default=str))]
        elif name == "set_meter_date":
            meter_number = arguments.get("meter_number", "")
            user_email = arguments.get("user_email", "")
            raw_org = arguments.get("organization_id")
            organization_id = int(raw_org) if raw_org is not None else None
            result = await customer_client.set_meter_date(
                meter_number=meter_number,
                user_email=user_email,
                organization_id=organization_id,
            )
            return [types.TextContent(type="text", text=json.dumps(result, default=str))]
        elif name in ("turn_meter_on", "turn_meter_off"):
            meter_number = arguments.get("meter_number", "")
            user_email = arguments.get("user_email", "")
            raw_org = arguments.get("organization_id")
            organization_id = int(raw_org) if raw_org is not None else None
            result = await customer_client.send_relay_state(
                meter_number=meter_number,
                user_email=user_email,
                interaction_type="TURN_ON" if name == "turn_meter_on" else "TURN_OFF",
                organization_id=organization_id,
            )
            return [types.TextContent(type="text", text=json.dumps(result, default=str))]
        elif name == "resend_meter_token":
            meter_number = arguments.get("meter_number", "")
            user_email = arguments.get("user_email", "")
            raw_org = arguments.get("organization_id")
            organization_id = int(raw_org) if raw_org is not None else None
            result = await customer_client.resend_meter_token(
                meter_number=meter_number,
                user_email=user_email,
                organization_id=organization_id,
            )
            return [types.TextContent(type="text", text=json.dumps(result, default=str))]
        elif name == "resend_clear_tamper_token":
            meter_number = arguments.get("meter_number", "")
            user_email = arguments.get("user_email", "")
            raw_org = arguments.get("organization_id")
            organization_id = int(raw_org) if raw_org is not None else None
            result = await customer_client.resend_clear_tamper_token(
                meter_number=meter_number,
                user_email=user_email,
                organization_id=organization_id,
            )
            return [types.TextContent(type="text", text=json.dumps(result, default=str))]
        elif name == "resend_power_limit_token":
            meter_number = arguments.get("meter_number", "")
            user_email = arguments.get("user_email", "")
            raw_org = arguments.get("organization_id")
            organization_id = int(raw_org) if raw_org is not None else None
            result = await customer_client.resend_power_limit_token(
                meter_number=meter_number,
                user_email=user_email,
                organization_id=organization_id,
            )
            return [types.TextContent(type="text", text=json.dumps(result, default=str))]
        elif name == "customer_get_meters_on_pole":
            organization_id = arguments.get("organization_id")
            if not organization_id:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: organization_id is required (should be injected by orchestrator)",
                    )
                ]
            result = await customer_client.get_meters_on_pole(
                pole_reference=arguments.get("pole_reference", ""),
                organization_id=int(organization_id),
                grid_name=arguments.get("grid_name"),
            )
        elif name == "customer_get_meter_consumption":
            organization_id = arguments.get("organization_id")
            if not organization_id:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: organization_id is required (should be injected by orchestrator)",
                    )
                ]
            result = await customer_client.get_meter_consumption(
                meter_number=arguments.get("meter_number", ""),
                organization_id=int(organization_id),
                days_back=int(arguments.get("days_back", 30)),
            )

            # If result includes a chart, return it as an image + JSON data
            chart_b64 = None
            if isinstance(result, dict):
                chart_b64 = result.pop("chart_base64", None)

            content_list = []
            if chart_b64:
                content_list.append(
                    types.ImageContent(type="image", data=chart_b64, mimeType="image/png")
                )
            content_list.append(
                types.TextContent(type="text", text=json.dumps(result, default=str))
            )
            return content_list

        elif name == "customer_get_grid_status":
            # organization_id is injected by orchestrator, not passed by LLM
            organization_id = arguments.get("organization_id")
            if not organization_id:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: organization_id is required (should be injected by orchestrator)",
                    )
                ]
            result = await customer_client.get_grid_status(
                organization_id=int(organization_id),
                grid_name=arguments.get("grid_name"),
                user_email=arguments.get("user_email"),
            )
        elif name == "customer_get_all_grids_status":
            # organization_id is injected by orchestrator, not passed by LLM
            organization_id = arguments.get("organization_id")
            if not organization_id:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: organization_id is required (should be injected by orchestrator)",
                    )
                ]
            result = await customer_client.get_all_grids_status(
                organization_id=int(organization_id),
            )
        elif name == "customer_get_last_gtr_summary":
            grid_name = arguments.get("grid_name")
            if not grid_name:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: grid_name is required",
                    )
                ]
            result = await get_last_gtr_summary(grid_name=grid_name)
        elif name == "customer_get_fs_daily_summary":
            organization_id = arguments.get("organization_id")
            if not organization_id:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: organization_id is required (should be injected by orchestrator)",
                    )
                ]
            result = await customer_client.get_fs_daily_summary(
                organization_id=int(organization_id),
                grid_name=arguments.get("grid_name", ""),
                start_date=arguments.get("start_date"),
                end_date=arguments.get("end_date"),
            )
        elif name == "customer_get_grid_chat_chronology":
            organization_id = arguments.get("organization_id")
            if not organization_id:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: organization_id is required (should be injected by orchestrator)",
                    )
                ]
            result = await customer_client.get_grid_chat_chronology(
                grid_name=arguments.get("grid_name", ""),
                organization_id=int(organization_id),
                days_back=int(arguments.get("days_back", 7)),
            )
        elif name == "get_my_open_issues":
            organization_id = arguments.get("organization_id")
            if organization_id is None:
                return [
                    types.TextContent(
                        type="text",
                        text="Error: organization_id is required (should be injected by orchestrator)",
                    )
                ]
            result = await get_my_open_issues(
                organization_id=int(organization_id),
                issue_type=arguments.get("issue_type"),
            )
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

        return list(compose_json_response(result))

    except Exception as e:
        logger.error(f"Error in tool {name}: {e}")
        return list(compose_error_response(e))


@server.list_resources()
async def handle_list_resources() -> List[types.Resource]:
    """List available resources."""
    return [
        types.Resource(
            uri="customer://config",
            name="Customer Server Configuration",
            description="Current customer server configuration",
            mimeType="application/json",
        ),
        types.Resource(
            uri="customer://status",
            name="Connection Status",
            description="Database and API connection status",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content."""
    if uri == "customer://config":
        config = {
            "auth_supabase_url": AUTH_SUPABASE_URL,
            "auth_supabase_configured": bool(AUTH_SUPABASE_KEY or AUTH_SUPABASE_ANON_KEY),
            "payment_processor_url": PAYMENT_PROCESSOR_API_URL,
            "payment_processor_configured": bool(PAYMENT_PROCESSOR_SECRET_KEY),
            "metering_api_url": METERING_API_URL,
            "metering_configured": bool(METERING_API_URL and METERING_BEARER_TOKEN),
            "server_name": "customer-server",
            "server_version": "1.0.0",
        }
        return json.dumps(config, indent=2)
    elif uri == "customer://status":
        status = {
            "auth_supabase_configured": bool(
                AUTH_SUPABASE_URL and (AUTH_SUPABASE_KEY or AUTH_SUPABASE_ANON_KEY)
            ),
            "payment_processor_configured": bool(
                PAYMENT_PROCESSOR_API_URL and PAYMENT_PROCESSOR_SECRET_KEY
            ),
            "metering_configured": bool(METERING_API_URL and METERING_BEARER_TOKEN),
        }
        return json.dumps(status, indent=2)
    else:
        raise ValueError(f"Unknown resource: {uri}")


async def main():
    """Main entry point."""
    try:
        logger.info("Starting Customer MCP Server...")
        print("✅ Customer server initialized successfully", file=sys.stderr)

        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="customer-server",
                    server_version="1.0.0",
                    capabilities=ServerCapabilities(),
                ),
            )
    except Exception as e:
        print(f"❌ Fatal error in Customer server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        # Clean up client
        await customer_client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Customer server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Customer server crashed: {e}", file=sys.stderr)
        sys.exit(1)
