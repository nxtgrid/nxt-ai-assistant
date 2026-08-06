"""Unit tests for SupabaseReader.get_run_usage_by_skill (Phase 0 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md).

A minimal fluent fake stands in for the real Supabase client -- only
table/select/gte/execute are needed for this reader method.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from services.supabase_reader import SupabaseReader


class _FakeQuery:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._preds: list[Any] = []

    def select(self, *_args, **_kwargs):
        return self

    def gte(self, col: str, val: str):
        self._preds.append(lambda r: r.get(col, "") >= val)
        return self

    def execute(self):
        rows = [r for r in self._rows if all(p(r) for p in self._preds)]
        return SimpleNamespace(data=rows)


class _FakeClient:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.queries: list[str] = []

    def table(self, name: str):
        self.queries.append(name)
        assert name == "agent_work_packets"
        return _FakeQuery(self._rows)


def _reader(rows: list[dict]) -> SupabaseReader:
    reader = SupabaseReader.__new__(SupabaseReader)  # bypass real DB init
    reader.client = _FakeClient(rows)
    return reader


def _row(
    packet_type: str = "kpi_report",
    packet_status: str = "completed",
    created_at: str = "2026-08-05T10:00:00",
    token_usage: dict | None = None,
) -> dict:
    return {
        "packet_type": packet_type,
        "packet_status": packet_status,
        "created_at": created_at,
        "token_usage": token_usage or {},
    }


def test_no_client_returns_empty_dict():
    reader = SupabaseReader.__new__(SupabaseReader)
    reader.client = None

    assert reader.get_run_usage_by_skill() == {}


def test_sums_tokens_and_cost_across_runs_of_same_packet_type():
    rows = [
        _row(token_usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": "0.001"}),
        _row(token_usage={"input_tokens": 200, "output_tokens": 75, "cost_usd": "0.002"}),
    ]
    reader = _reader(rows)

    result = reader.get_run_usage_by_skill()

    assert result["kpi_report"]["runs"] == 2
    assert result["kpi_report"]["input_tokens"] == 300
    assert result["kpi_report"]["output_tokens"] == 125
    assert result["kpi_report"]["cost_usd"] == "0.003"


def test_groups_by_packet_type_separately():
    rows = [
        _row(packet_type="kpi_report", token_usage={"input_tokens": 10, "output_tokens": 5}),
        _row(packet_type="grid_analysis", token_usage={"input_tokens": 20, "output_tokens": 10}),
    ]
    reader = _reader(rows)

    result = reader.get_run_usage_by_skill()

    assert set(result.keys()) == {"kpi_report", "grid_analysis"}
    assert result["kpi_report"]["input_tokens"] == 10
    assert result["grid_analysis"]["input_tokens"] == 20


def test_counts_failed_runs():
    rows = [
        _row(packet_status="completed"),
        _row(packet_status="failed"),
        _row(packet_status="failed"),
    ]
    reader = _reader(rows)

    result = reader.get_run_usage_by_skill()

    assert result["kpi_report"]["runs"] == 3
    assert result["kpi_report"]["failures"] == 2


def test_function_only_run_counts_but_contributes_no_tokens():
    # token_usage stays {} for a run with no [llm] steps (see
    # WorkflowExecutor._persist_token_usage's all-zero skip) -- it must still
    # count as a run, just with zero tokens and no cost contribution.
    rows = [_row(token_usage={})]
    reader = _reader(rows)

    result = reader.get_run_usage_by_skill()

    assert result["kpi_report"]["runs"] == 1
    assert result["kpi_report"]["input_tokens"] == 0
    # No priced (or unpriced) LLM run occurred -- cost is a real, known zero,
    # not "unknown". has_unpriced_run is only set when a model was seen and
    # unrecognized.
    assert result["kpi_report"]["cost_usd"] == "0"


def test_any_unpriced_run_makes_the_whole_bucket_cost_none():
    # One run's model wasn't in PRICES (cost_usd omitted) -- the aggregate
    # must not silently report the OTHER run's cost as if it were the total.
    rows = [
        _row(token_usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": "0.001"}),
        _row(token_usage={"input_tokens": 100, "output_tokens": 50}),  # no cost_usd: unpriced
    ]
    reader = _reader(rows)

    result = reader.get_run_usage_by_skill()

    assert result["kpi_report"]["input_tokens"] == 200  # tokens still sum
    assert result["kpi_report"]["cost_usd"] is None  # but cost is unknown, not partial

    # Never silently guess a total in place of the (correct) unknown.
    assert result["kpi_report"]["cost_usd"] != "0.001"


def test_only_includes_rows_within_the_window():
    reader = _reader([_row(created_at="2026-07-01T00:00:00")])

    result = reader.get_run_usage_by_skill(days_back=7)

    # Query is date-filtered server-side by the fake's .gte() -- a row from
    # over a month ago (relative to "now" in any real test run) is excluded.
    assert result == {}
