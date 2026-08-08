"""Regression tests for EscalationService's Task-4 rewiring onto TicketService.

Covers the checklist's two headline guarantees:
- mocked-healthy-Jira path reproduces today's DB writes + customer messages
  (jira_ticket_key still written alongside ticket_ref/ticket_backend, the
  return dict now carries "ticket_ref", customer wording unchanged), and
- down-Jira path files an internal ticket end-to-end (jira_ticket_key stays
  NULL, ticket_backend="internal", customer still gets a ref notification).

Plus: dedup-hit routing (jira vs internal), the after-hours auto-create
URL-vs-no-URL message rendering, the follow-up comment path routing through
TicketService, and the run_escalation_ticket_sweep rename + alias.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from orchestrator.services.escalation_service import EscalationService
from orchestrator.services.ticketing.backend import (
    TicketBackendError,
    TicketResult,
    TicketStatus,
)
from orchestrator.services.ticketing.service import TicketService

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeQuery:
    def __init__(self, table: "_FakeTable", op: str, payload: Optional[Dict] = None) -> None:
        self._t = table
        self._op = op
        self._payload = payload
        self._filters: Dict[str, Any] = {}
        self._range_filters: List[tuple] = []
        self._order: Optional[tuple] = None

    def select(self, *_a, **_k) -> "_FakeQuery":
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeQuery":
        self._op = "update"
        self._payload = payload
        return self

    def insert(self, payload: Dict[str, Any]) -> "_FakeQuery":
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload: Dict[str, Any], **_kwargs) -> "_FakeQuery":
        self._op = "upsert"
        self._payload = payload
        return self

    def eq(self, col: str, value: Any) -> "_FakeQuery":
        self._filters[col] = value
        return self

    def neq(self, col: str, value: Any) -> "_FakeQuery":
        self._filters[col] = f"neq:{value}"
        return self

    def in_(self, col: str, values: Any) -> "_FakeQuery":
        self._filters[col] = ("in", list(values))
        return self

    def is_(self, col: str, value: Any) -> "_FakeQuery":
        self._range_filters.append(("is_null" if value == "null" else "is", col, value))
        return self

    def gt(self, col: str, value: Any) -> "_FakeQuery":
        self._range_filters.append(("gt", col, value))
        return self

    def lt(self, col: str, value: Any) -> "_FakeQuery":
        self._range_filters.append(("lt", col, value))
        return self

    def gte(self, col: str, value: Any) -> "_FakeQuery":
        self._range_filters.append(("gte", col, value))
        return self

    @property
    def not_(self) -> "_FakeNot":
        return _FakeNot(self)

    def order(self, col: str, desc: bool = False) -> "_FakeQuery":
        self._order = (col, desc)
        return self

    def limit(self, *_a, **_k) -> "_FakeQuery":
        return self

    def execute(self) -> _FakeResponse:
        self._t.calls.append((self._op, dict(self._filters), self._payload))
        if self._op == "select":
            matches = self._t.rows_matching(self._filters)
            matches = self._t.rows_matching_range(matches, self._range_filters)
            if self._order is not None:
                col, desc = self._order
                matches = sorted(matches, key=lambda r: r.get(col), reverse=desc)
            return _FakeResponse(matches)
        if self._op in {"insert", "upsert"}:
            row = {"id": f"ticket-{len(self._t.rows) + 1}", **(self._payload or {})}
            self._t.rows.append(row)
            return _FakeResponse([row])
        if self._op == "update" and self._t.rows:
            updated = []
            for row in self._t.rows_matching(self._filters):
                row.update(self._payload or {})
                updated.append(row)
            return _FakeResponse(updated)
        return _FakeResponse([{"id": self._filters.get("id")}])


class _FakeNot:
    """Mirrors the supabase-py `query.not_.is_(...)` chaining shim."""

    def __init__(self, query: _FakeQuery) -> None:
        self._query = query

    def is_(self, col: str, value: Any) -> _FakeQuery:
        self._query._range_filters.append(
            ("not_null" if value == "null" else "not_is", col, value)
        )
        return self._query


class _FakeTable:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.rows = rows or []
        self.calls: List[tuple] = []

    def rows_matching(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        def match(row: Dict[str, Any], col: str, val: Any) -> bool:
            if isinstance(val, tuple) and len(val) == 2 and val[0] == "in":
                return row.get(col) in val[1]
            if isinstance(val, str) and val.startswith("neq:"):
                return str(row.get(col)) != val[len("neq:") :]
            return row.get(col) == val

        return [r for r in self.rows if all(match(r, k, v) for k, v in filters.items())]

    def rows_matching_range(
        self, rows: List[Dict[str, Any]], range_filters: List[tuple]
    ) -> List[Dict[str, Any]]:
        def keep(row: Dict[str, Any]) -> bool:
            for op, col, val in range_filters:
                v = row.get(col)
                if op == "is_null":
                    if v is not None:
                        return False
                elif op == "not_null":
                    if v is None:
                        return False
                elif op == "gt":
                    if v is None or not (v > val):
                        return False
                elif op == "lt":
                    if v is None or not (v < val):
                        return False
                elif op == "gte":
                    if v is None or not (v >= val):
                        return False
            return True

        return [r for r in rows if keep(r)]

    def select(self, *_a, **_k) -> _FakeQuery:
        return _FakeQuery(self, "select")

    def update(self, payload: Dict[str, Any]) -> _FakeQuery:
        return _FakeQuery(self, "update", payload)

    def insert(self, payload: Dict[str, Any]) -> _FakeQuery:
        return _FakeQuery(self, "insert", payload)

    def upsert(self, payload: Dict[str, Any], **_kwargs) -> _FakeQuery:
        return _FakeQuery(self, "upsert", payload)


class _FakeRaw:
    def __init__(self) -> None:
        self.tables: Dict[str, _FakeTable] = {
            "escalation_mappings": _FakeTable(),
            "internal_tickets": _FakeTable(),
            "tickets": _FakeTable(),
        }

    def table(self, name: str) -> _FakeTable:
        if name not in self.tables:
            self.tables[name] = _FakeTable()
        return self.tables[name]


class _FakeSupabase:
    """Stands in for SupabaseClient — exposes only what the tested paths touch."""

    def __init__(self, raw: _FakeRaw, internal_rows: Optional[Dict[str, Dict]] = None) -> None:
        self._raw = raw
        self._internal_rows = internal_rows or {}
        self.save_calls: List[Dict[str, Any]] = []
        self.tag_calls: List[tuple] = []
        self.saved_messages_return: List[Any] = [SimpleNamespace(id="msg-1")]
        self.mapping_for_reply: Optional[Dict[str, Any]] = None
        self.session_escalation_info: Optional[Dict[str, Any]] = None
        self.session_by_id_result: Optional[SimpleNamespace] = None
        self.escalation_by_session_result: Optional[Dict[str, Any]] = None
        self.reopen_escalation_result: bool = True
        self.reopen_escalation_calls: List[tuple] = []
        self.internal_ticket_lookup_calls: List[str] = []
        # Sweep fixtures — configure per-test, default to empty/no-op.
        self.stale_unfiled: List[Dict[str, Any]] = []
        self.old_unfiled: List[Dict[str, Any]] = []
        self.active_tracked: List[Dict[str, Any]] = []
        self.claim_returns: Dict[str, Optional[Dict[str, Any]]] = {}
        self.reactivate_calls: List[str] = []

    def _get_client(self) -> _FakeRaw:
        return self._raw

    def em_update_payloads(self) -> List[Dict[str, Any]]:
        return [p for op, _f, p in self._raw.tables["escalation_mappings"].calls if op == "update"]

    def em_update_filters(self) -> List[Dict[str, Any]]:
        return [f for op, f, _p in self._raw.tables["escalation_mappings"].calls if op == "update"]

    async def get_stale_unfiled_escalations(self, **_k):
        return self.stale_unfiled

    async def get_old_unfiled_escalations(self, **_k):
        return self.old_unfiled

    async def get_active_tracked_escalations(self, **_k):
        return self.active_tracked

    async def claim_escalation_for_tracking(self, mapping_id: str):
        return self.claim_returns.get(mapping_id)

    async def reactivate_escalation(self, mapping_id: str):
        self.reactivate_calls.append(mapping_id)
        return None

    async def get_session(self, _sid):
        return SimpleNamespace(id=uuid.uuid4())

    async def get_session_by_chat_id(self, **_k):
        return SimpleNamespace(id=uuid.uuid4())

    async def get_session_by_id(self, _session_uuid):
        return self.session_by_id_result

    async def get_messages(self, **_k):
        return []

    async def get_internal_ticket(self, ref: str):
        self.internal_ticket_lookup_calls.append(ref)
        return self._internal_rows.get(ref)

    async def count_active_blocking_escalations(self, _sid):
        return 0

    async def get_session_escalation_info(self, _session_id):
        return self.session_escalation_info

    async def update_session_escalation_status(self, **_k):
        return None

    async def reopen_escalation(self, session_id, escalation_message_id):
        self.reopen_escalation_calls.append((session_id, escalation_message_id))
        return self.reopen_escalation_result

    async def save_escalation_mapping(self, **kwargs):
        self.save_calls.append(kwargs)
        return "new-mapping-id"

    async def get_escalation_mapping(self, _msg_id):
        return self.mapping_for_reply

    async def get_escalation_by_session(self, _session_id):
        return self.escalation_by_session_result

    async def save_messages(self, **_k):
        return self.saved_messages_return

    async def tag_message_as_ticket_comment(self, message_id, ticket_ref, ticket_role="comment"):
        self.tag_calls.append((message_id, ticket_ref, ticket_role))
        return None


class _FakeBackend:
    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        ref: str = "REF-1",
        url: Optional[str] = None,
        dedup: Optional[str] = None,
    ) -> None:
        self.name = name
        self._available = available
        self._ref = ref
        self._url = url
        self._dedup = dedup
        self.create_calls = 0

    async def is_available(self) -> bool:
        return self._available

    async def create_ticket(self, req) -> TicketResult:
        self.create_calls += 1
        return TicketResult(ref=self._ref, backend=self.name, url=self._url)

    async def add_comment(self, ref, body, public: bool = False) -> bool:
        return True

    async def get_status(self, ref):
        return None

    async def transition_to_done(self, ref) -> None:
        return None

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        return self._dedup


class _FakeTickets:
    """Lightweight stand-in for TicketService for the follow-up/comment and
    sweep-reconciliation paths (neither needs find_by_escalation/create_ticket)."""

    def __init__(
        self,
        status: Optional[TicketStatus] = None,
        by_ref: Optional[Dict[str, Optional[TicketStatus]]] = None,
        sync_jira_ticket_statuses_result: Optional[Dict[str, int]] = None,
        ref_by_ticket_id: Optional[Dict[str, str]] = None,
        backend_by_ref: Optional[Dict[str, str]] = None,
    ) -> None:
        self._status = status
        self._by_ref = by_ref or {}
        self._ref_by_ticket_id = ref_by_ticket_id or {}
        self._backend_by_ref = backend_by_ref or {}
        self.get_status_calls: List[str] = []
        self.add_comment_calls: List[tuple] = []
        self.sync_jira_ticket_statuses_calls = 0
        self._sync_jira_ticket_statuses_result = sync_jira_ticket_statuses_result or {
            "checked": 0,
            "closed": 0,
        }

    async def get_status(self, ref: str):
        self.get_status_calls.append(ref)
        if ref in self._by_ref:
            return self._by_ref[ref]
        return self._status

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        self.add_comment_calls.append((ref, body, public))
        return True

    async def sync_jira_ticket_statuses(self) -> Dict[str, int]:
        self.sync_jira_ticket_statuses_calls += 1
        return self._sync_jira_ticket_statuses_result

    async def get_id_by_ref(self, _ref: str) -> Optional[str]:
        return None

    async def get_ref_by_id(self, ticket_id: str) -> Optional[str]:
        return self._ref_by_ticket_id.get(ticket_id)

    async def get_backend_name(self, ref: str) -> str:
        return self._backend_by_ref.get(ref, "jira")


class _CanonicalDedupTickets:
    """Ticket-service seam for an already-attached canonical escalation."""

    def __init__(self, ref: str, backend: str) -> None:
        self._ref = ref
        self._backend = backend
        self.find_calls: list[str] = []
        self.backend_calls: list[str] = []

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        self.find_calls.append(mapping_id)
        return self._ref

    async def get_backend_name(self, ref: str) -> str:
        self.backend_calls.append(ref)
        return self._backend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(fake_supabase: _FakeSupabase) -> EscalationService:
    svc = EscalationService(
        escalation_chat_id="-100123456",
        bot_token="TESTTOKEN",
        supabase_url="http://supabase.test",
        supabase_key="key",
    )
    svc._supabase_client = fake_supabase  # _get_supabase_client() now returns the fake
    return svc


def _install_ticket_service(
    svc: EscalationService, jira: _FakeBackend, internal: _FakeBackend
) -> None:
    svc._tickets = TicketService(
        get_supabase_client=svc._get_supabase_client,
        jira_backend=jira,
        internal_backend=internal,
    )


def _base_mapping() -> Dict[str, Any]:
    return {
        "session_id": "telegram_abc",
        "customer_chat_id": "12345",
        "customer_topic_id": None,  # None -> skips grid/auth resolution
        "id": "mapping-abcd1234",
        "org_hashtag": "#acme",
        "question_text": "my meter is broken",
        "escalation_message_id": 555,
    }


# ---------------------------------------------------------------------------
# track_as_ticket — Jira-healthy parity
# ---------------------------------------------------------------------------


async def test_track_as_ticket_jira_success_writes_jira_key_and_returns_ticket_ref():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, ref="OPS-100", url="https://jira.test/browse/OPS-100")
    internal = _FakeBackend("internal", ref="TKT-000001", url=None)
    _install_ticket_service(svc, jira, internal)

    sent: List[Dict[str, Any]] = []

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        sent.append({"chat_id": chat_id, "text": text})
        return {"ok": True, "result": {"message_id": 1}}

    svc._send_telegram_message = fake_send

    result = await svc.track_as_ticket(escalation_mapping=_base_mapping())

    # Return key is now backend-agnostic "ticket_ref" (not "jira_ticket_key"),
    # plus ticket_backend/ticket_url so callers (e.g. the sweep) can render a
    # link without a second backend lookup.
    assert result == {
        "success": True,
        "ticket_ref": "OPS-100",
        "ticket_backend": "jira",
        "ticket_url": "https://jira.test/browse/OPS-100",
    }
    assert "jira_ticket_key" not in result

    payloads = supa.em_update_payloads()
    # TicketService stamped ticket_ref/ticket_backend ...
    assert {"ticket_ref": "OPS-100", "ticket_backend": "jira"} in payloads
    # ... and _store_jira_key's own stamp ALSO carries the legacy jira_ticket_key
    # column in the same update (webhook back-compat).
    assert {
        "ticket_ref": "OPS-100",
        "ticket_backend": "jira",
        "jira_ticket_key": "OPS-100",
    } in payloads

    # Customer wording unchanged from today's ref-number based text.
    assert any(
        s["text"].startswith("Your issue is being tracked (ref: 100).") for s in sent
    ), sent


async def test_track_as_ticket_internal_success_leaves_jira_key_null():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)
    # Jira unavailable (down) -> resolve_backend routes to internal.
    jira = _FakeBackend("jira", available=False, ref="OPS-100")
    internal = _FakeBackend("internal", ref="TKT-000001", url=None)
    _install_ticket_service(svc, jira, internal)

    sent: List[Dict[str, Any]] = []

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        sent.append({"text": text})
        return {"ok": True, "result": {"message_id": 1}}

    svc._send_telegram_message = fake_send

    result = await svc.track_as_ticket(escalation_mapping=_base_mapping())

    assert result == {
        "success": True,
        "ticket_ref": "TKT-000001",
        "ticket_backend": "internal",
        "ticket_url": None,
    }
    assert internal.create_calls == 1

    payloads = supa.em_update_payloads()
    assert {"ticket_ref": "TKT-000001", "ticket_backend": "internal"} in payloads
    # jira_ticket_key must never be written for an internal ticket.
    assert all("jira_ticket_key" not in p for p in payloads), payloads

    # Customer still gets a ref-number notification (same wording, different ref).
    assert any(
        s["text"].startswith("Your issue is being tracked (ref: 000001).") for s in sent
    ), sent


async def test_track_as_ticket_attaches_the_canonical_ticket_to_the_escalation():
    raw = _FakeRaw()
    raw.table("escalations").rows = [{"id": "mapping-abcd1234", "state": "processing"}]
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, ref="OPS-100")
    internal = _FakeBackend("internal")
    _install_ticket_service(svc, jira, internal)

    await svc.track_as_ticket(escalation_mapping=_base_mapping())

    assert raw.tables["escalations"].rows == [
        {"id": "mapping-abcd1234", "state": "tracked", "ticket_id": "ticket-1"}
    ]


async def test_track_as_ticket_records_the_customer_notification_delivery():
    raw = _FakeRaw()
    raw.table("escalations").rows = [{"id": "mapping-abcd1234", "state": "processing"}]
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)
    _install_ticket_service(svc, _FakeBackend("jira", ref="OPS-100"), _FakeBackend("internal"))

    class _Deliveries:
        calls: list[dict[str, Any]] = []

        async def record(self, **kwargs):
            self.calls.append(kwargs)
            return kwargs

    deliveries = _Deliveries()
    svc._deliveries = deliveries

    async def fake_send(*_args, **_kwargs):
        return {"ok": True, "result": {"message_id": 77}}

    svc._send_telegram_message = fake_send
    await svc.track_as_ticket(escalation_mapping=_base_mapping())

    assert deliveries.calls == [
        {
            "ticket_id": "ticket-1", "escalation_id": "mapping-abcd1234",
            "purpose": "notification", "external_chat_id": "12345",
            "external_topic_id": None, "external_message_id": 77,
        }
    ]


async def test_track_as_ticket_creates_canonical_escalation_with_resolved_chat_session_uuid():
    """Regression: escalations.chat_session_id is a UUID FK to chat_sessions.id,
    not the text session_id ("telegram_abc") escalation_mappings.session_id
    stores. Passing the raw text (as this call site did before the fix) fails
    against real Postgres -- the fake table here doesn't enforce column types,
    which is exactly why that bug shipped silently. Deliberately does not
    pre-seed the escalations table, so the create() path actually runs."""
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    resolved_uuid = uuid.uuid4()

    async def fake_get_session(_sid):
        return SimpleNamespace(id=resolved_uuid)

    supa.get_session = fake_get_session
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, ref="OPS-100")
    internal = _FakeBackend("internal")
    _install_ticket_service(svc, jira, internal)

    await svc.track_as_ticket(escalation_mapping=_base_mapping())

    escalation_rows = raw.tables["escalations"].rows
    assert escalation_rows, "expected a canonical escalation row to be created"
    assert escalation_rows[0]["chat_session_id"] == str(resolved_uuid)


async def test_track_as_ticket_dedup_hit_jira_writes_jira_key():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)  # no internal_tickets rows -> recovered ref is Jira
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, dedup="OPS-55")
    internal = _FakeBackend("internal")
    _install_ticket_service(svc, jira, internal)
    canonical_tickets = _CanonicalDedupTickets("OPS-55", "jira")
    svc._tickets = canonical_tickets

    result = await svc.track_as_ticket(escalation_mapping=_base_mapping())

    assert result["success"] is True
    assert result["ticket_ref"] == "OPS-55"
    assert result["ticket_backend"] == "jira"
    assert result["ticket_url"] == f"{svc._jira_base_url}/browse/OPS-55"
    assert jira.create_calls == 0  # dedup skipped creation
    assert {"jira_ticket_key": "OPS-55"} in supa.em_update_payloads()
    assert canonical_tickets.find_calls == ["mapping-abcd1234"]
    assert canonical_tickets.backend_calls == ["OPS-55"]
    assert supa.internal_ticket_lookup_calls == []


async def test_track_as_ticket_dedup_hit_internal_skips_jira_key():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw, internal_rows={"TKT-9": {"ticket_ref": "TKT-9"}})
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, dedup=None)
    internal = _FakeBackend("internal", dedup="TKT-9")
    _install_ticket_service(svc, jira, internal)
    canonical_tickets = _CanonicalDedupTickets("TKT-9", "internal")
    svc._tickets = canonical_tickets

    result = await svc.track_as_ticket(escalation_mapping=_base_mapping())

    assert result == {
        "success": True,
        "ticket_ref": "TKT-9",
        "ticket_backend": "internal",
        "ticket_url": None,
    }
    # Recovered ref is internal -> the legacy jira_ticket_key must stay untouched.
    assert all("jira_ticket_key" not in p for p in supa.em_update_payloads())
    assert canonical_tickets.find_calls == ["mapping-abcd1234"]
    assert canonical_tickets.backend_calls == ["TKT-9"]
    assert supa.internal_ticket_lookup_calls == []


async def test_track_as_ticket_second_call_dedupes_via_canonical_escalation():
    """Regression: a second track_as_ticket() call for the same escalation
    (e.g. a sweep retry after the legacy ticket_ref stamp failed, or a race
    with the staff Track button) must reuse the already-filed ticket instead
    of filing a second one on whatever backend happens to be healthy the
    second time around.

    Deliberately does NOT pre-seed the `escalations` table (unlike
    test_track_as_ticket_attaches_the_canonical_ticket_to_the_escalation,
    which seeds state="processing" by hand) -- this exercises the real
    create()/claim()/attach_ticket() wiring end to end through the actual
    TicketService.find_by_escalation() chain, not the _CanonicalDedupTickets
    test double the dedup-hit tests above use.
    """
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)
    # First call: Jira down -> files an internal (TKT-*) ticket.
    jira = _FakeBackend("jira", available=False, ref="OPS-100")
    internal = _FakeBackend("internal", ref="TKT-000001", url=None)
    _install_ticket_service(svc, jira, internal)

    async def fake_send(*_a, **_k):
        return {"ok": True, "result": {"message_id": 1}}

    svc._send_telegram_message = fake_send

    mapping = _base_mapping()
    first = await svc.track_as_ticket(escalation_mapping=mapping)
    assert first["ticket_ref"] == "TKT-000001"
    assert first["ticket_backend"] == "internal"

    # Jira recovers before the retry -- this is what makes a broken dedup
    # guard file the second ticket on a *different* backend (TKT -> OPS).
    jira._available = True

    second = await svc.track_as_ticket(escalation_mapping=mapping)

    assert second["ticket_ref"] == "TKT-000001"
    assert second["ticket_backend"] == "internal"
    assert internal.create_calls == 1
    assert jira.create_calls == 0


async def test_track_as_ticket_creation_failure_returns_error():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)

    class _Boom(_FakeBackend):
        async def create_ticket(self, req):
            raise TicketBackendError("both backends down")

    jira = _Boom("jira", available=True)
    internal = _Boom("internal")
    _install_ticket_service(svc, jira, internal)

    result = await svc.track_as_ticket(escalation_mapping=_base_mapping())
    assert result["success"] is False
    assert "both backends down" in result["error"]


# ---------------------------------------------------------------------------
# _auto_create_jira_and_edit_message — URL vs no-URL rendering
# ---------------------------------------------------------------------------


async def _run_auto_create(svc: EscalationService):
    edits: List[Dict[str, Any]] = []

    async def fake_edit(chat_id, message_id, text, reply_markup=None):
        edits.append({"text": text})
        return {"ok": True}

    svc._edit_telegram_message = fake_edit

    await svc._auto_create_jira_and_edit_message(
        mapping_id="m1",
        escalation_message_id=42,
        escalation_topic_id=None,
        question_summary="Meter offline",
        conversation_context=None,
        customer_chat_id="123",
        customer_topic_id=None,
        organization_short_name="acme",
    )
    return edits


async def test_auto_create_jira_renders_link():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, ref="OPS-77", url="https://jira.test/browse/OPS-77")
    internal = _FakeBackend("internal", ref="TKT-1", url=None)
    _install_ticket_service(svc, jira, internal)

    edits = await _run_auto_create(svc)
    assert edits, "expected a message edit"
    text = edits[-1]["text"]
    assert "https://jira.test/browse/OPS-77" in text
    assert "](" in text  # markdown link syntax present
    # jira_ticket_key back-compat write happened.
    assert {"jira_ticket_key": "OPS-77"} in supa.em_update_payloads()


async def test_auto_create_internal_renders_plain_bold():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=False, ref="OPS-77")
    internal = _FakeBackend("internal", ref="TKT-000001", url=None)
    _install_ticket_service(svc, jira, internal)

    edits = await _run_auto_create(svc)
    text = edits[-1]["text"]
    assert "](" not in text  # no markdown link for internal
    assert "TKT" in text  # ref rendered as plain text
    # No jira_ticket_key for internal.
    assert all("jira_ticket_key" not in p for p in supa.em_update_payloads())


# ---------------------------------------------------------------------------
# Canonical escalation + delivery dual-write (inside _escalate_to_telegram)
# ---------------------------------------------------------------------------


async def test_new_escalation_dual_writes_canonical_escalation_and_delivery():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    resolved_uuid = uuid.uuid4()

    async def fake_get_session(_sid):
        return SimpleNamespace(id=resolved_uuid)

    supa.get_session = fake_get_session
    svc = _make_service(supa)

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        return {"ok": True, "result": {"message_id": 42}}

    svc._send_telegram_message = fake_send

    result = await svc.escalate_to_support(
        **_new_escalation_kwargs(reason="could_not_answer")
    )
    assert result["success"] is True

    escalation_rows = raw.tables["escalations"].rows
    assert len(escalation_rows) == 1
    assert escalation_rows[0]["chat_session_id"] == str(resolved_uuid)
    assert escalation_rows[0]["state"] == "open"
    assert escalation_rows[0]["reason"] == "could_not_answer"

    delivery_rows = raw.tables["message_deliveries"].rows
    assert len(delivery_rows) == 1
    delivery = delivery_rows[0]
    assert delivery["escalation_id"] == escalation_rows[0]["id"]
    assert delivery["ticket_id"] is None
    assert delivery["purpose"] == "escalation"
    assert delivery["external_message_id"] == 42


async def test_new_escalation_skips_legacy_write_once_legacy_writes_stopped(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    resolved_uuid = uuid.uuid4()

    async def fake_get_session(_sid):
        return SimpleNamespace(id=resolved_uuid)

    supa.get_session = fake_get_session
    svc = _make_service(supa)

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        return {"ok": True, "result": {"message_id": 42}}

    svc._send_telegram_message = fake_send

    result = await svc.escalate_to_support(**_new_escalation_kwargs(reason="could_not_answer"))

    assert result["success"] is True
    assert supa.save_calls == []  # legacy escalation_mappings write skipped entirely

    escalation_rows = raw.tables["escalations"].rows
    assert len(escalation_rows) == 1
    assert escalation_rows[0]["chat_session_id"] == str(resolved_uuid)
    assert escalation_rows[0]["state"] == "open"

    delivery_rows = raw.tables["message_deliveries"].rows
    assert len(delivery_rows) == 1
    assert delivery_rows[0]["escalation_id"] == escalation_rows[0]["id"]
    assert delivery_rows[0]["external_message_id"] == 42


async def test_followup_escalation_dual_writes_canonical_escalation_and_delivery():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    resolved_uuid = uuid.uuid4()

    async def fake_get_session(_sid):
        return SimpleNamespace(id=resolved_uuid)

    supa.get_session = fake_get_session
    svc = _make_service(supa)

    tickets = await _drive_followup(svc, is_done=False)
    assert tickets.get_status_calls == ["OPS-77"]

    escalation_rows = raw.tables["escalations"].rows
    assert len(escalation_rows) == 1
    assert escalation_rows[0]["chat_session_id"] == str(resolved_uuid)

    delivery_rows = raw.tables["message_deliveries"].rows
    assert len(delivery_rows) == 1
    assert delivery_rows[0]["escalation_id"] == escalation_rows[0]["id"]
    assert delivery_rows[0]["purpose"] == "escalation"
    assert delivery_rows[0]["external_message_id"] == 200  # fake_reply's message_id


async def test_followup_escalation_attaches_ticket_id_when_prelinked_to_existing_ticket():
    """Regression: attach_ticket() only ever fires for the escalation that
    originally filed a ticket. A follow-up pre-linked to that same ticket
    (existing_ref) must still get its canonical row's ticket_id set at
    creation time, or a Jira-key-based lookup would never find it."""
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)

    async def fake_get_session(_sid):
        return SimpleNamespace(id=uuid.uuid4())

    supa.get_session = fake_get_session
    svc = _make_service(supa)

    existing = {
        "is_active": True,
        "escalation_message_id": 100,
        "escalation_topic_id": None,
        "ticket_ref": "OPS-77",
        "ticket_backend": "jira",
        "jira_ticket_key": "OPS-77",
        "organization_id": None,
    }

    async def fake_get_info(_sid):
        return existing

    svc.get_escalation_info = fake_get_info

    async def fake_reply(chat_id, reply_to_message_id, text, reply_markup=None, topic_id=None):
        return {"ok": True, "result": {"message_id": 201}}

    svc._send_telegram_reply = fake_reply

    async def fake_get_id_by_ref(_ref: str):
        return "ticket-1"

    tickets = _FakeTickets(status=TicketStatus(summary="s", is_done=False))
    tickets.get_id_by_ref = fake_get_id_by_ref
    svc._tickets = tickets

    await svc.escalate_to_support(
        question_summary="follow up q",
        session_id="telegram_abc",
        customer_chat_id="123",
    )

    escalation_rows = raw.tables["escalations"].rows
    assert len(escalation_rows) == 1
    assert escalation_rows[0]["ticket_id"] == "ticket-1"

    delivery_rows = raw.tables["message_deliveries"].rows
    assert delivery_rows[0]["ticket_id"] == "ticket-1"


async def test_followup_escalation_skips_legacy_write_once_legacy_writes_stopped(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    resolved_uuid = uuid.uuid4()

    async def fake_get_session(_sid):
        return SimpleNamespace(id=resolved_uuid)

    supa.get_session = fake_get_session
    svc = _make_service(supa)

    tickets = await _drive_followup(svc, is_done=False)
    assert tickets.get_status_calls == ["OPS-77"]

    assert supa.save_calls == []  # legacy escalation_mappings write skipped entirely

    escalation_rows = raw.tables["escalations"].rows
    assert len(escalation_rows) == 1
    assert escalation_rows[0]["chat_session_id"] == str(resolved_uuid)

    delivery_rows = raw.tables["message_deliveries"].rows
    assert len(delivery_rows) == 1
    assert delivery_rows[0]["escalation_id"] == escalation_rows[0]["id"]


# ---------------------------------------------------------------------------
# escalate_verification_failure -- canonical dual-write, generates its own id
# (unlike the other two escalation-creation paths) since this call site
# never pre-generates a mapping id client-side.
# ---------------------------------------------------------------------------


async def test_verification_failure_escalation_skips_legacy_write_once_legacy_writes_stopped(
    monkeypatch,
):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    resolved_uuid = uuid.uuid4()

    async def fake_get_session(_sid):
        return SimpleNamespace(id=resolved_uuid)

    supa.get_session = fake_get_session
    svc = _make_service(supa)

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        return {"ok": True, "result": {"message_id": 77}}

    svc._send_telegram_message = fake_send

    result = await svc.escalate_verification_failure(
        original_message="what is my balance",
        failed_response="bad response",
        verification_feedback="hallucinated a number",
        session_id="telegram_abc",
        customer_chat_id="123",
    )

    assert result["success"] is True
    assert supa.save_calls == []  # legacy escalation_mappings write skipped entirely

    escalation_rows = raw.tables["escalations"].rows
    assert len(escalation_rows) == 1
    assert escalation_rows[0]["chat_session_id"] == str(resolved_uuid)
    assert escalation_rows[0]["reason"] == "verification_failed"

    delivery_rows = raw.tables["message_deliveries"].rows
    assert len(delivery_rows) == 1
    assert delivery_rows[0]["escalation_id"] == escalation_rows[0]["id"]
    assert delivery_rows[0]["external_message_id"] == 77


# ---------------------------------------------------------------------------
# Follow-up comment path (inside _escalate_to_telegram)
# ---------------------------------------------------------------------------


async def _drive_followup(svc: EscalationService, is_done: bool):
    existing = {
        "is_active": True,
        "escalation_message_id": 100,
        "escalation_topic_id": None,
        "ticket_ref": "OPS-77",
        "ticket_backend": "jira",
        "jira_ticket_key": "OPS-77",
        "organization_id": None,
    }

    async def fake_get_info(_sid):
        return existing

    svc.get_escalation_info = fake_get_info

    async def fake_reply(chat_id, reply_to_message_id, text, reply_markup=None, topic_id=None):
        return {"ok": True, "result": {"message_id": 200}}

    svc._send_telegram_reply = fake_reply

    tickets = _FakeTickets(status=TicketStatus(summary="s", is_done=is_done))

    async def fake_get_id_by_ref(ref: str) -> Optional[str]:
        # Only ever called with existing_ref, which _escalate_to_telegram
        # clears to None before this when the parent ticket is Done -- so
        # the done-ticket test never actually exercises this branch.
        return "ticket-1" if ref == "OPS-77" else None

    tickets.get_id_by_ref = fake_get_id_by_ref
    svc._tickets = tickets

    await svc.escalate_to_support(
        question_summary="follow up q",
        session_id="telegram_abc",
        customer_chat_id="123",
    )
    return tickets


async def test_followup_open_ticket_adds_comment_and_prelinks():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)

    tickets = await _drive_followup(svc, is_done=False)

    assert tickets.get_status_calls == ["OPS-77"]
    assert tickets.add_comment_calls == [
        ("OPS-77", "Follow-up from customer:\n\nfollow up q", False)
    ]
    assert supa.save_calls == []  # legacy escalation_mappings write skipped entirely

    escalation_rows = raw.tables["escalations"].rows
    assert len(escalation_rows) == 1
    assert escalation_rows[0]["ticket_id"] == "ticket-1"  # prelinked to the open parent


async def test_followup_done_ticket_does_not_comment_or_prelink():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)

    tickets = await _drive_followup(svc, is_done=True)

    assert tickets.get_status_calls == ["OPS-77"]
    assert tickets.add_comment_calls == []  # parent Done -> no comment
    assert supa.save_calls == []  # legacy escalation_mappings write skipped entirely

    escalation_rows = raw.tables["escalations"].rows
    assert len(escalation_rows) == 1
    assert escalation_rows[0]["ticket_id"] is None  # not prelinked -- sweep files fresh


# ---------------------------------------------------------------------------
# Sweep rename + alias
# ---------------------------------------------------------------------------


async def test_run_escalation_ticket_sweep_exists_and_alias_delegates():
    supa = _FakeSupabase(_FakeRaw())
    svc = _make_service(supa)

    assert callable(getattr(svc, "run_escalation_ticket_sweep", None))
    assert callable(getattr(svc, "run_escalation_jira_sweep", None))

    captured: Dict[str, Any] = {}

    async def fake_impl(min_age_hours=1, max_age_hours=24, limit=20):
        captured["args"] = (min_age_hours, max_age_hours, limit)
        return {"filed": 3}

    svc.run_escalation_ticket_sweep = fake_impl

    result = await svc.run_escalation_jira_sweep(min_age_hours=2, max_age_hours=5, limit=7)

    assert result == {"filed": 3}
    assert captured["args"] == (2, 5, 7)


# ---------------------------------------------------------------------------
# Sweep — filing loop body (claim -> track_as_ticket -> render + edit message)
# ---------------------------------------------------------------------------


def _stale_row(mapping_id: str = "m1") -> Dict[str, Any]:
    return {
        "id": mapping_id,
        "session_id": "telegram_abc",
        "customer_chat_id": "12345",
        "customer_topic_id": None,
        "org_hashtag": "#acme",
        "question_text": "my meter is broken",
        "escalation_message_id": 555,
        "escalation_topic_id": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "jira_ticket_key": None,
    }


def _wire_sweep_telegram(svc: EscalationService) -> Dict[str, List[Any]]:
    """Monkeypatch every Telegram send/edit the sweep can call; capture calls."""
    calls: Dict[str, List[Any]] = {"edits": [], "replies": [], "messages": []}

    async def fake_edit(chat_id, message_id, text, reply_markup=None):
        calls["edits"].append({"text": text})
        return {"ok": True}

    async def fake_reply(chat_id, reply_to_message_id, text, reply_markup=None, topic_id=None):
        calls["replies"].append({"text": text})
        return {"ok": True, "result": {"message_id": 999}}

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        calls["messages"].append({"chat_id": chat_id, "text": text})
        return {"ok": True, "result": {"message_id": 1000}}

    svc._edit_telegram_message = fake_edit
    svc._send_telegram_reply = fake_reply
    svc._send_telegram_message = fake_send
    return calls


async def test_sweep_files_jira_ticket_and_renders_link():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    supa.stale_unfiled = [_stale_row("m1")]
    supa.claim_returns = {"m1": _stale_row("m1")}
    svc = _make_service(supa)
    calls = _wire_sweep_telegram(svc)

    jira = _FakeBackend("jira", available=True, ref="OPS-42", url="https://jira.test/browse/OPS-42")
    internal = _FakeBackend("internal", ref="TKT-000001", url=None)
    _install_ticket_service(svc, jira, internal)

    result = await svc.run_escalation_ticket_sweep()

    assert result["filed"] == 1
    assert result["failed"] == 0
    assert calls["edits"], "expected the escalation message to be edited"
    edit_text = calls["edits"][-1]["text"]
    assert "https://jira.test/browse/OPS-42" in edit_text
    assert "](" in edit_text  # clickable link
    reply_text = calls["replies"][-1]["text"]
    assert "https://jira.test/browse/OPS-42" in reply_text


async def test_sweep_files_internal_ticket_and_renders_plain_bold():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    supa.stale_unfiled = [_stale_row("m1")]
    supa.claim_returns = {"m1": _stale_row("m1")}
    svc = _make_service(supa)
    calls = _wire_sweep_telegram(svc)

    # Jira unavailable (down) -> resolve_backend routes to internal.
    jira = _FakeBackend("jira", available=False, ref="OPS-42")
    internal = _FakeBackend("internal", ref="TKT-000007", url=None)
    _install_ticket_service(svc, jira, internal)

    result = await svc.run_escalation_ticket_sweep()

    assert result["filed"] == 1
    edit_text = calls["edits"][-1]["text"]
    assert "](" not in edit_text  # no clickable link for internal
    assert "TKT-000007" in edit_text
    reply_text = calls["replies"][-1]["text"]
    assert "](" not in reply_text
    assert "TKT-000007" in reply_text


# ---------------------------------------------------------------------------
# Sweep — reconciliation loop (closed-ticket cleanup + open-ticket notify)
# ---------------------------------------------------------------------------


async def test_sweep_reconciles_closed_ticket_and_notifies_open_one():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    # One Jira-backed row using the new ticket_ref column (closed -> reconciled),
    # one legacy row with only jira_ticket_key set (open -> customer notified),
    # exercising the `ticket_ref or jira_ticket_key` fallback on both sides.
    supa.active_tracked = [
        {
            "id": "closed-mapping",
            "ticket_ref": "OPS-1",
            "jira_ticket_key": "OPS-1",
            "customer_chat_id": "111",
            "customer_topic_id": None,
        },
        {
            "id": "open-legacy-mapping",
            "ticket_ref": None,
            "jira_ticket_key": "OPS-2",
            "customer_chat_id": "222",
            "customer_topic_id": None,
        },
    ]
    svc = _make_service(supa)
    calls = _wire_sweep_telegram(svc)

    tickets = _FakeTickets(
        by_ref={
            "OPS-1": TicketStatus(summary="Meter issue", is_done=True),
            "OPS-2": TicketStatus(summary="Billing issue", is_done=False),
        }
    )
    svc._tickets = tickets

    result = await svc.run_escalation_ticket_sweep()

    # Both refs were looked up via the fallback (ticket_ref for the first row,
    # jira_ticket_key for the legacy second row).
    assert set(tickets.get_status_calls) == {"OPS-1", "OPS-2"}

    # Closed ticket -> mapping reconciled (is_active=False), no customer message
    # sent for it.
    assert result["reconciled"] == 1
    close_updates = [
        f
        for op, f, p in raw.tables["escalation_mappings"].calls
        if op == "update" and f.get("id") == "closed-mapping"
    ]
    assert close_updates, "expected the closed mapping to be reconciled via an UPDATE"

    # Open ticket -> customer notified with the ticket ref and a "still open" message.
    assert result["notified_groups"] == 1
    assert calls["messages"], "expected a still-open notification"
    notify_text = calls["messages"][-1]["text"]
    assert "OPS-2" in notify_text
    assert calls["messages"][-1]["chat_id"] == "222"


async def test_sweep_also_syncs_canonical_jira_ticket_statuses():
    """The escalation-mapping reconciliation loop above only catches tickets
    tied to an active escalation mapping. Tickets filed via /notify (or any
    Jira ticket whose mapping already went inactive) have no mapping to
    reconcile through, so the sweep must separately sync canonical ticket
    status for every open Jira ticket, and surface those counts."""
    supa = _FakeSupabase(_FakeRaw())
    svc = _make_service(supa)

    tickets = _FakeTickets(sync_jira_ticket_statuses_result={"checked": 5, "closed": 2})
    svc._tickets = tickets

    result = await svc.run_escalation_ticket_sweep()

    assert tickets.sync_jira_ticket_statuses_calls == 1
    assert result["ticket_status_checked"] == 5
    assert result["ticket_status_closed"] == 2


# ---------------------------------------------------------------------------
# Sweep — canonical query rewrite (STOP_LEGACY_ESCALATION_WRITES)
# ---------------------------------------------------------------------------


def _canonical_escalation_row(
    escalation_id: str = "esc-1", *, age_hours: float = 2, ticket_id: Optional[str] = None
) -> Dict[str, Any]:
    created_at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    return {
        "id": escalation_id,
        "state": "open",
        "chat_session_id": "11111111-1111-1111-1111-111111111111",
        "ticket_id": ticket_id,
        "reason": "general",
        "org_hashtag": "#acme",
        "customer_username": None,
        "customer_email": None,
        "question_text": "my meter is broken",
        "created_at": created_at,
        "resolved_at": None,
    }


def _wire_canonical_session(supa: _FakeSupabase, *, chat_id: str = "12345") -> None:
    supa.session_by_id_result = SimpleNamespace(
        session_id="telegram_abc",
        telegram_chat_id=chat_id,
        telegram_topic_id=None,
        organization_id=7,
    )


async def test_sweep_canonical_eligible_list_files_ticket_via_list_unfiled(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    raw.table("escalations").rows = [_canonical_escalation_row("esc-1")]
    raw.table("message_deliveries").rows = [
        {"escalation_id": "esc-1", "purpose": "escalation", "external_message_id": 555}
    ]
    supa = _FakeSupabase(raw)
    _wire_canonical_session(supa)
    svc = _make_service(supa)
    _wire_sweep_telegram(svc)

    jira = _FakeBackend("jira", available=True, ref="OPS-42", url="https://jira.test/browse/OPS-42")
    internal = _FakeBackend("internal", ref="TKT-000001", url=None)
    _install_ticket_service(svc, jira, internal)

    result = await svc.run_escalation_ticket_sweep()

    assert result["filed"] == 1
    escalation_row = raw.table("escalations").rows[0]
    assert escalation_row["state"] == "tracked"
    assert escalation_row["ticket_id"]


async def test_sweep_canonical_query_failure_yields_zero_eligible_without_raising(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    supa = _FakeSupabase(_FakeRaw())
    svc = _make_service(supa)

    async def _boom(**_k):
        raise RuntimeError("db down")

    svc._escalations.list_unfiled = _boom

    result = await svc.run_escalation_ticket_sweep()

    assert result["eligible"] == 0
    assert result["filed"] == 0
    assert result["skipped"] == 0
    assert result["failed"] == 0


async def test_sweep_canonical_releases_claim_on_track_as_ticket_failure(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    raw.table("escalations").rows = [_canonical_escalation_row("esc-1")]
    raw.table("message_deliveries").rows = [
        {"escalation_id": "esc-1", "purpose": "escalation", "external_message_id": 555}
    ]
    supa = _FakeSupabase(raw)
    _wire_canonical_session(supa)
    svc = _make_service(supa)
    _wire_sweep_telegram(svc)

    async def _fail_track(**_k):
        return {"success": False, "error": "backend down"}

    svc.track_as_ticket = _fail_track

    result = await svc.run_escalation_ticket_sweep()

    assert result["failed"] == 1
    # Claim was released back to "open" via the canonical repository -- not
    # the legacy reactivate_escalation path.
    assert raw.table("escalations").rows[0]["state"] == "open"
    assert supa.reactivate_calls == []


async def test_sweep_canonical_old_escalations_alert_uses_list_unfiled_upper_bound_only(
    monkeypatch,
):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    # Aged out (30h > default max_age_hours=24) -- must surface in the alert
    # even though it's outside the eligible-filing window.
    raw.table("escalations").rows = [_canonical_escalation_row("esc-old", age_hours=30)]
    supa = _FakeSupabase(raw)
    _wire_canonical_session(supa)
    svc = _make_service(supa)
    calls = _wire_sweep_telegram(svc)

    await svc.run_escalation_ticket_sweep()

    assert calls["messages"], "expected an aged-out-orphan alert"
    assert "older than 24h" in calls["messages"][-1]["text"]


async def test_sweep_canonical_tracked_reconciliation_resolves_and_skips_legacy_update(
    monkeypatch,
):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    raw.table("escalations").rows = [
        _canonical_escalation_row("esc-tracked", ticket_id="ticket-1")
    ]
    raw.table("escalations").rows[0]["state"] = "open"  # tracked-but-open: follow-up prelink
    raw.table("tickets").rows = [
        {
            "id": "ticket-1",
            "ticket_ref": "OPS-1",
            "backend": "jira",
            "created_via": "escalation",
            "provisioning_state": "active",
        }
    ]
    supa = _FakeSupabase(raw)
    _wire_canonical_session(supa)
    svc = _make_service(supa)
    _wire_sweep_telegram(svc)
    svc._tickets = _FakeTickets(
        status=TicketStatus(summary="Meter issue", is_done=True),
        ref_by_ticket_id={"ticket-1": "OPS-1"},
        backend_by_ref={"OPS-1": "jira"},
    )

    result = await svc.run_escalation_ticket_sweep()

    assert result["reconciled"] == 1
    # Legacy escalation_mappings table must never be touched once legacy
    # writes are stopped.
    assert raw.table("escalation_mappings").calls == []
    # Canonical mirror: resolve() moved the escalation to "resolved".
    assert raw.table("escalations").rows[0]["state"] == "resolved"
    assert raw.table("escalations").rows[0]["resolved_at"] is not None


# ---------------------------------------------------------------------------
# recover_orphaned_claims -- canonical release once legacy writes are stopped
# ---------------------------------------------------------------------------


async def test_recover_orphaned_claims_uses_legacy_when_flag_off(monkeypatch):
    monkeypatch.delenv("STOP_LEGACY_ESCALATION_WRITES", raising=False)
    supa = _FakeSupabase(_FakeRaw())
    supa.reactivate_calls = []

    async def fake_get_orphaned(**_k):
        return [{"id": "m1"}]

    supa.get_orphaned_claimed_escalations = fake_get_orphaned
    svc = _make_service(supa)

    await svc.recover_orphaned_claims()

    assert supa.reactivate_calls == ["m1"]


async def test_recover_orphaned_claims_releases_canonical_claims_when_flag_on(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    raw.table("escalations").rows = [
        {
            "id": "esc-orphan",
            "state": "processing",
            "ticket_id": None,
            "resolved_at": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)

    await svc.recover_orphaned_claims()

    assert raw.table("escalations").rows[0]["state"] == "open"
    assert supa.reactivate_calls == []


async def test_recover_orphaned_claims_canonical_query_failure_is_swallowed(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    supa = _FakeSupabase(_FakeRaw())
    svc = _make_service(supa)

    async def _boom(**_k):
        raise RuntimeError("db down")

    svc._escalations.list_claimed_orphans = _boom

    await svc.recover_orphaned_claims()  # must not raise


# ---------------------------------------------------------------------------
# handle_support_reply — chat-message tagging
# ---------------------------------------------------------------------------


async def test_handle_support_reply_tags_message_when_ticket_linked():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    supa.mapping_for_reply = {
        "is_active": True,
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "customer_email": None,
        "session_id": "telegram_abc",
        "ticket_ref": "OPS-77",
        "escalation_topic_id": None,
    }
    svc = _make_service(supa)

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        return {"ok": True, "result": {"message_id": 1}}

    svc._send_telegram_message = fake_send

    res = await svc.handle_support_reply(reply_to_message_id=555, reply_text="hi there")

    assert res["success"] is True
    assert supa.tag_calls == [("msg-1", "OPS-77", "comment")]


async def test_handle_support_reply_skips_tag_when_no_ticket_ref():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    supa.mapping_for_reply = {
        "is_active": True,
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "customer_email": None,
        "session_id": "telegram_abc",
        "ticket_ref": None,
        "escalation_topic_id": None,
    }
    svc = _make_service(supa)

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        return {"ok": True, "result": {"message_id": 1}}

    svc._send_telegram_message = fake_send

    await svc.handle_support_reply(reply_to_message_id=555, reply_text="hi there")

    assert supa.tag_calls == []


async def test_handle_support_reply_tags_via_legacy_jira_ticket_key_fallback():
    """A pre-migration or stamp-failed row has jira_ticket_key but no ticket_ref --
    tagging must still fall back to it, consistent with every other reader in
    this file."""
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    supa.mapping_for_reply = {
        "is_active": True,
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "customer_email": None,
        "session_id": "telegram_abc",
        "ticket_ref": None,
        "jira_ticket_key": "OPS-99",
        "escalation_topic_id": None,
    }
    svc = _make_service(supa)

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        return {"ok": True, "result": {"message_id": 1}}

    svc._send_telegram_message = fake_send

    await svc.handle_support_reply(reply_to_message_id=555, reply_text="hi there")

    assert supa.tag_calls == [("msg-1", "OPS-99", "comment")]


# ---------------------------------------------------------------------------
# handle_support_reply -- canonical lookup via message_deliveries
# ---------------------------------------------------------------------------


async def test_handle_support_reply_resolves_via_canonical_tables_when_flag_on(monkeypatch):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    raw = _FakeRaw()
    raw.table("message_deliveries").rows = [
        {
            "escalation_id": "esc-1",
            "external_message_id": 555,
            "purpose": "escalation",
            "external_topic_id": "9",
        }
    ]
    raw.table("escalations").rows = [
        {
            "id": "esc-1",
            "chat_session_id": "session-uuid-1",
            "state": "open",
            "customer_email": "cust@example.com",
            "ticket_id": "ticket-1",
        }
    ]
    raw.table("tickets").rows = [
        {
            "id": "ticket-1",
            "ticket_ref": "OPS-77",
            "created_via": "escalation",
            "provisioning_state": "active",
            "summary": "Meter offline",
        }
    ]
    supa = _FakeSupabase(raw)
    supa.session_by_id_result = SimpleNamespace(
        telegram_chat_id="123", telegram_topic_id=None, session_id="telegram_abc"
    )
    # Legacy lookup must never be consulted when the canonical path succeeds.
    supa.mapping_for_reply = None
    svc = _make_service(supa)

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        return {"ok": True, "result": {"message_id": 1}}

    svc._send_telegram_message = fake_send

    result = await svc.handle_support_reply(reply_to_message_id=555, reply_text="hi there")

    assert result["success"] is True
    assert supa.tag_calls == [("msg-1", "OPS-77", "comment")]


async def test_handle_support_reply_falls_back_to_legacy_when_canonical_incomplete(monkeypatch):
    """If any step of the canonical resolution comes up empty (e.g. no
    matching delivery row -- an escalation created before Phase 1 shipped),
    the legacy lookup still runs rather than reporting "escalation not
    found"."""
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    raw = _FakeRaw()  # no message_deliveries rows at all
    supa = _FakeSupabase(raw)
    supa.mapping_for_reply = {
        "is_active": True,
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "customer_email": None,
        "session_id": "telegram_abc",
        "ticket_ref": "OPS-77",
        "escalation_topic_id": None,
    }
    svc = _make_service(supa)

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        return {"ok": True, "result": {"message_id": 1}}

    svc._send_telegram_message = fake_send

    result = await svc.handle_support_reply(reply_to_message_id=555, reply_text="hi there")

    assert result["success"] is True
    assert supa.tag_calls == [("msg-1", "OPS-77", "comment")]


