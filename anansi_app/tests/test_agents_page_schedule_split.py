"""Tests for agents.py's _split_schedule_rows -- the Runs page's User
Schedules table filtering/ordering, paired with the render-side change in
_render_scheduled_jobs_section (see agents.py).

Behavior under test (per the "Runs" filter/sort fix):
  * cancelled/completed rows older than 3 days (by updated_at) are dropped
    entirely -- stale history isn't operationally useful.
  * active/paused rows are never dropped by age.
  * everything else is split into (past, future) at `now`, compared against
    next_run_at, each group ascending -- so past+future read as one
    continuous earliest-to-latest timeline with `now` as the seam.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nicegui_app.pages.agents import _split_schedule_rows

_NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _schedule(
    name: str,
    status: str,
    *,
    next_run_at: str | None,
    updated_at: str | None = None,
) -> dict:
    return {
        "friendly_name": name,
        "status": status,
        "next_run_at": next_run_at,
        "updated_at": updated_at,
    }


# ── age filter (cancelled/completed only) ───────────────────────────────


def test_recently_cancelled_row_is_kept():
    row = _schedule(
        "a",
        "cancelled",
        next_run_at=_iso(_NOW - timedelta(days=1)),
        updated_at=_iso(_NOW - timedelta(days=1)),
    )
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == [row]
    assert future == []


def test_recently_completed_row_is_kept():
    row = _schedule(
        "a",
        "completed",
        next_run_at=_iso(_NOW - timedelta(hours=1)),
        updated_at=_iso(_NOW - timedelta(hours=1)),
    )
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == [row]


def test_cancelled_row_older_than_3_days_is_dropped():
    row = _schedule(
        "a",
        "cancelled",
        next_run_at=_iso(_NOW - timedelta(days=10)),
        updated_at=_iso(_NOW - timedelta(days=10)),
    )
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == []
    assert future == []


def test_completed_row_older_than_3_days_is_dropped():
    row = _schedule(
        "a",
        "completed",
        next_run_at=_iso(_NOW - timedelta(days=4)),
        updated_at=_iso(_NOW - timedelta(days=4)),
    )
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == []
    assert future == []


def test_cancelled_row_exactly_3_days_old_is_kept_inclusive_boundary():
    row = _schedule(
        "a",
        "cancelled",
        next_run_at=_iso(_NOW - timedelta(days=3)),
        updated_at=_iso(_NOW - timedelta(days=3)),
    )
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == [row]


def test_cancelled_row_just_over_3_days_old_is_dropped():
    row = _schedule(
        "a",
        "cancelled",
        next_run_at=_iso(_NOW - timedelta(days=3, seconds=1)),
        updated_at=_iso(_NOW - timedelta(days=3, seconds=1)),
    )
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == []
    assert future == []


def test_age_filter_uses_updated_at_not_next_run_at():
    # A recurring schedule cancelled today can still carry a stale
    # next_run_at from before it was cancelled (cancel/pause never clear
    # next_run_at -- see schedule_mcp_server.py's cancel/pause handlers).
    # The recency filter must key off updated_at (when it actually became
    # cancelled), not the stale next_run_at, or a long-ago-scheduled-but-
    # cancelled-today row would be wrongly dropped.
    row = _schedule(
        "a",
        "cancelled",
        next_run_at=_iso(_NOW - timedelta(days=30)),
        updated_at=_iso(_NOW - timedelta(minutes=5)),
    )
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == [row]


def test_active_row_never_dropped_regardless_of_age():
    row = _schedule(
        "a",
        "active",
        next_run_at=_iso(_NOW - timedelta(days=30)),
        updated_at=_iso(_NOW - timedelta(days=30)),
    )
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == [row]


def test_paused_row_never_dropped_regardless_of_age():
    row = _schedule(
        "a",
        "paused",
        next_run_at=_iso(_NOW + timedelta(days=1)),
        updated_at=_iso(_NOW - timedelta(days=30)),
    )
    past, future = _split_schedule_rows([row], now=_NOW)
    assert future == [row]


def test_missing_updated_at_on_terminal_status_fails_open_and_is_kept():
    row = _schedule("a", "completed", next_run_at=_iso(_NOW), updated_at=None)
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == [row]


# ── past/future split + ordering ────────────────────────────────────────


def test_past_row_next_run_at_before_now_goes_in_past_group():
    row = _schedule("a", "active", next_run_at=_iso(_NOW - timedelta(minutes=1)))
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == [row]
    assert future == []


def test_future_row_next_run_at_after_now_goes_in_future_group():
    row = _schedule("a", "active", next_run_at=_iso(_NOW + timedelta(minutes=1)))
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == []
    assert future == [row]


def test_next_run_at_exactly_now_goes_in_past_group():
    row = _schedule("a", "active", next_run_at=_iso(_NOW))
    past, future = _split_schedule_rows([row], now=_NOW)
    assert past == [row]


def test_each_group_sorted_ascending_by_next_run_at():
    soon = _schedule("soon", "active", next_run_at=_iso(_NOW + timedelta(hours=1)))
    later = _schedule("later", "active", next_run_at=_iso(_NOW + timedelta(days=2)))
    just_now = _schedule("just_now", "completed", next_run_at=_iso(_NOW - timedelta(minutes=1)), updated_at=_iso(_NOW))
    yesterday = _schedule("yesterday", "cancelled", next_run_at=_iso(_NOW - timedelta(days=1)), updated_at=_iso(_NOW - timedelta(days=1)))

    past, future = _split_schedule_rows([later, soon, just_now, yesterday], now=_NOW)

    assert [r["friendly_name"] for r in past] == ["yesterday", "just_now"]
    assert [r["friendly_name"] for r in future] == ["soon", "later"]


def test_terminal_row_missing_next_run_at_sorts_to_front_of_past():
    no_next_run = _schedule("no_next_run", "completed", next_run_at=None, updated_at=_iso(_NOW))
    yesterday = _schedule("yesterday", "cancelled", next_run_at=_iso(_NOW - timedelta(days=1)), updated_at=_iso(_NOW - timedelta(days=1)))

    past, future = _split_schedule_rows([yesterday, no_next_run], now=_NOW)

    assert [r["friendly_name"] for r in past] == ["no_next_run", "yesterday"]
    assert future == []


def test_active_row_missing_next_run_at_sorts_to_end_of_future():
    no_next_run = _schedule("no_next_run", "active", next_run_at=None)
    soon = _schedule("soon", "active", next_run_at=_iso(_NOW + timedelta(hours=1)))

    past, future = _split_schedule_rows([no_next_run, soon], now=_NOW)

    assert past == []
    assert [r["friendly_name"] for r in future] == ["soon", "no_next_run"]


def test_empty_input_returns_empty_groups():
    assert _split_schedule_rows([], now=_NOW) == ([], [])


def test_now_defaults_to_current_time_when_omitted():
    # Smoke test only -- just confirms no exception when `now` isn't passed,
    # since real callers (agents.py) never pass it explicitly.
    row = _schedule("a", "active", next_run_at=_iso(datetime.now(timezone.utc) + timedelta(days=1)))
    past, future = _split_schedule_rows([row])
    assert row in past or row in future
