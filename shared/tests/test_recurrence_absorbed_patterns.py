"""Patterns absorbed into shared/scheduling/recurrence.py from the schedule MCP server.

The schedule server carried its own 413-line `parse_time_expression` that had
drifted from the shared one in both directions. These tests pin the behaviour
that came *from* the schedule server, so the consolidated parser cannot quietly
lose it again.

The shared-only patterns (ordinal weekday, last day of month) are covered by
test_recurrence.py.
"""

from datetime import datetime

import pytest
import pytz  # type: ignore[import-untyped]

from shared.scheduling.recurrence import (
    normalize_time_expression,
    parse_time_expression,
)


class TestNormalizeTimeExpression:
    """Period-separated am/pm, stray whitespace, and invalid 24h+meridiem."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("at 9 a.m.", "at 9am"),
            ("at 3 p.m.", "at 3pm"),
            ("at 9 A.M.", "at 9am"),
            ("at 3 P.M.", "at 3pm"),
            ("at 9 am", "at 9am"),
            ("at 9am", "at 9am"),
            ("daily  at   9  am", "daily at 9am"),
        ],
    )
    def test_meridiem_and_whitespace(self, raw, expected):
        assert normalize_time_expression(raw) == expected

    def test_strips_meridiem_from_24h_time(self):
        # 13:02pm is not a real time; the meridiem is dropped, not applied.
        assert normalize_time_expression("13:02pm") == "13:02"

    @pytest.mark.parametrize(
        "raw",
        [
            "in a moment",
            "send a message at 5pm",
            "a memo at 9am",
            "9 american dates",
        ],
    )
    def test_does_not_corrupt_standalone_a_or_p_words(self, raw):
        """Regression guard.

        The original schedule-server implementation matched a bare ``\\ba.?\\s*m.?``
        anywhere, so "in a moment" became "in amoment" and "send a message"
        became "send amessage". The meridiem match is now anchored to a
        preceding digit.
        """
        out = normalize_time_expression(raw)
        assert "amoment" not in out
        assert "amessage" not in out
        assert "amemo" not in out
        # the words themselves survive intact
        for word in raw.split():
            if word not in ("9",):
                assert word.rstrip(".") in out or word.lower().rstrip(".") in out


class TestMeridiemActuallyApplied:
    """The bug this consolidation fixes.

    Before absorbing normalisation, the shared parser matched "at 3 p.m." with
    its optional ``(am|pm)?`` group unset and silently produced 03:00 -- twelve
    hours early. Broadcasts schedule through this parser.
    """

    def test_pm_with_periods_is_afternoon(self):
        _, next_run, _ = parse_time_expression("at 3 p.m.", "UTC")
        assert next_run.hour == 15, "3 p.m. must resolve to 15:00, not 03:00"

    def test_am_with_periods_is_morning(self):
        _, next_run, _ = parse_time_expression("at 9 a.m.", "UTC")
        assert next_run.hour == 9

    def test_daily_pm_with_periods(self):
        cron, _, stype = parse_time_expression("daily at 5 p.m.", "UTC")
        assert cron == "0 17 * * *"
        assert stype == "recurring"


class TestIsoDate:
    def test_iso_date_with_24h_time(self):
        cron, next_run, stype = parse_time_expression("2030-09-16 at 10:00", "UTC")
        assert cron is None
        assert stype == "once"
        assert (next_run.year, next_run.month, next_run.day) == (2030, 9, 16)
        assert (next_run.hour, next_run.minute) == (10, 0)

    def test_iso_date_with_meridiem_and_on_prefix(self):
        _, next_run, stype = parse_time_expression("on 2030-09-16 at 10am", "UTC")
        assert stype == "once"
        assert (next_run.month, next_run.day, next_run.hour) == (9, 16, 10)


class TestNamedMonth:
    @pytest.mark.parametrize(
        "expr",
        [
            "September 16th at 10am",
            "on September 16 at 10am",
            "16th September at 10am",
            "on 16 sept at 10am",
        ],
    )
    def test_named_month_forms(self, expr):
        cron, next_run, stype = parse_time_expression(expr, "UTC")
        assert cron is None
        assert stype == "once"
        assert (next_run.month, next_run.day, next_run.hour) == (9, 16, 10)

    def test_abbreviated_month(self):
        _, next_run, stype = parse_time_expression("dec 25th at 9am", "UTC")
        assert stype == "once"
        assert (next_run.month, next_run.day, next_run.hour) == (12, 25, 9)

    def test_past_date_rolls_to_next_year(self):
        now = datetime.now(pytz.UTC)
        # January 1st has passed for most of the year; whichever year is chosen,
        # the result must be in the future.
        _, next_run, _ = parse_time_expression("january 1st at 9am", "UTC")
        assert next_run > now


class TestRelativeMonths:
    def test_in_n_months(self):
        now = datetime.now(pytz.UTC)
        _, next_run, stype = parse_time_expression("in 2 months at 9am", "UTC")
        assert stype == "once"
        assert next_run > now
        assert next_run.hour == 9

    def test_in_months_and_days(self):
        now = datetime.now(pytz.UTC)
        _, next_run, stype = parse_time_expression("in 3 months and 19 days at 10am", "UTC")
        assert stype == "once"
        assert next_run > now
        assert next_run.hour == 10

    def test_month_overflow_clamps_to_valid_day(self):
        """A 31st rolling into a 30-day month must clamp, not raise."""
        _, next_run, _ = parse_time_expression("in 1 months at 9am", "UTC")
        assert next_run.day <= 31


class TestWeekdayOfNextRun:
    """next_run_at must land on the weekday the user actually named.

    The shared parser compared a cron weekday (Sunday=0, Monday=1) against
    ``datetime.weekday()`` (Monday=0, Sunday=6), so every weekly and biweekly
    schedule's first run was one day late -- "every monday" first firing on a
    Tuesday. The existing tests only asserted the cron string, which was
    correct, so nothing caught it.
    """

    # (expression, cron weekday, Python weekday)
    CASES = [
        ("every sunday at 10am", 0, 6),
        ("every monday at 10am", 1, 0),
        ("every tuesday at 10am", 2, 1),
        ("every wednesday at 10am", 3, 2),
        ("every thursday at 10am", 4, 3),
        ("every friday at 10am", 5, 4),
        ("every saturday at 10am", 6, 5),
    ]

    @pytest.mark.parametrize("expr,cron_wd,py_wd", CASES)
    def test_weekly_next_run_lands_on_named_day(self, expr, cron_wd, py_wd):
        cron, next_run, _ = parse_time_expression(expr, "UTC")
        assert cron == f"0 10 * * {cron_wd}"
        assert next_run.weekday() == py_wd, (
            f"{expr!r} produced {next_run:%A}, expected "
            f"{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][py_wd]}"
        )

    @pytest.mark.parametrize("expr,cron_wd,py_wd", CASES)
    def test_biweekly_next_run_lands_on_named_day(self, expr, cron_wd, py_wd):
        day = expr.split()[1]
        cron, next_run, stype = parse_time_expression(f"every other {day} at 10am", "UTC")
        assert stype == "biweekly"
        assert cron == f"0 10 * * {cron_wd}"
        assert next_run.weekday() == py_wd

    def test_next_run_is_in_the_future(self):
        now = datetime.now(pytz.UTC)
        for expr, _, _ in self.CASES:
            _, next_run, _ = parse_time_expression(expr, "UTC")
            assert next_run > now
            assert (next_run - now).days < 7


class TestPatternPrecedenceUnchanged:
    """The absorbed one-time patterns must not shadow existing recurring ones."""

    @pytest.mark.parametrize(
        "expr,cron,stype",
        [
            ("daily at 9am", "0 9 * * *", "recurring"),
            ("every monday at 10am", "0 10 * * 1", "recurring"),
            ("every other monday at 9am", "0 9 * * 1", "biweekly"),
            ("monthly on the 15th at 6pm", "0 18 15 * *", "recurring"),
            ("weekdays at 8am", "0 8 * * 1-5", "recurring"),
            ("hourly", "0 * * * *", "recurring"),
            ("first monday of the month at 9am", "0 9 * * 1#1", "recurring"),
            ("last day of the month at 6pm", "0 18 L * *", "recurring"),
        ],
    )
    def test_recurring_patterns_still_win(self, expr, cron, stype):
        got_cron, _, got_type = parse_time_expression(expr, "UTC")
        assert (got_cron, got_type) == (cron, stype)

    @pytest.mark.parametrize("expr", ["in a moment", "gibberish that is not a time", ""])
    def test_unparseable_still_raises(self, expr):
        with pytest.raises(ValueError):
            parse_time_expression(expr, "UTC")
