"""Regression tests for Task 5 (Jira-optional ticket backend plan): the staff
Track/Close Telegram buttons in callback_handlers.py.

Covers the bug track_as_ticket's Task-4 return-key rename introduced --
_handle_escalation_track_callback previously read result["jira_ticket_key"],
which would KeyError against the renamed result["ticket_ref"] -- and the
close-path's transition-to-done call, which previously only fired for
Jira-backed tickets (jira_ticket_key check) and silently never marked an
internal ticket done on close.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from orchestrator.services import callback_handlers as ch


class _FakeSupabase:
    def __init__(
        self,
        claim_row: Optional[Dict[str, Any]],
        escalations_state: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self._claim_row = claim_row
        self.reactivate_calls: List[str] = []
        self.resolved_at_calls: List[str] = []
        # (table_name, payload, [(col, val), ...]) for every .update(...).eq(...).execute()
        # issued through _get_client() -- covers both the legacy resolved_at
        # write and EscalationRepository's canonical resolve()/release() calls.
        self.canonical_calls: List[tuple] = []
        # In-memory canonical `escalations` rows, keyed by id. Gives
        # EscalationRepository's conditional updates (claim/resolve/release/
        # reopen) real match/no-match semantics instead of always touching
        # zero rows.
        self.escalations_state: Dict[str, Dict[str, Any]] = escalations_state or {}
        self.count_active_blocking_escalations_calls: List[str] = []
        self.update_session_escalation_status_calls: List[Dict[str, Any]] = []
        self.claim_escalation_for_tracking_calls: List[str] = []

    async def claim_escalation_for_tracking(self, mapping_id: str):
        self.claim_escalation_for_tracking_calls.append(mapping_id)
        return self._claim_row

    async def reactivate_escalation(self, mapping_id: str):
        self.reactivate_calls.append(mapping_id)

    async def count_active_blocking_escalations(self, session_id):
        self.count_active_blocking_escalations_calls.append(session_id)
        return 0

    async def update_session_escalation_status(self, **kwargs):
        self.update_session_escalation_status_calls.append(kwargs)
        return None

    def _get_client(self):
        supa = self

        class _Response:
            def __init__(self, data):
                self.data = data

        class _Query:
            def __init__(self, table_name: str, payload: Dict[str, Any]):
                self._table_name = table_name
                self._payload = payload
                self._filters: List[tuple] = []

            def update(self, payload):
                self._payload = payload
                return self

            def eq(self, col, val):
                self._filters.append((col, val))
                return self

            def execute(self):
                supa.canonical_calls.append(
                    (self._table_name, self._payload, list(self._filters))
                )
                if self._table_name != "escalations":
                    return _Response(None)
                def _matches(eid: str, row: Dict[str, Any]) -> bool:
                    for col, val in self._filters:
                        actual = eid if col == "id" else row.get(col)
                        if actual != val:
                            return False
                    return True

                matched_ids = [
                    eid for eid, row in supa.escalations_state.items() if _matches(eid, row)
                ]
                updated = []
                for eid in matched_ids:
                    supa.escalations_state[eid].update(self._payload)
                    updated.append({"id": eid, **supa.escalations_state[eid]})
                return _Response(updated)

        class _Table:
            def __init__(self, name: str):
                self._name = name

            def update(self, payload):
                return _Query(self._name, payload)

        class _Raw:
            def table(_self, name):
                return _Table(name)

        return _Raw()


class _FakeTickets:
    def __init__(self) -> None:
        self.transition_to_done_calls: List[str] = []

    async def transition_to_done(self, ref: str) -> None:
        self.transition_to_done_calls.append(ref)


class _FakeEscalationService:
    """Stands in for EscalationService() as constructed fresh inside the
    callback handlers (`from ...escalation_service import EscalationService`)."""

    instances: List["_FakeEscalationService"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.track_as_ticket = AsyncMock(
            return_value={"success": True, "ticket_ref": "TKT-000005", "ticket_backend": "internal", "ticket_url": None}
        )
        self.notify_customer_resolved = AsyncMock(return_value=None)
        self.get_escalation_by_id_canonical = AsyncMock(
            return_value={"id": "m1", "session_id": "telegram_abc", "customer_chat_id": "123"}
        )
        self._tickets = _FakeTickets()
        _FakeEscalationService.instances.append(self)


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    _FakeEscalationService.instances = []
    yield
    _FakeEscalationService.instances = []


@pytest.fixture(autouse=True)
def _patch_telegram_transport(monkeypatch):
    """Every callback handler answers the toast and edits/removes buttons on
    the Telegram message -- stub all of it so tests run with no network I/O."""
    calls: Dict[str, List[Any]] = {"answer": [], "edit_text": [], "remove_buttons": []}

    async def fake_answer(callback_id, text, show_alert: bool = False):
        calls["answer"].append({"text": text, "show_alert": show_alert})

    async def fake_edit_text(chat_id, message_id, text, reply_markup=None):
        calls["edit_text"].append({"text": text})
        return {"ok": True}

    async def fake_remove_buttons(chat_id, message_id):
        calls["remove_buttons"].append(message_id)

    monkeypatch.setattr(ch, "_answer_callback_query", fake_answer)
    monkeypatch.setattr(ch, "_edit_message_text", fake_edit_text)
    monkeypatch.setattr(ch, "_edit_message_remove_buttons", fake_remove_buttons)
    return calls


@pytest.fixture(autouse=True)
def _patch_escalation_service(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.services.escalation_service.EscalationService", _FakeEscalationService
    )


@pytest.fixture(autouse=True)
def _escalation_group_env(monkeypatch):
    monkeypatch.setenv("ESCALATION_TELEGRAM_CHAT_ID", "-100999")


def _install_escalation_service_returning(monkeypatch, escalation: Dict[str, Any]) -> None:
    """Every EscalationService() constructed for the rest of this test
    resolves get_escalation_by_id_canonical to `escalation` instead of
    _FakeEscalationService's fixed default -- needed for tests that check
    behavior keyed off escalation content (ticket_ref/jira_ticket_key) the
    default doesn't carry."""

    def _factory(*_a, **_k):
        svc = _FakeEscalationService()
        svc.get_escalation_by_id_canonical = AsyncMock(return_value=escalation)
        return svc

    monkeypatch.setattr(
        "orchestrator.services.escalation_service.EscalationService", _factory
    )