async def test_handle_support_reply_does_not_fall_back_once_legacy_writes_stopped(monkeypatch):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()  # no message_deliveries rows -- canonical resolution fails
    supa = _FakeSupabase(raw)
    supa.mapping_for_reply = {
        "is_active": True,
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "customer_email": None,
        "session_id": "telegram_abc",
        "ticket_ref": "OPS-77",
        "escalation_topic_id": None,
    }
    svc = _make_service(supa)

    result = await svc.handle_support_reply(reply_to_message_id=555, reply_text="hi there")

    assert result["success"] is False
    assert supa.tag_calls == []


async def test_handle_support_reply_uses_legacy_when_flag_off(monkeypatch):
    monkeypatch.delenv("CANONICAL_ESCALATION_READS_ENABLED", raising=False)
    raw = _FakeRaw()
    raw.table("message_deliveries").rows = [
        {"escalation_id": "esc-1", "external_message_id": 555, "purpose": "escalation"}
    ]
    raw.table("escalations").rows = [
        {"id": "esc-1", "chat_session_id": "session-uuid-1", "state": "open"}
    ]
    supa = _FakeSupabase(raw)
    supa.mapping_for_reply = {
        "is_active": True,
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "customer_email": None,
        "session_id": "telegram_abc",
        "ticket_ref": "OPS-77",
        "escalation_topic_id": None,
    }
    svc = _make_service(supa)

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        return {"ok": True, "result": {"message_id": 1}}

    svc._send_telegram_message = fake_send

    result = await svc.handle_support_reply(reply_to_message_id=555, reply_text="hi there")

    assert result["success"] is True
    # Legacy path used even though canonical rows exist -- flag is off.
    assert supa.tag_calls == [("msg-1", "OPS-77", "comment")]


