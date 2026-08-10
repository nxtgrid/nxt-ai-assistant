"""Tests for SupabaseClient's remaining (non-escalation-mapping) query surface.

Covers:
  - get_session_by_id's UUID-keyed lookup.
  - tag_message_as_ticket_comment's non-clobbering metadata merge and
    ticket_id resolution. The internal-ticket CRUD/comment helper regression
    cases that used to live here were deleted alongside their now-removed
    SupabaseClient methods once the SQL contract migration (0005b) landed.

save_escalation_mapping, the 4 has-ticket predicate readers
(get_stale_unfiled_escalations, get_orphaned_claimed_escalations,
get_old_unfiled_escalations, get_active_tracked_escalations),
get_escalation_mapping_by_ticket_ref, update_session_escalation_status, and
count_active_blocking_escalations are gone (STOP_LEGACY_ESCALATION_WRITES
cutover: writes/reads for escalation_mappings/chat_sessions.is_escalated are
now exclusively canonical, via EscalationRepository -- see
test_does_not_expose_legacy_escalation_mapping_helpers below).

Uses a small fake standing in for the real Supabase (postgrest) client's
fluent API -- the same style as
chat_orchestrator/tests/services/test_work_packet_service.py and
chat_orchestrator/tests/services/ticketing/test_service.py -- generalized
here to support the broader set of filter verbs
(eq/neq/gt/lt/gte/is_/filter/order/limit) that SupabaseClient's escalation
methods use, so filtering is exercised for real rather than only asserting
on call arguments.

SupabaseClient (orchestrator.services.supabase_client.EnhancedSupabaseClient)
lazily builds its own real supabase-py client via `_get_client()`; there is
no constructor-level injection seam, so tests construct a real instance and
monkeypatch the `_get_client` *method* on the instance to return our fake
raw client instead.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.supabase_client import SupabaseClient


class _FakeResponse:
    def __init__(self, data: Any, count: Optional[int] = None) -> None:
        self.data = data
        self.count = count


def _row_matches(row: Dict[str, Any], f: tuple) -> bool:
    kind = f[0]
    if kind == "eq":
        _, col, val = f
        return row.get(col) == val
    if kind == "neq":
        _, col, val = f
        return row.get(col) != val
    if kind in ("gt", "lt", "gte"):
        _, col, val = f
        rowval = row.get(col)
        if rowval is None:
            return False
        if kind == "gt":
            return rowval > val
        if kind == "lt":
            return rowval < val
        return rowval >= val
    if kind == "is":
        _, col, val = f
        if val == "null":
            return row.get(col) is None
        return row.get(col) == val
    if kind == "filter":
        _, col, op, val = f
        if "->>" in col:
            base, key = col.split("->>")
            data = row.get(base) or {}
            rowval = data.get(key)
        else:
            rowval = row.get(col)
        if op == "eq":
            return rowval == val
        if op == "not.is" and val == "null":
            return rowval is not None
        return False
    return True


class _FakeQuery:
    """Fluent fake matching supabase-py's table().select()/insert()/update()/
    .eq()/.neq()/.gt()/.lt()/.gte()/.is_()/.filter()/.order()/.limit() chain.
    """

    def __init__(self, table: "_FakeTable", op: str, payload: Any = None) -> None:
        self._table = table
        self._op = op
        self._payload = payload
        self._filters: List[tuple] = []
        self._order: Optional[tuple] = None
        self._limit_n: Optional[int] = None
        self._count_mode: Optional[str] = None

    def select(self, *_args, count: Optional[str] = None, **_kwargs) -> "_FakeQuery":
        self._count_mode = count
        return self

    def eq(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("neq", col, val))
        return self

    def gt(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("gt", col, val))
        return self

    def lt(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("lt", col, val))
        return self

    def gte(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("gte", col, val))
        return self

    def is_(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("is", col, val))
        return self

    def filter(self, col: str, op: str, val: Any) -> "_FakeQuery":
        self._filters.append(("filter", col, op, val))
        return self

    def order(self, col: str, desc: bool = False) -> "_FakeQuery":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "_FakeQuery":
        self._limit_n = n
        return self

    def execute(self) -> _FakeResponse:
        self._table.executed.append((self._op, list(self._filters), self._payload))

        if self._op == "select":
            matched = [r for r in self._table.rows if all(_row_matches(r, f) for f in self._filters)]
            if self._order is not None:
                col, desc = self._order
                matched.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._limit_n is not None:
                matched = matched[: self._limit_n]
            count = len(matched) if self._count_mode == "exact" else None
            return _FakeResponse(matched, count=count)

        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            for p in payloads:
                row = dict(p)
                row.setdefault("id", f"generated-{len(self._table.rows)}")
                self._table.rows.append(row)
            return _FakeResponse(list(payloads))

        if self._op == "update":
            matched = [r for r in self._table.rows if all(_row_matches(r, f) for f in self._filters)]
            for r in matched:
                r.update(self._payload or {})
            return _FakeResponse(matched)

        raise AssertionError(f"Unhandled op: {self._op}")


class _FakeTable:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.rows: List[Dict[str, Any]] = rows or []
        self.executed: List[tuple] = []

    def select(self, *args, **kwargs) -> _FakeQuery:
        return _FakeQuery(self, "select").select(*args, **kwargs)

    def insert(self, payload: Any) -> _FakeQuery:
        return _FakeQuery(self, "insert", payload)

    def update(self, payload: Dict[str, Any]) -> _FakeQuery:
        return _FakeQuery(self, "update", payload)


class _FakeRawClient:
    """Stands in for the raw postgrest client (`._get_client()`'s return value)."""

    def __init__(self, tables: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> None:
        tables = tables or {}
        self._tables: Dict[str, Any] = {
            "escalation_mappings": _FakeTable(tables.get("escalation_mappings")),
            "internal_tickets": _FakeTable(tables.get("internal_tickets")),
            "internal_ticket_comments": _FakeTable(tables.get("internal_ticket_comments")),
            "chat_messages": _FakeTable(tables.get("chat_messages")),
            "chat_sessions": _FakeTable(tables.get("chat_sessions")),
            "tickets": _FakeTable(tables.get("tickets")),
        }

    def table(self, name: str) -> Any:
        return self._tables[name]


def _make_client(raw: _FakeRawClient) -> SupabaseClient:
    client = SupabaseClient(url="https://example.test", key="test-key")
    client._get_client = lambda: raw  # type: ignore[method-assign]
    return client


def test_does_not_expose_legacy_internal_ticket_helpers():
    for method_name in (
        "get_internal_ticket",
        "list_internal_tickets",
        "update_internal_ticket_status",
        "add_internal_ticket_comment",
        "get_ticket_comments",
    ):
        assert not hasattr(SupabaseClient, method_name)


def test_does_not_expose_legacy_escalation_mapping_helpers():
    """STOP_LEGACY_ESCALATION_WRITES cutover: escalation_mappings/
    chat_sessions.is_escalated are no longer written or read anywhere --
    EscalationRepository (the canonical `escalations` table) is the sole
    writer/reader. These methods must stay gone."""
    for method_name in (
        "save_escalation_mapping",
        "get_escalation_mapping_by_ticket_ref",
        "update_session_escalation_status",
        "count_active_blocking_escalations",
        "get_stale_unfiled_escalations",
        "get_orphaned_claimed_escalations",
        "get_old_unfiled_escalations",
        "get_active_tracked_escalations",
    ):
        assert not hasattr(SupabaseClient, method_name)


# ---------------------------------------------------------------------------
# get_session_by_id
# ---------------------------------------------------------------------------


class TestGetSessionById:
    @pytest.mark.asyncio
    async def test_looks_up_by_uuid_not_text_session_id(self):
        raw = _FakeRawClient(
            tables={
                "chat_sessions": [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "session_id": "telegram_abc",
                        "telegram_chat_id": "123",
                        "telegram_topic_id": "9",
                    }
                ]
            }
        )
        client = _make_client(raw)

        session = await client.get_session_by_id("11111111-1111-1111-1111-111111111111")

        assert session is not None
        assert session.session_id == "telegram_abc"
        assert session.telegram_chat_id == "123"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        raw = _FakeRawClient()
        client = _make_client(raw)

        assert await client.get_session_by_id("missing") is None


# ---------------------------------------------------------------------------
# tag_message_as_ticket_comment
# ---------------------------------------------------------------------------


class TestTagMessageAsTicketComment:
    @pytest.mark.asyncio
    async def test_merges_into_existing_metadata_without_clobbering(self):
        row = {
            "id": "m1",
            "metadata": {"token_count": 42, "model": "sonnet"},
        }
        raw = _FakeRawClient(tables={"chat_messages": [row]})
        client = _make_client(raw)

        await client.tag_message_as_ticket_comment("m1", "TKT-1", ticket_role="comment")

        assert row["metadata"] == {
            "token_count": 42,
            "model": "sonnet",
            "ticket_ref": "TKT-1",
            "ticket_role": "comment",
        }

    @pytest.mark.asyncio
    async def test_handles_none_metadata_default(self):
        row = {"id": "m1", "metadata": None}
        raw = _FakeRawClient(tables={"chat_messages": [row]})
        client = _make_client(raw)

        await client.tag_message_as_ticket_comment("m1", "TKT-1")

        assert row["metadata"] == {"ticket_ref": "TKT-1", "ticket_role": "comment"}

    @pytest.mark.asyncio
    async def test_degrades_gracefully_when_message_not_found(self):
        raw = _FakeRawClient(tables={"chat_messages": []})
        client = _make_client(raw)

        # Should not raise.
        await client.tag_message_as_ticket_comment("missing", "TKT-1")

    @pytest.mark.asyncio
    async def test_degrades_gracefully_on_read_failure(self):
        raw = _FakeRawClient()

        class _RaisingTable:
            def select(self, *_a, **_k):
                raise RuntimeError("read boom")

        raw._tables["chat_messages"] = _RaisingTable()
        client = _make_client(raw)

        # Should not raise.
        await client.tag_message_as_ticket_comment("m1", "TKT-1")

    @pytest.mark.asyncio
    async def test_degrades_gracefully_on_write_failure(self):
        row = {"id": "m1", "metadata": {"existing": "value"}}

        class _WriteFailingTable(_FakeTable):
            def update(self, payload):
                raise RuntimeError("write boom")

        raw = _FakeRawClient()
        raw._tables["chat_messages"] = _WriteFailingTable(rows=[row])
        client = _make_client(raw)

        # Should not raise, and the row should be untouched since the write failed.
        await client.tag_message_as_ticket_comment("m1", "TKT-1")

        assert row["metadata"] == {"existing": "value"}

    @pytest.mark.asyncio
    async def test_sets_ticket_id_when_ticket_exists(self):
        """chat_messages.ticket_id is the canonical FK the ticket-schema
        contract migration (0005b) requires -- resolve it via the tickets
        table's ticket_ref, same as the metadata tag, instead of leaving it
        for a one-time backfill to keep drifting back out of sync."""
        message_row = {"id": "m1", "metadata": {}}
        raw = _FakeRawClient(
            tables={
                "chat_messages": [message_row],
                "tickets": [{"id": "ticket-uuid-1", "ticket_ref": "TKT-1"}],
            }
        )
        client = _make_client(raw)

        await client.tag_message_as_ticket_comment("m1", "TKT-1")

        assert message_row["ticket_id"] == "ticket-uuid-1"
        assert message_row["metadata"]["ticket_ref"] == "TKT-1"

    @pytest.mark.asyncio
    async def test_leaves_ticket_id_unset_when_ticket_not_found(self):
        """The canonical ticket row not existing yet (e.g. notify-source
        races) must not block the metadata tag -- degrade the same way the
        existing not-found/failure cases already do."""
        message_row = {"id": "m1", "metadata": {}}
        raw = _FakeRawClient(
            tables={"chat_messages": [message_row], "tickets": []}
        )
        client = _make_client(raw)

        await client.tag_message_as_ticket_comment("m1", "TKT-1")

        assert "ticket_id" not in message_row
        assert message_row["metadata"]["ticket_ref"] == "TKT-1"
