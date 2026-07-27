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

from orchestrator.api.app import (
    NotifyRequest,
    _resolve_notify_ticket,
    _resolve_notify_ticket_full,
    handle_notify,
)
from orchestrator.services.ticketing.backend import TicketBackendError, TicketResult, TicketStatus
from orchestrator.services.ticketing.correlation_render import AmendmentResult
from orchestrator.services.ticketing.correlator import CorrelationDecision
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


async def test_blank_ticket_id_ignores_close_flag():
    """close=True is only meaningful for the populated-ticket_id (comment) branch
    -- a freshly created ticket is never auto-closed."""
    body = _notify_body(ticket_id="", close=True)

    ref, error = await _resolve_notify_ticket(body, _target())

    assert error is None
    assert ref == "TKT-000001"
    svc = _FakeTicketService.instances[-1]
    assert svc.transition_to_done_calls == []


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


# ---------------------------------------------------------------------------
# ticket_id="auto" -- smart alert correlation
# ---------------------------------------------------------------------------


class _FakeCorrelator:
    """Stands in for AlertCorrelator, constructed fresh inside
    _resolve_notify_ticket_auto (`from ...ticketing.correlator import AlertCorrelator`)."""

    instances: List["_FakeCorrelator"] = []
    decision_to_return: Optional[CorrelationDecision] = None
    raise_error: Optional[Exception] = None

    def __init__(self, store=None, ticket_service=None, **kwargs) -> None:
        self.store = store
        self.ticket_service = ticket_service
        self.decide_calls: List[tuple] = []
        _FakeCorrelator.instances.append(self)

    async def decide(self, grid_name, alert, dedup_key=None, backend_override=None):
        self.decide_calls.append((grid_name, alert, dedup_key, backend_override))
        if _FakeCorrelator.raise_error:
            raise _FakeCorrelator.raise_error
        return _FakeCorrelator.decision_to_return


@pytest.fixture(autouse=True)
def _reset_fake_correlator():
    _FakeCorrelator.instances = []
    _FakeCorrelator.decision_to_return = None
    _FakeCorrelator.raise_error = None
    yield
    _FakeCorrelator.instances = []
    _FakeCorrelator.decision_to_return = None
    _FakeCorrelator.raise_error = None


@pytest.fixture(autouse=True)
def _patch_correlator(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.services.ticketing.correlator.AlertCorrelator", _FakeCorrelator
    )


@pytest.fixture
def fake_apply_amendment(monkeypatch):
    calls: List[Dict[str, Any]] = []
    result_holder: Dict[str, Optional[AmendmentResult]] = {"result": None}

    async def _fake(*, store, ticket_service, ticket_ref, alert, decision, raw_text, **kwargs):
        calls.append(
            {
                "ticket_ref": ticket_ref,
                "decision": decision.decision,
                "raw_text": raw_text,
            }
        )
        return result_holder["result"]

    monkeypatch.setattr(
        "orchestrator.services.ticketing.correlation_render.apply_amendment", _fake
    )
    return calls, result_holder


def _decision(**overrides: Any) -> CorrelationDecision:
    defaults: Dict[str, Any] = dict(
        decision="new",
        ticket_ref=None,
        confidence=None,
        decided_by="no_candidates",
        reason="no open candidates",
        affected_key=None,
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=[],
        llm_raw=None,
        needs_root_cause_ticket=False,
    )
    defaults.update(overrides)
    return CorrelationDecision(**defaults)


class TestResolveNotifyTicketAutoFlagOff:
    async def test_flag_off_files_plain_ticket(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "false")
        body = _notify_body(ticket_id="auto")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-000001"
        assert extra == {
            "decision": "new",
            "correlated_with": None,
            "confidence": None,
            "decided_by": "flag_off",
        }
        assert delivery is not None
        assert delivery.suppress is False
        assert delivery.reply_to_message_id is None
        assert delivery.record_message_id_for_ticket_ref == "TKT-000001"
        # Never even constructs a correlator when the flag is off.
        assert _FakeCorrelator.instances == []