# ---------------------------------------------------------------------------
# resolve_escalation_by_message_id -- the public extraction handler.py's
# Reopen/Closed reply commands use, and _resolve_support_reply_canonical's
# ticket_backend field (needed to gate the Jira-transition call correctly).
# ---------------------------------------------------------------------------


def _canonical_fixture_rows(*, ticket_backend: Optional[str]) -> "_FakeRaw":
    raw = _FakeRaw()
    raw.table("message_deliveries").rows = [
        {
            "escalation_id": "esc-1",
            "external_message_id": 555,
            "purpose": "escalation",
            "external_topic_id": "9",
        }
    ]
    raw.table("escalations").rows = [
        {
            "id": "esc-1",
            "chat_session_id": "session-uuid-1",
            "state": "open",
            "customer_email": "cust@example.com",
            "ticket_id": "ticket-1",
        }
    ]
    ticket_row: Dict[str, Any] = {
        "id": "ticket-1",
        "ticket_ref": "OPS-77",
        "created_via": "escalation",
        "provisioning_state": "active",
        "summary": "Meter offline",
    }
    if ticket_backend is not None:
        ticket_row["backend"] = ticket_backend
    raw.table("tickets").rows = [ticket_row]
    return raw


async def test_resolve_escalation_by_message_id_directly_callable(monkeypatch):
    """handler.py's Reopen/Closed commands call this public method directly
    (not through handle_support_reply) -- prove it works standalone."""
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    supa = _FakeSupabase(_canonical_fixture_rows(ticket_backend="jira"))
    supa.session_by_id_result = SimpleNamespace(
        telegram_chat_id="123", telegram_topic_id=None, session_id="telegram_abc"
    )
    svc = _make_service(supa)

    mapping = await svc.resolve_escalation_by_message_id(555)

    assert mapping is not None
    assert mapping["session_id"] == "telegram_abc"
    assert mapping["escalation_topic_id"] == "9"


