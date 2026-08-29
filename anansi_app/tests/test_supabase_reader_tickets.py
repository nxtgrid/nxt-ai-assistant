"""Unit tests for SupabaseReader's ticket list/detail readers.

The reader's ticket methods are SYNC (this file's convention), so no asyncio.
A small fluent fake supabase-py client backs the tests — table/select/eq/in_/
filter/order/limit/range/execute — applying filters/pagination in Python
against seeded rows.
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
        self._range = None

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
        self._range = (start, end)
        return self

    def execute(self):
        rows = [r for r in self._rows if all(p(r) for p in self._preds)]
        if self._order is not None:
            col, desc = self._order
            rows.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        total = len(rows)
        if self._range is not None:
            start, end = self._range
            rows = rows[start : end + 1]
        if self._limit is not None:
            rows = rows[: self._limit]
        return SimpleNamespace(data=rows, count=total)


class _FakeClient:
    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables
        self.queries: list[str] = []

    def table(self, name):
        self.queries.append(name)
        return _FakeQuery(self._tables.get(name, []))


def _seed() -> dict[str, list[dict]]:
    """Empty base fixture -- each test layers only the tables its method
    under test actually queries via ``seed.update({...})``/direct assignment."""
    return {}


def test_list_ticket_page_reads_the_canonical_view_with_database_pagination():
    seed = _seed()
    seed["ticket_list_view"] = [
        {
            "id": "ticket-new", "ticket_ref": "OPS-9", "backend": "internal",
            "created_via": "notification", "status": "open", "summary": "Newest",
            "has_escalation": False, "latest_activity_at": "2026-07-24T10:00:00",
            "provisioning_state": "active",
        },
        {
            "id": "ticket-old", "ticket_ref": "INT-2", "backend": "internal",
            "created_via": "escalation", "status": "open", "summary": "Older",
            "has_escalation": True, "latest_activity_at": "2026-07-23T10:00:00",
            "provisioning_state": "active",
        },
    ]
    reader = SupabaseReader.__new__(SupabaseReader)
    reader.client = _FakeClient(seed)

    result = reader.list_ticket_page(page=2, page_size=1, status="open", backend="internal")

    assert result.total == 2
    assert result.items == [seed["ticket_list_view"][1]]
    assert reader.client.queries == ["ticket_list_view"]


def test_list_ticket_page_accepts_a_list_of_statuses():
    seed = _seed()
    seed["ticket_list_view"] = [
        {
            "id": "t-open", "ticket_ref": "OPS-1", "backend": "internal",
            "created_via": "notification", "status": "open", "summary": "Open one",
            "has_escalation": False, "latest_activity_at": "2026-07-24T10:00:00",
            "provisioning_state": "active",
        },
        {
            "id": "t-progress", "ticket_ref": "OPS-2", "backend": "internal",
            "created_via": "notification", "status": "in_progress", "summary": "In progress one",
            "has_escalation": False, "latest_activity_at": "2026-07-23T10:00:00",
            "provisioning_state": "active",
        },
        {
            "id": "t-done", "ticket_ref": "OPS-3", "backend": "internal",
            "created_via": "notification", "status": "done", "summary": "Done one",
            "has_escalation": False, "latest_activity_at": "2026-07-22T10:00:00",
            "provisioning_state": "active",
        },
    ]
    reader = SupabaseReader.__new__(SupabaseReader)
    reader.client = _FakeClient(seed)

    result = reader.list_ticket_page(status=["open", "in_progress"])

    assert result.total == 2
    assert {item["ticket_ref"] for item in result.items} == {"OPS-1", "OPS-2"}


def test_list_ticket_page_excludes_tickets_never_activated_on_a_backend():
    """A ticket intent stuck "pending" (backend create never completed) or
    "failed" never reached Jira/internal, but its ``status`` column still
    defaults to "open" -- without this filter it renders as a real open
    ticket here forever, inflating this page's count against Jira's."""
    seed = _seed()
    seed["ticket_list_view"] = [
        {
            "id": "t-active", "ticket_ref": "OPS-1", "backend": "internal",
            "created_via": "notification", "status": "open", "summary": "Real ticket",
            "has_escalation": False, "latest_activity_at": "2026-07-24T10:00:00",
            "provisioning_state": "active",
        },
        {
            "id": "t-pending", "ticket_ref": None, "backend": None,
            "created_via": "notification", "status": "open", "summary": "Stuck intent",
            "has_escalation": False, "latest_activity_at": "2026-07-24T09:00:00",
            "provisioning_state": "pending",
        },
        {
            "id": "t-failed", "ticket_ref": None, "backend": None,
            "created_via": "notification", "status": "open", "summary": "Failed create",
            "has_escalation": False, "latest_activity_at": "2026-07-24T08:00:00",
            "provisioning_state": "failed",
        },
    ]
    reader = SupabaseReader.__new__(SupabaseReader)
    reader.client = _FakeClient(seed)

    result = reader.list_ticket_page()

    assert result.total == 1
    assert [item["id"] for item in result.items] == ["t-active"]


def test_canonical_ticket_detail_reads_recorded_delivery_with_a_safe_link():
    seed = _seed()
    seed.update(
        {
            "tickets": [
                {
                    "id": "ticket-1", "ticket_ref": "OPS-9", "backend": "jira",
                    "created_via": "notification", "status": "open", "summary": "Alert",
                }
            ],
            "ticket_comments": [
                {
                    "ticket_id": "ticket-1", "source": "staff", "body": "Investigating",
                    "author": "ops", "is_public": False, "created_at": "2026-07-24T10:00:00",
                }
            ],
            "message_deliveries": [
                {
                    "ticket_id": "ticket-1", "purpose": "notification", "channel": "telegram",
                    "external_chat_id": "-1001234567890", "external_message_id": 42,
                    "sent_at": "2026-07-24T10:01:00",
                }
            ],
        }
    )
    reader = SupabaseReader.__new__(SupabaseReader)
    reader.client = _FakeClient(seed)

    detail = reader.get_canonical_ticket_detail("ticket-1")

    assert detail is not None
    assert detail["ticket_ref"] == "OPS-9"
    assert detail["comments"] == [
        {
            "source": "staff", "body": "Investigating", "author": "ops",
            "is_public": False, "created_at": "2026-07-24T10:00:00",
        }
    ]
    assert detail["deliveries"] == [
        {
            "purpose": "notification", "sent_at": "2026-07-24T10:01:00",
            "message_url": "https://t.me/c/1234567890/42",
        }
    ]


def test_get_canonical_ticket_detail_includes_attachments():
    seed = _seed()
    seed.update(
        {
            "tickets": [
                {
                    "id": "ticket-1", "ticket_ref": "OPS-9", "backend": "internal",
                    "created_via": "escalation", "status": "open", "summary": "s",
                }
            ],
            "escalation_attachments": [
                {
                    "id": "att-1",
                    "escalation_id": "esc-1",
                    "ticket_id": "ticket-1",
                    "storage_path": "esc-1/a.jpg",
                    "media_type": "image",
                    "mime_type": "image/jpeg",
                    "size_bytes": 10,
                    "created_at": "2026-07-31T00:00:00Z",
                }
            ],
        }
    )
    reader = SupabaseReader.__new__(SupabaseReader)
    reader.client = _FakeClient(seed)

    detail = reader.get_canonical_ticket_detail("ticket-1")

    assert len(detail["attachments"]) == 1
    assert detail["attachments"][0]["media_type"] == "image"
    assert detail["attachments"][0]["mime_type"] == "image/jpeg"
