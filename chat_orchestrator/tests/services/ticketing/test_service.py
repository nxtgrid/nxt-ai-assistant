"""TicketService orchestration logic beyond resolve_backend().

Covers: _backend_for_ref()'s three branches (found in internal_tickets / not
found -> jira / lookup raises -> jira), and find_by_escalation()'s
jira-then-internal composition including the TICKET_BACKEND_OVERRIDE=internal
short-circuit.

resolve_backend() itself is covered by test_service_resolve_backend.py.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.ticketing.attachment_repository import EscalationAttachment
from orchestrator.services.ticketing.backend import (
    AttachmentSyncResult,
    BackendTicketResult,
    TicketBackendError,
    TicketCreateRequest,
    TicketResult,
    TicketStatus,
    TicketSummary,
)
from orchestrator.services.ticketing.repository import TicketRecord
from orchestrator.services.ticketing.service import TicketService


class _FakeBackend:
    """Minimal stand-in for either TicketBackend implementation."""

    def __init__(self, name: str, ref: str = "REF-1") -> None:
        self.name = name
        self._ref = ref
        self.find_by_escalation_calls: List[str] = []
        self.update_ticket_calls: List[tuple] = []
        self.find_open_by_grid_calls: List[tuple] = []
        self.find_open_by_grid_result: List[TicketSummary] = []
        self.update_ticket_result: bool = True
        self.create_error: Optional[Exception] = None
        self.create_calls: List[TicketCreateRequest] = []
        self.transition_to_done_calls: List[str] = []
        self.transition_returns: bool = True
        self.status_by_ref: Dict[str, Optional[TicketStatus]] = {}
        self.get_status_calls: List[str] = []
        self.get_status_error: Optional[Exception] = None

    async def is_available(self) -> bool:
        return True

    def has_credentials(self) -> bool:
        return True

    async def transition_to_done(self, ref: str) -> bool:
        self.transition_to_done_calls.append(ref)
        return self.transition_returns

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        self.get_status_calls.append(ref)
        if self.get_status_error is not None:
            raise self.get_status_error
        return self.status_by_ref.get(ref)

    async def create_ticket(self, req: TicketCreateRequest) -> TicketResult:
        self.create_calls.append(req)
        if self.create_error is not None:
            raise self.create_error
        return TicketResult(ref=self._ref, backend=self.name, url=None)

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        self.find_by_escalation_calls.append(mapping_id)
        return None

    async def update_ticket(self, ref: str, summary=None, description=None, priority_id=None) -> bool:
        self.update_ticket_calls.append((ref, summary, description, priority_id))
        return self.update_ticket_result

    async def find_open_by_grid(self, grid_name: str, limit: int = 20) -> List[TicketSummary]:
        self.find_open_by_grid_calls.append((grid_name, limit))
        return self.find_open_by_grid_result


class _FakeTicketRepository:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.records_by_ref: dict[str, TicketRecord] = {}
        self.records_by_id: dict[str, TicketRecord] = {}
        self.refs_by_escalation: dict[str, str] = {}
        self.find_ref_for_escalation_calls: list[str] = []
        self.transition_to_done_by_ref_calls: list[str] = []
        self.transition_returns: bool = True
        self.set_in_progress_by_ref_calls: list[str] = []
        self.in_progress_returns: bool = True
        self.comments_by_ref: dict[str, list[dict]] = {}
        self.open_refs_by_backend: dict[str, list[str]] = {}
        self.list_open_by_backend_calls: list[tuple] = []
        self.recently_done_refs_by_backend: dict[str, list[str]] = {}
        self.list_recently_done_by_backend_calls: list[tuple] = []
        self.sync_backend_status_calls: list[tuple[str, str]] = []
        self.reopen_by_ref_calls: list[tuple[str, str]] = []
        self.reopen_returns: bool = True

    async def create_intent(self, req, *, created_via):
        self.calls.append(("intent", created_via, req.summary))
        return TicketRecord(
            id="ticket-1", summary=req.summary, created_via=created_via,
            provisioning_state="pending",
        )

    async def set_pending_backend(self, ticket_id, backend):
        self.calls.append(("backend", ticket_id, backend))

    async def activate(self, ticket_id, result):
        self.calls.append(("activate", ticket_id, result.ref, result.backend))
        return TicketRecord(
            id=ticket_id, ticket_ref=result.ref, backend=result.backend,
            summary="x", created_via="notification", provisioning_state="active",
        )

    async def get_by_ref(self, ref: str) -> Optional[TicketRecord]:
        return self.records_by_ref.get(ref)

    async def get_by_id(self, ticket_id: str) -> Optional[TicketRecord]:
        return self.records_by_id.get(ticket_id)

    async def find_ref_for_escalation(self, escalation_id: str) -> Optional[str]:
        self.find_ref_for_escalation_calls.append(escalation_id)
        return self.refs_by_escalation.get(escalation_id)

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        return True

    async def get_status(self, ref: str):
        return None

    async def get_status_by_ref(self, ref: str) -> Optional[TicketStatus]:
        record = self.records_by_ref.get(ref)
        if record is None:
            return None
        return TicketStatus(
            summary=record.summary, is_done=record.status == "done",
            raw_status=record.status, ticket_type=record.ticket_type,
        )

    async def list_comments_by_ref(self, ref: str, *, limit: int = 5) -> List[dict]:
        return self.comments_by_ref.get(ref, [])

    async def transition_to_done_by_ref(self, ref: str) -> bool:
        self.transition_to_done_by_ref_calls.append(ref)
        return self.transition_returns

    async def set_in_progress_by_ref(self, ref: str) -> bool:
        self.set_in_progress_by_ref_calls.append(ref)
        return self.in_progress_returns

    async def list_open_by_backend(self, backend: str, *, limit: int = 200) -> List[str]:
        self.list_open_by_backend_calls.append((backend, limit))
        return self.open_refs_by_backend.get(backend, [])

    async def list_recently_done_by_backend(
        self, backend: str, *, since: str, limit: int = 200
    ) -> List[str]:
        self.list_recently_done_by_backend_calls.append((backend, since, limit))
        return self.recently_done_refs_by_backend.get(backend, [])

    async def sync_backend_status_by_ref(self, ref: str, backend_status: str) -> None:
        self.sync_backend_status_calls.append((ref, backend_status))

    async def reopen_by_ref(self, ref: str, *, to_status: str) -> bool:
        self.reopen_by_ref_calls.append((ref, to_status))
        return self.reopen_returns

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        self.find_by_escalation_calls.append(mapping_id)
        return None

    async def update_ticket(
        self,
        ref: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        priority_id: Optional[str] = None,
    ) -> bool:
        self.update_ticket_calls.append((ref, summary, description, priority_id))
        return self.update_ticket_result

    async def find_open_by_grid(self, grid_name: str, limit: int = 20) -> List[TicketSummary]:
        self.find_open_by_grid_calls.append((grid_name, limit))
        return self.find_open_by_grid_result


class _FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeQuery:
    """Fluent fake matching supabase-py's table().select()/update().eq()... chain."""

    def __init__(self, table: "_FakeTable", op: str, payload: Optional[Dict] = None) -> None:
        self._table = table
        self._op = op
        self._payload = payload
        self._filters: Dict[str, Any] = {}

    def select(self, *_args, **_kwargs) -> "_FakeQuery":
        return self

    def eq(self, col: str, value: Any) -> "_FakeQuery":
        self._filters[col] = value
        return self

    def limit(self, *_args, **_kwargs) -> "_FakeQuery":
        return self

    def execute(self) -> _FakeResponse:
        self._table.executed.append((self._op, dict(self._filters), self._payload))
        if self._table.raise_on_execute is not None:
            raise self._table.raise_on_execute
        if self._op == "select":
            match = self._table.rows_matching(self._filters)
            return _FakeResponse(match)
        if self._op == "update":
            return _FakeResponse([{"id": self._filters.get("id")}])
        return _FakeResponse([])


