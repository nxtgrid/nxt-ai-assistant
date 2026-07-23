"""Tests for InternalTicketBackend (create/comment/status/close).

Uses a small fake standing in for the real Supabase (postgrest) client's
fluent API -- the same style as
chat_orchestrator/tests/services/test_work_packet_service.py -- so tests can
assert on what actually got persisted rather than just call arguments.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing.backend import TicketBackendError, TicketCreateRequest
from orchestrator.services.ticketing.internal_backend import InternalTicketBackend


class _FakeResult:
    def __init__(self, data: List[Dict[str, Any]]):
        self.data = data


class _InternalTicketsTable:
    """Fakes .select()/.update()/.eq()/.limit()/.execute() for internal_tickets."""

    def __init__(self, client: "FakeSupabaseClient"):
        self._client = client
        self._mode: Optional[str] = None
        self._eq_field: Optional[str] = None
        self._eq_value: Any = None
        self._update_payload: Optional[Dict[str, Any]] = None

    def select(self, *_args, **_kwargs) -> "_InternalTicketsTable":
        self._mode = "select"
        return self

    def update(self, payload: Dict[str, Any]) -> "_InternalTicketsTable":
        self._mode = "update"
        self._update_payload = payload
        return self

    def eq(self, field: str, value: Any) -> "_InternalTicketsTable":
        self._eq_field = field
        self._eq_value = value
        return self

    def limit(self, _n: int) -> "_InternalTicketsTable":
        return self

    def execute(self) -> _FakeResult:
        if self._mode == "select":
            matches = [
                t for t in self._client.tickets if t.get(self._eq_field) == self._eq_value
            ]
            return _FakeResult(matches)
        if self._mode == "update":
            for t in self._client.tickets:
                if t.get(self._eq_field) == self._eq_value:
                    t.update(self._update_payload or {})
            return _FakeResult([])
        raise AssertionError("execute() called before select()/update()")


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
        self.raise_on_rpc: Optional[Exception] = None

    def table(self, name: str):
        if name == "internal_tickets":
            return _InternalTicketsTable(self)
        if name == "internal_ticket_comments":
            return _CommentsTable(self)
        raise AssertionError(f"Unexpected table: {name}")

    def rpc(self, name: str, params: Dict[str, Any]):
        if name != "create_internal_ticket":
            raise AssertionError(f"Unexpected rpc: {name}")
        self.rpc_calls.append(params)
        return _RpcCall(self, params)


class _RpcCall:
    def __init__(self, client: FakeSupabaseClient, params: Dict[str, Any]):
        self._client = client
        self._params = params

    def execute(self) -> _FakeResult:
        if self._client.raise_on_rpc:
            raise self._client.raise_on_rpc
        self._client._seq += 1
        prefix = self._params.get("p_prefix") or "TKT"
        ref = f"{prefix}-{self._client._seq:06d}"
        row = {
            "ticket_ref": ref,
            "escalation_mapping_id": self._params.get("p_escalation_mapping_id"),
            "session_id": self._params.get("p_session_id"),
            "organization_id": self._params.get("p_organization_id"),
            "grid_name": self._params.get("p_grid_name"),
            "summary": self._params.get("p_summary"),
            "description": self._params.get("p_description"),
            "assignee_email": self._params.get("p_assignee_email"),
            "labels": self._params.get("p_labels") or [],
            "source": self._params.get("p_source") or "escalation",
            "status": "open",
        }
        self._client.tickets.append(row)
        return _FakeResult([row])


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

        call = fake.rpc_calls[0]
        assert call["p_escalation_mapping_id"] == "11111111-1111-1111-1111-111111111111"
        assert call["p_session_id"] == "sess-1"
        assert call["p_organization_id"] == 7
        assert call["p_grid_name"] == "MainGrid"
        assert call["p_assignee_email"] == "a@b.com"
        assert call["p_labels"] == ["escalation-abc"]
        assert call["p_source"] == "escalation"

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
