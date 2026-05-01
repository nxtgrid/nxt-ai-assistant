"""Tests for event summary compaction logic."""

from datetime import datetime, timedelta, timezone

from orchestrator.graphs.persistent_agent_graph import (
    _apply_compaction,
    _build_compaction_prompt,
    _should_compact,
)


def test_should_compact_returns_false_when_recently_compacted():
    last_compacted = datetime.now(timezone.utc) - timedelta(days=3)
    assert _should_compact(last_compacted, event_count=50) is False


def test_should_compact_returns_true_after_7_days():
    last_compacted = datetime.now(timezone.utc) - timedelta(days=8)
    assert _should_compact(last_compacted, event_count=10) is True


def test_should_compact_returns_true_when_never_compacted():
    assert _should_compact(None, event_count=10) is True


def test_should_compact_returns_false_with_few_events():
    assert _should_compact(None, event_count=3) is False


def test_build_compaction_prompt_includes_events():
    events = [
        {
            "event_type": "scheduled_wake",
            "created_at": "2026-03-10T08:00:00Z",
            "result": {"assessment": "Grid stable, 480 connections"},
        },
        {
            "event_type": "equipment_alert",
            "created_at": "2026-03-12T14:30:00Z",
            "result": {"assessment": "DCU offline, 3 meters affected"},
        },
    ]
    prompt = _build_compaction_prompt(events, "ExampleGrid", "2026-03-10", "2026-03-16")
    assert "ExampleGrid" in prompt
    assert "480 connections" in prompt
    assert "DCU offline" in prompt


def test_apply_compaction_trims_to_max_weeks():
    existing = [{"week_start": f"2026-01-{i:02d}", "summary": f"Week {i}"} for i in range(1, 10)]
    new_summary = {
        "week_start": "2026-03-10",
        "week_end": "2026-03-16",
        "summary": "All stable",
        "event_count": 5,
    }
    result = _apply_compaction(existing, new_summary, max_weeks=8)
    assert len(result) == 8
    assert result[-1]["week_start"] == "2026-03-10"