class TestResolveNotifyTicketAutoNew:
    async def test_new_decision_creates_ticket(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _FakeCorrelator.decision_to_return = _decision(
            decision="new", decided_by="no_candidates", confidence=None
        )
        body = _notify_body(ticket_id="auto", text="Meter offline\n\ndetails")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-000001"
        assert extra["decision"] == "new"
        assert extra["decided_by"] == "no_candidates"
        assert extra["correlated_with"] is None
        svc = _FakeTicketService.instances[-1]
        assert len(svc.create_ticket_calls) == 1
        assert delivery is not None
        assert delivery.record_message_id_for_ticket_ref == "TKT-000001"
        assert delivery.suppress is False
        assert delivery.reply_to_message_id is None


class TestResolveNotifyTicketAutoAmend:
    async def test_amend_decision_calls_apply_amendment(self, fake_apply_amendment, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-000042",
            decision="amend",
            escalated=False,
            affected_keys_count=2,
            occurrence_count=3,
            telegram_chat_id="-100555",
            telegram_topic_id="42",
            telegram_message_id=123,
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="amend",
            ticket_ref="TKT-000042",
            confidence=0.9,
            decided_by="llm",
            affected_key={"kind": "mppt", "key": "A7", "label": "MPPT A7"},
        )
        body = _notify_body(ticket_id="auto")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-000042"
        assert extra == {
            "decision": "amend",
            "correlated_with": "TKT-000042",
            "confidence": 0.9,
            "decided_by": "llm",
        }
        assert len(calls) == 1
        assert calls[0]["ticket_ref"] == "TKT-000042"
        # No separate ticket-creation call for an amend.
        assert _FakeTicketService.instances[-1].create_ticket_calls == []
        assert delivery is not None
        assert delivery.suppress is False
        assert delivery.reply_to_message_id == 123
        assert delivery.top_level is False
        assert delivery.text_override.startswith("↻")


class TestResolveNotifyTicketAutoDuplicate:
    async def test_duplicate_decision_returns_existing_ref_without_new_ticket(
        self, fake_apply_amendment, monkeypatch
    ):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-000042",
            decision="duplicate",
            escalated=False,
            affected_keys_count=1,
            occurrence_count=5,
            telegram_chat_id="-100555",
            telegram_topic_id="42",
            telegram_message_id=123,
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="duplicate",
            ticket_ref="TKT-000042",
            confidence=1.0,
            decided_by="signature",
        )
        body = _notify_body(ticket_id="auto")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-000042"
        assert extra["decision"] == "duplicate"
        assert extra["decided_by"] == "signature"
        assert _FakeTicketService.instances[-1].create_ticket_calls == []
        assert delivery is not None
        assert delivery.suppress is True  # occurrence 5 is not a rollup-every-10th


class TestResolveNotifyTicketAutoRootCauseFirst:
    async def test_root_cause_ticket_filed_then_amended(self, fake_apply_amendment, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-000001",  # matches the fake TicketService's create_ticket result
            decision="amend",
            escalated=False,
            affected_keys_count=1,
            occurrence_count=1,
            telegram_chat_id="-100555",
            telegram_topic_id="42",
            telegram_message_id=None,
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="amend",
            ticket_ref=None,
            confidence=0.9,
            decided_by="llm",
            root_cause_kind="grid_off",
            needs_root_cause_ticket=True,
            affected_key={"kind": "mppt", "key": "A7", "label": "MPPT A7"},
        )
        body = _notify_body(ticket_id="auto")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-000001"
        assert extra["decision"] == "amend"
        assert extra["correlated_with"] is None  # nothing pre-existing -- a fresh parent was filed
        # The root-cause ticket was actually filed via TicketService.create_ticket...
        svc = _FakeTicketService.instances[-1]
        assert len(svc.create_ticket_calls) == 1
        req, _backend_override = svc.create_ticket_calls[0]
        assert "grid_off" in req.summary or "root cause" in req.summary.lower()
        # ...then attached to via apply_amendment, targeting the newly-created ref.
        assert calls[0]["ticket_ref"] == "TKT-000001"
        # Delivery is "new ticket" style -- full post, no reply -- since there's
        # no prior message for this brand-new parent to reply to.
        assert delivery is not None
        assert delivery.record_message_id_for_ticket_ref == "TKT-000001"
        assert delivery.reply_to_message_id is None


