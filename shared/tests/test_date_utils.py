"""Tests for shared.utils.date_utils.to_local_time.

This helper was duplicated verbatim in customer_mcp_server.py and
equipment_diagnostics_mcp_server.py. Neither copy had any coverage, so these
pin the behaviour — including the error path, which is easy to get subtly
wrong — before the two copies collapse into one.
"""

from datetime import datetime, timezone

from shared.utils.date_utils import to_local_time


class TestToLocalTime:
    def test_none_passes_through(self):
        assert to_local_time(None, "Africa/Lagos") is None

    def test_naive_input_is_assumed_utc(self):
        # 12:00 naive is read as 12:00 UTC, which is 13:00 in Lagos (UTC+1).
        out = to_local_time(datetime(2026, 1, 15, 12, 0), "Africa/Lagos")
        assert (out.hour, out.minute) == (13, 0)
        assert out.utcoffset().total_seconds() == 3600

    def test_aware_input_is_converted_not_relabelled(self):
        utc = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        out = to_local_time(utc, "Africa/Lagos")
        assert out.hour == 13
        # Same instant, different wall clock.
        assert out.timestamp() == utc.timestamp()

    def test_utc_to_utc_is_identity(self):
        utc = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        assert to_local_time(utc, "UTC").timestamp() == utc.timestamp()

    def test_dst_is_honoured(self):
        """New York is UTC-5 in January and UTC-4 in July."""
        jan = to_local_time(datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc), "America/New_York")
        jul = to_local_time(datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc), "America/New_York")
        assert jan.hour == 12
        assert jul.hour == 13


class TestToLocalTimeErrorPath:
    """An unknown timezone must not raise — the caller gets its input back."""

    def test_unknown_timezone_returns_input(self):
        utc = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        out = to_local_time(utc, "Not/AZone")
        assert out == utc

    def test_unknown_timezone_with_naive_input_returns_utc_aware(self):
        """The UTC tzinfo is applied before the timezone lookup, so it survives
        the failure. Pinning this because it is the non-obvious half: the value
        handed back is not the object that was passed in."""
        out = to_local_time(datetime(2026, 1, 15, 12, 0), "Not/AZone")
        assert out.tzinfo is timezone.utc
        assert out.hour == 12

    def test_empty_timezone_returns_input(self):
        utc = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        assert to_local_time(utc, "") == utc
