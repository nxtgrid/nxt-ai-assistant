"""TicketService orchestration logic beyond resolve_backend().

Covers: escalation_mappings stamping on create_ticket() (success and
swallowed-failure), _backend_for_ref()'s three branches (found in
internal_tickets / not found -> jira / lookup raises -> jira), and
find_by_escalation()'s jira-then-internal composition including the
TICKET_BACKEND_OVERRIDE=internal short-circuit.

resolve_backend() itself is covered by test_service_resolve_backend.py.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing.backend import (
    TicketBackendError,
    TicketCreateRequest,
    TicketResult,
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

    async def is_available(self) -> bool:
        return True

    def has_credentials(self) -> bool:
        return True

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

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        return True

    async def get_status(self, ref: str):
        return None

    async def transition_to_done(self, ref: str) -> None:
        return None

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
            "escalation_mappings": _FakeTable(),
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


class TestCreateTicketStamping:
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
    async def test_stamps_ticket_ref_and_backend_on_success(self):
        raw = _FakeRawClient()
        jira = _FakeBackend("jira", ref="OPS-42")
        service = _make_service(raw, jira=jira)

        req = TicketCreateRequest(summary="x", escalation_mapping_id="mapping-1")
        result = await service.create_ticket(req)

        assert result.ref == "OPS-42"
        update_calls = [e for e in raw.tables["escalation_mappings"].executed if e[0] == "update"]
        assert len(update_calls) == 1
        _, filters, payload = update_calls[0]
        assert filters == {"id": "mapping-1"}
        assert payload == {"ticket_ref": "OPS-42", "ticket_backend": "jira"}

    @pytest.mark.asyncio
    async def test_does_not_stamp_when_no_escalation_mapping_id(self):
        raw = _FakeRawClient()
        service = _make_service(raw)

        req = TicketCreateRequest(summary="x")  # no escalation_mapping_id -> e.g. /notify tickets
        await service.create_ticket(req)

        assert raw.tables["escalation_mappings"].executed == []

    @pytest.mark.asyncio
    async def test_create_ticket_still_succeeds_when_stamp_fails(self):
        """A failed stamp UPDATE must not fail the overall create_ticket() call --
        the ticket already exists in the backend; the mapping row just won't
        know its ref until the next dedup lookup finds it independently."""
        raw = _FakeRawClient()
        raw.tables["escalation_mappings"].raise_on_execute = RuntimeError("db blip")
        jira = _FakeBackend("jira", ref="OPS-7")
        service = _make_service(raw, jira=jira)

        req = TicketCreateRequest(summary="x", escalation_mapping_id="mapping-2")
        result = await service.create_ticket(req)

        assert result.ref == "OPS-7"  # no exception propagated


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
        assert outcome.error is None
        assert len(jira.create_calls) == 1
        assert len(internal.create_calls) == 1

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
    async def test_checks_jira_first_then_internal(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "auto")
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        service = _make_service(raw_client=None, jira=jira, internal=internal)

        await service.find_by_escalation("mapping-3")

        assert jira.find_by_escalation_calls == ["mapping-3"]
        assert internal.find_by_escalation_calls == ["mapping-3"]

    @pytest.mark.asyncio
    async def test_returns_jira_ref_without_checking_internal_when_jira_finds_it(
        self, monkeypatch
    ):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "auto")

        class _JiraFinds(_FakeBackend):
            async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
                self.find_by_escalation_calls.append(mapping_id)
                return "OPS-55"

        jira = _JiraFinds("jira")
        internal = _FakeBackend("internal")
        service = _make_service(raw_client=None, jira=jira, internal=internal)

        result = await service.find_by_escalation("mapping-4")

        assert result == "OPS-55"
        assert internal.find_by_escalation_calls == []

    @pytest.mark.asyncio
    async def test_override_internal_skips_jira_entirely(self, monkeypatch):
        monkeypatch.setenv("TICKET_BACKEND_OVERRIDE", "internal")
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        service = _make_service(raw_client=None, jira=jira, internal=internal)

        await service.find_by_escalation("mapping-5")

        assert jira.find_by_escalation_calls == []
        assert internal.find_by_escalation_calls == ["mapping-5"]


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
