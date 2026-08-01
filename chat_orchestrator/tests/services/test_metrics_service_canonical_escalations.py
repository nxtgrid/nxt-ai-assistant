"""Regression tests for the canonical (escalations-table) escalation-metrics
query in metrics_service.py.

escalations has no customer_chat_id column (unlike escalation_mappings), so
this resolves it via a batched chat_sessions lookup on chat_session_id.
These tests exist to prove that resolution behaves correctly and degrades
safely (returns None, not partial/wrong data) on any failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from orchestrator.services.metrics_service import _canonical_escalation_metrics_rows

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
_END = _START + timedelta(days=1)


class _FakeResponse:
    def __init__(self, data: List[Dict[str, Any]]):
        self.data = data


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], raise_on_execute: Optional[Exception] = None):
        self._rows = rows
        self._filters: List[tuple] = []
        self._raise = raise_on_execute

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, list(vals)))
        return self

    def gte(self, *_a, **_k):
        return self

    def lt(self, *_a, **_k):
        return self

    def execute(self):
        if self._raise is not None:
            raise self._raise
        matches = []
        for row in self._rows:
            ok = True
            for col, val in self._filters:
                if isinstance(val, list):
                    if row.get(col) not in val:
                        ok = False
                elif row.get(col) != val:
                    ok = False
            if ok:
                matches.append(row)
        return _FakeResponse(matches)


class _FakeClient:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]], raise_on: Optional[str] = None):
        self._tables = tables
        self._raise_on = raise_on

    def table(self, name):
        raise_exc = RuntimeError("db down") if name == self._raise_on else None
        return _FakeQuery(self._tables.get(name, []), raise_on_execute=raise_exc)


def test_resolves_customer_chat_id_via_chat_sessions():
    client = _FakeClient(
        {
            "escalations": [
                {
                    "chat_session_id": "session-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "resolved_at": "2026-01-01T01:00:00Z",
                }
            ],
            "chat_sessions": [{"id": "session-1", "telegram_chat_id": "12345"}],
        }
    )

    rows = _canonical_escalation_metrics_rows(client, _START, _END)

    assert rows == [
        {
            "customer_chat_id": "12345",
            "created_at": "2026-01-01T00:00:00Z",
            "resolved_at": "2026-01-01T01:00:00Z",
        }
    ]


def test_skips_rows_whose_session_has_no_telegram_chat_id():
    client = _FakeClient(
        {
            "escalations": [{"chat_session_id": "session-1", "created_at": "t", "resolved_at": None}],
            "chat_sessions": [{"id": "session-1", "telegram_chat_id": None}],
        }
    )

    assert _canonical_escalation_metrics_rows(client, _START, _END) == []


def test_returns_empty_list_when_no_escalations_in_window():
    client = _FakeClient({"escalations": []})

    assert _canonical_escalation_metrics_rows(client, _START, _END) == []


def test_returns_none_on_error_so_caller_falls_back_to_legacy():
    client = _FakeClient({}, raise_on="escalations")

    assert _canonical_escalation_metrics_rows(client, _START, _END) is None


def test_returns_none_when_session_batch_lookup_fails():
    client = _FakeClient(
        {"escalations": [{"chat_session_id": "session-1", "created_at": "t", "resolved_at": None}]},
        raise_on="chat_sessions",
    )

    assert _canonical_escalation_metrics_rows(client, _START, _END) is None