class _FakeTable:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.rows = rows or []
        self.executed: List[tuple] = []
        self.raise_on_execute: Optional[Exception] = None

    def rows_matching(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [r for r in self.rows if all(r.get(k) == v for k, v in filters.items())]

    def select(self, *args, **kwargs) -> _FakeQuery:
        return _FakeQuery(self, "select").select(*args, **kwargs)

    def update(self, payload: Dict[str, Any]) -> _FakeQuery:
        return _FakeQuery(self, "update", payload)


class _FakeRawClient:
    """Stands in for the raw postgrest client (`._get_client()`'s return value)."""

    def __init__(self) -> None:
        self.tables: Dict[str, _FakeTable] = {
            "internal_tickets": _FakeTable(),
        }

    def table(self, name: str) -> _FakeTable:
        return self.tables[name]


def _make_service(
    raw_client: Optional[_FakeRawClient],
    jira: Optional[_FakeBackend] = None,
    internal: Optional[_FakeBackend] = None,
    ticket_repository=None,
) -> TicketService:
    jira = jira or _FakeBackend("jira")
    internal = internal or _FakeBackend("internal")
    service = TicketService(
        get_supabase_client=(lambda: _RawClientWrapper(raw_client)) if raw_client else None,
        jira_backend=jira,
        internal_backend=internal,
        ticket_repository=ticket_repository or _FakeTicketRepository(),
    )
    return service


class _RawClientWrapper:
    """Mimics EnhancedSupabaseClient's `_get_client()` accessor."""

    def __init__(self, raw: _FakeRawClient) -> None:
        self._raw = raw

    def _get_client(self) -> _FakeRawClient:
        return self._raw


class TestCreateTicket:
    def test_default_internal_backend_shares_the_service_canonical_repository(self):
        repository = _FakeTicketRepository()

        service = TicketService(ticket_repository=repository)

        assert service._internal._tickets is repository

    @pytest.mark.asyncio
    async def test_persists_and_activates_one_canonical_intent(self):
        repository = _FakeTicketRepository()
        jira = _FakeBackend("jira", ref="OPS-42")
        service = _make_service(raw_client=None, jira=jira, ticket_repository=repository)

        result = await service.create_ticket(TicketCreateRequest(summary="x", source="notify"))

        assert result.ticket_id == "ticket-1"
        assert repository.calls == [
            ("intent", "notification", "x"),
            ("backend", "ticket-1", "jira"),
            ("activate", "ticket-1", "OPS-42", "jira"),
        ]

    @pytest.mark.asyncio
    async def test_escalation_mapping_id_does_not_touch_escalation_mappings(self):
        """create_ticket() links an escalation to its ticket purely through the
        canonical repository (EscalationService.track_as_ticket calls
        escalations.attach_ticket() itself) -- it must never reach for the
        legacy escalation_mappings table, regardless of escalation_mapping_id."""
        raw = _FakeRawClient()
        jira = _FakeBackend("jira", ref="OPS-42")
        service = _make_service(raw, jira=jira)

        req = TicketCreateRequest(summary="x", escalation_mapping_id="mapping-1")
        result = await service.create_ticket(req)

        assert result.ref == "OPS-42"
        assert "escalation_mappings" not in raw.tables


class TestNotifyTicketFallback:
    @pytest.mark.asyncio
    async def test_jira_fallback_activates_the_original_canonical_intent(self):
        repository = _FakeTicketRepository()
        jira = _FakeBackend("jira")
        jira.create_error = TicketBackendError("Jira unavailable")
        internal = _FakeBackend("internal", ref="TKT-000101")
        service = _make_service(
            raw_client=None, jira=jira, internal=internal, ticket_repository=repository
        )

        outcome = await service.create_ticket_with_internal_fallback(
            TicketCreateRequest(summary="Grid down", source="notify"), backend_override="jira"
        )

        assert outcome.result is not None
        assert outcome.result.ticket_id == "ticket-1"
        assert repository.calls == [
            ("intent", "notification", "Grid down"),
            ("backend", "ticket-1", "jira"),
            ("backend", "ticket-1", "internal"),
            ("activate", "ticket-1", "TKT-000101", "internal"),
        ]

    @pytest.mark.asyncio
    async def test_jira_failure_creates_internal_ticket_once(self):
        jira = _FakeBackend("jira", ref="OPS-42")
        jira.create_error = TicketBackendError("Jira unavailable")
        internal = _FakeBackend("internal", ref="TKT-000101")
        service = _make_service(raw_client=None, jira=jira, internal=internal)

        outcome = await service.create_ticket_with_internal_fallback(
            TicketCreateRequest(summary="! Urgent: Grid down"), backend_override="jira"
        )

        assert outcome.result == TicketResult(
            ref="TKT-000101", backend="internal", url=None, ticket_id="ticket-1"
        )
        assert outcome.fallback_used is True
        assert outcome.error == "Jira unavailable"
        assert len(jira.create_calls) == 1
        assert len(internal.create_calls) == 1

    @pytest.mark.asyncio
    async def test_jira_fallback_success_still_surfaces_the_jira_error(self):
        """Even when the internal fallback succeeds, the Jira failure reason
        must not be discarded -- it's the only thing that explains why a
        notify ticket didn't land in Jira despite NOTIFY_TICKETS_BACKEND=auto."""
        jira = _FakeBackend("jira", ref="OPS-42")
        jira.create_error = TicketBackendError("field X is required")
        internal = _FakeBackend("internal", ref="TKT-000101")
        service = _make_service(raw_client=None, jira=jira, internal=internal)

        outcome = await service.create_ticket_with_internal_fallback(
            TicketCreateRequest(summary="! Urgent: Grid down"), backend_override="jira"
        )

        assert outcome.result is not None
        assert outcome.fallback_used is True
        assert outcome.error is not None
        assert "field X is required" in outcome.error

    @pytest.mark.asyncio
    async def test_double_failure_returns_error_without_raising(self):
        jira = _FakeBackend("jira")
        jira.create_error = TicketBackendError("Jira unavailable")
        internal = _FakeBackend("internal")
        internal.create_error = TicketBackendError("internal unavailable")
        service = _make_service(raw_client=None, jira=jira, internal=internal)

        outcome = await service.create_ticket_with_internal_fallback(
            TicketCreateRequest(summary="! Urgent: Grid down"), backend_override="jira"
        )

        assert outcome.result is None
        assert outcome.fallback_used is True
        assert outcome.error == "Jira: Jira unavailable; internal: internal unavailable"

    @pytest.mark.asyncio
    async def test_primary_internal_failure_is_not_retried(self):
        internal = _FakeBackend("internal")
        internal.create_error = TicketBackendError("internal unavailable")
        service = _make_service(raw_client=None, internal=internal)

        outcome = await service.create_ticket_with_internal_fallback(
            TicketCreateRequest(summary="! Urgent: Grid down"), backend_override="internal"
        )

        assert outcome.result is None
        assert outcome.fallback_used is False
        assert outcome.error == "internal unavailable"
        assert len(internal.create_calls) == 1


class TestBackendForRef:
    @pytest.mark.asyncio
    async def test_canonical_internal_ticket_routes_internal(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_ref["TKT-000001"] = TicketRecord(
            id="ticket-1", ticket_ref="TKT-000001", backend="internal",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, internal=internal, ticket_repository=repository)

        backend = await service._backend_for_ref("TKT-000001")

        assert backend is internal

    @pytest.mark.asyncio
    async def test_canonical_jira_ticket_routes_jira(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, internal=internal, ticket_repository=repository)

        backend = await service._backend_for_ref("OPS-99")

        assert backend is jira

    @pytest.mark.asyncio
    async def test_untracked_ticket_does_not_infer_a_backend(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        service = _make_service(None, jira=jira, internal=internal)

        with pytest.raises(TicketBackendError, match="no canonical ticket"):
            await service._backend_for_ref("OPS-99")


class TestFindByEscalation:
    @pytest.mark.asyncio
    async def test_resolves_only_through_the_canonical_escalation_relation(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.refs_by_escalation["mapping-3"] = "TKT-3"
        service = _make_service(
            raw_client=None, jira=jira, internal=internal, ticket_repository=repository
        )

        result = await service.find_by_escalation("mapping-3")

        assert result == "TKT-3"
        assert repository.find_ref_for_escalation_calls == ["mapping-3"]
        assert jira.find_by_escalation_calls == []
        assert internal.find_by_escalation_calls == []


class TestGetRefById:
    @pytest.mark.asyncio
    async def test_resolves_ticket_ref_from_canonical_id(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_id["ticket-9"] = TicketRecord(
            id="ticket-9", ticket_ref="TKT-9", backend="internal",
            summary="x", created_via="escalation", provisioning_state="active",
        )
        service = _make_service(
            raw_client=None, jira=jira, internal=internal, ticket_repository=repository
        )

        assert await service.get_ref_by_id("ticket-9") == "TKT-9"

    @pytest.mark.asyncio
    async def test_returns_none_when_ticket_id_unknown(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        service = _make_service(
            raw_client=None, jira=jira, internal=internal, ticket_repository=repository
        )

        assert await service.get_ref_by_id("missing") is None


class TestTransitionToDone:
    """jira_backend.transition_to_done() only calls the Jira transitions API --
    it has no repository reference, so nothing marks the canonical ``tickets``
    row done for Jira-backed tickets. TicketService must close that gap
    itself; the internal backend already persists via the shared repository
    (see internal_backend.transition_to_done), so it must not be double-written."""

    @pytest.mark.asyncio
    async def test_persists_canonical_done_status_when_backend_is_jira(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, internal=internal, ticket_repository=repository)

        await service.transition_to_done("OPS-99")

        assert jira.transition_to_done_calls == ["OPS-99"]
        assert repository.transition_to_done_by_ref_calls == ["OPS-99"]

    @pytest.mark.asyncio
    async def test_does_not_double_write_when_backend_is_internal(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_ref["TKT-1"] = TicketRecord(
            id="ticket-1", ticket_ref="TKT-1", backend="internal",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, internal=internal, ticket_repository=repository)

        await service.transition_to_done("TKT-1")

        assert internal.transition_to_done_calls == ["TKT-1"]
        # Internal backend already persists via the shared repository itself --
        # TicketService must not also call it, or resolved_at would be bumped twice.
        assert repository.transition_to_done_by_ref_calls == []

    @pytest.mark.asyncio
    async def test_announces_an_internal_closure(self, monkeypatch):
        """Internal tickets take their flip signal from the backend, which is
        the only thing that wrote the canonical row."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_ref["TKT-1"] = TicketRecord(
            id="ticket-1", ticket_ref="TKT-1", backend="internal",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, internal=internal, ticket_repository=repository)
        service._update_notifier = _Notifier()

        await service.transition_to_done("TKT-1")

        assert len(events) == 1
        assert events[0].ticket_ref == "TKT-1"
        assert events[0].kind == "transition"
        assert events[0].to_status == "done"
        # The invariant from this class's existing tests still holds.
        assert repository.transition_to_done_by_ref_calls == []

    @pytest.mark.asyncio
    async def test_announces_a_jira_closure(self, monkeypatch):
        """Jira tickets take their flip signal from the canonical write, since
        the Jira backend only talks to the Jira API."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        jira = _FakeBackend("jira")
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, ticket_repository=repository)
        service._update_notifier = _Notifier()

        await service.transition_to_done("OPS-99")

        assert [e.ticket_ref for e in events] == ["OPS-99"]

    @pytest.mark.asyncio
    async def test_stays_silent_on_a_redundant_jira_close(self, monkeypatch):
        """A retried Jira webhook must not re-announce an already-closed ticket
        -- and must still report success, since the ticket genuinely is closed."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        jira = _FakeBackend("jira")
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira", status="done",
            summary="x", created_via="notification", provisioning_state="active",
        )
        repository.transition_returns = False  # row was already done
        service = _make_service(None, jira=jira, ticket_repository=repository)
        service._update_notifier = _Notifier()

        closed = await service.transition_to_done("OPS-99")

        assert closed is True
        assert events == []

    @pytest.mark.asyncio
    async def test_returns_false_when_an_actively_initiated_jira_close_genuinely_fails(
        self, monkeypatch
    ):
        """A chat-initiated (or escalation-button, or /notify) close where Jira
        itself refuses the transition must be reported as a failure, and must
        NOT flip the canonical row to "done" out from under a ticket Jira
        still considers open -- that would announce a closure that never
        actually happened."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        jira = _FakeBackend("jira")
        jira.transition_returns = False  # Jira itself refused/failed the transition
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira", status="open",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, ticket_repository=repository)
        service._update_notifier = _Notifier()

        closed = await service.transition_to_done("OPS-99")

        assert closed is False
        assert events == []
        # The canonical write must never even be attempted -- that's the bug
        # this test guards against.
        assert repository.transition_to_done_by_ref_calls == []

    @pytest.mark.asyncio
    async def test_already_confirmed_externally_skips_the_live_jira_call(self, monkeypatch):
        """The Jira webhook handler already knows Jira applied the closure --
        it should sync the canonical row directly rather than waste a live API
        call trying to re-drive a transition that already happened."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        jira = _FakeBackend("jira")
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira", status="open",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, ticket_repository=repository)
        service._update_notifier = _Notifier()

        closed = await service.transition_to_done("OPS-99", already_confirmed_externally=True)

        assert closed is True
        assert len(events) == 1
        assert jira.transition_to_done_calls == []
        assert repository.transition_to_done_by_ref_calls == ["OPS-99"]

    @pytest.mark.asyncio
    async def test_stays_silent_on_a_redundant_internal_close(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        internal = _FakeBackend("internal")
        internal.transition_returns = False  # row was already done
        repository = _FakeTicketRepository()
        repository.records_by_ref["TKT-1"] = TicketRecord(
            id="ticket-1", ticket_ref="TKT-1", backend="internal", status="done",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, internal=internal, ticket_repository=repository)
        service._update_notifier = _Notifier()

        closed = await service.transition_to_done("TKT-1")

        assert closed is True
        assert events == []


class TestMarkInProgressFromWebhook:
    """Unlike transition_to_done, there is no actively-initiated counterpart
    here -- the Jira webhook is the only caller, so the canonical write's own
    guarded result is the sole flip signal, with no live-transition branch to
    reconcile against."""

    @pytest.mark.asyncio
    async def test_syncs_canonical_status_and_notifies(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-1"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-1", backend="jira", status="open",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, ticket_repository=repository)
        service._update_notifier = _Notifier()

        flipped = await service.mark_in_progress_from_webhook(
            "OPS-1", fallback_chat_id="-100999", fallback_topic_id="42"
        )

        assert flipped is True
        assert repository.set_in_progress_by_ref_calls == ["OPS-1"]
        assert len(events) == 1
        assert events[0].ticket_ref == "OPS-1"
        assert events[0].kind == "transition"
        assert events[0].to_status == "in_progress"
        assert events[0].fallback_chat_id == "-100999"
        assert events[0].fallback_topic_id == "42"

    @pytest.mark.asyncio
    async def test_stays_silent_on_a_redundant_transition(self, monkeypatch):
        """A retried or duplicate webhook delivery must not re-announce a
        transition that already happened."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        repository = _FakeTicketRepository()
        repository.in_progress_returns = False  # already in progress
        service = _make_service(None, ticket_repository=repository)
        service._update_notifier = _Notifier()

        flipped = await service.mark_in_progress_from_webhook("OPS-1")

        assert flipped is False
        assert events == []

    @pytest.mark.asyncio
    async def test_a_persistence_failure_is_non_fatal_and_does_not_notify(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        events: list = []

        class _Notifier:
            async def notify(self, event):
                events.append(event)
                return True

        class _RaisingRepository(_FakeTicketRepository):
            async def set_in_progress_by_ref(self, ref: str) -> bool:
                raise RuntimeError("db down")

        service = _make_service(None, ticket_repository=_RaisingRepository())
        service._update_notifier = _Notifier()

        flipped = await service.mark_in_progress_from_webhook("OPS-1")

        assert flipped is False
        assert events == []


class TestSyncJiraTicketStatuses:
    """Sweep entry point: reconciles canonical status for Jira tickets that
    aren't tied to any escalation mapping (e.g. filed via /notify), which the
    escalation sweep's own reconciliation loop never sees."""

    @pytest.mark.asyncio
    async def test_closes_canonical_tickets_whose_jira_status_is_done(self):
        jira = _FakeBackend("jira")
        jira.status_by_ref = {
            "OPS-1": TicketStatus(summary="Grid down", is_done=True),
            "OPS-2": TicketStatus(summary="Meter fault", is_done=False),
        }
        repository = _FakeTicketRepository()
        repository.open_refs_by_backend["jira"] = ["OPS-1", "OPS-2"]
        service = _make_service(None, jira=jira, ticket_repository=repository)

        result = await service.sync_jira_ticket_statuses()

        assert set(jira.get_status_calls) == {"OPS-1", "OPS-2"}
        assert repository.transition_to_done_by_ref_calls == ["OPS-1"]
        assert result == {"checked": 2, "closed": 1, "reopened": 0}

    @pytest.mark.asyncio
    async def test_no_open_tickets_is_a_no_op(self):
        jira = _FakeBackend("jira")
        repository = _FakeTicketRepository()
        service = _make_service(None, jira=jira, ticket_repository=repository)

        result = await service.sync_jira_ticket_statuses()

        assert result == {"checked": 0, "closed": 0, "reopened": 0}
        assert repository.transition_to_done_by_ref_calls == []

    @pytest.mark.asyncio
    async def test_get_status_failure_for_one_ref_does_not_abort_the_rest(self):
        jira = _FakeBackend("jira")
        jira.status_by_ref = {"OPS-2": TicketStatus(summary="Meter fault", is_done=True)}
        repository = _FakeTicketRepository()
        repository.open_refs_by_backend["jira"] = ["OPS-1", "OPS-2"]
        service = _make_service(None, jira=jira, ticket_repository=repository)

        original_get_status = jira.get_status

        async def flaky_get_status(ref: str):
            if ref == "OPS-1":
                raise RuntimeError("Jira API down")
            return await original_get_status(ref)

        jira.get_status = flaky_get_status

        result = await service.sync_jira_ticket_statuses()

        assert repository.transition_to_done_by_ref_calls == ["OPS-2"]
        assert result == {"checked": 2, "closed": 1, "reopened": 0}

    @pytest.mark.asyncio
    async def test_sets_in_progress_for_an_indeterminate_category_status(self):
        """Jira's own vocabulary for its in-progress-shaped statuses ("In
        Review", "Blocked", etc.) is the "indeterminate" category, not a
        specific name -- this is what the sweep was silently discarding
        before, leaving Jira-side "In Progress" tickets reading "open" here
        indefinitely."""
        jira = _FakeBackend("jira")
        jira.status_by_ref = {
            "OPS-1": TicketStatus(
                summary="Grid down", is_done=False, raw_status="In Progress",
                status_category="indeterminate",
            ),
        }
        repository = _FakeTicketRepository()
        repository.open_refs_by_backend["jira"] = ["OPS-1"]
        service = _make_service(None, jira=jira, ticket_repository=repository)

        result = await service.sync_jira_ticket_statuses()

        assert repository.set_in_progress_by_ref_calls == ["OPS-1"]
        assert repository.transition_to_done_by_ref_calls == []
        assert result == {"checked": 1, "closed": 0, "reopened": 0}

    @pytest.mark.asyncio
    async def test_does_not_mark_in_progress_a_status_still_in_the_new_category(self):
        """A Jira ticket that's still just "Open"/"To Do" (category "new")
        must not flip the canonical row to in_progress."""
        jira = _FakeBackend("jira")
        jira.status_by_ref = {
            "OPS-1": TicketStatus(
                summary="Grid down", is_done=False, raw_status="Open",
                status_category="new",
            ),
        }
        repository = _FakeTicketRepository()
        repository.open_refs_by_backend["jira"] = ["OPS-1"]
        service = _make_service(None, jira=jira, ticket_repository=repository)

        await service.sync_jira_ticket_statuses()

        assert repository.set_in_progress_by_ref_calls == []

    @pytest.mark.asyncio
    async def test_records_live_backend_status_for_every_checked_ticket(self):
        jira = _FakeBackend("jira")
        jira.status_by_ref = {
            "OPS-1": TicketStatus(
                summary="Grid down", is_done=False, raw_status="In Review",
                status_category="indeterminate",
            ),
        }
        repository = _FakeTicketRepository()
        repository.open_refs_by_backend["jira"] = ["OPS-1"]
        service = _make_service(None, jira=jira, ticket_repository=repository)

        await service.sync_jira_ticket_statuses()

        assert repository.sync_backend_status_calls == [("OPS-1", "In Review")]

    @pytest.mark.asyncio
    async def test_reopens_a_recently_done_ticket_jira_now_reports_as_reopened(self):
        jira = _FakeBackend("jira")
        jira.status_by_ref = {
            "OPS-9": TicketStatus(
                summary="Grid down", is_done=False, raw_status="Reopened",
                status_category="new",
            ),
        }
        repository = _FakeTicketRepository()
        repository.recently_done_refs_by_backend["jira"] = ["OPS-9"]
        service = _make_service(None, jira=jira, ticket_repository=repository)

        result = await service.sync_jira_ticket_statuses()

        assert repository.reopen_by_ref_calls == [("OPS-9", "open")]
        assert result == {"checked": 0, "closed": 0, "reopened": 1}

    @pytest.mark.asyncio
    async def test_reopens_a_recently_done_ticket_to_in_progress_when_jira_says_so(self):
        jira = _FakeBackend("jira")
        jira.status_by_ref = {
            "OPS-9": TicketStatus(
                summary="Grid down", is_done=False, raw_status="In Progress",
                status_category="indeterminate",
            ),
        }
        repository = _FakeTicketRepository()
        repository.recently_done_refs_by_backend["jira"] = ["OPS-9"]
        service = _make_service(None, jira=jira, ticket_repository=repository)

        await service.sync_jira_ticket_statuses()

        assert repository.reopen_by_ref_calls == [("OPS-9", "in_progress")]

    @pytest.mark.asyncio
    async def test_does_not_reopen_a_recently_done_ticket_still_done_in_jira(self):
        jira = _FakeBackend("jira")
        jira.status_by_ref = {
            "OPS-9": TicketStatus(summary="Grid down", is_done=True, status_category="done"),
        }
        repository = _FakeTicketRepository()
        repository.recently_done_refs_by_backend["jira"] = ["OPS-9"]
        service = _make_service(None, jira=jira, ticket_repository=repository)

        result = await service.sync_jira_ticket_statuses()

        assert repository.reopen_by_ref_calls == []
        assert result == {"checked": 0, "closed": 0, "reopened": 0}


class TestUpdateTicket:
    """update_ticket() routes by the ref's persisted backend, same as
    add_comment/get_status/transition_to_done (see TestBackendForRef)."""

    @pytest.mark.asyncio
    async def test_routes_to_internal_when_canonical_ticket_is_internal(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_ref["TKT-000001"] = TicketRecord(
            id="ticket-1", ticket_ref="TKT-000001", backend="internal",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, internal=internal, ticket_repository=repository)

        ok = await service.update_ticket("TKT-000001", summary="s", description="d")

        assert ok is True
        assert internal.update_ticket_calls == [("TKT-000001", "s", "d", None)]
        assert jira.update_ticket_calls == []

    @pytest.mark.asyncio
    async def test_routes_to_jira_when_canonical_ticket_is_jira(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, internal=internal, ticket_repository=repository)

        ok = await service.update_ticket("OPS-99", priority_id="10001")

        assert ok is True
        assert jira.update_ticket_calls == [("OPS-99", None, None, "10001")]
        assert internal.update_ticket_calls == []

    @pytest.mark.asyncio
    async def test_propagates_backend_failure(self):
        jira = _FakeBackend("jira")
        jira.update_ticket_result = False
        internal = _FakeBackend("internal")
        repository = _FakeTicketRepository()
        repository.records_by_ref["OPS-99"] = TicketRecord(
            id="ticket-1", ticket_ref="OPS-99", backend="jira",
            summary="x", created_via="notification", provisioning_state="active",
        )
        service = _make_service(None, jira=jira, internal=internal, ticket_repository=repository)

        ok = await service.update_ticket("OPS-99", summary="s")

        assert ok is False


class TestFindOpenByGrid:
    """find_open_by_grid() delegates to resolve_backend(), not _backend_for_ref
    -- there's no single ref to route by; it's a fresh search on whichever
    backend is currently active (see resolve_backend's own coverage in
    test_service_resolve_backend.py for the override matrix)."""

    @pytest.mark.asyncio
    async def test_delegates_to_resolved_backend(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "internal")
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        expected = [TicketSummary(ref="TKT-000001", backend="internal", summary="s")]
        internal.find_open_by_grid_result = expected
        service = _make_service(raw_client=None, jira=jira, internal=internal)

        results = await service.find_open_by_grid("Kudi", limit=5)

        assert results == expected
        assert internal.find_open_by_grid_calls == [("Kudi", 5)]
        assert jira.find_open_by_grid_calls == []

    @pytest.mark.asyncio
    async def test_backend_override_param_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "internal")
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        service = _make_service(raw_client=None, jira=jira, internal=internal)

        await service.find_open_by_grid("Kudi", backend_override="jira")

        assert jira.find_open_by_grid_calls == [("Kudi", 20)]
        assert internal.find_open_by_grid_calls == []


class TestDefaultJiraBackendGetsStorageGetter:
    def test_default_constructed_jira_backend_can_download_attachments(self) -> None:
        """Regression guard for the exact bug caught in this task's self-review:
        TicketService's default `self._jira = jira_backend or JiraTicketBackend()`
        must pass get_storage_client=self._raw_client, or add_attachments()
        silently no-ops in production (only the DI'd tests in Task 8 supply one)."""
        service = TicketService(supabase_client=MagicMock())
        assert service._jira._get_storage_client is not None
        assert service._jira._get_storage_client == service._raw_client


class TestCreateTicketAttachments:
    @pytest.mark.asyncio
    async def test_links_and_syncs_attachments_when_present_for_the_escalation(self) -> None:
        attachment = EscalationAttachment(
            id="att-1",
            escalation_id="mapping-1",
            storage_path="mapping-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        internal_backend = MagicMock()
        internal_backend.name = "internal"
        internal_backend.create_ticket = AsyncMock(
            return_value=BackendTicketResult(ref="INT-1", backend="internal")
        )
        internal_backend.add_attachments = AsyncMock(
            return_value=[AttachmentSyncResult(attachment_id="att-1", external_id="ext-1")]
        )

        service = TicketService(supabase_client=MagicMock(), internal_backend=internal_backend)
        service._tickets = MagicMock()
        service._tickets.create_intent = AsyncMock(
            return_value=TicketRecord(
                id="ticket-1",
                created_via="escalation",
                provisioning_state="pending",
                summary="s",
            )
        )
        service._tickets.set_pending_backend = AsyncMock()
        service._tickets.activate = AsyncMock()
        service._attachments = MagicMock()
        service._attachments.list_by_escalation = AsyncMock(return_value=[attachment])
        service._attachments.link_ticket = AsyncMock()
        service._attachments.mark_synced = AsyncMock()

        await service.create_ticket(
            TicketCreateRequest(
                summary="s", escalation_mapping_id="mapping-1", source="escalation"
            ),
            backend_override="internal",
        )

        service._attachments.list_by_escalation.assert_awaited_once_with("mapping-1")
        service._attachments.link_ticket.assert_awaited_once_with("mapping-1", "ticket-1")
        internal_backend.add_attachments.assert_awaited_once_with("INT-1", [attachment])
        service._attachments.mark_synced.assert_awaited_once_with("att-1", "ext-1")

    @pytest.mark.asyncio
    async def test_skips_attachment_work_when_none_exist_for_the_escalation(self) -> None:
        internal_backend = MagicMock()
        internal_backend.name = "internal"
        internal_backend.create_ticket = AsyncMock(
            return_value=BackendTicketResult(ref="INT-1", backend="internal")
        )
        internal_backend.add_attachments = AsyncMock(return_value=[])

        service = TicketService(supabase_client=MagicMock(), internal_backend=internal_backend)
        service._tickets = MagicMock()
        service._tickets.create_intent = AsyncMock(
            return_value=TicketRecord(
                id="ticket-1",
                created_via="escalation",
                provisioning_state="pending",
                summary="s",
            )
        )
        service._tickets.set_pending_backend = AsyncMock()
        service._tickets.activate = AsyncMock()
        service._attachments = MagicMock()
        service._attachments.list_by_escalation = AsyncMock(return_value=[])
        service._attachments.link_ticket = AsyncMock()

        await service.create_ticket(
            TicketCreateRequest(
                summary="s", escalation_mapping_id="mapping-1", source="escalation"
            ),
            backend_override="internal",
        )

        service._attachments.link_ticket.assert_not_awaited()
        internal_backend.add_attachments.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_attachment_lookup_when_no_escalation_mapping_id(self) -> None:
        internal_backend = MagicMock()
        internal_backend.name = "internal"
        internal_backend.create_ticket = AsyncMock(
            return_value=BackendTicketResult(ref="INT-1", backend="internal")
        )

        service = TicketService(supabase_client=MagicMock(), internal_backend=internal_backend)
        service._tickets = MagicMock()
        service._tickets.create_intent = AsyncMock(
            return_value=TicketRecord(
                id="ticket-1",
                created_via="notification",
                provisioning_state="pending",
                summary="s",
            )
        )
        service._tickets.set_pending_backend = AsyncMock()
        service._tickets.activate = AsyncMock()
        service._attachments = MagicMock()
        service._attachments.list_by_escalation = AsyncMock()

        await service.create_ticket(
            TicketCreateRequest(summary="s", source="notify"), backend_override="internal"
        )

        service._attachments.list_by_escalation.assert_not_awaited()
