"""Tests for agents.py's _cost_display -- the run-cost table's UI-layer
formatting, paired with SupabaseReader.get_run_usage_by_skill's None-means-
unknown contract (see test_supabase_reader_run_usage.py for the reader side).
"""

from nicegui_app.pages.agents import _cost_display


def test_none_renders_as_unknown_dash_not_zero():
    # None means "at least one run used an unpriced model" -- must never
    # read as "$0.00", which would imply the runs were free.
    assert _cost_display(None) == "—"


def test_known_cost_renders_with_four_decimal_places():
    # LLM costs are often fractions of a cent; 2dp would round a real,
    # nonzero cost down to "$0.00" and misleadingly imply free.
    assert _cost_display("0.003") == "$0.0030"


def test_zero_cost_renders_as_real_zero_not_dash():
    # A real, known $0 (e.g. a function-only workflow) is different from
    # "cost unknown" -- it must render as an actual amount, not "—".
    assert _cost_display("0") == "$0.0000"
