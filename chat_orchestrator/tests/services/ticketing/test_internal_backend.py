"""Tests for InternalTicketBackend (create/comment/status/close).

Uses a small fake standing in for the real Supabase (postgrest) client's
fluent API -- the same style as
chat_orchestrator/tests/services/test_work_packet_service.py -- so tests can
assert on what actually got persisted rather than just call arguments.

``create_ticket`` only allocates a reference through
``.rpc("next_internal_ticket_ref", {"p_prefix": ...})``.  The enclosing
``TicketService`` owns canonical ticket intent creation and activation;
this backend must never write the legacy ``internal_tickets`` relation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from orchestrator.services.ticketing.attachment_repository import EscalationAttachment
from orchestrator.services.ticketing.backend import (
    TicketBackendError,
    TicketCreateRequest,
    TicketStatus,
    TicketSummary,
)
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


class FakeCanonicalTicketRepository:
    """In-memory canonical ticket boundary for backend delegation tests."""

    def __init__(self):
        self.tickets: List[Dict[str, Any]] = []
        self.comments: List[Dict[str, Any]] = []
        self.raise_on_comment: Optional[Exception] = None
        self.raise_on_update: Optional[Exception] = None
        self.raise_on_find: Optional[Exception] = None

    def _ticket(self, ref: str) -> Optional[Dict[str, Any]]:
        return next((ticket for ticket in self.tickets if ticket["ticket_ref"] == ref), None)

    async def add_comment_by_ref(self, ref: str, body: str, *, is_public: bool = False) -> None:
        if self.raise_on_comment:
            raise self.raise_on_comment
        self.comments.append(
            {"ticket_ref": ref, "body": body, "is_public": is_public, "source": "staff"}
        )

    async def get_status_by_ref(self, ref: str) -> Optional[TicketStatus]:
        ticket = self._ticket(ref)
        if ticket is None:
            return None
        return TicketStatus(
            summary=ticket["summary"],
            is_done=ticket["status"] == "done",
            raw_status=ticket["status"],
            ticket_type=ticket["ticket_type"],
        )

    async def transition_to_done_by_ref(self, ref: str) -> bool:
        ticket = self._ticket(ref)
        if ticket is None or ticket["status"] == "done":
            return False
        ticket["status"] = "done"
        ticket["resolved_at"] = "2026-01-01T00:01:00Z"
        return True

    async def find_ref_for_escalation(self, mapping_id: str) -> Optional[str]:
        if self.raise_on_find:
            raise self.raise_on_find
        ticket = next(
            (ticket for ticket in self.tickets if ticket["escalation_mapping_id"] == mapping_id), None
        )
        return ticket["ticket_ref"] if ticket else None

    async def update_by_ref(
        self, ref: str, *, summary: Optional[str] = None, description: Optional[str] = None
    ) -> None:
        if self.raise_on_update:
            raise self.raise_on_update
        ticket = self._ticket(ref)
        if ticket is not None:
            if summary is not None:
                ticket["summary"] = summary
            if description is not None:
                ticket["description"] = description

    async def find_open_internal_by_grid(self, grid_name: str, *, limit: int = 20):
        if self.raise_on_find:
            raise self.raise_on_find
        rows = [
            ticket
            for ticket in self.tickets
            if ticket["grid_name"] == grid_name and ticket["status"] != "done"
        ]
        rows.sort(key=lambda ticket: ticket["created_at"], reverse=True)
        return [
            TicketSummary(
                ref=ticket["ticket_ref"],
                backend="internal",
                summary=ticket["summary"],
                description=ticket["description"] or "",
                status=ticket["status"],
                is_done=ticket["status"] == "done",
                created_at=ticket["created_at"],
                labels=ticket["labels"],
            )
            for ticket in rows[:limit]
        ]


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
    fake.canonical_tickets = FakeCanonicalTicketRepository()
    backend = InternalTicketBackend(client=fake, ticket_repository=fake.canonical_tickets)
    return backend, fake


def _seed_ticket(
    fake: FakeSupabaseClient,
    *,
    ref: str = "TKT-000001",
    summary: str = "s",
    description: Optional[str] = None,
    escalation_mapping_id: Optional[str] = None,
    grid_name: Optional[str] = None,
    ticket_type: str = "Task",
) -> None:
    """Seed legacy data only for the remaining legacy-operation tests.

    Creation does not seed this table: that is the regression this suite
    protects while the other operations are migrated in the next slice.
    """
    fake.canonical_tickets.tickets.append(
        {
            "ticket_ref": ref,
            "summary": summary,
            "description": description,
            "escalation_mapping_id": escalation_mapping_id,
            "grid_name": grid_name,
            "ticket_type": ticket_type,
            "status": "open",
            "created_at": f"2026-01-01T00:00:{len(fake.canonical_tickets.tickets):02d}Z",
            "labels": [],
        }
    )


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
        assert fake.tickets == []
        assert fake.insert_calls == []
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
    async def test_does_not_write_request_fields_to_legacy_ticket_table(self):
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

        assert fake.insert_calls == []
        assert fake.tickets == []

    @pytest.mark.asyncio
    async def test_persists_requested_ticket_type(self):
        backend, fake = _make_backend()

        result = await backend.create_ticket(
            TicketCreateRequest(summary="s", ticket_type="Electricity Service Disruption")
        )

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
    async def test_ignores_legacy_insert_errors(self):
        fake = FakeSupabaseClient()
        fake.raise_on_insert = RuntimeError("insert failed")
        backend, _ = _make_backend(fake)

        result = await backend.create_ticket(TicketCreateRequest(summary="x"))

        assert result.ref == "TKT-000001"
        assert len(fake.rpc_calls) == 1
        assert fake.insert_calls == []


class TestAddComment:
    @pytest.mark.asyncio
    async def test_writes_comment_row(self):
        backend, fake = _make_backend()

        ok = await backend.add_comment("TKT-000001", "hello customer", public=True)

        assert ok is True
        assert fake.canonical_tickets.comments[0]["ticket_ref"] == "TKT-000001"
        assert fake.canonical_tickets.comments[0]["body"] == "hello customer"
        assert fake.canonical_tickets.comments[0]["is_public"] is True
        assert fake.canonical_tickets.comments[0]["source"] == "staff"

    @pytest.mark.asyncio
    async def test_defaults_to_not_public(self):
        backend, fake = _make_backend()

        await backend.add_comment("TKT-000001", "internal note")

        assert fake.canonical_tickets.comments[0]["is_public"] is False

    @pytest.mark.asyncio
    async def test_false_when_no_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        assert await backend.add_comment("TKT-000001", "x") is False


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_returns_status_for_existing_ticket(self):
        backend, fake = _make_backend()
        _seed_ticket(fake)

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
        _seed_ticket(fake)

        flipped = await backend.transition_to_done("TKT-000001")

        assert flipped is True
        assert fake.canonical_tickets.tickets[0]["status"] == "done"
        assert "resolved_at" in fake.canonical_tickets.tickets[0]

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
        _seed_ticket(fake, escalation_mapping_id=mapping_id)

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
        _seed_ticket(fake, summary="orig", description="orig d")

        ok = await backend.update_ticket(
            "TKT-000001", summary="new summary", description="new description"
        )

        assert ok is True
        assert fake.canonical_tickets.tickets[0]["summary"] == "new summary"
        assert fake.canonical_tickets.tickets[0]["description"] == "new description"

    @pytest.mark.asyncio
    async def test_ignores_priority_id(self):
        """Internal backend has no priority concept -- must not raise or write it."""
        backend, fake = _make_backend()
        _seed_ticket(fake, summary="orig")

        ok = await backend.update_ticket("TKT-000001", summary="s2", priority_id="10001")

        assert ok is True
        assert "priority_id" not in fake.canonical_tickets.tickets[0]
        assert "priority" not in fake.canonical_tickets.tickets[0]

    @pytest.mark.asyncio
    async def test_partial_update_omits_unset_fields(self):
        backend, fake = _make_backend()
        _seed_ticket(fake, summary="orig", description="orig d")

        await backend.update_ticket("TKT-000001", summary="new summary only")

        assert fake.canonical_tickets.tickets[0]["summary"] == "new summary only"
        assert fake.canonical_tickets.tickets[0]["description"] == "orig d"

    @pytest.mark.asyncio
    async def test_false_when_no_client(self):
        backend = InternalTicketBackend(get_client=lambda: None)
        assert await backend.update_ticket("TKT-000001", summary="x") is False

    @pytest.mark.asyncio
    async def test_false_on_error(self):
        fake = FakeSupabaseClient()
        fake.raise_on_insert = None
        backend, _ = _make_backend(fake)
        _seed_ticket(fake)

        fake.canonical_tickets.raise_on_update = RuntimeError("db down")

        assert await backend.update_ticket("TKT-000001", summary="x") is False


class TestFindOpenByGrid:
    @pytest.mark.asyncio
    async def test_returns_open_tickets_for_grid_most_recent_first(self):
        backend, fake = _make_backend()
        _seed_ticket(fake, summary="first", grid_name="Kudi")
        _seed_ticket(fake, ref="TKT-000002", summary="second", grid_name="Kudi")
        _seed_ticket(fake, ref="TKT-000003", summary="other grid", grid_name="Other")

        results = await backend.find_open_by_grid("Kudi")

        assert [r.summary for r in results] == ["second", "first"]
        assert all(r.backend == "internal" for r in results)
        assert all(r.is_done is False for r in results)

    @pytest.mark.asyncio
    async def test_excludes_done_tickets(self):
        backend, fake = _make_backend()
        _seed_ticket(fake, summary="s1", grid_name="Kudi")
        _seed_ticket(fake, ref="TKT-000002", summary="s2", grid_name="Kudi")
        await backend.transition_to_done("TKT-000001")

        results = await backend.find_open_by_grid("Kudi")

        assert [r.ref for r in results] == ["TKT-000002"]

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        backend, fake = _make_backend()
        for i in range(5):
            _seed_ticket(fake, ref=f"TKT-{i:06d}", summary=f"s{i}", grid_name="Kudi")

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

        fake.canonical_tickets.raise_on_find = RuntimeError("db down")

        assert await backend.find_open_by_grid("Kudi") == []


class TestAddAttachments:
    @pytest.mark.asyncio
    async def test_returns_empty_list_without_touching_anything(self) -> None:
        backend = InternalTicketBackend(client=MagicMock())
        attachment = EscalationAttachment(
            id="att-1",
            escalation_id="esc-1",
            ticket_id="ticket-1",
            storage_path="esc-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        result = await backend.add_attachments("INT-1", [attachment])
        assert result == []
