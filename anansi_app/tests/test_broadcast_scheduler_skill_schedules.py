"""Tests for broadcast_scheduler.py's skill-schedule handoff (Phase 5 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 1): "the
scheduler starts the run and hands off... it does not execute steps".

process_due_skill_schedules queries user_schedules directly (skill rows
don't fit the scheduled_messages queue's one-row-one-chat model the
existing command path uses) and POSTs each due row to chat_orchestrator's
/skills/dispatch-schedule -- entity fan-out and authorization happen there,
not in this process, since only chat_orchestrator has Auth DB credentials
configured.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import broadcast_scheduler as bs  # noqa: E402


class _FakeNot:
    """Mirrors the supabase-py `query.not_.is_(...)` chaining shim -- `not_`
    is a property, not a method, matching escalation_repository.py's tests
    elsewhere in this repo."""

    def __init__(self, query: "_FakeQuery") -> None:
        self._query = query

    def is_(self, *_a, **_k) -> "_FakeQuery":
        return self._query


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    @property
    def not_(self) -> _FakeNot:
        return _FakeNot(self)

    def eq(self, *_a, **_k):
        return self

    def lte(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


class _RecordingUpdateQuery:
    def __init__(self, table):
        self._table = table

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def select(self, *_a, **_k):
        return _FakeQuery(self._rows)

    def update(self, payload):
        self.updates.append(payload)
        return _RecordingUpdateQuery(self)


class _FakeSupabase:
    def __init__(self, rows):
        self.table_obj = _FakeTable(rows)

    def table(self, _name):
        return self.table_obj


def _schedule_row(**overrides):
    row = {
        "id": "sched-1",
        "skill_id": "skill-1",
        "schedule_type": "recurring",
        "cron_expression": "0 6 * * *",
        "is_active": True,
    }
    row.update(overrides)
    return row


class TestProcessDueSkillSchedules:
    def test_no_credentials_configured_returns_zero(self, monkeypatch):
        monkeypatch.delenv("CHAT_DB_URL", raising=False)
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("CHAT_DB_SERVICE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)

        assert bs.process_due_skill_schedules(verbose=False) == 0

    def test_no_due_schedules_returns_zero(self, monkeypatch):
        monkeypatch.setenv("CHAT_DB_URL", "https://example.test")
        monkeypatch.setenv("CHAT_DB_SERVICE_KEY", "key")
        fake_supabase = _FakeSupabase([])

        with patch("supabase.create_client", return_value=fake_supabase):
            assert bs.process_due_skill_schedules(verbose=False) == 0

    def test_dispatches_each_due_schedule_and_advances_next_run(self, monkeypatch):
        monkeypatch.setenv("CHAT_DB_URL", "https://example.test")
        monkeypatch.setenv("CHAT_DB_SERVICE_KEY", "key")
        monkeypatch.setenv("API_KEY", "test-key")
        bs.API_KEY = "test-key"  # module-level constant read at import time

        fake_supabase = _FakeSupabase([_schedule_row()])

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "dispatched": 2,
            "skipped": 0,
            "failed": 0,
            "reason": None,
        }
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with (
            patch("supabase.create_client", return_value=fake_supabase),
            patch("httpx.Client", return_value=mock_client),
            patch("broadcast_scheduler.advance_recurrence", return_value=datetime.now(timezone.utc) + timedelta(days=1)),
        ):
            result = bs.process_due_skill_schedules(verbose=False)

        assert result == 1
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.args[0].endswith("/skills/dispatch-schedule")
        assert call_kwargs.kwargs["json"] == {"schedule_id": "sched-1"}
        assert call_kwargs.kwargs["headers"] == {"X-Api-Key": "test-key"}
        # next_run_at must have been advanced, not left as-is.
        assert fake_supabase.table_obj.updates
        assert "next_run_at" in fake_supabase.table_obj.updates[0]

    def test_dispatch_failure_still_advances_next_run_at(self, monkeypatch):
        # A single bad tick must not wedge a recurring schedule forever --
        # same rationale as _update_recurring_schedule's own comment.
        monkeypatch.setenv("CHAT_DB_URL", "https://example.test")
        monkeypatch.setenv("CHAT_DB_SERVICE_KEY", "key")

        fake_supabase = _FakeSupabase([_schedule_row()])
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.side_effect = RuntimeError("connection refused")

        with (
            patch("supabase.create_client", return_value=fake_supabase),
            patch("httpx.Client", return_value=mock_client),
            patch("broadcast_scheduler.advance_recurrence", return_value=datetime.now(timezone.utc) + timedelta(days=1)),
        ):
            bs.process_due_skill_schedules(verbose=False)

        assert fake_supabase.table_obj.updates
        assert "next_run_at" in fake_supabase.table_obj.updates[0]

    def test_one_time_schedule_is_completed_not_rescheduled(self, monkeypatch):
        monkeypatch.setenv("CHAT_DB_URL", "https://example.test")
        monkeypatch.setenv("CHAT_DB_SERVICE_KEY", "key")

        fake_supabase = _FakeSupabase([_schedule_row(schedule_type="once")])
        mock_response = MagicMock()
        mock_response.json.return_value = {"dispatched": 1, "skipped": 0, "failed": 0, "reason": None}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with (
            patch("supabase.create_client", return_value=fake_supabase),
            patch("httpx.Client", return_value=mock_client),
        ):
            bs.process_due_skill_schedules(verbose=False)

        assert fake_supabase.table_obj.updates[0]["status"] == "completed"
        assert fake_supabase.table_obj.updates[0]["is_active"] is False
