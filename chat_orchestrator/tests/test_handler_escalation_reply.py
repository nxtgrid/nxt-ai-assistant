"""Tests for _handle_escalation_reply's Reopen/Closed reply commands.

Covers the switch from calling supabase_client.get_escalation_mapping()
directly to escalation_service.resolve_escalation_by_message_id() (the
canonical-first, flag-gated resolver already used by handle_support_reply --
see escalation_service.py), and the Close flow's Jira-transition gate, which
moved from reading the legacy jira_ticket_key field to checking
ticket_backend == "jira" explicitly (both resolution paths carry
ticket_backend; only the legacy row happened to also carry jira_ticket_key).

No prior coverage existed for this function at all -- _handle_escalation_reply
constructs EscalationService fresh from env vars
(`from orchestrator.services.escalation_service import EscalationService`),
so tests patch that import at its source module, mirroring the pattern
already established in tests/services/test_callback_handlers_ticketing.py
for the same construction style.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import handler
import pytest


class _FakeEscalationService:
    """Stands in for EscalationService() as constructed fresh inside
    _handle_escalation_reply. Return values are configured via class
    attributes *before* the call, since the instance itself only exists once
    the function under test constructs one."""

    instances: List["_FakeEscalationService"] = []

    mapping_result: Optional[Dict[str, Any]] = None
    reopen_result: Dict[str, Any] = {"success": True}
    close_result: Dict[str, Any] = {"success": True}
    support_reply_result: Dict[str, Any] = {"success": True}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        cls = type(self)
        self._escalation_chat_id = "-100999"
        self.resolve_escalation_by_message_id = AsyncMock(return_value=cls.mapping_result)
        self.reopen_escalation = AsyncMock(return_value=cls.reopen_result)
        self.close_escalation = AsyncMock(return_value=cls.close_result)
        self.handle_support_reply = AsyncMock(return_value=cls.support_reply_result)
        self._send_telegram_reply = AsyncMock(return_value={"ok": True})
        self._send_telegram_message = AsyncMock(return_value={"ok": True})
        self._transition_jira_to_done = AsyncMock(return_value=None)
        _FakeEscalationService.instances.append(self)


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    _FakeEscalationService.instances = []
    _FakeEscalationService.mapping_result = None
    _FakeEscalationService.reopen_result = {"success": True}
    _FakeEscalationService.close_result = {"success": True}
    _FakeEscalationService.support_reply_result = {"success": True}
    monkeypatch.setattr(
        "orchestrator.services.escalation_service.EscalationService", _FakeEscalationService
    )
    yield
    _FakeEscalationService.instances = []


@pytest.fixture(autouse=True)
def _patch_remove_buttons(monkeypatch):
    calls: List[Dict[str, Any]] = []

    async def fake_remove_buttons(chat_id, message_id, topic_id=None):
        calls.append({"chat_id": chat_id, "message_id": message_id, "topic_id": topic_id})

    monkeypatch.setattr(handler, "_edit_message_remove_buttons", fake_remove_buttons)
    return calls


def _telegram_msg(text: str, reply_to_message_id: int = 555) -> Dict[str, Any]:
    return {
        "reply_to_message": {"message_id": reply_to_message_id},
        "text": text,
        "from": {"first_name": "Staff"},
    }


# ---------------------------------------------------------------------------
# Reopen
# ---------------------------------------------------------------------------


async def test_reopen_resolves_via_canonical_resolver_not_legacy_client():
    """The bug this guards: reverting to supabase_client.get_escalation_mapping
    would bypass the flag-gated canonical/legacy fallback entirely."""
    _FakeEscalationService.mapping_result = {
        "session_id": "telegram_abc",
        "escalation_topic_id": "9",
    }

    result = await handler._handle_escalation_reply(_telegram_msg("Reopen", 555))

    svc = _FakeEscalationService.instances[-1]
    svc.resolve_escalation_by_message_id.assert_awaited_once_with(555)
    svc.reopen_escalation.assert_awaited_once_with("telegram_abc", 555)
    assert result["success"] is True
    assert result["message"] == "Reopen command processed"


async def test_reopen_sends_success_reply_when_reopen_succeeds():
    _FakeEscalationService.mapping_result = {
        "session_id": "telegram_abc",
        "escalation_topic_id": "9",
    }
    _FakeEscalationService.reopen_result = {"success": True}

    await handler._handle_escalation_reply(_telegram_msg("reopened", 555))

    svc = _FakeEscalationService.instances[-1]
    svc._send_telegram_reply.assert_awaited_once()
    kwargs = svc._send_telegram_reply.call_args.kwargs
    assert "reopened" in kwargs["text"].lower()
    assert kwargs["topic_id"] == "9"


async def test_reopen_with_no_mapping_found_does_not_crash():
    _FakeEscalationService.mapping_result = None

    result = await handler._handle_escalation_reply(_telegram_msg("re open", 555))

    svc = _FakeEscalationService.instances[-1]
    svc.reopen_escalation.assert_not_awaited()
    assert result["success"] is True
    assert result["message"] == "Reopen command processed"


# ---------------------------------------------------------------------------
# Close -- including the Jira-transition gate fix
# ---------------------------------------------------------------------------


async def test_close_transitions_jira_when_backend_is_jira():
    """The fix this guards: gating on ticket_backend == "jira" (both
    resolution paths carry it) instead of the legacy-only jira_ticket_key
    field, which the canonical resolver never populated."""
    _FakeEscalationService.mapping_result = {
        "session_id": "telegram_abc",
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "escalation_topic_id": "9",
        "ticket_ref": "OPS-77",
        "ticket_backend": "jira",
    }

    result = await handler._handle_escalation_reply(_telegram_msg("Closed", 555))

    svc = _FakeEscalationService.instances[-1]
    svc.close_escalation.assert_awaited_once_with("telegram_abc")
    svc._transition_jira_to_done.assert_awaited_once_with("OPS-77")
    assert result["success"] is True
    assert result["message"] == "Escalation closed"


async def test_close_skips_jira_transition_when_backend_is_internal():
    _FakeEscalationService.mapping_result = {
        "session_id": "telegram_abc",
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "escalation_topic_id": "9",
        "ticket_ref": "TKT-000005",
        "ticket_backend": "internal",
    }

    await handler._handle_escalation_reply(_telegram_msg("close", 555))

    svc = _FakeEscalationService.instances[-1]
    svc._transition_jira_to_done.assert_not_awaited()


async def test_close_skips_jira_transition_when_no_ticket_at_all():
    _FakeEscalationService.mapping_result = {
        "session_id": "telegram_abc",
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "escalation_topic_id": "9",
        "ticket_ref": None,
        "ticket_backend": None,
    }

    await handler._handle_escalation_reply(_telegram_msg("close", 555))

    svc = _FakeEscalationService.instances[-1]
    svc._transition_jira_to_done.assert_not_awaited()


async def test_close_removes_buttons_regardless_of_ticket_backend(_patch_remove_buttons):
    _FakeEscalationService.mapping_result = {
        "session_id": "telegram_abc",
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "escalation_topic_id": "9",
        "ticket_ref": None,
        "ticket_backend": None,
    }

    await handler._handle_escalation_reply(_telegram_msg("close", 555))

    assert _patch_remove_buttons == [
        {"chat_id": "-100999", "message_id": 555, "topic_id": "9"}
    ]


async def test_close_with_no_mapping_found_does_not_crash():
    _FakeEscalationService.mapping_result = None

    result = await handler._handle_escalation_reply(_telegram_msg("closed", 555))

    svc = _FakeEscalationService.instances[-1]
    svc.close_escalation.assert_not_awaited()
    assert result["success"] is True
    assert result["message"] == "Closed command processed"


async def test_close_reports_db_failure_without_touching_jira_or_buttons(_patch_remove_buttons):
    _FakeEscalationService.mapping_result = {
        "session_id": "telegram_abc",
        "customer_chat_id": "123",
        "customer_topic_id": None,
        "escalation_topic_id": "9",
        "ticket_ref": "OPS-77",
        "ticket_backend": "jira",
    }
    _FakeEscalationService.close_result = {"success": False}

    result = await handler._handle_escalation_reply(_telegram_msg("close", 555))

    svc = _FakeEscalationService.instances[-1]
    svc._transition_jira_to_done.assert_not_awaited()
    assert _patch_remove_buttons == []
    assert result["success"] is False
    assert result["statusCode"] == 500


# ---------------------------------------------------------------------------
# Neither Reopen nor Close -- falls through to the normal reply-forward path
# ---------------------------------------------------------------------------


async def test_non_command_reply_still_forwards_via_handle_support_reply():
    result = await handler._handle_escalation_reply(
        _telegram_msg("Please try power-cycling the inverter.", 555)
    )

    svc = _FakeEscalationService.instances[-1]
    svc.resolve_escalation_by_message_id.assert_not_awaited()
    svc.handle_support_reply.assert_awaited_once_with(
        reply_to_message_id=555,
        reply_text="Please try power-cycling the inverter.",
        from_username="Staff",
    )
    assert result["success"] is True