class TestResolveNotifyTicketAutoFailureModes:
    async def test_correlator_exception_falls_back_to_plain_create(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _FakeCorrelator.raise_error = RuntimeError("LLM gateway down")
        body = _notify_body(ticket_id="auto")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-000001"
        assert extra == {
            "decision": "new",
            "correlated_with": None,
            "confidence": None,
            "decided_by": "fallback",
        }
        assert delivery is not None
        assert delivery.record_message_id_for_ticket_ref == "TKT-000001"

    async def test_lock_timeout_falls_back_to_plain_create(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")

        import orchestrator.api.app as app_module
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _never_available(grid_name, timeout_seconds):
            yield False

        monkeypatch.setattr(app_module, "_acquire_grid_correlation_lock", _never_available)
        body = _notify_body(ticket_id="auto")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-000001"
        assert extra["decided_by"] == "fallback"
        assert delivery is not None
        assert delivery.record_message_id_for_ticket_ref == "TKT-000001"

    async def test_ticket_creation_failure_still_returns_500(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "false")
        body = _notify_body(ticket_id="auto")

        async def _boom(self, req, backend_override=None):
            raise TicketBackendError("both backends down")

        monkeypatch.setattr(_FakeTicketService, "create_ticket", _boom)
        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert ref is None
        assert extra is None
        assert delivery is None
        assert error is not None
        assert error.status_code == 500


class TestHandleNotifyAutoResponseShape:
    async def test_response_includes_decision_fields(self, monkeypatch):
        monkeypatch.setattr(
            "shared.auth.get_auth_service", lambda: _FakeAuthService(_target())
        )
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "false")
        request = _FakeRequest(headers={"X-Notify-Secret": "test-secret"})
        body = _notify_body(ticket_id="auto")
        background_tasks = BackgroundTasks()

        response = await handle_notify(request, body, background_tasks)  # type: ignore[arg-type]

        assert response.status_code == 202
        import json

        content = json.loads(response.body)
        assert content == {
            "ok": True,
            "ticket_ref": "TKT-000001",
            "decision": "new",
            "correlated_with": None,
            "confidence": None,
            "decided_by": "flag_off",
        }

    async def test_omitted_ticket_id_response_unaffected_by_auto_wiring(self, monkeypatch):
        """Sanity re-check at the handle_notify level: passthrough stays
        byte-identical even with all the new auto-sentinel plumbing in place."""
        monkeypatch.setattr(
            "shared.auth.get_auth_service", lambda: _FakeAuthService(_target())
        )
        request = _FakeRequest(headers={"X-Notify-Secret": "test-secret"})
        body = _notify_body(ticket_id=None)
        background_tasks = BackgroundTasks()

        response = await handle_notify(request, body, background_tasks)  # type: ignore[arg-type]

        import json

        assert json.loads(response.body) == {"ok": True}


# ---------------------------------------------------------------------------
# _deliver_notification -- reply/suppress/escalation Telegram behavior
# ---------------------------------------------------------------------------


class _FakeSendResult:
    def __init__(self, message_id: Optional[int]) -> None:
        self.message_id = message_id
        self.calls: List[Dict[str, Any]] = []

    async def send(self, bot_token, chat_id, text, reply_markup=None, parse_mode=None, topic_id=None, reply_to_message_id=None):
        self.calls.append(
            {
                "text": text,
                "parse_mode": parse_mode,
                "topic_id": topic_id,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return self.message_id


@pytest.fixture
def fake_telegram_send(monkeypatch):
    fake = _FakeSendResult(message_id=999)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")

    async def _send(bot_token, chat_id, text, reply_markup=None, parse_mode=None, topic_id=None, reply_to_message_id=None):
        return await fake.send(
            bot_token, chat_id, text, reply_markup, parse_mode, topic_id, reply_to_message_id
        )

    monkeypatch.setattr(
        "shared.utils.telegram_send.send_telegram_message_with_fallback", _send
    )
    return fake


@pytest.fixture(autouse=True)
def _stub_chat_db_logging(monkeypatch):
    """_log_notification_to_chat_db does its own supabase lookups -- irrelevant
    to delivery behavior, so make it a no-op for these tests."""
    from orchestrator.api import app as app_module

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(app_module, "_log_notification_to_chat_db", _noop)


class TestDeliverNotificationDelivery:
    async def test_new_ticket_delivery_sends_full_text_no_reply(self, fake_telegram_send):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        body = _notify_body(text="Full alert text")
        delivery = NotificationDelivery(record_message_id_for_ticket_ref="TKT-000001")

        await _deliver_notification(body, _target(), "TKT-000001", delivery)

        assert len(fake_telegram_send.calls) == 1
        call = fake_telegram_send.calls[0]
        assert "Full alert text" in call["text"]
        assert call["reply_to_message_id"] is None

    async def test_amend_delivery_sends_short_text_as_reply(self, fake_telegram_send):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        body = _notify_body()
        delivery = NotificationDelivery(
            text_override="↻ TKT-000042: MPPT A7 also affected", reply_to_message_id=555
        )

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        call = fake_telegram_send.calls[0]
        assert call["text"] == "↻ TKT-000042: MPPT A7 also affected"
        assert call["reply_to_message_id"] == 555

    async def test_duplicate_suppressed_sends_nothing(self, fake_telegram_send):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        body = _notify_body()
        delivery = NotificationDelivery(suppress=True)

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        assert fake_telegram_send.calls == []

    async def test_rollup_delivery_sends_one_message(self, fake_telegram_send):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        body = _notify_body()
        delivery = NotificationDelivery(
            text_override="↻ TKT-000042: still firing — 10 occurrences", reply_to_message_id=555
        )

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        assert len(fake_telegram_send.calls) == 1
        assert "still firing" in fake_telegram_send.calls[0]["text"]

    async def test_escalation_delivery_is_top_level_not_a_reply(self, fake_telegram_send):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        body = _notify_body()
        delivery = NotificationDelivery(
            text_override="🔴 4 MPPTs in Kudi affected (A3, A5, A6, A7) !",
            reply_to_message_id=555,  # present but must be ignored -- top_level wins
            top_level=True,
        )

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        call = fake_telegram_send.calls[0]
        assert call["text"].startswith("🔴")
        assert call["reply_to_message_id"] is None

    async def test_none_delivery_is_unchanged_full_send(self, fake_telegram_send):
        """delivery=None (every non-"auto" ticket_id path) must behave exactly
        like before this task existed."""
        from orchestrator.api.app import _deliver_notification

        body = _notify_body(text="Plain passthrough alert")

        await _deliver_notification(body, _target(), None, None)

        call = fake_telegram_send.calls[0]
        assert "Plain passthrough alert" in call["text"]
        assert call["reply_to_message_id"] is None

    async def test_new_ticket_delivery_records_message_id(self, fake_telegram_send, monkeypatch):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        recorded: List[tuple] = []

        class _FakeCorrelationStore:
            def __init__(self, get_client=None):
                pass

            async def record_message_id(self, ticket_ref, message_id):
                recorded.append((ticket_ref, message_id))
                return True

        monkeypatch.setattr(
            "orchestrator.services.ticketing.correlation_store.CorrelationStore",
            _FakeCorrelationStore,
        )
        body = _notify_body()
        delivery = NotificationDelivery(record_message_id_for_ticket_ref="TKT-000001")

        await _deliver_notification(body, _target(), "TKT-000001", delivery)

        assert recorded == [("TKT-000001", 999)]