# ---------------------------------------------------------------------------
# Track callback
# ---------------------------------------------------------------------------


async def test_track_callback_reads_ticket_ref_not_jira_ticket_key(monkeypatch):
    """The bug this test guards: track_as_ticket's return key is "ticket_ref"
    (renamed in Task 4). If the handler still reads "jira_ticket_key" this
    raises a KeyError instead of returning a result."""
    mapping_id = "00000000-0000-0000-0000-000000000001"
    supa = _FakeSupabase(claim_row=None, escalations_state={mapping_id: {"state": "open"}})
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    result = await ch._handle_escalation_track_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
    )

    assert result["success"] is True
    assert result["statusCode"] == 200
    assert "TKT-000005" in result["message"]
    svc = _FakeEscalationService.instances[-1]
    svc.track_as_ticket.assert_awaited_once()


async def test_track_callback_edits_message_with_ticket_ref(_patch_telegram_transport, monkeypatch):
    mapping_id = "00000000-0000-0000-0000-000000000001"
    supa = _FakeSupabase(claim_row=None, escalations_state={mapping_id: {"state": "open"}})
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    await ch._handle_escalation_track_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
    )

    edits = _patch_telegram_transport["edit_text"]
    assert edits, "expected the escalation message to be edited"
    assert "TKT-000005" in edits[-1]["text"]
    assert "Tracked as" in edits[-1]["text"]


async def test_track_callback_uses_canonical_claim_once_legacy_writes_stopped(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    mapping_id = "00000000-0000-0000-0000-000000000001"
    supa = _FakeSupabase(
        claim_row=None,  # legacy claim must never be consulted
        escalations_state={mapping_id: {"state": "open"}},
    )
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    result = await ch._handle_escalation_track_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
    )

    assert result["success"] is True
    assert "TKT-000005" in result["message"]
    assert supa.claim_escalation_for_tracking_calls == []
    assert supa.escalations_state[mapping_id]["state"] == "processing"
    svc = _FakeEscalationService.instances[-1]
    svc.get_escalation_by_id_canonical.assert_awaited_once_with(mapping_id)
    svc.track_as_ticket.assert_awaited_once()


async def test_track_callback_canonical_claim_fails_when_already_processing(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    mapping_id = "00000000-0000-0000-0000-000000000001"
    supa = _FakeSupabase(
        claim_row=None,
        escalations_state={mapping_id: {"state": "processing"}},  # already claimed
    )
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    result = await ch._handle_escalation_track_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
    )

    assert result["message"] == "Already claimed"
    svc = _FakeEscalationService.instances[-1]
    svc.track_as_ticket.assert_not_awaited()
    svc.get_escalation_by_id_canonical.assert_not_awaited()


async def test_track_callback_releases_canonical_claim_on_failure_once_legacy_writes_stopped(
    monkeypatch,
):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    mapping_id = "00000000-0000-0000-0000-000000000001"
    supa = _FakeSupabase(
        claim_row=None,
        escalations_state={mapping_id: {"state": "open"}},
    )
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    def _install_failing(*_a, **_k):
        svc = _FakeEscalationService()
        svc.track_as_ticket = AsyncMock(return_value={"success": False, "error": "boom"})
        return svc

    monkeypatch.setattr(
        "orchestrator.services.escalation_service.EscalationService", _install_failing
    )

    result = await ch._handle_escalation_track_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
    )

    assert result["message"] == "Escalation tracking: failed"
    assert supa.reactivate_calls == []  # legacy reactivate skipped
    assert supa.escalations_state[mapping_id]["state"] == "open"  # released back


# ---------------------------------------------------------------------------
# Close callback — transition-to-done routing
# ---------------------------------------------------------------------------