async def test_resolve_support_reply_canonical_reports_jira_backend(monkeypatch):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    supa = _FakeSupabase(_canonical_fixture_rows(ticket_backend="jira"))
    supa.session_by_id_result = SimpleNamespace(
        telegram_chat_id="123", telegram_topic_id=None, session_id="telegram_abc"
    )
    svc = _make_service(supa)

    mapping = await svc.resolve_escalation_by_message_id(555)

    assert mapping["ticket_ref"] == "OPS-77"
    assert mapping["ticket_backend"] == "jira"


async def test_resolve_support_reply_canonical_reports_internal_backend(monkeypatch):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    supa = _FakeSupabase(_canonical_fixture_rows(ticket_backend="internal"))
    supa.session_by_id_result = SimpleNamespace(
        telegram_chat_id="123", telegram_topic_id=None, session_id="telegram_abc"
    )
    svc = _make_service(supa)

    mapping = await svc.resolve_escalation_by_message_id(555)

    assert mapping["ticket_backend"] == "internal"


async def test_resolve_support_reply_canonical_backend_none_when_not_yet_recorded(monkeypatch):
    """A ticket that's still provisioning (no backend recorded yet) must not
    blow up the whole resolution -- get_backend_name raises TicketBackendError
    for exactly this case, which is fatal for its usual callers but not here."""
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    supa = _FakeSupabase(_canonical_fixture_rows(ticket_backend=None))
    supa.session_by_id_result = SimpleNamespace(
        telegram_chat_id="123", telegram_topic_id=None, session_id="telegram_abc"
    )
    svc = _make_service(supa)

    mapping = await svc.resolve_escalation_by_message_id(555)

    assert mapping is not None
    assert mapping["ticket_ref"] == "OPS-77"
    assert mapping["ticket_backend"] is None


