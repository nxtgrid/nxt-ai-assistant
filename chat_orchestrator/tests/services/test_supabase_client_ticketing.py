"""Tests for SupabaseClient's Task 3 ticket-backend-agnostic query surface.

Covers:
  - save_escalation_mapping persisting ticket_ref/ticket_backend alongside
    the pre-existing jira_ticket_key.
  - The 4 has-ticket predicate readers (get_stale_unfiled_escalations,
    get_orphaned_claimed_escalations, get_old_unfiled_escalations,
    get_active_tracked_escalations) filtering on ticket_ref rather than
    jira_ticket_key.
  - get_escalation_mapping_by_ticket_ref mirroring
    get_escalation_mapping_by_jira_key.
  - internal_tickets CRUD (get/list/update_status).
  - internal_ticket_comments writes + get_ticket_comments' merge of
    internal_ticket_comments with tagged chat_messages.
  - tag_message_as_ticket_comment's non-clobbering metadata merge.

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
        }

    def table(self, name: str) -> Any:
        return self._tables[name]


def _make_client(raw: _FakeRawClient) -> SupabaseClient:
    client = SupabaseClient(url="https://example.test", key="test-key")
    client._get_client = lambda: raw  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------------------
# save_escalation_mapping
# ---------------------------------------------------------------------------


class TestSaveEscalationMapping:
    @pytest.mark.asyncio
    async def test_persists_ticket_ref_and_backend_alongside_jira_key(self):
        raw = _FakeRawClient()
        client = _make_client(raw)

        result = await client.save_escalation_mapping(
            escalation_message_id=123,
            customer_chat_id="chat-1",
            session_id="session-1",
            mapping_id="mapping-1",
            jira_ticket_key=None,
            ticket_ref="TKT-000001",
            ticket_backend="internal",
        )

        assert result == "mapping-1"
        row = raw.table("escalation_mappings").rows[0]
        assert row["ticket_ref"] == "TKT-000001"
        assert row["ticket_backend"] == "internal"
        assert row["jira_ticket_key"] is None

    @pytest.mark.asyncio
    async def test_defaults_new_params_to_none_when_omitted(self):
        raw = _FakeRawClient()
        client = _make_client(raw)

        await client.save_escalation_mapping(
            escalation_message_id=1,
            customer_chat_id="chat-1",
            session_id="session-1",
            mapping_id="mapping-1",
        )

        row = raw.table("escalation_mappings").rows[0]
        assert row["ticket_ref"] is None
        assert row["ticket_backend"] is None


# ---------------------------------------------------------------------------
# Has-ticket predicate readers -- ticket_ref, not jira_ticket_key
# ---------------------------------------------------------------------------


class TestHasTicketPredicateReaders:
    @pytest.mark.asyncio
    async def test_get_stale_unfiled_escalations_filters_on_ticket_ref(self):
        # Row A: no jira key AND no ticket_ref -> genuinely unfiled, should appear.
        # Row B: no jira key BUT ticket_ref set (internal ticket already filed)
        #        -> must NOT appear. If the code still filtered on
        #        jira_ticket_key this row would incorrectly show up, since its
        #        jira_ticket_key is also None.
        unfiled = {
            "id": "row-a",
            "session_id": "s-a",
            "org_hashtag": None,
            "customer_email": None,
            "customer_username": None,
            "customer_chat_id": "c-a",
            "customer_topic_id": None,
            "organization_id": 1,
            "escalation_message_id": 1,
            "escalation_topic_id": None,
            "reason": "could_not_answer",
            "jira_ticket_key": None,
            "ticket_ref": None,
            "ticket_backend": None,
            "question_text": "q",
            "created_at": "2026-07-20T00:00:00+00:00",
            "is_active": True,
        }
        already_filed_internally = {
            **unfiled,
            "id": "row-b",
            "jira_ticket_key": None,
            "ticket_ref": "TKT-000009",
            "ticket_backend": "internal",
        }
        raw = _FakeRawClient(
            tables={"escalation_mappings": [unfiled, already_filed_internally]}
        )
        client = _make_client(raw)

        result = await client.get_stale_unfiled_escalations(
            min_age_hours=0, max_age_hours=999, limit=20
        )

        ids = [r["id"] for r in result]
        assert ids == ["row-a"]

        # Confirm the executed select's filters reference ticket_ref, not
        # jira_ticket_key, for the is-null predicate.
        op, filters, _ = raw.table("escalation_mappings").executed[0]
        assert op == "select"
        assert ("is", "ticket_ref", "null") in filters
        assert ("is", "jira_ticket_key", "null") not in filters

    @pytest.mark.asyncio
    async def test_get_orphaned_claimed_escalations_filters_on_ticket_ref(self):
        orphaned = {
            "id": "row-a",
            "session_id": "s-a",
            "created_at": "2026-07-20T00:00:00+00:00",
            "is_active": False,
            "jira_ticket_key": None,
            "ticket_ref": None,
            "ticket_backend": None,
            "resolved_at": None,
        }
        already_ticketed = {
            **orphaned,
            "id": "row-b",
            "ticket_ref": "TKT-000009",
            "ticket_backend": "internal",
        }
        raw = _FakeRawClient(tables={"escalation_mappings": [orphaned, already_ticketed]})
        client = _make_client(raw)

        result = await client.get_orphaned_claimed_escalations(max_age_hours=999, limit=50)

        ids = [r["id"] for r in result]
        assert ids == ["row-a"]
        op, filters, _ = raw.table("escalation_mappings").executed[0]
        assert ("is", "ticket_ref", "null") in filters
        assert ("is", "jira_ticket_key", "null") not in filters

    @pytest.mark.asyncio
    async def test_get_old_unfiled_escalations_filters_on_ticket_ref(self):
        unfiled = {
            "id": "row-a",
            "org_hashtag": None,
            "customer_username": None,
            "customer_email": None,
            "escalation_message_id": 1,
            "created_at": "2000-01-01T00:00:00+00:00",
            "is_active": True,
            "jira_ticket_key": None,
            "ticket_ref": None,
            "ticket_backend": None,
            "reason": "could_not_answer",
        }
        already_filed_internally = {
            **unfiled,
            "id": "row-b",
            "ticket_ref": "TKT-000009",
            "ticket_backend": "internal",
        }
        raw = _FakeRawClient(
            tables={"escalation_mappings": [unfiled, already_filed_internally]}
        )
        client = _make_client(raw)

        result = await client.get_old_unfiled_escalations(max_age_hours=1, limit=20)

        ids = [r["id"] for r in result]
        assert ids == ["row-a"]
        op, filters, _ = raw.table("escalation_mappings").executed[0]
        assert ("is", "ticket_ref", "null") in filters
        assert ("is", "jira_ticket_key", "null") not in filters

    @pytest.mark.asyncio
    async def test_get_active_tracked_escalations_filters_on_ticket_ref(self):
        # Row A: ticket_ref set -> tracked, should appear.
        # Row B: jira_ticket_key set but ticket_ref NOT set -> must NOT appear
        #        under the new ticket_ref-keyed predicate (proves the filter
        #        moved off jira_ticket_key, since row B *would* have matched
        #        the old `.filter("jira_ticket_key", "not.is", "null")`).
        tracked = {
            "id": "row-a",
            "session_id": "s-a",
            "customer_chat_id": "c-a",
            "customer_topic_id": None,
            "jira_ticket_key": None,
            "ticket_ref": "TKT-000001",
            "ticket_backend": "internal",
            "org_hashtag": None,
            "customer_username": None,
            "created_at": "2026-07-20T00:00:00+00:00",
            "is_active": True,
        }
        legacy_jira_only = {
            **tracked,
            "id": "row-b",
            "jira_ticket_key": "OPS-1",
            "ticket_ref": None,
            "ticket_backend": None,
        }
        raw = _FakeRawClient(tables={"escalation_mappings": [tracked, legacy_jira_only]})
        client = _make_client(raw)

        result = await client.get_active_tracked_escalations(limit=100)

        ids = [r["id"] for r in result]
        assert ids == ["row-a"]
        op, filters, _ = raw.table("escalation_mappings").executed[0]
        assert ("filter", "ticket_ref", "not.is", "null") in filters
        assert ("filter", "jira_ticket_key", "not.is", "null") not in filters


# ---------------------------------------------------------------------------
# get_escalation_mapping_by_ticket_ref
# ---------------------------------------------------------------------------


class TestGetEscalationMappingByTicketRef:
    @pytest.mark.asyncio
    async def test_returns_most_recent_active_mapping(self):
        older = {
            "id": "map-old",
            "ticket_ref": "TKT-1",
            "is_active": True,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        newer = {
            "id": "map-new",
            "ticket_ref": "TKT-1",
            "is_active": True,
            "created_at": "2026-02-01T00:00:00+00:00",
        }
        inactive_newest = {
            "id": "map-inactive",
            "ticket_ref": "TKT-1",
            "is_active": False,
            "created_at": "2026-03-01T00:00:00+00:00",
        }
        raw = _FakeRawClient(
            tables={"escalation_mappings": [older, newer, inactive_newest]}
        )
        client = _make_client(raw)

        result = await client.get_escalation_mapping_by_ticket_ref("TKT-1")

        assert result is not None
        assert result["id"] == "map-new"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        raw = _FakeRawClient(tables={"escalation_mappings": []})
        client = _make_client(raw)

        result = await client.get_escalation_mapping_by_ticket_ref("TKT-missing")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self):
        raw = _FakeRawClient()

        class _RaisingTable:
            def select(self, *_a, **_k):
                raise RuntimeError("boom")

        raw._tables["escalation_mappings"] = _RaisingTable()
        client = _make_client(raw)

        result = await client.get_escalation_mapping_by_ticket_ref("TKT-1")

        assert result is None


# ---------------------------------------------------------------------------
# internal_tickets CRUD
# ---------------------------------------------------------------------------


class TestInternalTicketsCrud:
    @pytest.mark.asyncio
    async def test_get_internal_ticket_found(self):
        row = {"id": "1", "ticket_ref": "TKT-1", "status": "open"}
        raw = _FakeRawClient(tables={"internal_tickets": [row]})
        client = _make_client(raw)

        result = await client.get_internal_ticket("TKT-1")

        assert result == row

    @pytest.mark.asyncio
    async def test_get_internal_ticket_not_found(self):
        raw = _FakeRawClient(tables={"internal_tickets": []})
        client = _make_client(raw)

        result = await client.get_internal_ticket("TKT-missing")

        assert result is None

    @pytest.mark.asyncio
    async def test_list_internal_tickets_no_filters(self):
        rows = [
            {"id": "1", "ticket_ref": "TKT-1", "status": "open", "organization_id": 1,
             "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": "2", "ticket_ref": "TKT-2", "status": "done", "organization_id": 2,
             "created_at": "2026-02-01T00:00:00+00:00"},
        ]
        raw = _FakeRawClient(tables={"internal_tickets": rows})
        client = _make_client(raw)

        result = await client.list_internal_tickets()

        assert [r["id"] for r in result] == ["2", "1"]  # created_at desc

    @pytest.mark.asyncio
    async def test_list_internal_tickets_filtered_by_status_and_org(self):
        rows = [
            {"id": "1", "ticket_ref": "TKT-1", "status": "open", "organization_id": 1,
             "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": "2", "ticket_ref": "TKT-2", "status": "done", "organization_id": 1,
             "created_at": "2026-02-01T00:00:00+00:00"},
            {"id": "3", "ticket_ref": "TKT-3", "status": "open", "organization_id": 2,
             "created_at": "2026-03-01T00:00:00+00:00"},
        ]
        raw = _FakeRawClient(tables={"internal_tickets": rows})
        client = _make_client(raw)

        result = await client.list_internal_tickets(status="open", organization_id=1)

        assert [r["id"] for r in result] == ["1"]

    @pytest.mark.asyncio
    async def test_update_internal_ticket_status_to_done_sets_resolved_at(self):
        row = {"id": "1", "ticket_ref": "TKT-1", "status": "open", "resolved_at": None}
        raw = _FakeRawClient(tables={"internal_tickets": [row]})
        client = _make_client(raw)

        ok = await client.update_internal_ticket_status("TKT-1", "done")

        assert ok is True
        assert row["status"] == "done"
        assert row["resolved_at"] is not None

    @pytest.mark.asyncio
    async def test_update_internal_ticket_status_non_done_leaves_resolved_at_untouched(self):
        row = {"id": "1", "ticket_ref": "TKT-1", "status": "open", "resolved_at": None}
        raw = _FakeRawClient(tables={"internal_tickets": [row]})
        client = _make_client(raw)

        ok = await client.update_internal_ticket_status("TKT-1", "in_progress")

        assert ok is True
        assert row["status"] == "in_progress"
        assert row["resolved_at"] is None

    @pytest.mark.asyncio
    async def test_update_internal_ticket_status_returns_false_on_error(self):
        raw = _FakeRawClient()

        class _RaisingTable:
            def update(self, *_a, **_k):
                raise RuntimeError("boom")

        raw._tables["internal_tickets"] = _RaisingTable()
        client = _make_client(raw)

        ok = await client.update_internal_ticket_status("TKT-1", "done")

        assert ok is False


# ---------------------------------------------------------------------------
# internal_ticket_comments writes + get_ticket_comments merge
# ---------------------------------------------------------------------------


class TestInternalTicketComments:
    @pytest.mark.asyncio
    async def test_add_internal_ticket_comment_inserts_expected_row(self):
        raw = _FakeRawClient()
        client = _make_client(raw)

        ok = await client.add_internal_ticket_comment(
            ticket_ref="TKT-1",
            body="Looking into it",
            author="staff@example.com",
            is_public=True,
            source="staff",
        )

        assert ok is True
        row = raw.table("internal_ticket_comments").rows[0]
        assert row["ticket_ref"] == "TKT-1"
        assert row["body"] == "Looking into it"
        assert row["author"] == "staff@example.com"
        assert row["is_public"] is True
        assert row["source"] == "staff"

    @pytest.mark.asyncio
    async def test_add_internal_ticket_comment_returns_false_on_error(self):
        raw = _FakeRawClient()

        class _RaisingTable:
            def insert(self, *_a, **_k):
                raise RuntimeError("boom")

        raw._tables["internal_ticket_comments"] = _RaisingTable()
        client = _make_client(raw)

        ok = await client.add_internal_ticket_comment(ticket_ref="TKT-1", body="x")

        assert ok is False

    @pytest.mark.asyncio
    async def test_get_ticket_comments_merges_and_sorts_both_sources(self):
        comments = [
            {
                "id": "c1",
                "ticket_ref": "TKT-1",
                "author": "staff@example.com",
                "body": "Second comment",
                "is_public": True,
                "source": "staff",
                "created_at": "2026-01-02T00:00:00+00:00",
            },
        ]
        messages = [
            {
                "id": "m1",
                "content": "First message forwarded to ticket",
                "sender_telegram_id": "12345",
                "role": "user",
                "metadata": {"ticket_ref": "TKT-1", "ticket_role": "comment"},
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "m2",
                "content": "Unrelated message for a different ticket",
                "sender_telegram_id": "999",
                "role": "user",
                "metadata": {"ticket_ref": "TKT-OTHER", "ticket_role": "comment"},
                "created_at": "2026-01-01T12:00:00+00:00",
            },
        ]
        raw = _FakeRawClient(
            tables={"internal_ticket_comments": comments, "chat_messages": messages}
        )
        client = _make_client(raw)

        result = await client.get_ticket_comments("TKT-1")

        # Only the TKT-1-tagged message should be included, not the unrelated one.
        assert len(result) == 2
        assert [entry["body"] for entry in result] == [
            "First message forwarded to ticket",
            "Second comment",
        ]
        assert [entry["source"] for entry in result] == ["chat_message", "internal_ticket_comments"]
        # Chronological order (message before comment).
        assert result[0]["created_at"] < result[1]["created_at"]
        assert result[0]["author"] == "12345"
        assert result[1]["author"] == "staff@example.com"

    @pytest.mark.asyncio
    async def test_get_ticket_comments_chat_message_is_public_defaults_true(self):
        """A tagged chat_message with no explicit is_public in its metadata
        represents a forwarded customer<->staff exchange, so it must default
        to public -- not silently read as an internal-only note."""
        messages = [
            {
                "id": "m1",
                "content": "No explicit is_public key",
                "sender_telegram_id": "12345",
                "role": "user",
                "metadata": {"ticket_ref": "TKT-1", "ticket_role": "comment"},
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "m2",
                "content": "Explicitly marked non-public",
                "sender_telegram_id": "12345",
                "role": "user",
                "metadata": {
                    "ticket_ref": "TKT-1",
                    "ticket_role": "comment",
                    "is_public": False,
                },
                "created_at": "2026-01-01T01:00:00+00:00",
            },
        ]
        raw = _FakeRawClient(tables={"internal_ticket_comments": [], "chat_messages": messages})
        client = _make_client(raw)

        result = await client.get_ticket_comments("TKT-1")

        assert result[0]["is_public"] is True
        assert result[1]["is_public"] is False

    @pytest.mark.asyncio
    async def test_get_ticket_comments_caps_total_at_limit(self):
        comments = [
            {
                "id": f"c{i}",
                "ticket_ref": "TKT-1",
                "author": "staff",
                "body": f"comment {i}",
                "is_public": True,
                "created_at": f"2026-01-01T00:0{i}:00+00:00",
            }
            for i in range(3)
        ]
        raw = _FakeRawClient(tables={"internal_ticket_comments": comments, "chat_messages": []})
        client = _make_client(raw)

        result = await client.get_ticket_comments("TKT-1", limit=2)

        assert len(result) == 2
        # Most recent 2 of the 3 kept, still chronologically ordered.
        assert [entry["body"] for entry in result] == ["comment 1", "comment 2"]

    @pytest.mark.asyncio
    async def test_get_ticket_comments_returns_empty_list_on_error(self):
        raw = _FakeRawClient()

        class _RaisingTable:
            def select(self, *_a, **_k):
                raise RuntimeError("boom")

        raw._tables["internal_ticket_comments"] = _RaisingTable()
        client = _make_client(raw)

        result = await client.get_ticket_comments("TKT-1")

        assert result == []


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
