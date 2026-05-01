"""
Cron Parser Utility

Parses natural language time expressions into cron expressions and calculates next run times.
Default timezone is configured by DEFAULT_TIMEZONE env var (default: UTC).
All times are stored in UTC in the database.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pytz  # type: ignore[import-untyped]

# Default timezone for user inputs
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "UTC")

# Common time expression patterns
TIME_PATTERNS = {
    # Daily patterns
    r"daily\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "daily",
    r"every\s+day\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "daily",
    # Monthly patterns
    r"(?:on\s+the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?every\s+month\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "monthly",
    r"monthly\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "monthly",
    r"every\s+month\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "monthly",
    # Biweekly patterns
    r"every\s+other\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "biweekly",
    r"biweekly\s+on\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "biweekly",
    r"every\s+2\s+weeks?\s+on\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "biweekly",
    r"fortnightly\s+on\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "biweekly",
    # Weekly patterns
    r"every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "weekly",
    r"(mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "weekly",
    # Weekday patterns
    r"weekdays?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "weekdays",
    r"every\s+weekday\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "weekdays",
    # Hourly patterns
    r"every\s+(\d+)\s+hours?": "hourly",
    r"hourly": "hourly_simple",
    # One-time patterns
    r"tomorrow\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?": "tomorrow",
    r"in\s+(\d+)\s+(minutes?|hours?|days?)": "relative",
}

# Day name to cron weekday mapping (0 = Sunday in cron)
DAY_TO_CRON = {
    "sunday": 0,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
}


def parse_time_to_24h(hour: int, minute: int, ampm: Optional[str]) -> Tuple[int, int]:
    """Convert 12-hour time to 24-hour format."""
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    return hour, minute


def parse_time_expression(
    expr: str, timezone: str = DEFAULT_TIMEZONE
) -> Tuple[Optional[str], datetime, str]:
    """
    Parse natural language time expression to cron expression and next_run_at.

    Args:
        expr: Natural language time expression (e.g., "daily at 9am", "tomorrow at 3pm")
        timezone: User's timezone (default: configured by DEFAULT_TIMEZONE env var)

    Returns:
        Tuple of (cron_expression, next_run_at_utc, schedule_type)
        - cron_expression is None for one-time schedules
        - next_run_at_utc is the next execution time in UTC
        - schedule_type is 'once' or 'recurring'
    """
    expr_lower = expr.lower().strip()
    tz = pytz.timezone(timezone)
    now_local = datetime.now(tz)
    now_utc = datetime.now(pytz.UTC)

    # Daily patterns
    daily_match = re.match(
        r"daily\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", expr_lower, re.IGNORECASE
    )
    if not daily_match:
        daily_match = re.match(
            r"every\s+day\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", expr_lower, re.IGNORECASE
        )

    if daily_match:
        hour = int(daily_match.group(1))
        minute = int(daily_match.group(2) or 0)
        ampm = daily_match.group(3)
        hour, minute = parse_time_to_24h(hour, minute, ampm)

        # Create cron in local timezone, then convert to UTC
        local_time = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        utc_time = local_time.astimezone(pytz.UTC)

        # Adjust to next occurrence if time has passed today
        if utc_time <= now_utc:
            utc_time += timedelta(days=1)

        # Cron expression in UTC
        utc_hour = utc_time.hour
        cron = f"{minute} {utc_hour} * * *"
        return cron, utc_time, "recurring"

    # Monthly patterns
    monthly_match = re.match(
        r"(?:on\s+the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?every\s+month\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        expr_lower,
        re.IGNORECASE,
    )
    if not monthly_match:
        monthly_match = re.match(
            r"monthly\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
            expr_lower,
            re.IGNORECASE,
        )
    if not monthly_match:
        monthly_match = re.match(
            r"every\s+month\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
            expr_lower,
            re.IGNORECASE,
        )

    if monthly_match:
        day_of_month = int(monthly_match.group(1))
        if day_of_month < 1 or day_of_month > 31:
            raise ValueError(f"Invalid day of month: {day_of_month}. Must be 1-31.")
        hour = int(monthly_match.group(2))
        minute = int(monthly_match.group(3) or 0)
        ampm = monthly_match.group(4)
        hour, minute = parse_time_to_24h(hour, minute, ampm)

        # Calculate next occurrence
        local_time = now_local.replace(
            day=day_of_month, hour=hour, minute=minute, second=0, microsecond=0
        )
        utc_time = local_time.astimezone(pytz.UTC)

        # If this month's occurrence has passed, move to next month
        if utc_time <= now_utc:
            if now_local.month == 12:
                local_time = local_time.replace(year=now_local.year + 1, month=1)
            else:
                local_time = local_time.replace(month=now_local.month + 1)
            utc_time = local_time.astimezone(pytz.UTC)

        utc_hour = utc_time.hour
        cron = f"{minute} {utc_hour} {day_of_month} * *"
        return cron, utc_time, "recurring"

    # Biweekly patterns (must be before weekly to match "every other" first)
    biweekly_match = re.match(
        r"every\s+other\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        expr_lower,
        re.IGNORECASE,
    )
    if not biweekly_match:
        biweekly_match = re.match(
            r"biweekly\s+on\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
            expr_lower,
            re.IGNORECASE,
        )
    if not biweekly_match:
        biweekly_match = re.match(
            r"every\s+2\s+weeks?\s+on\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
            expr_lower,
            re.IGNORECASE,
        )
    if not biweekly_match:
        biweekly_match = re.match(
            r"fortnightly\s+on\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
            expr_lower,
            re.IGNORECASE,
        )

    if biweekly_match:
        day_name = biweekly_match.group(1).rstrip("s").lower()
        hour = int(biweekly_match.group(2))
        minute = int(biweekly_match.group(3) or 0)
        ampm = biweekly_match.group(4)
        hour, minute = parse_time_to_24h(hour, minute, ampm)

        # Calculate next occurrence (same logic as weekly)
        target_weekday = DAY_TO_CRON[day_name]
        local_time = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

        days_ahead = target_weekday - now_local.weekday()
        if days_ahead < 0 or (days_ahead == 0 and local_time <= now_local):
            days_ahead += 7

        local_time += timedelta(days=days_ahead)
        utc_time = local_time.astimezone(pytz.UTC)

        # Cron stores the weekly pattern; biweekly skip logic is in update_next_run()
        utc_hour = utc_time.hour
        cron_weekday = target_weekday
        cron = f"{minute} {utc_hour} * * {cron_weekday}"
        return cron, utc_time, "biweekly"

    # Weekly patterns
    weekly_match = re.match(
        r"every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        expr_lower,
        re.IGNORECASE,
    )
    if not weekly_match:
        weekly_match = re.match(
            r"(mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
            expr_lower,
            re.IGNORECASE,
        )

    if weekly_match:
        day_name = weekly_match.group(1).rstrip("s").lower()
        hour = int(weekly_match.group(2))
        minute = int(weekly_match.group(3) or 0)
        ampm = weekly_match.group(4)
        hour, minute = parse_time_to_24h(hour, minute, ampm)

        # Calculate next occurrence
        target_weekday = DAY_TO_CRON[day_name]
        local_time = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # Find next occurrence of this weekday
        days_ahead = target_weekday - now_local.weekday()
        if days_ahead < 0 or (days_ahead == 0 and local_time <= now_local):
            days_ahead += 7

        local_time += timedelta(days=days_ahead)
        utc_time = local_time.astimezone(pytz.UTC)

        # Cron expression in UTC
        utc_hour = utc_time.hour
        cron_weekday = target_weekday
        cron = f"{minute} {utc_hour} * * {cron_weekday}"
        return cron, utc_time, "recurring"

    # Weekdays pattern
    weekdays_match = re.match(
        r"(?:every\s+)?weekdays?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        expr_lower,
        re.IGNORECASE,
    )
    if weekdays_match:
        hour = int(weekdays_match.group(1))
        minute = int(weekdays_match.group(2) or 0)
        ampm = weekdays_match.group(3)
        hour, minute = parse_time_to_24h(hour, minute, ampm)

        local_time = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        utc_time = local_time.astimezone(pytz.UTC)

        # Find next weekday
        while utc_time <= now_utc or utc_time.weekday() >= 5:  # 5=Sat, 6=Sun
            utc_time += timedelta(days=1)

        utc_hour = utc_time.hour
        cron = f"{minute} {utc_hour} * * 1-5"
        return cron, utc_time, "recurring"

    # Hourly patterns
    hourly_match = re.match(r"every\s+(\d+)\s+hours?", expr_lower, re.IGNORECASE)
    if hourly_match:
        interval = int(hourly_match.group(1))
        # Round to next interval
        utc_time = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=interval)
        cron = f"0 */{interval} * * *"
        return cron, utc_time, "recurring"

    if expr_lower == "hourly":
        utc_time = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        cron = "0 * * * *"
        return cron, utc_time, "recurring"

    # Tomorrow pattern (one-time)
    tomorrow_match = re.match(
        r"tomorrow\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", expr_lower, re.IGNORECASE
    )
    if tomorrow_match:
        hour = int(tomorrow_match.group(1))
        minute = int(tomorrow_match.group(2) or 0)
        ampm = tomorrow_match.group(3)
        hour, minute = parse_time_to_24h(hour, minute, ampm)

        local_time = (now_local + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        utc_time = local_time.astimezone(pytz.UTC)

        return None, utc_time, "once"

    # Relative time patterns (one-time)
    relative_match = re.match(r"in\s+(\d+)\s+(minutes?|hours?|days?)", expr_lower, re.IGNORECASE)
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2).lower().rstrip("s")

        if unit == "minute":
            utc_time = now_utc + timedelta(minutes=amount)
        elif unit == "hour":
            utc_time = now_utc + timedelta(hours=amount)
        elif unit == "day":
            utc_time = now_utc + timedelta(days=amount)
        else:
            raise ValueError(f"Unknown time unit: {unit}")

        return None, utc_time, "once"

    # Try to parse as a specific time today/tomorrow
    time_only_match = re.match(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", expr_lower, re.IGNORECASE)
    if time_only_match:
        hour = int(time_only_match.group(1))
        minute = int(time_only_match.group(2) or 0)
        ampm = time_only_match.group(3)
        hour, minute = parse_time_to_24h(hour, minute, ampm)

        local_time = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        utc_time = local_time.astimezone(pytz.UTC)

        # If time has passed, schedule for tomorrow
        if utc_time <= now_utc:
            utc_time += timedelta(days=1)

        return None, utc_time, "once"

    raise ValueError(
        f"Could not parse time expression: '{expr}'. "
        "Try formats like: 'daily at 9am', 'every monday at 10am', "
        "'every other monday at 9am', 'monthly on the 1st at 9am', "
        "'tomorrow at 3pm', 'in 2 hours'"
    )


def calculate_next_run(cron_expression: str, after: Optional[datetime] = None) -> datetime:
    """
    Calculate the next run time from a cron expression.

    Args:
        cron_expression: Standard cron format (minute hour day month weekday)
        after: Calculate next run after this time (default: now UTC)

    Returns:
        Next run time in UTC
    """
    try:
        from croniter import croniter  # type: ignore[import-untyped]
    except ImportError:
        raise ImportError("croniter library required. Install with: pip install croniter")

    if after is None:
        after = datetime.now(pytz.UTC)
    elif after.tzinfo is None:
        after = after.replace(tzinfo=pytz.UTC)

    cron = croniter(cron_expression, after)
    next_run: datetime = cron.get_next(datetime)

    # Ensure UTC timezone
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=pytz.UTC)

    return next_run


def format_schedule_display(
    schedule_type: str,
    cron_expression: Optional[str],
    next_run_at: datetime,
    timezone: str = DEFAULT_TIMEZONE,
) -> str:
    """
    Format a schedule for user display.

    Args:
        schedule_type: 'once' or 'recurring'
        cron_expression: Cron expression (for recurring)
        next_run_at: Next run time in UTC
        timezone: User's display timezone

    Returns:
        Human-readable schedule description
    """
    tz = pytz.timezone(timezone)
    local_next = next_run_at.astimezone(tz)

    if schedule_type == "once":
        return f"Once: {local_next.strftime('%b %d, %Y at %I:%M %p')} {tz.zone}"

    # Parse cron for recurring description
    if cron_expression:
        parts = cron_expression.split()
        if len(parts) >= 5:
            minute, hour, day, month, weekday = parts[:5]

            # Hourly (every hour or every N hours) - check first before trying int(hour)
            if hour == "*":
                return "Every hour"

            if "*/" in hour:
                interval = hour.split("/")[1]
                return f"Every {interval} hours"

            # Monthly (day is numeric, weekday is *)
            if day != "*" and day.isdigit() and weekday == "*":
                day_num = int(day)
                suffix = (
                    "th"
                    if 11 <= day_num <= 13
                    else {1: "st", 2: "nd", 3: "rd"}.get(day_num % 10, "th")
                )
                utc_time = datetime.now(pytz.UTC).replace(
                    hour=int(hour), minute=int(minute), second=0, microsecond=0
                )
                local_time = utc_time.astimezone(tz)
                return (
                    f"Monthly on the {day_num}{suffix} "
                    f"at {local_time.strftime('%I:%M %p')} {tz.zone}"
                )

            # Daily
            if weekday == "*" and day == "*":
                utc_time = datetime.now(pytz.UTC).replace(
                    hour=int(hour), minute=int(minute), second=0, microsecond=0
                )
                local_time = utc_time.astimezone(tz)
                return f"Daily at {local_time.strftime('%I:%M %p')} {tz.zone}"

            # Weekdays
            if weekday == "1-5":
                utc_time = datetime.now(pytz.UTC).replace(
                    hour=int(hour), minute=int(minute), second=0, microsecond=0
                )
                local_time = utc_time.astimezone(tz)
                return f"Weekdays at {local_time.strftime('%I:%M %p')} {tz.zone}"

            # Specific weekday (weekly or biweekly)
            if weekday.isdigit():
                day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                day_name = day_names[int(weekday)]
                utc_time = datetime.now(pytz.UTC).replace(
                    hour=int(hour), minute=int(minute), second=0, microsecond=0
                )
                local_time = utc_time.astimezone(tz)
                if schedule_type == "biweekly":
                    return f"Every other {day_name} at {local_time.strftime('%I:%M %p')} {tz.zone}"
                return f"Every {day_name} at {local_time.strftime('%I:%M %p')} {tz.zone}"

    return f"Next: {local_next.strftime('%b %d at %I:%M %p')} {tz.zone}"


def generate_friendly_name(command: str, schedule_type: str, time_expr: str) -> str:
    """Generate a user-friendly name for a schedule."""
    cmd_short = command[:30] + "..." if len(command) > 30 else command
    if schedule_type in ("recurring", "biweekly"):
        return f"{time_expr.title()} - {cmd_short}"
    else:
        return f"Once: {time_expr.title()} - {cmd_short}"
