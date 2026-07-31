"""handle_jira_issue_updated: closing a Jira ticket must also close the
canonical ticket record, not just the escalation_mappings/session bookkeeping.

Before this, the webhook closed the Telegram escalation session but left the
canonical ``tickets`` row (what the internal tickets admin page reads) stuck
at status="open" forever, since jira_backend.transition_to_done() only calls
the Jira API and has no repository reference of its own.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from orchestrator.services.escalation_service import EscalationService

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

    def select(self, *_a, **_k) -> "_FakeQuery":
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeQuery":
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col: str, value: Any) -> "_FakeQuery":
        self._filters[col] = value
        return self

    def neq(self, col: str, value: Any) -> "_FakeQuery":
        self._filters[col] = f"neq:{value}"
        return self

    def is_(self, col: str, value: Any) -> "_FakeQuery":
        self._filters[col] = f"is:{value}"
        return self

    def order(self, *_a, **_k) -> "_FakeQuery":
        return self

    def limit(self, *_a, **_k) -> "_FakeQuery":
        return self

    def execute(self) -> _FakeResponse:
        self._t.calls.append((self._op, dict(self._filters), self._payload))
        if self._op == "select":
            return _FakeResponse(self._t.rows_matching(self._filters))
        if self._op == "update" and self._t.rows:
            updated = []
            for row in self._t.rows_matching(self._filters):
                row.update(self._payload or {})
                updated.append(row)
            return _FakeResponse(updated)
        return _FakeResponse([])


class _FakeTable:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.rows = rows or []
        self.calls: List[tuple] = []

    def rows_matching(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        def match(row: Dict[str, Any], col: str, val: Any) -> bool:
            if isinstance(val, str) and val.startswith("neq:"):
                return str(row.get(col)) != val[len("neq:") :]
            if isinstance(val, str) and val.startswith("is:"):
                target = val[len("is:") :]
                return row.get(col) is None if target == "null" else row.get(col) == target
            return row.get(col) == val

        return [r for r in self.rows if all(match(r, k, v) for k, v in filters.items())]

    def select(self, *_a, **_k) -> _FakeQuery:
        return _FakeQuery(self, "select")

    def update(self, payload: Dict[str, Any]) -> _FakeQuery:
        return _FakeQuery(self, "update", payload)


class _FakeRaw:
    def __init__(self) -> None:
        self.tables: Dict[str, _FakeTable] = {"escalation_mappings": _FakeTable()}

    def table(self, name: str) -> _FakeTable:
        if name not in self.tables:
            self.tables[name] = _FakeTable()
        return self.tables[name]


class _FakeSupabase:
    def __init__(self, raw: _FakeRaw, mapping_by_jira_key: Optional[Dict[str, Dict]] = None) -> None:
        self._raw = raw
        self._mapping_by_jira_key = mapping_by_jira_key or {}
        self.close_escalation_calls: List[str] = []
        self.session_result: Optional[SimpleNamespace] = None
        self.session_by_id_result: Optional[SimpleNamespace] = None

    def _get_client(self) -> _FakeRaw:
        return self._raw

    async def get_escalation_mapping_by_jira_key(self, jira_ticket_key: str):
        return self._mapping_by_jira_key.get(jira_ticket_key)

    async def close_escalation(self, session_id: str) -> bool:
        self.close_escalation_calls.append(session_id)
        return True

    async def get_session(self, _session_id):
        return self.session_result

    async def get_session_by_id(self, _session_uuid):
        return self.session_by_id_result


class _FakeTickets:
    def __init__(
        self,
        transition_error: Optional[Exception] = None,
        ref_by_ticket_id: Optional[Dict[str, str]] = None,
        id_by_ref: Optional[Dict[str, str]] = None,
        backend_by_ref: Optional[Dict[str, str]] = None,
    ) -> None:
        self.transition_to_done_calls: List[str] = []
        self._transition_error = transition_error
        self._ref_by_ticket_id = ref_by_ticket_id or {}
        self._id_by_ref = id_by_ref or {}
        self._backend_by_ref = backend_by_ref or {}

    async def transition_to_done(self, ref: str) -> None:
        self.transition_to_done_calls.append(ref)
        if self._transition_error is not None:
            raise self._transition_error

    async def get_id_by_ref(self, ref: str) -> Optional[str]:
        return self._id_by_ref.get(ref)

    async def get_ref_by_id(self, ticket_id: str) -> Optional[str]:
        return self._ref_by_ticket_id.get(ticket_id)

    async def get_backend_name(self, ref: str) -> str:
        return self._backend_by_ref.get(ref, "jira")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mapping(ticket_ref: Optional[str]) -> Dict[str, Any]:
    return {
        "id": "mapping-1",
        "session_id": "telegram_abc",
        "organization_id": None,  # skips org/topic lookups
        "ticket_ref": ticket_ref,
        "customer_chat_id": "111",
        "customer_topic_id": None,
    }


def _closed_payload(issue_key: str = "OPS-1") -> Dict[str, Any]:
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": issue_key, "fields": {}},
        "changelog": {"items": [{"field": "status", "toString": "Closed"}]},
    }


def _make_service(supa: _FakeSupabase, tickets: _FakeTickets) -> EscalationService:
    svc = EscalationService(
        escalation_chat_id="-100123456",
        bot_token="TESTTOKEN",
        supabase_url="http://supabase.test",
        supabase_key="key",
    )
    svc._supabase_client = supa
    svc._tickets = tickets

    async def fake_send(*_a, **_k):
        return {"ok": True}

    svc._send_telegram_message = fake_send
    return svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_closure_webhook_marks_the_canonical_ticket_done():
    raw = _FakeRaw()
    raw.tables["escalation_mappings"].rows = [
        {"id": "mapping-1", "session_id": "telegram_abc", "is_active": True}
    ]
    supa = _FakeSupabase(raw, mapping_by_jira_key={"OPS-1": _mapping("OPS-1")})
    tickets = _FakeTickets()
    svc = _make_service(supa, tickets)

    await svc.handle_jira_issue_updated(_closed_payload("OPS-1"))

    assert tickets.transition_to_done_calls == ["OPS-1"]
    # The existing escalation-session closure must still happen alongside it.
    assert supa.close_escalation_calls == ["telegram_abc"]


async def test_closure_webhook_skips_canonical_close_when_mapping_has_no_ticket_ref():
    """Legacy mappings that only ever recorded jira_ticket_key (pre-canonical-
    ``tickets``-table) have no canonical row to close -- must not attempt it."""
    raw = _FakeRaw()
    raw.tables["escalation_mappings"].rows = [
        {"id": "mapping-1", "session_id": "telegram_abc", "is_active": True}
    ]
    supa = _FakeSupabase(raw, mapping_by_jira_key={"OPS-1": _mapping(None)})
    tickets = _FakeTickets()
    svc = _make_service(supa, tickets)

    await svc.handle_jira_issue_updated(_closed_payload("OPS-1"))

    assert tickets.transition_to_done_calls == []
    assert supa.close_escalation_calls == ["telegram_abc"]


async def test_closure_webhook_still_closes_escalation_when_canonical_close_fails():
    """A failure marking the canonical ticket done is non-fatal -- the
    Telegram-facing escalation close must still go through."""
    raw = _FakeRaw()
    raw.tables["escalation_mappings"].rows = [
        {"id": "mapping-1", "session_id": "telegram_abc", "is_active": True}
    ]
    supa = _FakeSupabase(raw, mapping_by_jira_key={"OPS-1": _mapping("OPS-1")})
    tickets = _FakeTickets(transition_error=RuntimeError("db blip"))
    svc = _make_service(supa, tickets)

    await svc.handle_jira_issue_updated(_closed_payload("OPS-1"))

    assert tickets.transition_to_done_calls == ["OPS-1"]
    assert supa.close_escalation_calls == ["telegram_abc"]


async def test_non_closure_status_change_is_ignored():
    supa = _FakeSupabase(_FakeRaw())
    tickets = _FakeTickets()
    svc = _make_service(supa, tickets)

    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": "OPS-1", "fields": {}},
        "changelog": {"items": [{"field": "status", "toString": "In Progress"}]},
    }
    await svc.handle_jira_issue_updated(payload)

    assert tickets.transition_to_done_calls == []
    assert supa.close_escalation_calls == []


# ---------------------------------------------------------------------------
# Canonical rewrite (STOP_LEGACY_ESCALATION_WRITES) -- Task 12
# ---------------------------------------------------------------------------


async def test_closure_webhook_resolves_canonically_once_legacy_writes_stopped(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    session_uuid = str(uuid.uuid4())
    raw = _FakeRaw()
    raw.table("escalations").rows = [
        {
            "id": "esc-1",
            "chat_session_id": session_uuid,
            "state": "tracked",
            "ticket_id": "ticket-1",
            "org_hashtag": None,
            "question_text": None,
            "created_at": "2026-01-01T00:00:00Z",
            "resolved_at": None,
        }
    ]
    supa = _FakeSupabase(raw)
    supa.session_by_id_result = SimpleNamespace(
        session_id="telegram_abc",
        telegram_chat_id="111",
        telegram_topic_id=None,
        organization_id=None,
    )
    supa.session_result = SimpleNamespace(id=uuid.UUID(session_uuid))
    tickets = _FakeTickets(
        id_by_ref={"OPS-1": "ticket-1"},
        ref_by_ticket_id={"ticket-1": "OPS-1"},
        backend_by_ref={"OPS-1": "jira"},
    )
    svc = _make_service(supa, tickets)

    await svc.handle_jira_issue_updated(_closed_payload("OPS-1"))

    assert tickets.transition_to_done_calls == ["OPS-1"]
    # Legacy escalation_mappings/close_escalation must never be touched once
    # legacy writes are stopped.
    assert supa.close_escalation_calls == []
    assert raw.table("escalation_mappings").calls == []
    escalation_row = raw.table("escalations").rows[0]
    assert escalation_row["state"] == "resolved"
    assert escalation_row["resolved_at"] is not None


async def test_handle_jira_comment_finds_nothing_when_ticket_ref_unmapped(monkeypatch):
    """A Jira comment on a ticket ref with no canonical tickets row (or no
    escalation attached to it) must be a no-op, not raise."""
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    supa = _FakeSupabase(_FakeRaw())
    tickets = _FakeTickets()  # id_by_ref empty -> ticket_id resolution misses
    svc = _make_service(supa, tickets)

    payload = {
        "comment": {
            "author": {"emailAddress": "support@example.com", "displayName": "Support"},
            "jsdPublic": True,
            "body": {"content": [{"content": [{"type": "text", "text": "hi"}]}]},
        },
        "issue": {"key": "OPS-404", "fields": {}},
    }

    await svc.handle_jira_comment(payload)  # must not raise


async def test_close_escalation_by_mapping_canonical_skips_notification_on_race(monkeypatch):
    """Two concurrent webhook deliveries for the same closed ticket must not
    both notify the customer -- resolve_if_active's guard makes the second
    call a no-op, mirroring the legacy is_active=True UPDATE guard."""
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    raw = _FakeRaw()
    raw.table("escalations").rows = [
        {"id": "esc-1", "state": "resolved", "resolved_at": "already-closed"}
    ]
    supa = _FakeSupabase(raw)
    svc = _make_service(supa, _FakeTickets())

    notified = []

    async def fake_notify(**kwargs):
        notified.append(kwargs)

    svc.notify_customer_resolved = fake_notify

    await svc.close_escalation_by_mapping(
        mapping={"id": "esc-1", "session_id": "telegram_abc", "customer_chat_id": "111"},
        notify_customer=True,
    )

    assert notified == []


async def test_close_escalation_canonical_resolves_all_non_resolved_escalations_for_session(
    monkeypatch,
):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    session_uuid = str(uuid.uuid4())
    raw = _FakeRaw()
    raw.table("escalations").rows = [
        {"id": "esc-1", "chat_session_id": session_uuid, "state": "open", "resolved_at": None},
        {"id": "esc-2", "chat_session_id": session_uuid, "state": "tracked", "resolved_at": None},
        {
            "id": "esc-other-session",
            "chat_session_id": "other-uuid",
            "state": "open",
            "resolved_at": None,
        },
    ]
    supa = _FakeSupabase(raw)
    supa.session_result = SimpleNamespace(id=uuid.UUID(session_uuid))
    svc = _make_service(supa, _FakeTickets())

    result = await svc.close_escalation("telegram_abc")

    assert result == {"success": True, "message": "Escalation closed"}
    rows_by_id = {r["id"]: r for r in raw.table("escalations").rows}
    assert rows_by_id["esc-1"]["state"] == "resolved"
    assert rows_by_id["esc-2"]["state"] == "resolved"
    # A different session's escalation must be untouched.
    assert rows_by_id["esc-other-session"]["state"] == "open"
    assert supa.close_escalation_calls == []


async def test_close_escalation_canonical_fails_when_session_unresolvable(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    supa = _FakeSupabase(_FakeRaw())
    supa.session_result = None  # unknown session
    svc = _make_service(supa, _FakeTickets())

    result = await svc.close_escalation("telegram_abc")

    assert result["success"] is False
