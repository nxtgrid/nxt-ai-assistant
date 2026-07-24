"""Regression tests for Task 6 (Jira-optional ticket backend plan): the
/chat/notify endpoint's bidirectional ticketing (ticket_id + close).

Covers the plan's explicit test list: create (blank ticket_id) returns a
ref, comment (populated ticket_id) is appended, close transitions the
ticket, an unknown ref 404s, and the no-ticket_id passthrough path is
byte-identical to today's behavior.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import BackgroundTasks

from orchestrator.api.app import NotifyRequest, _resolve_notify_ticket, handle_notify
from orchestrator.services.ticketing.backend import TicketBackendError, TicketResult, TicketStatus
from shared.auth.auth_service import GridNotificationTarget


class _FakeTicketService:
    """Stands in for TicketService as constructed fresh inside
    _resolve_notify_ticket (`from ...ticketing.service import TicketService`)."""

    instances: List["_FakeTicketService"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.create_ticket_calls: List[tuple] = []
        self.add_comment_calls: List[tuple] = []
        self.transition_to_done_calls: List[str] = []
        self.get_status_return: Optional[TicketStatus] = TicketStatus(
            summary="s", is_done=False
        )
        self.create_result = TicketResult(ref="TKT-000001", backend="internal", url=None)
        self.create_error: Optional[Exception] = None
        _FakeTicketService.instances.append(self)

    async def create_ticket(self, req, backend_override=None):
        self.create_ticket_calls.append((req, backend_override))
        if self.create_error:
            raise self.create_error
        return self.create_result

    async def get_status(self, ref: str):
        return self.get_status_return

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        self.add_comment_calls.append((ref, body, public))
        return True

    async def transition_to_done(self, ref: str) -> None:
        self.transition_to_done_calls.append(ref)


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    _FakeTicketService.instances = []
    yield
    _FakeTicketService.instances = []


@pytest.fixture(autouse=True)
def _patch_ticket_service(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.services.ticketing.service.TicketService", _FakeTicketService
    )


def _target() -> GridNotificationTarget:
    return GridNotificationTarget(
        grid_name="Acme Grid", chat_id="-100555", topic_id="42", was_fuzzy=False
    )


def _notify_body(**overrides: Any) -> NotifyRequest:
    defaults: Dict[str, Any] = dict(source="grafana", grid_name="Acme Grid", text="Meter offline")
    defaults.update(overrides)
    return NotifyRequest(**defaults)


# ---------------------------------------------------------------------------
# _resolve_notify_ticket — the core new logic
# ---------------------------------------------------------------------------


async def test_ticket_id_omitted_is_pure_passthrough():
    body = _notify_body(ticket_id=None)

    ref, error = await _resolve_notify_ticket(body, _target())

    assert ref is None
    assert error is None
    assert _FakeTicketService.instances == []  # no TicketService even constructed


async def test_blank_ticket_id_creates_ticket_and_returns_ref():
    body = _notify_body(ticket_id="")

    ref, error = await _resolve_notify_ticket(body, _target())

    assert error is None
    assert ref == "TKT-000001"
    svc = _FakeTicketService.instances[-1]
    assert len(svc.create_ticket_calls) == 1
    req, backend_override = svc.create_ticket_calls[0]
    assert req.summary == "Meter offline"
    assert req.description == "Meter offline"
    assert req.grid_name == "Acme Grid"
    assert req.source == "notify"
    assert backend_override == "internal"  # NOTIFY_TICKETS_BACKEND default


async def test_blank_ticket_id_uses_first_line_as_summary():
    body = _notify_body(ticket_id="", text="Meter offline\n\nFull details below...")

    await _resolve_notify_ticket(body, _target())

    svc = _FakeTicketService.instances[-1]
    req, _ = svc.create_ticket_calls[0]
    assert req.summary == "Meter offline"
    assert req.description == "Meter offline\n\nFull details below..."


async def test_blank_ticket_id_creation_failure_returns_500(monkeypatch):
    body = _notify_body(ticket_id="")

    async def _boom(self, req, backend_override=None):
        raise TicketBackendError("both backends down")

    monkeypatch.setattr(_FakeTicketService, "create_ticket", _boom)
    ref, error = await _resolve_notify_ticket(body, _target())

    assert ref is None
    assert error is not None
    assert error.status_code == 500


async def test_populated_ticket_id_appends_comment():
    body = _notify_body(ticket_id="OPS-42")

    ref, error = await _resolve_notify_ticket(body, _target())

    assert error is None
    assert ref == "OPS-42"
    svc = _FakeTicketService.instances[-1]
    assert svc.add_comment_calls == [("OPS-42", "Meter offline", False)]
    assert svc.transition_to_done_calls == []


async def test_populated_ticket_id_with_close_transitions_to_done():
    body = _notify_body(ticket_id="OPS-42", close=True)

    ref, error = await _resolve_notify_ticket(body, _target())

    assert error is None
    assert ref == "OPS-42"
    svc = _FakeTicketService.instances[-1]
    assert svc.add_comment_calls == [("OPS-42", "Meter offline", False)]
    assert svc.transition_to_done_calls == ["OPS-42"]


async def test_close_without_ticket_id_is_ignored():
    """close=True with no ticket_id at all is a no-op -- passthrough behavior,
    same as omitting ticket_id."""
    body = _notify_body(ticket_id=None, close=True)

    ref, error = await _resolve_notify_ticket(body, _target())

    assert ref is None
    assert error is None
    assert _FakeTicketService.instances == []


async def test_unknown_ticket_id_returns_404(monkeypatch):
    body = _notify_body(ticket_id="OPS-999")

    async def _not_found(self, ref):
        return None

    monkeypatch.setattr(_FakeTicketService, "get_status", _not_found)
    ref, error = await _resolve_notify_ticket(body, _target())

    assert ref is None
    assert error is not None
    assert error.status_code == 404
    # Unknown ref: no comment/close attempted.
    svc = _FakeTicketService.instances[-1]
    assert svc.add_comment_calls == []
    assert svc.transition_to_done_calls == []


# ---------------------------------------------------------------------------
# handle_notify — end-to-end wiring (auth/gating + response shape)
# ---------------------------------------------------------------------------


class _FakeAuthService:
    def __init__(self, target: Optional[GridNotificationTarget]) -> None:
        self._target = target

    async def resolve_grid_notification_target(self, _grid_name: str):
        return self._target


class _FakeRequest:
    def __init__(self, headers: Dict[str, str]) -> None:
        self.headers = headers


@pytest.fixture(autouse=True)
def _notify_env(monkeypatch):
    monkeypatch.setenv("NOTIFY_SHARED_SECRET", "test-secret")
    monkeypatch.setenv("NOTIFY_ENDPOINT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")


async def test_handle_notify_passthrough_response_byte_identical(monkeypatch):
    """No ticket_id -> the response body must be exactly {"ok": True}, same
    as before this task -- no ticket_ref key, no other additions."""
    monkeypatch.setattr(
        "shared.auth.get_auth_service", lambda: _FakeAuthService(_target())
    )
    request = _FakeRequest(headers={"X-Notify-Secret": "test-secret"})
    body = _notify_body(ticket_id=None)
    background_tasks = BackgroundTasks()

    response = await handle_notify(request, body, background_tasks)  # type: ignore[arg-type]

    assert response.status_code == 202
    import json

    assert json.loads(response.body) == {"ok": True}
    assert len(background_tasks.tasks) == 1


async def test_handle_notify_create_ticket_returns_ref_in_response(monkeypatch):
    monkeypatch.setattr(
        "shared.auth.get_auth_service", lambda: _FakeAuthService(_target())
    )
    request = _FakeRequest(headers={"X-Notify-Secret": "test-secret"})
    body = _notify_body(ticket_id="")
    background_tasks = BackgroundTasks()

    response = await handle_notify(request, body, background_tasks)  # type: ignore[arg-type]

    assert response.status_code == 202
    import json

    content = json.loads(response.body)
    assert content == {"ok": True, "ticket_ref": "TKT-000001"}


async def test_handle_notify_unknown_ticket_returns_404_before_scheduling_delivery(monkeypatch):
    monkeypatch.setattr(
        "shared.auth.get_auth_service", lambda: _FakeAuthService(_target())
    )
    request = _FakeRequest(headers={"X-Notify-Secret": "test-secret"})
    body = _notify_body(ticket_id="OPS-999")
    background_tasks = BackgroundTasks()

    async def _not_found(self, ref):
        return None

    monkeypatch.setattr(_FakeTicketService, "get_status", _not_found)
    response = await handle_notify(request, body, background_tasks)  # type: ignore[arg-type]

    assert response.status_code == 404
    # The alert must not be delivered when the ticket ref is unresolvable.
    assert len(background_tasks.tasks) == 0