# ---------------------------------------------------------------------------
# ALWAYS_FILE_ESCALATION_AS_TICKET
# ---------------------------------------------------------------------------


def _new_escalation_kwargs(**overrides) -> Dict[str, Any]:
    defaults: Dict[str, Any] = dict(
        question_summary="my meter is broken",
        session_id="telegram_xyz",
        customer_chat_id="12345",
        customer_topic_id=None,
        organization_id=None,  # skips forum-topic resolution
    )
    defaults.update(overrides)
    return defaults


async def test_always_file_flag_off_keeps_the_track_button_during_business_hours(monkeypatch):
    """Default (flag unset) behavior must be unchanged: the Track button still
    shows during business hours, and no ticket is auto-filed."""
    monkeypatch.delenv("ALWAYS_FILE_ESCALATION_AS_TICKET", raising=False)
    supa = _FakeSupabase(_FakeRaw())
    svc = _make_service(supa)
    monkeypatch.setattr(
        "orchestrator.services.escalation_service._is_after_hours", lambda: False
    )
    auto_create_calls: List[Any] = []

    async def fake_auto_create(**kwargs):
        auto_create_calls.append(kwargs)

    svc._auto_create_jira_and_edit_message = fake_auto_create

    sent: List[Dict[str, Any]] = []

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        sent.append({"reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": 42}}

    svc._send_telegram_message = fake_send

    result = await svc.escalate_to_support(**_new_escalation_kwargs())

    assert result["success"] is True
    assert auto_create_calls == []
    track_row_texts = [
        button["text"] for row in sent[0]["reply_markup"]["inline_keyboard"] for button in row
    ]
    assert any("Track as ticket" in text for text in track_row_texts)