async def test_close_callback_resolves_canonical_escalation(monkeypatch):
    """Regression: a close that never goes through track_as_ticket (no ticket
    filed) is the only escalation lifecycle exit that had zero canonical
    dual-write -- without this, escalations.state stays stuck at
    open/processing forever for a silently- or notify-closed escalation."""
    mapping_id = "00000000-0000-0000-0000-000000000009"
    supa = _FakeSupabase(claim_row=None, escalations_state={mapping_id: {"state": "open"}})
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    await ch._handle_escalation_close_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
        notify_customer=False,
    )

    resolve_calls = [
        c for c in supa.canonical_calls if c[0] == "escalations" and c[1].get("state") == "resolved"
    ]
    assert len(resolve_calls) == 1
    _table, payload, filters = resolve_calls[0]
    assert payload["state"] == "resolved"
    assert "resolved_at" in payload
    assert filters == [("id", "00000000-0000-0000-0000-000000000009")]


async def test_close_callback_uses_canonical_claim_once_legacy_writes_stopped(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    mapping_id = "00000000-0000-0000-0000-000000000009"
    supa = _FakeSupabase(
        claim_row=None,  # legacy claim must never be consulted
        escalations_state={mapping_id: {"state": "open"}},
    )
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    result = await ch._handle_escalation_close_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
        notify_customer=False,
    )

    assert result["success"] is True
    assert supa.claim_escalation_for_tracking_calls == []
    assert supa.escalations_state[mapping_id]["state"] == "resolved"

    # Legacy resolved_at write skipped entirely -- no escalation_mappings call at all.
    assert all(c[0] != "escalation_mappings" for c in supa.canonical_calls)

    # Legacy session-status bookkeeping skipped too (nothing left to release
    # once chat_sessions.is_escalated is no longer read).
    assert supa.count_active_blocking_escalations_calls == []
    assert supa.update_session_escalation_status_calls == []


async def test_close_callback_canonical_claim_fails_when_already_processing(monkeypatch):
    monkeypatch.setenv("STOP_LEGACY_ESCALATION_WRITES", "true")
    mapping_id = "00000000-0000-0000-0000-000000000009"
    supa = _FakeSupabase(
        claim_row=None,
        escalations_state={mapping_id: {"state": "processing"}},  # already claimed
    )
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    result = await ch._handle_escalation_close_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
        notify_customer=False,
    )

    assert result["message"] == "Already claimed"
    assert supa.escalations_state[mapping_id]["state"] == "processing"  # untouched


async def test_close_callback_transitions_internal_ticket_to_done(monkeypatch):
    """Regression: previously this only fired for jira_ticket_key, so an
    internal ticket's status was never set to 'done' on close. Now it must
    route through TicketService via ticket_ref."""
    mapping_id = "00000000-0000-0000-0000-000000000002"
    supa = _FakeSupabase(claim_row=None, escalations_state={mapping_id: {"state": "open"}})
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)
    _install_escalation_service_returning(
        monkeypatch,
        {
            "id": mapping_id,
            "session_id": "telegram_abc",
            "ticket_ref": "TKT-000005",
            "jira_ticket_key": None,
            "customer_chat_id": "123",
            "customer_topic_id": None,
        },
    )

    await ch._handle_escalation_close_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
        notify_customer=False,
    )

    svc = _FakeEscalationService.instances[-1]
    assert svc._tickets.transition_to_done_calls == ["TKT-000005"]


async def test_close_callback_transitions_jira_ticket_to_done_via_legacy_fallback(monkeypatch):
    """A legacy/pre-migration row with only jira_ticket_key (no ticket_ref)
    must still route through TicketService (which correctly dispatches to
    the Jira backend for a Jira-shaped ref)."""
    mapping_id = "00000000-0000-0000-0000-000000000003"
    supa = _FakeSupabase(claim_row=None, escalations_state={mapping_id: {"state": "open"}})
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)
    _install_escalation_service_returning(
        monkeypatch,
        {
            "id": mapping_id,
            "session_id": "telegram_abc",
            "ticket_ref": None,
            "jira_ticket_key": "OPS-42",
            "customer_chat_id": "123",
            "customer_topic_id": None,
        },
    )

    await ch._handle_escalation_close_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
        notify_customer=False,
    )

    svc = _FakeEscalationService.instances[-1]
    assert svc._tickets.transition_to_done_calls == ["OPS-42"]


async def test_close_callback_skips_transition_when_no_ticket(monkeypatch):
    mapping_id = "00000000-0000-0000-0000-000000000004"
    supa = _FakeSupabase(claim_row=None, escalations_state={mapping_id: {"state": "open"}})
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)
    _install_escalation_service_returning(
        monkeypatch,
        {
            "id": mapping_id,
            "session_id": "telegram_abc",
            "ticket_ref": None,
            "jira_ticket_key": None,
            "customer_chat_id": "123",
            "customer_topic_id": None,
        },
    )

    await ch._handle_escalation_close_callback(
        callback_id="cb1",
        mapping_id=mapping_id,
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
        notify_customer=False,
    )

    # No second EscalationService is even constructed for the transition step
    # when there's no ticket_ref/jira_ticket_key to transition.
    assert all(
        svc._tickets.transition_to_done_calls == []
        for svc in _FakeEscalationService.instances
    )
