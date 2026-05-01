"""
Shared date/time utilities with function composition.

Provides reusable date parsing, filtering, and formatting functions.
"""

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


def parse_iso_with_timezone(date_string: str) -> datetime:
    """
    Parse ISO format date string with timezone handling.

    Pure function: date_string -> datetime

    Args:
        date_string: ISO format date string

    Returns:
        Parsed datetime object
    """
    return datetime.fromisoformat(date_string.replace("Z", "+00:00"))


def compose_date_filter(
    start_date: Optional[str] = None, end_date: Optional[str] = None, date_field: str = "created"
) -> Callable[[Dict[str, Any]], bool]:
    """
    Compose a date filter function.

    Higher-order function that returns a predicate.

    Args:
        start_date: Start date (ISO format)
        end_date: End date (ISO format)
        date_field: Field name containing date

    Returns:
        Predicate function for filtering
    """
    start_dt = parse_iso_with_timezone(start_date) if start_date else None
    end_dt = parse_iso_with_timezone(end_date) if end_date else None

    def filter_predicate(item: Dict[str, Any]) -> bool:
        """Check if item falls within date range."""
        item_date = item.get(date_field)
        if not item_date:
            return False

        try:
            item_dt = parse_iso_with_timezone(item_date)

            if start_dt and item_dt < start_dt:
                return False

            if end_dt and item_dt > end_dt:
                return False

            return True

        except ValueError:
            # Skip items with invalid dates
            return False

    return filter_predicate


def filter_by_date_range(
    items: List[Dict[str, Any]],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    date_field: str = "created",
) -> List[Dict[str, Any]]:
    """
    Filter items by date range.

    Functional approach using composed predicate.

    Args:
        items: List of items to filter
        start_date: Start date (ISO format)
        end_date: End date (ISO format)
        date_field: Field name containing date

    Returns:
        Filtered list of items
    """
    if not start_date and not end_date:
        return items

    predicate = compose_date_filter(start_date, end_date, date_field)
    return list(filter(predicate, items))


def get_default_date_range(days_ago: int = 90) -> Dict[str, str]:
    """
    Get default date range (now - days_ago to now).

    Pure function: days_ago -> {start_date, end_date}

    Args:
        days_ago: Number of days back from today

    Returns:
        Dict with 'start_date' and 'end_date' in YYYY-MM-DD format
        Note: end_date includes the full day by adding one day for proper <= comparison
    """
    today = datetime.now()
    start = today - timedelta(days=days_ago)
    # Add one day to end_date to ensure full day inclusion with <= operator
    end = today + timedelta(days=1)

    return {"start_date": start.strftime("%Y-%m-%d"), "end_date": end.strftime("%Y-%m-%d")}


def format_timestamp(timestamp: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    Format datetime to string.

    Pure function: datetime -> string

    Args:
        timestamp: Datetime object
        fmt: Format string

    Returns:
        Formatted date string
    """
    return timestamp.strftime(fmt)


def ensure_full_day_inclusion(date_str: str) -> str:
    """
    Ensure a date string includes the full day for <= comparisons.

    If the date is just YYYY-MM-DD, add one day to capture the full day
    when using <= operator in queries.

    Args:
        date_str: Date string in YYYY-MM-DD format

    Returns:
        Modified date string that ensures full day inclusion
    """
    try:
        # Parse the date
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        # Add one day to ensure full day inclusion with <= operator
        next_day = date_obj + timedelta(days=1)
        return next_day.strftime("%Y-%m-%d")
    except ValueError:
        # If it's not a simple YYYY-MM-DD date, return as-is
        return date_str


def compose_date_range_query(
    start_date: Optional[str] = None, end_date: Optional[str] = None, default_days: int = 90
) -> Dict[str, str]:
    """
    Compose date range query with defaults.

    Functional composition of default date range and overrides.
    Ensures end dates include the full day for proper <= comparisons.

    Args:
        start_date: Optional start date
        end_date: Optional end date
        default_days: Default days back if not specified

    Returns:
        Dict with resolved start_date and end_date
    """
    defaults = get_default_date_range(default_days)

    resolved_end_date = end_date or defaults["end_date"]
    # If user provided an explicit end date, ensure it includes the full day
    if end_date:
        resolved_end_date = ensure_full_day_inclusion(end_date)

    return {"start_date": start_date or defaults["start_date"], "end_date": resolved_end_date}
