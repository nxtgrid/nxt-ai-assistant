"""Pure formatting/time helpers for the customer server.

Extracted verbatim out of customer_mcp_server.py as part of the Phase 4 file
split (see client.py for the composed CustomerServiceClient and the index of
which mixin holds what).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from servers.customer_server.client_base import DEFAULT_TIMEZONE, STALENESS_THRESHOLD

from shared.utils.date_utils import to_local_time


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
    local_dt = to_local_time(utc_dt, tz_name)
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

