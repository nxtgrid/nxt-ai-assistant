"""handle_jira_issue_updated: closing a Jira ticket must also close the
canonical ticket record, not just the escalation_mappings/session bookkeeping.

Before this, the webhook closed the Telegram escalation session but left the
canonical ``tickets`` row (what the internal tickets admin page reads) stuck
at status="open" forever, since jira_backend.transition_to_done() only calls
the Jira API and has no repository reference of its own.
"""

from __future__ import annotations

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

    def limit(self, *_a, **_k) -> "_FakeQuery":
        return self

    def execute(self) -> _FakeResponse:
        self._t.calls.append((self._op, dict(self._filters), self._payload))
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
        return [r for r in self.rows if all(r.get(k) == v for k, v in filters.items())]

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

    def _get_client(self) -> _FakeRaw:
        return self._raw

    async def get_escalation_mapping_by_jira_key(self, jira_ticket_key: str):
        return self._mapping_by_jira_key.get(jira_ticket_key)

    async def close_escalation(self, session_id: str) -> bool:
        self.close_escalation_calls.append(session_id)
        return True


class _FakeTickets:
    def __init__(self, transition_error: Optional[Exception] = None) -> None:
        self.transition_to_done_calls: List[str] = []
        self._transition_error = transition_error

    async def transition_to_done(self, ref: str) -> None:
        self.transition_to_done_calls.append(ref)
        if self._transition_error is not None:
            raise self._transition_error


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