async def test_always_file_flag_on_hides_track_button_and_auto_files_during_business_hours(
    monkeypatch,
):
    """The flag must behave like after-hours auto-filing even when it isn't
    after hours: no Track button, ticket filed automatically."""
    monkeypatch.setenv("ALWAYS_FILE_ESCALATION_AS_TICKET", "true")
    supa = _FakeSupabase(_FakeRaw())
    svc = _make_service(supa)
    monkeypatch.setattr(
        "orchestrator.services.escalation_service._is_after_hours", lambda: False
    )
    auto_create_calls: List[Any] = []

    async def fake_auto_create(**kwargs):
        auto_create_calls.append(kwargs)

    svc._auto_create_jira_and_edit_message = fake_auto_create

    sent: List[Dict[str, Any]] = []

    async def fake_send(chat_id, text, parse_mode="Markdown", topic_id=None, reply_markup=None):
        sent.append({"reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": 42}}

    svc._send_telegram_message = fake_send

    result = await svc.escalate_to_support(**_new_escalation_kwargs())

    assert result["success"] is True
    assert len(auto_create_calls) == 1
    assert auto_create_calls[0]["escalation_message_id"] == 42
    track_row_texts = [
        button["text"] for row in sent[0]["reply_markup"]["inline_keyboard"] for button in row
    ]
    assert not any("Track as ticket" in text for text in track_row_texts)


