"""Tests for async_main's "STAFF GROUP + UNKNOWN GROUP PRE-CHECK" block --
the pre-check that, for a message in a registered staff group from a
confirmed staff sender, injects metadata.staff_group_auth so resolve_auth
can grant staff permissions instead of the generic chat-based (grid-owner)
lookup.

Regression test for a str/int type mismatch in the "verify user is staff"
step: it compared auth_svc.get_org_id_for_telegram_user's return value
(always a str -- see auth_service.py's _get_organization_from_telegram_id,
which explicitly does `return str(org_id)`) against _STAFF_ORG_ID (an int
-- see auth_service.py's `STAFF_ORG_ID: int = int(os.getenv(...))`) with
`user_org_id != _STAFF_ORG_ID`. "2" != 2 is always True in Python, so this
comparison could never pass for any user, staff or not: every staff-group
message from a real staff member was misclassified as "non-staff" and
silently dropped. No prior test exercised this branch at all.

_handle_webhook_async is monkeypatched to a recorder so these tests exercise
exactly the pre-check's decision (does it forward staff_group_auth
metadata, or drop the message) without needing to mock the entire
downstream conversation graph.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import handler
import pytest

from orchestrator.services import instructions_provider as ip


def _staff_group_reply_args(chat_id: int = -1009999) -> Dict[str, Any]:
    return {
        "message": {
            "message_id": 1,
            "from": {"id": 111, "username": "staffer", "first_name": "Staffer"},
            "text": "status check",
            "chat": {"id": chat_id, "type": "supergroup", "title": "Internal Ops"},
            "message_thread_id": 7,
            "reply_to_message": {
                "message_id": 900,
                "from": {"is_bot": True, "username": "YourSupportBot"},
                "text": "an earlier bot message",
            },
        },
    }


class _FakeAuthService:
    """Only the one method this pre-check calls; anything else is an error."""

    def __init__(self, org_id_for_user):
        self.get_org_id_for_telegram_user = AsyncMock(return_value=org_id_for_user)


@pytest.fixture(autouse=True)
def _reset_staff_groups():
    ip._staff_groups = {"-1009999": {"name": "Internal Ops", "purposes": ["alerts"]}}
    yield
    ip._staff_groups = {}


@pytest.fixture
def _recorded_calls(monkeypatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    async def fake(normalized_args):
        calls.append(normalized_args)
        return {"success": True, "statusCode": 200}

    monkeypatch.setattr(handler, "_handle_webhook_async", fake)
    return calls


@pytest.mark.asyncio
async def test_staff_user_in_staff_group_is_granted_staff_auth(monkeypatch, _recorded_calls):
    monkeypatch.setattr(handler, "get_auth_service", lambda: _FakeAuthService("2"))
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "YourSupportBot")

    result = await handler.async_main(_staff_group_reply_args())

    assert result == {"success": True, "statusCode": 200}
    assert len(_recorded_calls) == 1
    forwarded_metadata = _recorded_calls[0]["metadata"]
    assert forwarded_metadata["staff_group_auth"] is True
    assert forwarded_metadata["staff_group_organization_id"] == handler._STAFF_ORG_ID


@pytest.mark.asyncio
async def test_non_staff_user_in_staff_group_is_ignored(monkeypatch, _recorded_calls):
    monkeypatch.setattr(handler, "get_auth_service", lambda: _FakeAuthService("7"))
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "YourSupportBot")

    result = await handler.async_main(_staff_group_reply_args())

    assert result["message"] == "Ignored (non-staff in staff group)"
    assert _recorded_calls == []


@pytest.mark.asyncio
async def test_unknown_telegram_user_in_staff_group_is_ignored(monkeypatch, _recorded_calls):
    monkeypatch.setattr(handler, "get_auth_service", lambda: _FakeAuthService(None))
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "YourSupportBot")

    result = await handler.async_main(_staff_group_reply_args())

    assert result["message"] == "Ignored (non-staff in staff group)"
    assert _recorded_calls == []
