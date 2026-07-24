"""Unit tests for SupabaseReader's unified Tickets reader (Task 8).

The reader's ticket methods are SYNC (this file's convention), so no asyncio.
A small fluent fake supabase-py client backs the tests — table/select/eq/in_/
filter/order/limit/execute — applying filters in Python against seeded rows,
including jsonb-path filters like ``metadata->>ticket_ref``.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

from services.supabase_reader import SupabaseReader


def _json_path(row: dict, col: str) -> Any:
    """Resolve ``metadata->>ticket_ref``-style columns; else a plain column."""
    if "->>" in col:
        base, key = col.split("->>", 1)
        container = row.get(base) or {}
        return container.get(key) if isinstance(container, dict) else None
    return row.get(col)


class _FakeQuery:
    def __init__(self, rows: list[dict]):
        self._rows = [copy.deepcopy(r) for r in rows]
        self._preds = []
        self._order = None
        self._limit = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, col, val):
        self._preds.append(lambda r: _json_path(r, col) == val)
        return self

    def in_(self, col, values):
        vals = list(values)
        self._preds.append(lambda r: _json_path(r, col) in vals)
        return self

    def filter(self, col, op, val):
        assert op == "eq", f"fake only supports eq filter, got {op}"
        self._preds.append(lambda r: _json_path(r, col) == val)
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._rows = self._rows[start : end + 1]
        return self

    def execute(self):
        rows = [r for r in self._rows if all(p(r) for p in self._preds)]
        if self._order is not None:
            col, desc = self._order
            rows.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return SimpleNamespace(data=rows, count=len(rows))


class _FakeClient:
    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables

    def table(self, name):
        return _FakeQuery(self._tables.get(name, []))


_LONG_Q = "Q " + ("word " * 80)  # > 200 chars, to exercise summary truncation


def _seed() -> dict[str, list[dict]]:
    return {
        "internal_tickets": [
            {
                "ticket_ref": "INT-1",
                "escalation_mapping_id": 10,
                "session_id": "s1",
                "organization_id": 1,
                "grid_name": "GridA",
                "summary": "Internal summary one",
                "description": "desc int1",
                "status": "open",
                "source": "escalation",
                "assignee_email": None,
                "labels": [],
                "updated_at": "2026-07-20T12:00:00",
                "resolved_at": None,
                "created_at": "2026-07-20T10:00:00",
            },
            {
                "ticket_ref": "INT-2",
                "escalation_mapping_id": None,
                "session_id": "s2",
                "organization_id": 2,
                "grid_name": "GridB",
                "summary": "Second internal ticket",
                "description": "desc int2",
                "status": "done",
                "source": "notify",
                "assignee_email": None,
                "labels": [],
                "updated_at": "2026-07-19T10:00:00",
                "resolved_at": "2026-07-19T11:00:00",
                "created_at": "2026-07-19T10:00:00",
            },
        ],
        "escalation_mappings": [
            {
                "id": 10,
                "session_id": "s1",
                "ticket_ref": "INT-1",
                "ticket_backend": "internal",
                "organization_id": 1,
                "org_hashtag": "#orgA",
                "reason": "needs help",
                "question_text": "why is it broken",
                "customer_username": "alice",
                "customer_chat_id": 111,
                "customer_topic_id": 7,
                "customer_email": "alice@example.com",
                "escalation_message_id": 5001,
                "is_active": True,
                "resolved_at": None,
                "created_at": "2026-07-20T09:00:00",
            },
            {
                "id": 20,
                "session_id": "s3",
                "ticket_ref": "JIRA-100",
                "ticket_backend": "jira",
                "organization_id": 3,
                "org_hashtag": "#orgC",
                "reason": "jira reason",
                "question_text": _LONG_Q,
                "customer_username": "bob",
                "customer_chat_id": 222,
                "customer_topic_id": 9,
                "customer_email": "bob@example.com",
                "escalation_message_id": 6002,
                "is_active": True,
                "resolved_at": None,
                "created_at": "2026-07-22T12:00:00",
            },
            {
                "id": 21,
                "session_id": "s4",
                "ticket_ref": "JIRA-101",
                "ticket_backend": "jira",
                "organization_id": 3,
                "org_hashtag": "#orgC",
                "reason": None,
                "question_text": "resolved question",
                "customer_username": "carol",
                "customer_chat_id": 333,
                "customer_topic_id": None,
                "customer_email": None,
                "escalation_message_id": 6003,
                "is_active": False,
                "resolved_at": "2026-07-15T10:00:00",
                "created_at": "2026-07-15T10:00:00",
            },
        ],
        "internal_ticket_comments": [
            {
                "ticket_ref": "INT-1",
                "author": "staff1",
                "body": "first staff note",
                "is_public": True,
                "source": "staff",
                "created_at": "2026-07-20T11:00:00",
            },
            {
                "ticket_ref": "INT-1",
                "author": "jira-sync",
                "body": "synced from jira",
                "is_public": True,
                "source": "jira",
                "created_at": "2026-07-20T12:00:00",
            },
        ],
        "chat_messages": [
            {
                "content": "customer follow-up",
                "role": "user",
                "metadata": {"ticket_ref": "INT-1", "ticket_role": "comment", "is_public": True},
                "created_at": "2026-07-20T11:30:00",
            },
            {
                "content": "jira tagged message",
                "role": "model",
                "metadata": {"ticket_ref": "JIRA-100", "ticket_role": "comment"},
                "created_at": "2026-07-22T13:00:00",
            },
            {
                "content": "unrelated noise",
                "role": "user",
                "metadata": {},
                "created_at": "2026-07-22T14:00:00",
            },
        ],
    }


def _reader() -> SupabaseReader:
    reader = SupabaseReader.__new__(SupabaseReader)  # bypass real DB init
    reader.client = _FakeClient(_seed())
    return reader


def test_list_tickets_unifies_both_backends():
    rows = _reader().list_tickets()
    refs = [r["ticket_ref"] for r in rows]
    # Newest-first by created_at.
    assert refs == ["JIRA-100", "INT-1", "INT-2", "JIRA-101"]
    backends = {r["ticket_ref"]: r["backend"] for r in rows}
    assert backends == {
        "JIRA-100": "jira",
        "INT-1": "internal",
        "INT-2": "internal",
        "JIRA-101": "jira",
    }


def test_internal_row_is_enriched_from_mapping():
    row = next(r for r in _reader().list_tickets() if r["ticket_ref"] == "INT-1")
    assert row["org_hashtag"] == "#orgA"
    assert row["grid_name"] == "GridA"
    assert row["customer_username"] == "alice"
    assert row["escalation_message_id"] == 5001
    assert row["reason"] == "needs help"


def test_jira_status_is_lifecycle_proxy_and_summary_truncated():
    rows = {r["ticket_ref"]: r for r in _reader().list_tickets(status_filter=None)}
    assert rows["JIRA-100"]["status"] == "open"  # is_active True
    assert rows["JIRA-101"]["status"] == "done"  # resolved / inactive
    assert rows["JIRA-100"]["summary"].endswith("…")
    assert len(rows["JIRA-100"]["summary"]) <= 200


def test_unified_comment_counts():
    rows = {r["ticket_ref"]: r for r in _reader().list_tickets()}
    # INT-1: 2 internal comments + 1 tagged chat message.
    assert rows["INT-1"]["comment_count"] == 3
    # JIRA-100: 0 internal + 1 tagged chat message.
    assert rows["JIRA-100"]["comment_count"] == 1
    assert rows["INT-2"]["comment_count"] == 0


def test_status_filter():
    rows = _reader().list_tickets(status_filter=["open", "in_progress"])
    assert sorted(r["ticket_ref"] for r in rows) == ["INT-1", "JIRA-100"]


def test_backend_filter():
    jira = _reader().list_tickets(backend_filter="jira")
    assert sorted(r["ticket_ref"] for r in jira) == ["JIRA-100", "JIRA-101"]
    internal = _reader().list_tickets(backend_filter="internal")
    assert sorted(r["ticket_ref"] for r in internal) == ["INT-1", "INT-2"]


def test_org_filter():
    rows = _reader().list_tickets(status_filter=None, org_filter="orgC")
    assert sorted(r["ticket_ref"] for r in rows) == ["JIRA-100", "JIRA-101"]


def test_search_matches_customer_summary_and_ref():
    assert [r["ticket_ref"] for r in _reader().list_tickets(search="bob")] == ["JIRA-100"]
    assert [r["ticket_ref"] for r in _reader().list_tickets(search="Second")] == ["INT-2"]
    assert [r["ticket_ref"] for r in _reader().list_tickets(search="jira-100")] == ["JIRA-100"]


def test_pagination_slices_merged_list():
    page1 = _reader().list_tickets(limit=2, offset=0)
    page2 = _reader().list_tickets(limit=2, offset=2)
    assert [r["ticket_ref"] for r in page1] == ["JIRA-100", "INT-1"]
    assert [r["ticket_ref"] for r in page2] == ["INT-2", "JIRA-101"]


def test_get_ticket_detail_internal_with_timeline():
    detail = _reader().get_ticket_detail("INT-1")
    assert detail is not None
    assert detail["backend"] == "internal"
    assert detail["description"] == "desc int1"
    assert detail["comment_count"] == 3
    # Chronological, unified across both comment sources.
    assert [c["source"] for c in detail["comments"]] == ["staff", "customer", "jira"]
    assert detail["comments"][0]["created_at"] <= detail["comments"][-1]["created_at"]


def test_get_ticket_detail_jira():
    detail = _reader().get_ticket_detail("JIRA-100")
    assert detail is not None
    assert detail["backend"] == "jira"
    assert detail["status"] == "open"
    assert detail["comment_count"] == 1
    assert detail["comments"][0]["body"] == "jira tagged message"


def test_get_ticket_detail_unknown_returns_none():
    assert _reader().get_ticket_detail("DOES-NOT-EXIST") is None


def test_list_tickets_returns_empty_when_no_client():
    reader = SupabaseReader.__new__(SupabaseReader)
    reader.client = None
    assert reader.list_tickets() == []
    assert reader.get_ticket_detail("INT-1") is None