# ---------------------------------------------------------------------------
# is_session_escalated -- CANONICAL_ESCALATION_READS_ENABLED flag flip
# ---------------------------------------------------------------------------


class _FakeEscalationsRepo:
    def __init__(self, result: Any = False):
        self._result = result
        self.calls: List[tuple] = []

    async def has_blocking_escalation(self, chat_session_id, *, exclude_reasons=None):
        self.calls.append((chat_session_id, exclude_reasons))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


async def test_is_session_escalated_uses_legacy_when_flag_off(monkeypatch):
    monkeypatch.delenv("CANONICAL_ESCALATION_READS_ENABLED", raising=False)
    supa = _FakeSupabase(_FakeRaw())
    supa.session_escalation_info = {"is_escalated": True}
    svc = _make_service(supa)
    canonical = _FakeEscalationsRepo(result=False)
    svc._escalations = canonical

    assert await svc.is_session_escalated("telegram_abc") is True
    assert canonical.calls == []  # canonical check never consulted


async def test_is_session_escalated_uses_canonical_when_flag_on(monkeypatch):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    supa = _FakeSupabase(_FakeRaw())
    supa.session_escalation_info = {"is_escalated": False}  # legacy disagrees
    svc = _make_service(supa)
    canonical = _FakeEscalationsRepo(result=True)
    svc._escalations = canonical

    assert await svc.is_session_escalated("telegram_abc") is True
    assert len(canonical.calls) == 1
    _chat_session_uuid, exclude_reasons = canonical.calls[0]
    assert set(exclude_reasons) == {"safety_escalation", "system_error"}


