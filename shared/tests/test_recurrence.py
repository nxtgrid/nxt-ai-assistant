"""Tests for shared recurrence / cron parsing utilities."""

from datetime import datetime

import pytest
import pytz  # type: ignore[import-untyped]

from shared.scheduling.recurrence import (
    advance,
    calculate_next_run,
    format_schedule_display,
    parse_time_expression,
)


class TestParseExistingPatterns:
    """Existing /schedule patterns must keep working after the move to shared/."""

    def test_daily(self):
        cron, next_run, stype = parse_time_expression("daily at 9am", "UTC")
        assert cron == "0 9 * * *"
        assert stype == "recurring"
        assert next_run.tzinfo is not None

    def test_weekly(self):
        cron, _, stype = parse_time_expression("every monday at 10am", "UTC")
        # 0=Sun, 1=Mon
        assert cron == "0 10 * * 1"
        assert stype == "recurring"

    def test_biweekly_type(self):
        cron, _, stype = parse_time_expression("every other monday at 9am", "UTC")
        assert cron == "0 9 * * 1"
        assert stype == "biweekly"

    def test_monthly_by_date(self):
        cron, _, stype = parse_time_expression("monthly on the 15th at 6pm", "UTC")
        assert cron == "0 18 15 * *"
        assert stype == "recurring"

    def test_one_time(self):
        cron, next_run, stype = parse_time_expression("in 2 hours", "UTC")
        assert cron is None
        assert stype == "once"

    def test_unparseable_raises(self):
        with pytest.raises(ValueError):
            parse_time_expression("sometime soon-ish", "UTC")


class TestOrdinalWeekday:
    """New: 'first monday of the month' etc. (cron weekday#N)."""

    def test_first_monday(self):
        cron, next_run, stype = parse_time_expression("first monday of the month at 9am", "UTC")
        assert cron == "0 9 * * 1#1"
        assert stype == "recurring"
        # Next run must actually be the first Monday of some month at 09:00 UTC
        assert next_run.hour == 9
        assert next_run.weekday() == 0  # Monday in Python's Mon=0 scheme
        assert next_run.day <= 7

    def test_third_friday_every_month(self):
        cron, _, stype = parse_time_expression("third friday of every month at 5pm", "UTC")
        assert cron == "0 17 * * 5#3"
        assert stype == "recurring"

    def test_ordinal_digit_form(self):
        cron, _, _ = parse_time_expression("2nd wednesday of the month at 8am", "UTC")
        assert cron == "0 8 * * 3#2"

    def test_last_day_of_month(self):
        cron, next_run, stype = parse_time_expression("last day of the month at 9am", "UTC")
        assert cron == "0 9 L * *"
        assert stype == "recurring"
        assert next_run.day >= 28  # last day is always late in the month


class TestAdvance:
    """advance() is the single source of truth, including the biweekly skip."""

    def test_recurring_weekly_advance(self):
        cron = "0 10 * * 1"  # every Monday 10:00 UTC
        base = datetime(2026, 6, 1, tzinfo=pytz.UTC)  # Mon Jun 1 2026, 00:00
        nxt = advance("recurring", cron, after=base)
        # Next Monday 10:00 is the same calendar day (10:00 > 00:00)
        assert nxt.weekday() == 0
        assert nxt.hour == 10
        assert nxt.day == 1

    def test_biweekly_skips_one_week(self):
        cron = "0 10 * * 1"
        base = datetime(2026, 6, 1, tzinfo=pytz.UTC)
        weekly = advance("recurring", cron, after=base)
        biweekly = advance("biweekly", cron, after=base)
        # Biweekly must be exactly one extra week beyond the weekly next-run
        assert (biweekly - weekly).days == 7
        assert biweekly.weekday() == 0

    def test_ordinal_advance_is_monthly(self):
        cron = "0 9 * * 1#1"  # first Monday
        base = datetime(2026, 6, 1, tzinfo=pytz.UTC)
        first = calculate_next_run(cron, after=base)
        second = advance("recurring", cron, after=first)
        # Consecutive first-Mondays land in different months
        assert (first.month, first.year) != (second.month, second.year)
        assert first.weekday() == 0 and second.weekday() == 0


class TestFormatDisplay:
    def test_ordinal_display(self):
        out = format_schedule_display(
            "recurring", "0 9 * * 1#1", datetime(2026, 7, 6, 9, tzinfo=pytz.UTC), "UTC"
        )
        assert "1st Mon" in out

    def test_last_day_display(self):
        out = format_schedule_display(
            "recurring", "0 9 L * *", datetime(2026, 6, 30, 9, tzinfo=pytz.UTC), "UTC"
        )
        assert "Last day of month" in out

    def test_biweekly_display(self):
        out = format_schedule_display(
            "biweekly", "0 10 * * 1", datetime(2026, 6, 1, 10, tzinfo=pytz.UTC), "UTC"
        )
        assert "Every other Mon" in out
