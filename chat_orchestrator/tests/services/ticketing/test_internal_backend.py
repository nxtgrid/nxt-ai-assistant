"""Tests for InternalTicketBackend (create/comment/status/close).

Uses a small fake standing in for the real Supabase (postgrest) client's
fluent API -- the same style as
chat_orchestrator/tests/services/test_work_packet_service.py -- so tests can
assert on what actually got persisted rather than just call arguments.

``create_ticket`` is a two-round-trip call against the fake: first
``.rpc("next_internal_ticket_ref", {"p_prefix": ...})`` (returns a scalar
ref string, matching PostgREST's response shape for a non-set-returning
SQL function), then ``.table("internal_tickets").insert({...}).execute()``
for the actual row write -- mirroring the real
InternalTicketBackend.create_ticket() implementation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing.backend import TicketBackendError, TicketCreateRequest
from orchestrator.services.ticketing.internal_backend import InternalTicketBackend


class _FakeResult:
    def __init__(self, data: Any):
        self.data = data


class _InternalTicketsTable:
    """Fakes .select()/.insert()/.update()/.eq()/.neq()/.order()/.limit()/.execute()
    for internal_tickets. Predicates chain (AND); order/limit apply only to select."""

    def __init__(self, client: "FakeSupabaseClient"):
        self._client = client
        self._mode: Optional[str] = None
        self._predicates: List[tuple] = []  # (field, op, value)
        self._order_field: Optional[str] = None
        self._order_desc: bool = False
        self._limit: Optional[int] = None
        self._payload: Optional[Dict[str, Any]] = None

    def select(self, *_args, **_kwargs) -> "_InternalTicketsTable":
        self._mode = "select"
        return self

    def insert(self, payload: Dict[str, Any]) -> "_InternalTicketsTable":
        self._mode = "insert"
        self._payload = payload
        return self

    def update(self, payload: Dict[str, Any]) -> "_InternalTicketsTable":
        self._mode = "update"
        self._payload = payload
        return self

    def eq(self, field: str, value: Any) -> "_InternalTicketsTable":
        self._predicates.append((field, "eq", value))
        return self

    def neq(self, field: str, value: Any) -> "_InternalTicketsTable":
        self._predicates.append((field, "neq", value))
        return self

    def order(self, field: str, desc: bool = False) -> "_InternalTicketsTable":
        self._order_field = field
        self._order_desc = desc
        return self

    def limit(self, n: int) -> "_InternalTicketsTable":
        self._limit = n
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        for field, op, value in self._predicates:
            if op == "eq" and row.get(field) != value:
                return False
            if op == "neq" and row.get(field) == value:
                return False
        return True

    def execute(self) -> _FakeResult:
        if self._mode == "select":
            matches = [t for t in self._client.tickets if self._matches(t)]
            if self._order_field:
                matches.sort(
                    key=lambda t: t.get(self._order_field) or "", reverse=self._order_desc
                )
            if self._limit is not None:
                matches = matches[: self._limit]
            return _FakeResult(matches)
        if self._mode == "insert":
            if self._client.raise_on_insert:
                raise self._client.raise_on_insert
            row = {
                "ticket_ref": self._payload.get("ticket_ref"),
                "escalation_mapping_id": self._payload.get("escalation_mapping_id"),
                "session_id": self._payload.get("session_id"),
                "organization_id": self._payload.get("organization_id"),
                "grid_name": self._payload.get("grid_name"),
                "summary": self._payload.get("summary"),
                "description": self._payload.get("description"),
                "ticket_type": self._payload.get("ticket_type"),
                "assignee_email": self._payload.get("assignee_email"),
                "labels": self._payload.get("labels") or [],
                "source": self._payload.get("source") or "escalation",
                "status": "open",
                "created_at": f"2026-01-01T00:00:{len(self._client.tickets):02d}Z",
            }
            self._client.tickets.append(row)
            self._client.insert_calls.append(dict(self._payload))
            return _FakeResult([row])
        if self._mode == "update":
            for t in self._client.tickets:
                if self._matches(t):
                    t.update(self._payload or {})
            return _FakeResult([])
        raise AssertionError("execute() called before select()/insert()/update()")


class _CommentsTable:
    def __init__(self, client: "FakeSupabaseClient"):
        self._client = client
        self._insert_payload: Optional[Dict[str, Any]] = None

    def insert(self, payload: Dict[str, Any]) -> "_CommentsTable":
        self._insert_payload = payload
        return self

    def execute(self) -> _FakeResult:
        self._client.comments.append(self._insert_payload)
        return _FakeResult([self._insert_payload])


class FakeSupabaseClient:
    """Minimal fake for the raw postgrest client (what ``_get_client()`` returns)."""

    def __init__(self, seed_seq: int = 0):
        self.tickets: List[Dict[str, Any]] = []
        self.comments: List[Dict[str, Any]] = []
        self._seq = seed_seq
        self.rpc_calls: List[Dict[str, Any]] = []
        self.insert_calls: List[Dict[str, Any]] = []
        self.raise_on_rpc: Optional[Exception] = None
        self.raise_on_insert: Optional[Exception] = None

    def table(self, name: str):
        if name == "internal_tickets":
            return _InternalTicketsTable(self)
        if name == "internal_ticket_comments":
            return _CommentsTable(self)
        raise AssertionError(f"Unexpected table: {name}")

    def rpc(self, name: str, params: Dict[str, Any]):
        if name != "next_internal_ticket_ref":
            raise AssertionError(f"Unexpected rpc: {name}")
        self.rpc_calls.append(params)
        return _RpcCall(self, params)


class _RpcCall:
    """Fakes the ref-allocation RPC. Returns a bare scalar string in
    ``.data``, matching PostgREST's response shape for a function that
    returns a plain (non-set) type rather than SETOF/TABLE."""

    def __init__(self, client: FakeSupabaseClient, params: Dict[str, Any]):
        self._client = client
        self._params = params

    def execute(self) -> _FakeResult:
        if self._client.raise_on_rpc:
            raise self._client.raise_on_rpc
        self._client._seq += 1
        prefix = self._params.get("p_prefix") or "TKT"
        ref = f"{prefix}-{self._client._seq:06d}"
        return _FakeResult(ref)


def _make_backend(client: Optional[FakeSupabaseClient] = None):
    fake = client or FakeSupabaseClient()
    backend = InternalTicketBackend(client=fake)
    return backend, fake


class TestConstruction:
    def test_requires_client_or_getter(self):
        with pytest.raises(ValueError):
            InternalTicketBackend()

    @pytest.mark.asyncio
    async def test_is_available_true_with_client(self):
        backend, _fake = _make_backend()
        assert await backend.is_available() is True

    @pytest.mark.asyncio
    async def test_is_available_false_without_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        assert await backend.is_available() is False


class TestCreateTicket:
    @pytest.mark.asyncio
    async def test_allocates_prefixed_ref(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_TICKET_PREFIX", "TKT")
        backend, fake = _make_backend()
        req = TicketCreateRequest(summary="Customer needs help", description="details here")

        result = await backend.create_ticket(req)

        assert result.backend == "internal"
        assert result.url is None
        assert result.ref == "TKT-000001"
        assert fake.tickets[0]["summary"] == "Customer needs help"
        assert fake.tickets[0]["ticket_type"] == "Task"
        assert result.ticket_type == "Task"

    @pytest.mark.asyncio
    async def test_sequential_refs_increment(self):
        backend, fake = _make_backend()
        r1 = await backend.create_ticket(TicketCreateRequest(summary="first"))
        r2 = await backend.create_ticket(TicketCreateRequest(summary="second"))

        assert r1.ref == "TKT-000001"
        assert r2.ref == "TKT-000002"

    @pytest.mark.asyncio
    async def test_uses_configured_prefix(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_TICKET_PREFIX", "SUP")
        backend, fake = _make_backend()

        result = await backend.create_ticket(TicketCreateRequest(summary="x"))

        assert result.ref == "SUP-000001"

    @pytest.mark.asyncio
    async def test_passes_through_escalation_fields(self):
        backend, fake = _make_backend()
        req = TicketCreateRequest(
            summary="s",
            description="d",
            escalation_mapping_id="11111111-1111-1111-1111-111111111111",
            session_id="sess-1",
            organization_id=7,
            grid_name="MainGrid",
            assignee_email="a@b.com",
            labels=["escalation-abc"],
            source="escalation",
        )

        await backend.create_ticket(req)

        # The ref-allocation RPC only ever receives the prefix.
        rpc_call = fake.rpc_calls[0]
        assert set(rpc_call.keys()) == {"p_prefix"}

        # Everything else flows through the plain insert().
        insert_call = fake.insert_calls[0]
        assert insert_call["escalation_mapping_id"] == "11111111-1111-1111-1111-111111111111"
        assert insert_call["session_id"] == "sess-1"
        assert insert_call["organization_id"] == 7
        assert insert_call["grid_name"] == "MainGrid"
        assert insert_call["assignee_email"] == "a@b.com"
        assert insert_call["labels"] == ["escalation-abc"]
        assert insert_call["source"] == "escalation"
        assert insert_call["ticket_ref"] == "TKT-000001"

    @pytest.mark.asyncio
    async def test_persists_requested_ticket_type(self):
        backend, fake = _make_backend()

        result = await backend.create_ticket(
            TicketCreateRequest(summary="s", ticket_type="Electricity Service Disruption")
        )

        assert fake.tickets[0]["ticket_type"] == "Electricity Service Disruption"
        assert result.ticket_type == "Electricity Service Disruption"

    @pytest.mark.asyncio
    async def test_raises_when_no_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        with pytest.raises(TicketBackendError):
            await backend.create_ticket(TicketCreateRequest(summary="x"))

    @pytest.mark.asyncio
    async def test_raises_when_rpc_errors(self):
        fake = FakeSupabaseClient()
        fake.raise_on_rpc = RuntimeError("db down")
        backend, _ = _make_backend(fake)

        with pytest.raises(TicketBackendError):
            await backend.create_ticket(TicketCreateRequest(summary="x"))

    @pytest.mark.asyncio
    async def test_raises_when_insert_errors(self):
        fake = FakeSupabaseClient()
        fake.raise_on_insert = RuntimeError("insert failed")
        backend, _ = _make_backend(fake)

        with pytest.raises(TicketBackendError):
            await backend.create_ticket(TicketCreateRequest(summary="x"))

        # The ref-allocation call still happened -- the failure was on the
        # second round-trip (the insert), confirming this really exercises
        # a two-call sequence rather than short-circuiting on the RPC.
        assert len(fake.rpc_calls) == 1


class TestAddComment:
    @pytest.mark.asyncio
    async def test_writes_comment_row(self):
        backend, fake = _make_backend()

        ok = await backend.add_comment("TKT-000001", "hello customer", public=True)

        assert ok is True
        assert fake.comments[0]["ticket_ref"] == "TKT-000001"
        assert fake.comments[0]["body"] == "hello customer"
        assert fake.comments[0]["is_public"] is True
        assert fake.comments[0]["source"] == "staff"

    @pytest.mark.asyncio
    async def test_defaults_to_not_public(self):
        backend, fake = _make_backend()

        await backend.add_comment("TKT-000001", "internal note")

        assert fake.comments[0]["is_public"] is False

    @pytest.mark.asyncio
    async def test_false_when_no_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        assert await backend.add_comment("TKT-000001", "x") is False


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_returns_status_for_existing_ticket(self):
        backend, fake = _make_backend()
        await backend.create_ticket(TicketCreateRequest(summary="s"))

        status = await backend.get_status("TKT-000001")

        assert status is not None
        assert status.summary == "s"
        assert status.is_done is False
        assert status.raw_status == "open"
        assert status.ticket_type == "Task"

    @pytest.mark.asyncio
    async def test_none_for_unknown_ref(self):
        backend, _fake = _make_backend()
        assert await backend.get_status("TKT-999999") is None

    @pytest.mark.asyncio
    async def test_none_when_no_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        assert await backend.get_status("TKT-000001") is None


class TestTransitionToDone:
    @pytest.mark.asyncio
    async def test_marks_ticket_done(self):
        backend, fake = _make_backend()
        await backend.create_ticket(TicketCreateRequest(summary="s"))

        await backend.transition_to_done("TKT-000001")

        assert fake.tickets[0]["status"] == "done"
        assert "resolved_at" in fake.tickets[0]

        status = await backend.get_status("TKT-000001")
        assert status is not None
        assert status.is_done is True

    @pytest.mark.asyncio
    async def test_noop_when_no_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        # Should not raise.
        await backend.transition_to_done("TKT-000001")


class TestFindByEscalation:
    @pytest.mark.asyncio
    async def test_finds_ticket_by_mapping_id(self):
        backend, fake = _make_backend()
        mapping_id = "22222222-2222-2222-2222-222222222222"
        await backend.create_ticket(
            TicketCreateRequest(summary="s", escalation_mapping_id=mapping_id)
        )

        found = await backend.find_by_escalation(mapping_id)

        assert found == "TKT-000001"

    @pytest.mark.asyncio
    async def test_none_when_not_found(self):
        backend, _fake = _make_backend()
        assert await backend.find_by_escalation("does-not-exist") is None

    @pytest.mark.asyncio
    async def test_none_when_no_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        assert await backend.find_by_escalation("anything") is None


class TestUpdateTicket:
    @pytest.mark.asyncio
    async def test_updates_summary_and_description(self):
        backend, fake = _make_backend()
        await backend.create_ticket(TicketCreateRequest(summary="orig", description="orig d"))

        ok = await backend.update_ticket(
            "TKT-000001", summary="new summary", description="new description"
        )

        assert ok is True
        assert fake.tickets[0]["summary"] == "new summary"
        assert fake.tickets[0]["description"] == "new description"

    @pytest.mark.asyncio
    async def test_ignores_priority_id(self):
        """Internal backend has no priority concept -- must not raise or write it."""
        backend, fake = _make_backend()
        await backend.create_ticket(TicketCreateRequest(summary="orig"))

        ok = await backend.update_ticket("TKT-000001", summary="s2", priority_id="10001")

        assert ok is True
        assert "priority_id" not in fake.tickets[0]
        assert "priority" not in fake.tickets[0]

    @pytest.mark.asyncio
    async def test_partial_update_omits_unset_fields(self):
        backend, fake = _make_backend()
        await backend.create_ticket(TicketCreateRequest(summary="orig", description="orig d"))

        await backend.update_ticket("TKT-000001", summary="new summary only")

        assert fake.tickets[0]["summary"] == "new summary only"
        assert fake.tickets[0]["description"] == "orig d"

    @pytest.mark.asyncio
    async def test_false_when_no_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        assert await backend.update_ticket("TKT-000001", summary="x") is False

    @pytest.mark.asyncio
    async def test_false_on_error(self):
        fake = FakeSupabaseClient()
        fake.raise_on_insert = None
        backend, _ = _make_backend(fake)
        await backend.create_ticket(TicketCreateRequest(summary="s"))

        class _RaisingTable:
            def update(self, *_a, **_k):
                raise RuntimeError("db down")

        original_table = fake.table
        fake.table = lambda name: _RaisingTable() if name == "internal_tickets" else original_table(name)

        assert await backend.update_ticket("TKT-000001", summary="x") is False


class TestFindOpenByGrid:
    @pytest.mark.asyncio
    async def test_returns_open_tickets_for_grid_most_recent_first(self):
        backend, fake = _make_backend()
        await backend.create_ticket(TicketCreateRequest(summary="first", grid_name="Kudi"))
        await backend.create_ticket(TicketCreateRequest(summary="second", grid_name="Kudi"))
        await backend.create_ticket(TicketCreateRequest(summary="other grid", grid_name="Other"))

        results = await backend.find_open_by_grid("Kudi")

        assert [r.summary for r in results] == ["second", "first"]
        assert all(r.backend == "internal" for r in results)
        assert all(r.is_done is False for r in results)

    @pytest.mark.asyncio
    async def test_excludes_done_tickets(self):
        backend, fake = _make_backend()
        await backend.create_ticket(TicketCreateRequest(summary="s1", grid_name="Kudi"))
        await backend.create_ticket(TicketCreateRequest(summary="s2", grid_name="Kudi"))
        await backend.transition_to_done("TKT-000001")

        results = await backend.find_open_by_grid("Kudi")

        assert [r.ref for r in results] == ["TKT-000002"]

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        backend, fake = _make_backend()
        for i in range(5):
            await backend.create_ticket(TicketCreateRequest(summary=f"s{i}", grid_name="Kudi"))

        results = await backend.find_open_by_grid("Kudi", limit=2)

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_empty_when_no_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        assert await backend.find_open_by_grid("Kudi") == []

    @pytest.mark.asyncio
    async def test_empty_on_error(self):
        fake = FakeSupabaseClient()
        backend, _ = _make_backend(fake)

        class _RaisingTable:
            def select(self, *_a, **_k):
                raise RuntimeError("db down")

        fake.table = lambda name: _RaisingTable()

        assert await backend.find_open_by_grid("Kudi") == []
