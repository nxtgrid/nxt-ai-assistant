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

from orchestrator.services.ticketing.backend import TicketCreateRequest, TicketResult
from orchestrator.services.ticketing.service import TicketService


class _FakeBackend:
    """Minimal stand-in for either TicketBackend implementation."""

    def __init__(self, name: str, ref: str = "REF-1") -> None:
        self.name = name
        self._ref = ref
        self.find_by_escalation_calls: List[str] = []

    async def is_available(self) -> bool:
        return True

    async def create_ticket(self, req: TicketCreateRequest) -> TicketResult:
        return TicketResult(ref=self._ref, backend=self.name, url=None)

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        return True

    async def get_status(self, ref: str):
        return None

    async def transition_to_done(self, ref: str) -> None:
        return None

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        self.find_by_escalation_calls.append(mapping_id)
        return None


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
) -> TicketService:
    jira = jira or _FakeBackend("jira")
    internal = internal or _FakeBackend("internal")
    service = TicketService(
        get_supabase_client=(lambda: _RawClientWrapper(raw_client)) if raw_client else None,
        jira_backend=jira,
        internal_backend=internal,
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


class TestBackendForRef:
    @pytest.mark.asyncio
    async def test_ref_found_in_internal_tickets_routes_internal(self):
        raw = _FakeRawClient()
        raw.tables["internal_tickets"].rows = [{"ticket_ref": "TKT-000001"}]
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        service = _make_service(raw, jira=jira, internal=internal)

        backend = await service._backend_for_ref("TKT-000001")

        assert backend is internal

    @pytest.mark.asyncio
    async def test_ref_not_found_routes_jira(self):
        raw = _FakeRawClient()  # internal_tickets empty
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        service = _make_service(raw, jira=jira, internal=internal)

        backend = await service._backend_for_ref("OPS-99")

        assert backend is jira

    @pytest.mark.asyncio
    async def test_lookup_failure_falls_back_to_jira(self):
        raw = _FakeRawClient()
        raw.tables["internal_tickets"].raise_on_execute = RuntimeError("network blip")
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        service = _make_service(raw, jira=jira, internal=internal)

        backend = await service._backend_for_ref("OPS-99")

        assert backend is jira

    @pytest.mark.asyncio
    async def test_no_raw_client_falls_back_to_jira(self):
        jira = _FakeBackend("jira")
        internal = _FakeBackend("internal")
        service = _make_service(raw_client=None, jira=jira, internal=internal)

        backend = await service._backend_for_ref("anything")

        assert backend is jira


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
