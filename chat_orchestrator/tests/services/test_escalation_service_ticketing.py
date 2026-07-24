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
        if self._op == "select":
            return _FakeResponse(self._t.rows_matching(self._filters))
        return _FakeResponse([{"id": self._filters.get("id")}])


class _FakeTable:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.rows = rows or []
        self.calls: List[tuple] = []

    def rows_matching(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [r for r in self.rows if all(r.get(k) == v for k, v in filters.items())]

    def select(self, *_a, **_k) -> _FakeQuery:
        return _FakeQuery(self, "select")

    def update(self, payload: Dict[str, Any]) -> _FakeQuery:
        return _FakeQuery(self, "update", payload)


class _FakeRaw:
    def __init__(self) -> None:
        self.tables: Dict[str, _FakeTable] = {
            "escalation_mappings": _FakeTable(),
            "internal_tickets": _FakeTable(),
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

    async def get_messages(self, **_k):
        return []

    async def get_internal_ticket(self, ref: str):
        return self._internal_rows.get(ref)

    async def count_active_blocking_escalations(self, _sid):
        return 0

    async def update_session_escalation_status(self, **_k):
        return None

    async def save_escalation_mapping(self, **kwargs):
        self.save_calls.append(kwargs)
        return "new-mapping-id"

    async def get_escalation_mapping(self, _msg_id):
        return self.mapping_for_reply

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
    ) -> None:
        self._status = status
        self._by_ref = by_ref or {}
        self.get_status_calls: List[str] = []
        self.add_comment_calls: List[tuple] = []

    async def get_status(self, ref: str):
        self.get_status_calls.append(ref)
        if ref in self._by_ref:
            return self._by_ref[ref]
        return self._status

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        self.add_comment_calls.append((ref, body, public))
        return True


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
    # ... and the legacy jira_ticket_key column was ALSO written (webhook back-compat).
    assert {"jira_ticket_key": "OPS-100"} in payloads

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


async def test_track_as_ticket_dedup_hit_jira_writes_jira_key():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)  # no internal_tickets rows -> recovered ref is Jira
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, dedup="OPS-55")
    internal = _FakeBackend("internal")
    _install_ticket_service(svc, jira, internal)

    result = await svc.track_as_ticket(escalation_mapping=_base_mapping())

    assert result["success"] is True
    assert result["ticket_ref"] == "OPS-55"
    assert result["ticket_backend"] == "jira"
    assert result["ticket_url"] == f"{svc._jira_base_url}/browse/OPS-55"
    assert jira.create_calls == 0  # dedup skipped creation
    assert {"jira_ticket_key": "OPS-55"} in supa.em_update_payloads()


async def test_track_as_ticket_dedup_hit_internal_skips_jira_key():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw, internal_rows={"TKT-9": {"ticket_ref": "TKT-9"}})
    svc = _make_service(supa)
    jira = _FakeBackend("jira", available=True, dedup=None)
    internal = _FakeBackend("internal", dedup="TKT-9")
    _install_ticket_service(svc, jira, internal)

    result = await svc.track_as_ticket(escalation_mapping=_base_mapping())

    assert result == {
        "success": True,
        "ticket_ref": "TKT-9",
        "ticket_backend": "internal",
        "ticket_url": None,
    }
    # Recovered ref is internal -> the legacy jira_ticket_key must stay untouched.
    assert all("jira_ticket_key" not in p for p in supa.em_update_payloads())


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
    assert supa.save_calls, "expected a follow-up mapping to be saved"
    saved = supa.save_calls[-1]
    assert saved["ticket_ref"] == "OPS-77"
    assert saved["ticket_backend"] == "jira"
    assert saved["jira_ticket_key"] == "OPS-77"


async def test_followup_done_ticket_does_not_comment_or_prelink():
    raw = _FakeRaw()
    supa = _FakeSupabase(raw)
    svc = _make_service(supa)

    tickets = await _drive_followup(svc, is_done=True)

    assert tickets.get_status_calls == ["OPS-77"]
    assert tickets.add_comment_calls == []  # parent Done -> no comment
    saved = supa.save_calls[-1]
    assert saved["ticket_ref"] is None
    assert saved["jira_ticket_key"] is None


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