async def test_is_session_escalated_falls_back_to_legacy_on_canonical_error(monkeypatch):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    supa = _FakeSupabase(_FakeRaw())
    supa.session_escalation_info = {"is_escalated": True}
    svc = _make_service(supa)
    svc._escalations = _FakeEscalationsRepo(result=RuntimeError("db down"))

    assert await svc.is_session_escalated("telegram_abc") is True


async def test_is_session_escalated_does_not_fall_back_once_legacy_writes_stopped(monkeypatch):
    """Once STOP_LEGACY_ESCALATION_WRITES is on, legacy is guaranteed stale --
    an inconclusive canonical check must not trust it, even though the
    legacy row here claims the session is escalated."""
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    supa = _FakeSupabase(_FakeRaw())
    supa.session_escalation_info = {"is_escalated": True}
    svc = _make_service(supa)
    svc._escalations = _FakeEscalationsRepo(result=RuntimeError("db down"))

    assert await svc.is_session_escalated("telegram_abc") is False


async def test_is_session_escalated_falls_back_to_legacy_when_session_unresolvable(monkeypatch):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    supa = _FakeSupabase(_FakeRaw())
    supa.session_escalation_info = {"is_escalated": True}

    async def fake_get_session(_sid):
        return None  # unknown session -- can't resolve a chat_sessions.id

    supa.get_session = fake_get_session
    svc = _make_service(supa)
    canonical = _FakeEscalationsRepo(result=False)
    svc._escalations = canonical

    assert await svc.is_session_escalated("telegram_abc") is True
    assert canonical.calls == []


# ---------------------------------------------------------------------------
# get_escalation_info -- CANONICAL_ESCALATION_READS_ENABLED flag flip
# ---------------------------------------------------------------------------


async def test_get_escalation_info_uses_legacy_when_flag_off(monkeypatch):
    monkeypatch.delenv("CANONICAL_ESCALATION_READS_ENABLED", raising=False)
    supa = _FakeSupabase(_FakeRaw())
    supa.escalation_by_session_result = {"is_active": True, "escalation_message_id": 42}
    svc = _make_service(supa)

    result = await svc.get_escalation_info("telegram_abc")

    assert result == {"is_active": True, "escalation_message_id": 42}


async def test_get_escalation_info_resolves_via_canonical_tables_when_flag_on(monkeypatch):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    raw = _FakeRaw()
    raw.table("escalations").rows = [
        {
            "id": "esc-1",
            "chat_session_id": "11111111-1111-1111-1111-111111111111",
            "state": "processing",
            "org_hashtag": "#acme",
            "ticket_id": "ticket-1",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    raw.table("message_deliveries").rows = [
        {
            "escalation_id": "esc-1",
            "purpose": "escalation",
            "external_message_id": 555,
            "external_topic_id": "9",
        }
    ]
    raw.table("tickets").rows = [
        {
            "id": "ticket-1",
            "ticket_ref": "OPS-77",
            "backend": "jira",
            "created_via": "escalation",
            "provisioning_state": "active",
            "summary": "Meter offline",
        }
    ]
    supa = _FakeSupabase(raw)

    async def fake_get_session(_sid):
        return SimpleNamespace(
            id=uuid.UUID("11111111-1111-1111-1111-111111111111"), organization_id=7
        )

    supa.get_session = fake_get_session
    # Legacy path must never be consulted when the canonical path succeeds.
    supa.escalation_by_session_result = None
    svc = _make_service(supa)

    result = await svc.get_escalation_info("telegram_abc")

    assert result == {
        "is_active": True,
        "session_id": "telegram_abc",
        "escalation_message_id": 555,
        "escalation_topic_id": "9",
        "organization_id": 7,
        "org_hashtag": "#acme",
        "ticket_ref": "OPS-77",
        "ticket_backend": "jira",
        "jira_ticket_key": None,
    }


async def test_get_escalation_info_falls_back_to_legacy_when_no_active_canonical_escalation(
    monkeypatch,
):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    raw = _FakeRaw()  # no escalations rows -- e.g. a pre-Phase-1 escalation
    supa = _FakeSupabase(raw)
    supa.escalation_by_session_result = {"is_active": True, "escalation_message_id": 42}

    async def fake_get_session(_sid):
        return SimpleNamespace(id=uuid.uuid4(), organization_id=7)

    supa.get_session = fake_get_session
    svc = _make_service(supa)

    result = await svc.get_escalation_info("telegram_abc")

    assert result == {"is_active": True, "escalation_message_id": 42}


async def test_get_escalation_info_does_not_fall_back_once_legacy_writes_stopped(monkeypatch):
    monkeypatch.setenv("CANONICAL_ESCALATION_READS_ENABLED", "true")
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()  # no escalations rows -- canonical finds nothing
    supa = _FakeSupabase(raw)
    supa.escalation_by_session_result = {"is_active": True, "escalation_message_id": 42}

    async def fake_get_session(_sid):
        return SimpleNamespace(id=uuid.uuid4(), organization_id=7)

    supa.get_session = fake_get_session
    svc = _make_service(supa)

    result = await svc.get_escalation_info("telegram_abc")

    assert result is None


# ---------------------------------------------------------------------------
# reopen_escalation -- canonical dual-write (mirrors the close/resolve fix)
# ---------------------------------------------------------------------------


async def test_reopen_escalation_reopens_the_canonical_escalation_row():
    raw = _FakeRaw()
    raw.table("message_deliveries").rows = [
        {"escalation_id": "esc-1", "external_message_id": 555, "purpose": "escalation"}
    ]
    raw.table("escalations").rows = [{"id": "esc-1", "state": "resolved", "resolved_at": "t"}]
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)

    result = await svc.reopen_escalation("telegram_abc", 555)

    assert result == {"success": True, "message": "Escalation reopened"}
    escalation_row = raw.table("escalations").rows[0]
    assert escalation_row["state"] == "open"
    assert escalation_row["resolved_at"] is None


async def test_reopen_escalation_uses_canonical_only_once_legacy_writes_stopped(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    raw.table("message_deliveries").rows = [
        {"escalation_id": "esc-1", "external_message_id": 555, "purpose": "escalation"}
    ]
    raw.table("escalations").rows = [{"id": "esc-1", "state": "resolved", "resolved_at": "t"}]
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)

    result = await svc.reopen_escalation("telegram_abc", 555)

    assert result == {"success": True, "message": "Escalation reopened"}
    assert supa.reopen_escalation_calls == []  # legacy reopen skipped entirely
    escalation_row = raw.table("escalations").rows[0]
    assert escalation_row["state"] == "open"
    assert escalation_row["resolved_at"] is None


async def test_reopen_escalation_fails_when_no_canonical_delivery_once_legacy_writes_stopped(
    monkeypatch,
):
    """Without a legacy fallback to lean on, an unresolvable escalation must
    be reported as a failure rather than silently claiming success."""
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()  # no message_deliveries rows
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)

    result = await svc.reopen_escalation("telegram_abc", 555)

    assert result["success"] is False
    assert supa.reopen_escalation_calls == []
