"""Tests for archived_at filtering in SupabaseClient's message loaders
(Phase 4 of docs/superpowers/plans/2026-08-06-user-designed-skills.md).

The skill builder's Rewind button archives a message and everything after
it in the session (see 0012_message_archive.sql); an archived message must
never resurface through *any* history-loading path. There are three raw
queries that read chat_messages -- get_messages, get_messages_filtered (which
delegates to get_messages when no exclude_types is given, and runs its own
query otherwise), and get_messages_around_timestamp (a before-window and an
after-window query) -- and all three must filter archived_at IS NULL
independently, at the query level, so a caller can't see an archived row by
forgetting to ask. This is deliberately the same shape of gap CLAUDE.md's
2026-08-02 incident describes: a history filter that covered one path but not
another.

Uses a small fake standing in for the real Supabase (postgrest) client's
fluent API, the same style as
chat_orchestrator/tests/services/test_supabase_client_ticketing.py --
generalized here only as far as the eq/is_/gte/lte/gt/order/limit verbs these
three methods actually use.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.supabase_client import SupabaseClient


class _FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


def _row_matches(row: Dict[str, Any], f: tuple) -> bool:
    kind = f[0]
    if kind == "eq":
        _, col, val = f
        return row.get(col) == val
    if kind == "is":
        _, col, val = f
        if val == "null":
            return row.get(col) is None
        return row.get(col) == val
    if kind in ("gt", "gte", "lte"):
        _, col, val = f
        rowval = row.get(col)
        if rowval is None:
            return False
        if kind == "gt":
            return rowval > val
        if kind == "gte":
            return rowval >= val
        return rowval <= val
    return True


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self._filters: List[tuple] = []
        self._order: Optional[tuple] = None
        self._limit_n: Optional[int] = None

    def select(self, *_args, **_kwargs) -> "_FakeQuery":
        return self

    def eq(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("eq", col, val))
        return self

    def is_(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("is", col, val))
        return self

    def gt(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("gt", col, val))
        return self

    def gte(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("gte", col, val))
        return self

    def lte(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("lte", col, val))
        return self

    def order(self, col: str, desc: bool = False) -> "_FakeQuery":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "_FakeQuery":
        self._limit_n = n
        return self

    def execute(self) -> _FakeResponse:
        matched = [r for r in self._rows if all(_row_matches(r, f) for f in self._filters)]
        if self._order is not None:
            col, desc = self._order
            matched.sort(key=lambda r: r.get(col), reverse=desc)
        if self._limit_n is not None:
            matched = matched[: self._limit_n]
        return _FakeResponse(matched)


class _FakeTable:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def select(self, *args, **kwargs) -> _FakeQuery:
        return _FakeQuery(self._rows).select(*args, **kwargs)


class _FakeRawClient:
    def __init__(self, chat_messages: List[Dict[str, Any]]) -> None:
        self._chat_messages = _FakeTable(chat_messages)

    def table(self, name: str) -> Any:
        assert name == "chat_messages"
        return self._chat_messages


def _make_client(rows: List[Dict[str, Any]]) -> SupabaseClient:
    client = SupabaseClient(url="https://example.test", key="test-key")
    client._get_client = lambda: _FakeRawClient(rows)  # type: ignore[method-assign]
    return client


SESSION = "11111111-1111-1111-1111-111111111111"
_NOW = datetime.now(timezone.utc)


def _row(index: int, content: str, archived_at: Optional[str] = None, **extra) -> Dict[str, Any]:
    # Timestamps count *up* from an hour ago by index, well inside
    # get_messages/get_messages_filtered's default 12-hour window, and still
    # far enough apart to give get_messages_around_timestamp's target/before/
    # after comparisons unambiguous ordering.
    created_at = _NOW - timedelta(hours=1) + timedelta(minutes=index)
    return {
        "session_id": SESSION,
        "role": "user",
        "content": content,
        "message_index": index,
        "created_at": created_at.isoformat(),
        "archived_at": archived_at,
        **extra,
    }


class TestGetMessagesExcludesArchived:
    @pytest.mark.asyncio
    async def test_archived_row_is_not_returned(self):
        rows = [
            _row(0, "step 1"),
            _row(1, "step 2 (rewound)", archived_at="2026-08-07T00:00:00+00:00"),
        ]
        client = _make_client(rows)

        messages = await client.get_messages(SESSION)

        assert [m.content for m in messages] == ["step 1"]

    @pytest.mark.asyncio
    async def test_all_live_returns_everything(self):
        rows = [_row(0, "step 1"), _row(1, "step 2")]
        client = _make_client(rows)

        messages = await client.get_messages(SESSION)

        assert [m.content for m in messages] == ["step 1", "step 2"]


class TestGetMessagesFilteredExcludesArchived:
    @pytest.mark.asyncio
    async def test_archived_row_excluded_via_delegate_path(self):
        # No exclude_types -> delegates straight to get_messages.
        rows = [
            _row(0, "step 1"),
            _row(1, "step 2 (rewound)", archived_at="2026-08-07T00:00:00+00:00"),
        ]
        client = _make_client(rows)

        messages = await client.get_messages_filtered(SESSION)

        assert [m.content for m in messages] == ["step 1"]

    @pytest.mark.asyncio
    async def test_archived_row_excluded_alongside_type_filter(self):
        rows = [
            _row(0, "step 1"),
            _row(1, "scheduled ping", metadata={"message_type": "scheduled"}),
            _row(2, "step 2 (rewound)", archived_at="2026-08-07T00:00:00+00:00"),
        ]
        client = _make_client(rows)

        messages = await client.get_messages_filtered(
            SESSION, exclude_types=["scheduled", "scheduled_user"]
        )

        assert [m.content for m in messages] == ["step 1"]


class TestGetMessagesAroundTimestampExcludesArchived:
    @pytest.mark.asyncio
    async def test_archived_row_excluded_from_before_window(self):
        rows = [
            _row(0, "before, live"),
            _row(1, "before, rewound", archived_at=_NOW.isoformat()),
        ]
        client = _make_client(rows)

        messages = await client.get_messages_around_timestamp(
            session_uuid=SESSION,
            target_timestamp=_NOW.isoformat(),
            window_before=5,
            window_after=3,
        )

        assert [m.content for m in messages] == ["before, live"]

    @pytest.mark.asyncio
    async def test_archived_row_excluded_from_after_window(self):
        rows = [
            _row(0, "before, live"),
            _row(5, "after, rewound", archived_at=_NOW.isoformat()),
            _row(6, "after, live"),
        ]
        # _row's timestamps count up by minute from index -- pick a target
        # that sits strictly between index 0 and index 5 so the before/after
        # split is unambiguous.
        target = (_NOW - timedelta(minutes=58)).isoformat()
        client = _make_client(rows)

        messages = await client.get_messages_around_timestamp(
            session_uuid=SESSION,
            target_timestamp=target,
            window_before=5,
            window_after=3,
        )

        assert [m.content for m in messages] == ["before, live", "after, live"]
