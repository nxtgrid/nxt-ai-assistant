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
    def __init__(self, claim_row: Optional[Dict[str, Any]]) -> None:
        self._claim_row = claim_row
        self.reactivate_calls: List[str] = []
        self.resolved_at_calls: List[str] = []

    async def claim_escalation_for_tracking(self, mapping_id: str):
        return self._claim_row

    async def reactivate_escalation(self, mapping_id: str):
        self.reactivate_calls.append(mapping_id)

    async def count_active_blocking_escalations(self, _sid):
        return 0

    async def update_session_escalation_status(self, **_k):
        return None

    def _get_client(self):
        class _Table:
            def update(_self, payload):
                return _self

            def eq(_self, *_a, **_k):
                return _self

            def execute(_self):
                return None

        class _Raw:
            def table(_self, _name):
                return _Table()

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


# ---------------------------------------------------------------------------
# Track callback
# ---------------------------------------------------------------------------


async def test_track_callback_reads_ticket_ref_not_jira_ticket_key(monkeypatch):
    """The bug this test guards: track_as_ticket's return key is "ticket_ref"
    (renamed in Task 4). If the handler still reads "jira_ticket_key" this
    raises a KeyError instead of returning a result."""
    monkeypatch.setattr(
        ch, "get_supabase_client", lambda: _FakeSupabase(claim_row={"id": "m1"})
    )

    result = await ch._handle_escalation_track_callback(
        callback_id="cb1",
        mapping_id="00000000-0000-0000-0000-000000000001",
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
    monkeypatch.setattr(
        ch, "get_supabase_client", lambda: _FakeSupabase(claim_row={"id": "m1"})
    )

    await ch._handle_escalation_track_callback(
        callback_id="cb1",
        mapping_id="00000000-0000-0000-0000-000000000001",
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
    )

    edits = _patch_telegram_transport["edit_text"]
    assert edits, "expected the escalation message to be edited"
    assert "TKT-000005" in edits[-1]["text"]
    assert "Tracked as" in edits[-1]["text"]


async def test_track_callback_reactivates_on_failure(monkeypatch):
    monkeypatch.setattr(
        ch, "get_supabase_client", lambda: _FakeSupabase(claim_row={"id": "m1"})
    )

    def _install_failing(*_a, **_k):
        svc = _FakeEscalationService()
        svc.track_as_ticket = AsyncMock(return_value={"success": False, "error": "boom"})
        return svc

    monkeypatch.setattr(
        "orchestrator.services.escalation_service.EscalationService", _install_failing
    )
    supa = _FakeSupabase(claim_row={"id": "m1"})
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    result = await ch._handle_escalation_track_callback(
        callback_id="cb1",
        mapping_id="00000000-0000-0000-0000-000000000001",
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
    )

    assert result["message"] == "Escalation tracking: failed"
    assert supa.reactivate_calls == ["00000000-0000-0000-0000-000000000001"]


# ---------------------------------------------------------------------------
# Close callback — transition-to-done routing
# ---------------------------------------------------------------------------


async def test_close_callback_transitions_internal_ticket_to_done(monkeypatch):
    """Regression: previously this only fired for jira_ticket_key, so an
    internal ticket's status was never set to 'done' on close. Now it must
    route through TicketService via ticket_ref."""
    supa = _FakeSupabase(
        claim_row={
            "id": "m1",
            "session_id": "telegram_abc",
            "ticket_ref": "TKT-000005",
            "jira_ticket_key": None,
            "customer_chat_id": "123",
            "customer_topic_id": None,
        }
    )
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    await ch._handle_escalation_close_callback(
        callback_id="cb1",
        mapping_id="00000000-0000-0000-0000-000000000002",
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
    supa = _FakeSupabase(
        claim_row={
            "id": "m1",
            "session_id": "telegram_abc",
            "ticket_ref": None,
            "jira_ticket_key": "OPS-42",
            "customer_chat_id": "123",
            "customer_topic_id": None,
        }
    )
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    await ch._handle_escalation_close_callback(
        callback_id="cb1",
        mapping_id="00000000-0000-0000-0000-000000000003",
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
        notify_customer=False,
    )

    svc = _FakeEscalationService.instances[-1]
    assert svc._tickets.transition_to_done_calls == ["OPS-42"]


async def test_close_callback_skips_transition_when_no_ticket(monkeypatch):
    supa = _FakeSupabase(
        claim_row={
            "id": "m1",
            "session_id": "telegram_abc",
            "ticket_ref": None,
            "jira_ticket_key": None,
            "customer_chat_id": "123",
            "customer_topic_id": None,
        }
    )
    monkeypatch.setattr(ch, "get_supabase_client", lambda: supa)

    await ch._handle_escalation_close_callback(
        callback_id="cb1",
        mapping_id="00000000-0000-0000-0000-000000000004",
        chat_id="-100999",
        message_id=42,
        original_text="original escalation text",
        notify_customer=False,
    )

    # No EscalationService is even constructed for the transition step when
    # there's no ticket_ref/jira_ticket_key to transition.
    assert all(
        svc._tickets.transition_to_done_calls == []
        for svc in _FakeEscalationService.instances
    )
