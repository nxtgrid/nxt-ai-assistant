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
    NotificationDelivery,
    NotificationTicket,
    NotifyRequest,
    _resolve_notify_ticket,
    _resolve_notify_ticket_full,
    handle_notify,
)
from orchestrator.services.ticketing.backend import (
    TicketBackendError,
    TicketCreateOutcome,
    TicketResult,
    TicketStatus,
)
from orchestrator.services.ticketing.correlation_render import AmendmentResult
from orchestrator.services.ticketing.correlator import CorrelationDecision
from orchestrator.services.ticketing.repository import TicketRecord
from orchestrator.services.ticketing.service import TicketService
from orchestrator.services.urgent_alert_context import build_urgent_alert_context
from shared.auth.auth_service import GridNotificationTarget


class _FakeTicketService:
    """Stands in for TicketService as constructed fresh inside
    _resolve_notify_ticket (`from ...ticketing.service import TicketService`)."""

    instances: List["_FakeTicketService"] = []
    backend_for_ref = "internal"

    def __init__(self, *args, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.create_ticket_calls: List[tuple] = []
        self.add_comment_calls: List[tuple] = []
        self.update_ticket_calls: List[Dict[str, Any]] = []
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

    async def create_ticket_with_internal_fallback(self, req, backend_override=None):
        self.create_ticket_calls.append((req, backend_override))
        if self.create_error:
            return TicketCreateOutcome(result=None, error=str(self.create_error), fallback_used=True)
        return TicketCreateOutcome(result=self.create_result)

    async def resolve_backend(self, override=None):
        class _Backend:
            name = "internal" if override == "internal" else "jira"

        return _Backend()

    async def get_status(self, ref: str):
        return self.get_status_return

    async def get_backend_name(self, ref: str) -> str:
        return self.backend_for_ref

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        self.add_comment_calls.append((ref, body, public))
        return True

    async def update_ticket(
        self,
        ref: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        priority_id: Optional[str] = None,
    ) -> bool:
        self.update_ticket_calls.append(
            {
                "ref": ref,
                "summary": summary,
                "description": description,
                "priority_id": priority_id,
            }
        )
        return True

    async def transition_to_done(self, ref: str) -> None:
        self.transition_to_done_calls.append(ref)


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    _FakeTicketService.instances = []
    _FakeTicketService.backend_for_ref = "internal"
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


def _live_context(output_kw: Optional[float]):
    async def read_output(_grid_name: str) -> Optional[float]:
        return output_kw

    return build_urgent_alert_context(
        subject="! Urgent: Grid down",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_output=read_output,
    )


async def _return_live_output(output_kw: float) -> float:
    return output_kw


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


async def test_blank_ticket_id_prefers_alert_subject_for_summary():
    body = _notify_body(
        ticket_id="",
        text="Long raw alert body",
        alert={"subject": "Inverter output stopped"},
    )

    await _resolve_notify_ticket(body, _target())

    req, _ = _FakeTicketService.instances[-1].create_ticket_calls[0]
    assert req.summary == "Inverter output stopped"


async def test_structured_urgent_severity_is_carried_to_ticket_creation():
    body = _notify_body(
        ticket_id="",
        alert={"subject": "Grid down", "severity": "urgent"},
    )

    await _resolve_notify_ticket(body, _target())

    req, _ = _FakeTicketService.instances[-1].create_ticket_calls[0]
    assert req.severity == "urgent"


async def test_jira_ticket_type_selection_receives_live_output_context(monkeypatch):
    monkeypatch.setenv("NOTIFY_TICKETS_BACKEND", "jira")
    body = _notify_body(ticket_id="", alert={"subject": "! Urgent: Grid down"})

    await _resolve_notify_ticket_full(body, _target(), _live_context(0.0))

    req, backend_override = _FakeTicketService.instances[-1].create_ticket_calls[0]
    assert backend_override == "jira"
    assert req.llm_context == {"live_inverter_output_kw": 0.0}


async def test_auto_new_ticket_prefers_alert_subject_for_summary(monkeypatch):
    monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "false")
    body = _notify_body(
        ticket_id="auto",
        text="Long raw alert body",
        alert={"subject": "Inverter output stopped"},
    )

    await _resolve_notify_ticket_full(body, _target())

    req, _ = _FakeTicketService.instances[-1].create_ticket_calls[0]
    assert req.summary == "Inverter output stopped"


async def test_new_ticket_delivery_keeps_backend_and_url_context():
    _FakeTicketService.instances = []
    body = _notify_body(ticket_id="")
    ticket_ref, error, _extra, delivery = await _resolve_notify_ticket_full(body, _target())

    assert error is None
    assert ticket_ref == "TKT-000001"
    assert delivery is not None
    assert delivery.ticket == NotificationTicket(
        ref="TKT-000001", backend="internal", url=None
    )


async def test_existing_ticket_delivery_uses_persisted_backend_context():
    _FakeTicketService.backend_for_ref = "internal"
    body = _notify_body(ticket_id="TKT-000101")
    ticket_ref, error, _extra, delivery = await _resolve_notify_ticket_full(body, _target())

    assert error is None
    assert ticket_ref == "TKT-000101"
    assert delivery is not None
    assert delivery.ticket == NotificationTicket(
        ref="TKT-000101", backend="internal", url=None
    )


async def test_blank_ticket_id_ignores_close_flag():
    """close=True is only meaningful for the populated-ticket_id (comment) branch
    -- a freshly created ticket is never auto-closed."""
    body = _notify_body(ticket_id="", close=True)

    ref, error = await _resolve_notify_ticket(body, _target())

    assert error is None
    assert ref == "TKT-000001"
    svc = _FakeTicketService.instances[-1]
    assert svc.transition_to_done_calls == []


async def test_blank_ticket_id_creation_failure_is_fail_open(monkeypatch):
    body = _notify_body(ticket_id="")

    async def _both_backends_down(self, req, backend_override=None):
        return TicketCreateOutcome(result=None, error="both backends down", fallback_used=True)

    monkeypatch.setattr(
        _FakeTicketService, "create_ticket_with_internal_fallback", _both_backends_down
    )
    ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

    assert ref is None
    assert error is None
    assert extra == {"ticket_error": "Ticket creation failed: both backends down"}
    assert delivery is not None
    assert delivery.text_override == "Meter offline"


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


async def test_handle_notify_falls_open_to_internal_when_jira_has_no_compatible_type(
    monkeypatch,
):
    class _CanonicalTicketRepository:
        async def create_intent(self, request, *, created_via):
            return TicketRecord(
                id="ticket-1",
                summary=request.summary,
                created_via=created_via,
                provisioning_state="pending",
            )

        async def set_pending_backend(self, _ticket_id, _backend):
            return None

        async def activate(self, ticket_id, result):
            return TicketRecord(
                id=ticket_id,
                ticket_ref=result.ref,
                backend=result.backend,
                summary="Grid down",
                created_via="notification",
                provisioning_state="active",
            )

    class _JiraWithoutCompatibleType:
        name = "jira"

        def has_credentials(self):
            return True

        async def create_ticket(self, _request):
            raise TicketBackendError("Jira cannot supply a compatible issue type")

    class _InternalFallback:
        name = "internal"

        async def create_ticket(self, _request):
            return TicketResult(ref="TKT-000002", backend="internal", url=None)

    service = TicketService(
        jira_backend=_JiraWithoutCompatibleType(),
        internal_backend=_InternalFallback(),
        ticket_repository=_CanonicalTicketRepository(),
    )
    monkeypatch.setenv("JIRA_PROJECT_KEY", "OPS")
    monkeypatch.setenv("NOTIFY_TICKETS_BACKEND", "jira")
    monkeypatch.setattr(
        "orchestrator.services.ticketing.service.TicketService",
        lambda **_kwargs: service,
    )
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: _FakeAuthService(_target()))

    response = await handle_notify(
        _FakeRequest(headers={"X-Notify-Secret": "test-secret"}),
        _notify_body(ticket_id="", alert={"subject": "Grid down"}),
        BackgroundTasks(),
    )

    import json

    assert response.status_code == 202
    assert json.loads(response.body)["ticket_ref"].startswith("TKT-")


async def test_handle_notify_still_sends_when_all_ticket_backends_fail(
    monkeypatch, fake_telegram_send
):
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: _FakeAuthService(_target()))

    async def _both_backends_down(self, req, backend_override=None):
        return TicketCreateOutcome(result=None, error="both backends down", fallback_used=True)

    monkeypatch.setattr(
        _FakeTicketService, "create_ticket_with_internal_fallback", _both_backends_down
    )
    background_tasks = BackgroundTasks()
    response = await handle_notify(
        _FakeRequest(headers={"X-Notify-Secret": "test-secret"}),
        _notify_body(ticket_id="", alert={"subject": "! Urgent: Grid down"}),
        background_tasks,
    )

    import json

    assert response.status_code == 202
    assert json.loads(response.body) == {
        "ok": True,
        "ticket_error": "Ticket creation failed: both backends down",
    }
    await background_tasks()
    assert fake_telegram_send.calls[0]["text"].startswith("! Urgent: Grid down")


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

    async def decide(
        self, grid_name, alert, dedup_key=None, backend_override=None, get_live_facts=None
    ):
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
        ticket_severity="",
    )
    defaults.update(overrides)
    return CorrelationDecision(**defaults)


class _RecordingCorrelationStore:
    """Fake CorrelationStore for the fail-open (uncorrelated-ticket) paths.

    Task 6 fixes 4 fallback sites (flag off, lock timeout, decision is None,
    outer exception) that used to file a ticket with no ``ticket_correlations``
    row at all -- making it permanently invisible to
    ``open_candidates_for_grid`` and therefore a guaranteed future duplicate.
    This fake captures ``upsert_correlation``/``record_event_ticket_ref``
    calls so tests can assert every one of those paths now seeds a row.
    Mirrors the interface those paths actually touch (compare to the
    similarly-shaped, but test-local, ``_FakeStore`` in
    ``test_new_ticket_backfills_ticket_ref_onto_its_event_row`` above -- that
    one isn't reusable outside its own test function, so this is a separate,
    shared fixture rather than a duplicate of it).

    ``open_candidates_for_grid`` and ``record_event`` back the lock-free
    deterministic-only correlation attempt on the grid-lock-timeout path:
    ``open_candidates_to_return`` is a class attribute (following the same
    pattern as ``_FakeCorrelator.decision_to_return``) so a test can seed it
    before the store gets constructed fresh inside ``_resolve_notify_ticket_auto``.
    It defaults to ``[]`` so every existing caller that never seeds it keeps
    seeing "no open candidates", unchanged.
    """

    instances: List["_RecordingCorrelationStore"] = []
    open_candidates_to_return: List[Dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.upsert_calls: List[Dict[str, Any]] = []
        self.backfill_calls: List[tuple] = []
        self.open_candidates_for_grid_calls: List[tuple] = []
        self.record_event_calls: List[Dict[str, Any]] = []
        _RecordingCorrelationStore.instances.append(self)

    async def upsert_correlation(self, **kwargs: Any) -> bool:
        self.upsert_calls.append(kwargs)
        return True

    async def record_event_ticket_ref(self, dedup_key: str, ticket_ref: str) -> bool:
        self.backfill_calls.append((dedup_key, ticket_ref))
        return True

    async def open_candidates_for_grid(
        self, grid_name: str, since_iso: str, limit: int = 15
    ) -> List[Dict[str, Any]]:
        self.open_candidates_for_grid_calls.append((grid_name, since_iso, limit))
        return _RecordingCorrelationStore.open_candidates_to_return

    async def record_event(self, **kwargs: Any) -> bool:
        self.record_event_calls.append(kwargs)
        return True


def _patch_recording_store(monkeypatch) -> None:
    monkeypatch.setattr(
        "orchestrator.services.ticketing.correlation_store.CorrelationStore",
        _RecordingCorrelationStore,
    )


@pytest.fixture(autouse=True)
def _reset_recording_correlation_store_seed():
    _RecordingCorrelationStore.open_candidates_to_return = []
    yield
    _RecordingCorrelationStore.open_candidates_to_return = []


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

    async def test_flag_off_ticket_still_gets_a_correlation_row(self, monkeypatch):
        """Regression test for Task 6: before this fix, the flag-off path
        filed a ticket via _create_notify_ticket with no follow-up
        upsert_correlation call, so the ticket was invisible to
        open_candidates_for_grid forever and every future identical alert on
        this grid would file yet another ticket."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "false")
        _RecordingCorrelationStore.instances = []
        _patch_recording_store(monkeypatch)
        body = _notify_body(ticket_id="auto", dedup_key="alert-flagoff-1")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert extra["decided_by"] == "flag_off"
        upserts = [
            u for s in _RecordingCorrelationStore.instances for u in s.upsert_calls
        ]
        assert [u["ticket_ref"] for u in upserts] == [ref]
        backfills = [
            b for s in _RecordingCorrelationStore.instances for b in s.backfill_calls
        ]
        assert backfills == [("alert-flagoff-1", ref)]


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
            component_added=True,
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="amend",
            ticket_ref="TKT-000042",
            confidence=0.9,
            decided_by="llm",
            affected_key={"kind": "mppt", "key": "A7", "label": "MPPT A7"},
        )
        body = _notify_body(
            ticket_id="auto", alert={"subject": "! Warning: Multiple MPPTs offline"}
        )

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
        assert delivery.text_override == "Added MPPT A7 (2 affected components)"
        assert delivery.ticket == NotificationTicket(ref="TKT-000042", backend="internal")

    async def test_replayed_amendment_uses_silent_executor_result(
        self, fake_apply_amendment, monkeypatch
    ):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-000042",
            decision="duplicate",
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
            decided_by="replay",
            ticket_severity="urgent",
        )

        ref, error, extra, delivery = await _resolve_notify_ticket_full(
            _notify_body(
                ticket_id="auto",
                alert={
                    "subject": "! Urgent: Multiple MPPTs offline",
                    "severity": "urgent",
                },
            ),
            _target(),
        )

        assert error is None
        assert ref == "TKT-000042"
        assert extra["decided_by"] == "replay"
        assert delivery is not None
        assert delivery.suppress is True

    async def test_jira_only_urgent_amendment_replay_is_durably_silent(
        self, fake_telegram_send, monkeypatch
    ):
        from orchestrator.api import app as app_module

        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _FakeTicketService.backend_for_ref = "jira"

        class _PersistentStore:
            def __init__(self) -> None:
                self.correlation: Optional[Dict[str, Any]] = None

            async def get_correlation(self, ticket_ref):
                return self.correlation

            async def bump_occurrence(self, ticket_ref, occurred_at=None):
                if self.correlation is not None:
                    self.correlation["occurrence_count"] += 1
                return self.correlation is not None

            async def merge_affected_key(self, *args, **kwargs):
                return None

            async def upsert_correlation(self, **kwargs):
                self.correlation = {
                    **kwargs,
                    "summary_current": kwargs["summary_base"],
                    "occurrence_count": 1,
                    "escalated_at": None,
                }
                return True

            async def record_amendment(
                self,
                ticket_ref,
                *,
                summary_current,
                severity=None,
                escalated=False,
            ):
                assert self.correlation is not None
                self.correlation["summary_current"] = summary_current
                self.correlation["severity"] = severity
                if escalated:
                    self.correlation["escalated_at"] = "now"
                return True

        store = _PersistentStore()

        class _TwoPassCorrelator:
            calls: List[Optional[str]] = []

            def __init__(self, store, ticket_service):
                self.store = store

            async def decide(
                self,
                grid_name,
                alert,
                dedup_key=None,
                backend_override=None,
                get_live_facts=None,
            ):
                self.calls.append(dedup_key)
                if len(self.calls) == 1:
                    return _decision(
                        decision="amend",
                        ticket_ref="OPS-42",
                        confidence=0.9,
                        decided_by="llm",
                        affected_key={
                            "kind": "inverter",
                            "key": "INV-1",
                            "label": "Inverter INV-1",
                        },
                        amended_summary="Kudi inverter outage",
                        ticket_severity="warning",
                    )
                correlation = await self.store.get_correlation("OPS-42")
                return _decision(
                    decision="amend",
                    ticket_ref="OPS-42",
                    confidence=0.9,
                    decided_by="replay",
                    ticket_severity=(
                        str(correlation.get("severity") or "")
                        if correlation is not None
                        else ""
                    ),
                )

        monkeypatch.setattr(
            "orchestrator.services.ticketing.correlation_store.CorrelationStore",
            lambda get_client=None: store,
        )
        monkeypatch.setattr(
            "orchestrator.services.ticketing.correlator.AlertCorrelator",
            _TwoPassCorrelator,
        )
        body = _notify_body(
            ticket_id="auto",
            dedup_key="jira-urgent-1",
            text="urgent raw text",
            alert={
                "subject": "Inverter outage in Kudi",
                "severity": "urgent",
                "component_kind": "inverter",
                "component_key": "INV-1",
                "component_label": "Inverter INV-1",
            },
        )
        alert_context = _live_context(3.1)

        first_ref, first_error, _first_extra, first_delivery = (
            await _resolve_notify_ticket_full(body, _target(), alert_context)
        )
        await app_module._deliver_notification(
            body,
            _target(),
            first_ref,
            first_delivery,
        )
        second_ref, second_error, _second_extra, second_delivery = (
            await _resolve_notify_ticket_full(body, _target(), alert_context)
        )
        await app_module._deliver_notification(
            body,
            _target(),
            second_ref,
            second_delivery,
        )

        services = _FakeTicketService.instances
        update_calls = [
            call for service in services for call in service.update_ticket_calls
        ]
        comment_calls = [
            call for service in services for call in service.add_comment_calls
        ]
        assert first_error is None
        assert second_error is None
        assert _TwoPassCorrelator.calls == ["jira-urgent-1", "jira-urgent-1"]
        assert update_calls == [
            {
                "ref": "OPS-42",
                "summary": "🔴 ! Urgent: Kudi inverter outage",
                "description": None,
                "priority_id": "highest",
            }
        ]
        assert comment_calls == [("OPS-42", "urgent raw text", False)]
        assert len(fake_telegram_send.calls) == 1


class TestResolveNotifyTicketAutoReplay:
    async def test_replayed_amend_does_not_renotify_or_recomment(
        self, fake_apply_amendment, monkeypatch
    ):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, _result_holder = fake_apply_amendment
        _FakeCorrelator.decision_to_return = _decision(
            decision="amend",
            ticket_ref="OPS-3353",
            confidence=0.9,
            decided_by="replay",
            reason="replayed prior decision (dedup_key match)",
            affected_key=None,
            ticket_severity="warning",
        )
        body = _notify_body(ticket_id="auto", dedup_key="alert-42")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "OPS-3353"
        assert extra["decided_by"] == "replay"
        assert delivery is not None
        assert delivery.suppress is True
        # The prior run already amended the ticket and already posted.
        assert calls == []

    async def test_replayed_urgent_escalation_still_applies(
        self, fake_apply_amendment, monkeypatch
    ):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="OPS-3353",
            decision="amend",
            escalated=True,
            affected_keys_count=0,
            occurrence_count=4,
            telegram_chat_id="-100555",
            telegram_topic_id="42",
            telegram_message_id=123,
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="amend",
            ticket_ref="OPS-3353",
            confidence=0.9,
            decided_by="replay",
            reason="replayed prior decision (dedup_key match)",
            affected_key=None,
            ticket_severity="warning",
        )
        body = _notify_body(
            ticket_id="auto",
            dedup_key="alert-42",
            alert={"subject": "! Urgent: Grid outage", "severity": "urgent"},
        )

        ref, error, _extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "OPS-3353"
        assert len(calls) == 1
        assert delivery is not None
        assert delivery.suppress is False
        assert delivery.top_level is True

    async def test_replayed_new_decision_with_urgent_escalation_amends_not_duplicates(
        self, fake_apply_amendment, monkeypatch
    ):
        """Regression test for a holistic-review bug: a replay whose ORIGINAL
        decision was "new" (ticket_ref backfilled after the ticket was
        actually created -- see
        test_new_ticket_backfills_ticket_ref_onto_its_event_row) must still
        escalate the existing ticket on an urgent severity bump, not file a
        second one. decision.decision on a replay is whatever the original
        decision type was, so the "new"-ticket branch's bare
        `decision.decision == "new"` check would otherwise fire."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="OPS-3363",
            decision="amend",
            escalated=True,
            affected_keys_count=0,
            occurrence_count=2,
            telegram_chat_id="-100555",
            telegram_topic_id="42",
            telegram_message_id=123,
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="new",
            ticket_ref="OPS-3363",
            decided_by="replay",
            confidence=None,
            ticket_severity="warning",
        )
        body = _notify_body(
            ticket_id="auto",
            dedup_key="alert-42",
            alert={"subject": "! Urgent: Grid outage", "severity": "urgent"},
        )

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "OPS-3363"
        # Must escalate the existing ticket, not file a second one.
        assert len(calls) == 1
        svc = _FakeTicketService.instances[-1]
        assert svc.create_ticket_calls == []
        assert delivery is not None
        assert delivery.suppress is False
        assert delivery.top_level is True

    async def test_new_ticket_backfills_ticket_ref_onto_its_event_row(self, monkeypatch):
        """Regression test for the delivery-idempotency gap a code reviewer
        flagged in this fix: a "new" decision's ``ticket_correlation_events``
        row is written with ``ticket_ref=None`` (nothing exists yet at
        decide-time -- see AlertCorrelator._finalize), so without a backfill
        a later replay of the same dedup_key would find ``ticket_ref=None``,
        fail the replay guard's truthiness check, and file a duplicate
        ticket. This exercises the actual app.py wiring added in
        _resolve_notify_ticket_auto -- that it calls
        store.record_event_ticket_ref(dedup_key, <new ticket ref>)
        immediately after a brand-new ticket is created and correlated."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")

        class _FakeStore:
            def __init__(self) -> None:
                self.upsert_calls: List[Dict[str, Any]] = []
                self.backfill_calls: List[tuple] = []

            async def upsert_correlation(self, **kwargs):
                self.upsert_calls.append(kwargs)
                return True

            async def record_event_ticket_ref(self, dedup_key, ticket_ref):
                self.backfill_calls.append((dedup_key, ticket_ref))
                return True

        store = _FakeStore()
        monkeypatch.setattr(
            "orchestrator.services.ticketing.correlation_store.CorrelationStore",
            lambda get_client=None: store,
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="new", ticket_ref=None, decided_by="no_candidates"
        )
        body = _notify_body(ticket_id="auto", dedup_key="alert-42")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert extra["decision"] == "new"
        # The new ticket's ref must be backfilled onto the dedup_key's event
        # row -- exactly once, with the ref the ticket service actually
        # returned -- so a later replay's get_by_dedup_key() lookup finds a
        # populated ticket_ref and the guard above can suppress correctly.
        assert store.backfill_calls == [("alert-42", ref)]


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
        body = _notify_body(
            ticket_id="auto", alert={"subject": "! Warning: Multiple MPPTs offline"}
        )

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
        assert delivery.ticket_summary.startswith("! Urgent: Acme Grid root cause")


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

    async def test_correlator_exception_ticket_still_gets_a_correlation_row(self, monkeypatch):
        """Regression test for Task 6: this fallback (correlator.decide()
        raises -> decision = None -> plain ticket) used to call
        _create_notify_ticket with no upsert_correlation follow-up, leaving
        the ticket invisible to open_candidates_for_grid for every future
        alert on this grid."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _RecordingCorrelationStore.instances = []
        _patch_recording_store(monkeypatch)
        _FakeCorrelator.raise_error = RuntimeError("LLM gateway down")
        body = _notify_body(ticket_id="auto", dedup_key="alert-decision-none-1")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert extra["decided_by"] == "fallback"
        upserts = [
            u for s in _RecordingCorrelationStore.instances for u in s.upsert_calls
        ]
        assert [u["ticket_ref"] for u in upserts] == [ref]
        backfills = [
            b for s in _RecordingCorrelationStore.instances for b in s.backfill_calls
        ]
        assert backfills == [("alert-decision-none-1", ref)]

    async def test_lock_timeout_falls_back_to_plain_create(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")

        from contextlib import asynccontextmanager

        import orchestrator.api.app as app_module
        from orchestrator.services.ticketing.correlation_rules import (
            DEFAULT_CORRELATION_POLICY,
        )

        observed_timeout_seconds: list[float] = []

        @asynccontextmanager
        async def _never_available(grid_name, timeout_seconds):
            observed_timeout_seconds.append(timeout_seconds)
            yield False

        monkeypatch.setattr(app_module, "_acquire_grid_correlation_lock", _never_available)
        body = _notify_body(ticket_id="auto")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-000001"
        assert extra["decided_by"] == "fallback"
        assert observed_timeout_seconds == [
            DEFAULT_CORRELATION_POLICY.grid_lock_timeout_seconds
        ]
        assert delivery is not None
        assert delivery.record_message_id_for_ticket_ref == "TKT-000001"

    async def test_lock_timeout_ticket_still_gets_a_correlation_row(self, monkeypatch):
        """Regression test for Task 6: the grid-lock-timeout fallback used to
        call _create_notify_ticket with no upsert_correlation follow-up,
        leaving the ticket invisible to open_candidates_for_grid for every
        future alert on this grid."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _RecordingCorrelationStore.instances = []
        _patch_recording_store(monkeypatch)

        from contextlib import asynccontextmanager

        import orchestrator.api.app as app_module

        @asynccontextmanager
        async def _never_available(grid_name, timeout_seconds):
            yield False

        monkeypatch.setattr(app_module, "_acquire_grid_correlation_lock", _never_available)
        body = _notify_body(ticket_id="auto", dedup_key="alert-lock-timeout-1")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert extra["decided_by"] == "fallback"
        upserts = [
            u for s in _RecordingCorrelationStore.instances for u in s.upsert_calls
        ]
        assert [u["ticket_ref"] for u in upserts] == [ref]
        backfills = [
            b for s in _RecordingCorrelationStore.instances for b in s.backfill_calls
        ]
        assert backfills == [("alert-lock-timeout-1", ref)]

    async def test_lock_timeout_with_matching_candidate_amends_instead_of_filing_new(
        self, monkeypatch, fake_apply_amendment
    ):
        """Regression test for the grid-lock-timeout fallback duplicating
        tickets under a burst: today, timing out on the lock skips
        correlation entirely and blindly files a new ticket even when an
        open candidate is an exact (signature, component_key) match. The
        lock-free deterministic-only correlation attempt must catch this
        case -- amend/dup the existing candidate, not mint a fresh
        TKT-000001 -- without ever touching the LLM (that's the whole point
        of staying lock-free)."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _RecordingCorrelationStore.instances = []
        _patch_recording_store(monkeypatch)

        from contextlib import asynccontextmanager

        import orchestrator.api.app as app_module
        from orchestrator.services.ticketing.alert_facts import AlertFacts, enrich_alert_facts

        @asynccontextmanager
        async def _never_available(grid_name, timeout_seconds):
            yield False

        monkeypatch.setattr(app_module, "_acquire_grid_correlation_lock", _never_available)

        subject = "! Warning: MPPT A3 in Acme Grid seems to perform lower !"
        base_alert = AlertFacts(subject=subject, details="mppt A3 [Acme Grid]")
        computed_alert = enrich_alert_facts(base_alert, grid_name="Acme Grid")
        assert computed_alert.component_kind == "mppt"
        assert computed_alert.component_key == "A3"

        _RecordingCorrelationStore.open_candidates_to_return = [
            {
                "ticket_ref": "TKT-EXISTING-1",
                "grid_name": "Acme Grid",
                "status": "open",
                "severity": "warning",
                "signatures": [computed_alert.signature],
                "affected_keys": [
                    {
                        "kind": computed_alert.component_kind,
                        "key": computed_alert.component_key,
                        "label": computed_alert.component_label,
                    }
                ],
                "created_at": "2026-07-20T00:00:00+00:00",
            }
        ]

        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-EXISTING-1",
            decision="duplicate",
            escalated=False,
            affected_keys_count=1,
            occurrence_count=2,
            telegram_chat_id=None,
            telegram_topic_id=None,
            telegram_message_id=None,
        )

        body = _notify_body(ticket_id="auto", text=subject, alert=base_alert.model_dump())

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        # The existing candidate's ref -- not a freshly minted TKT-000001.
        assert ref == "TKT-EXISTING-1"
        assert extra["decided_by"] == "fallback_signature"
        assert extra["decision"] == "duplicate"
        assert extra["correlated_with"] == "TKT-EXISTING-1"

        # No new ticket was ever filed via the plain-create path.
        assert all(
            not svc.create_ticket_calls for svc in _FakeTicketService.instances
        )
        assert len(calls) == 1
        assert calls[0]["ticket_ref"] == "TKT-EXISTING-1"
        assert calls[0]["decision"] == "duplicate"

        # Best-effort correlation event recorded, matching _finalize's shape.
        recorded = [
            e for s in _RecordingCorrelationStore.instances for e in s.record_event_calls
        ]
        assert len(recorded) == 1
        assert recorded[0]["ticket_ref"] == "TKT-EXISTING-1"
        assert recorded[0]["decided_by"] == "fallback_signature"

        assert delivery is not None
        assert delivery.suppress is True

    async def test_lock_timeout_with_no_matching_candidate_still_falls_back_to_plain_create(
        self, monkeypatch
    ):
        """Regression guard for the still-no-match case: when the lock-free
        deterministic check finds candidates but none of them match this
        alert's (signature, component_key), the timeout path must still fall
        back to _file_uncorrelated_ticket exactly as it did before this fix
        -- see test_lock_timeout_falls_back_to_plain_create above for the
        no-candidates-at-all variant of the same guarantee."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _RecordingCorrelationStore.instances = []
        _patch_recording_store(monkeypatch)

        from contextlib import asynccontextmanager

        import orchestrator.api.app as app_module

        @asynccontextmanager
        async def _never_available(grid_name, timeout_seconds):
            yield False

        monkeypatch.setattr(app_module, "_acquire_grid_correlation_lock", _never_available)

        # A candidate exists on the grid, but its signature doesn't match --
        # not a duplicate, just an unrelated open ticket.
        _RecordingCorrelationStore.open_candidates_to_return = [
            {
                "ticket_ref": "TKT-UNRELATED-1",
                "grid_name": "Acme Grid",
                "status": "open",
                "severity": "warning",
                "signatures": ["some-other-signature"],
                "affected_keys": [{"kind": "inverter", "key": "B1", "label": "Inverter B1"}],
                "created_at": "2026-07-20T00:00:00+00:00",
            }
        ]

        body = _notify_body(ticket_id="auto")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-000001"
        assert extra["decided_by"] == "fallback"
        # The read happened (lock-free attempt ran)...
        read_calls = [
            c
            for s in _RecordingCorrelationStore.instances
            for c in s.open_candidates_for_grid_calls
        ]
        assert len(read_calls) == 1
        # ...but no correlation event was recorded for a match that never
        # happened, and the plain-create path still seeded the usual row.
        assert all(not s.record_event_calls for s in _RecordingCorrelationStore.instances)
        upserts = [u for s in _RecordingCorrelationStore.instances for u in s.upsert_calls]
        assert [u["ticket_ref"] for u in upserts] == [ref]

    async def test_outer_exception_ticket_still_gets_a_correlation_row(self, monkeypatch):
        """Regression test for Task 6: when a decision comes back (not None)
        but something raises while executing it (e.g. apply_amendment
        crashes), the outer `except Exception:` fallback used to call
        _create_notify_ticket with no upsert_correlation follow-up, leaving
        the ticket invisible to open_candidates_for_grid for every future
        alert on this grid."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _RecordingCorrelationStore.instances = []
        _patch_recording_store(monkeypatch)

        async def _boom(**kwargs: Any):
            raise RuntimeError("apply_amendment blew up")

        monkeypatch.setattr(
            "orchestrator.services.ticketing.correlation_render.apply_amendment", _boom
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="amend",
            ticket_ref="OPS-9001",
            confidence=0.9,
            decided_by="llm",
        )
        body = _notify_body(ticket_id="auto", dedup_key="alert-outer-exc-1")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert extra["decided_by"] == "fallback"
        upserts = [
            u for s in _RecordingCorrelationStore.instances for u in s.upsert_calls
        ]
        assert [u["ticket_ref"] for u in upserts] == [ref]
        backfills = [
            b for s in _RecordingCorrelationStore.instances for b in s.backfill_calls
        ]
        assert backfills == [("alert-outer-exc-1", ref)]

    async def test_ticket_creation_failure_still_delivers_base_alert(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "false")
        body = _notify_body(ticket_id="auto")

        async def _both_backends_down(self, req, backend_override=None):
            return TicketCreateOutcome(result=None, error="both backends down", fallback_used=True)

        monkeypatch.setattr(
            _FakeTicketService, "create_ticket_with_internal_fallback", _both_backends_down
        )
        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert ref is None
        assert error is None
        assert extra == {"ticket_error": "Ticket creation failed: both backends down"}
        assert delivery is not None


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
    async def test_records_a_receipt_for_a_canonical_ticket(self, fake_telegram_send, monkeypatch):
        from orchestrator.api.app import _deliver_notification

        calls: list[dict[str, Any]] = []

        class _Deliveries:
            def __init__(self, **_kwargs):
                pass

            async def record(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(
            "orchestrator.services.ticketing.delivery_repository.DeliveryRepository", _Deliveries
        )
        ticket = NotificationTicket(ref="TKT-9", backend="internal", ticket_id="ticket-9")

        await _deliver_notification(
            _notify_body(), _target(), ticket.ref, NotificationDelivery(ticket=ticket)
        )

        assert calls == [{
            "ticket_id": "ticket-9", "escalation_id": None, "purpose": "notification",
            "external_chat_id": "-100555", "external_topic_id": "42", "external_message_id": 999,
        }]

    async def test_ticketed_jira_notification_links_only_the_reference(self, fake_telegram_send):
        from orchestrator.api.app import _deliver_notification

        body = _notify_body(
            text="Inverter output stopped",
            alert={"subject": "! Warning: Inverter offline"},
        )
        ticket = NotificationTicket(
            ref="OPS-3124", backend="jira", url="https://jira.test/browse/OPS-3124"
        )

        await _deliver_notification(
            body, _target(), ticket.ref, NotificationDelivery(ticket=ticket)
        )

        assert fake_telegram_send.calls[0]["text"] == (
            "*! Warning: Inverter offline*\n"
            "📍 Grid: Acme Grid\n"
            "🎫 Ticket: [OPS-3124](https://jira.test/browse/OPS-3124)"
        )
        assert "Inverter output stopped" not in fake_telegram_send.calls[0]["text"]

    async def test_internal_ticket_notification_uses_app_ticket_link(
        self, fake_telegram_send, monkeypatch
    ):
        from orchestrator.api.app import _deliver_notification

        monkeypatch.setenv("APP_URL", "https://anansi.test/")
        body = _notify_body(text="Inverter output stopped", alert={"subject": "Inverter offline"})
        ticket = NotificationTicket(ref="TKT-00101", backend="internal")

        await _deliver_notification(
            body, _target(), ticket.ref, NotificationDelivery(ticket=ticket)
        )

        assert fake_telegram_send.calls[0]["text"] == (
            "*Inverter offline*\n"
            "📍 Grid: Acme Grid\n"
            "🎫 Ticket: [TKT-00101](https://anansi.test/tickets/TKT-00101)"
        )

    async def test_ticket_url_is_escaped_for_telegram_markdown(self, fake_telegram_send):
        from orchestrator.api.app import _deliver_notification

        body = _notify_body(alert={"subject": "Inverter offline"})
        ticket = NotificationTicket(
            ref="OPS-42", backend="jira", url="https://jira.test/browse/(OPS-42)"
        )

        await _deliver_notification(
            body, _target(), ticket.ref, NotificationDelivery(ticket=ticket)
        )

        assert "[OPS-42](https://jira.test/browse/%28OPS-42%29)" in fake_telegram_send.calls[0]["text"]

    async def test_urgent_ticket_notification_uses_red_indicator(
        self, fake_telegram_send
    ):
        from orchestrator.api.app import _deliver_notification

        body = _notify_body(text="Grid is unreachable", alert={"subject": "! Urgent: Grid down"})
        ticket = NotificationTicket(ref="OPS-77", backend="jira", url="https://jira.test/browse/OPS-77")

        await _deliver_notification(
            body, _target(), ticket.ref, NotificationDelivery(ticket=ticket)
        )

        assert fake_telegram_send.calls[0]["text"].startswith("🔴 *! Urgent: Grid down*")

    async def test_urgent_ticket_notification_includes_cached_live_output(
        self, fake_telegram_send
    ):
        from orchestrator.api.app import _deliver_notification

        body = _notify_body(text="Grid is unreachable", alert={"subject": "! Urgent: Grid down"})
        ticket = NotificationTicket(ref="OPS-77", backend="jira", url="https://jira.test/browse/OPS-77")

        await _deliver_notification(
            body,
            _target(),
            ticket.ref,
            NotificationDelivery(ticket=ticket, alert_context=_live_context(2.4)),
        )

        assert "⚡ Live output: 2.4 kW" in fake_telegram_send.calls[0]["text"]

    async def test_warning_update_to_urgent_ticket_includes_unavailable_output(
        self, fake_telegram_send
    ):
        from orchestrator.api.app import _deliver_notification

        body = _notify_body(text="A component changed", alert={"subject": "! Warning: Component changed"})
        ticket = NotificationTicket(ref="TKT-1", backend="internal")

        await _deliver_notification(
            body,
            _target(),
            ticket.ref,
            NotificationDelivery(
                ticket=ticket,
                alert_context=_live_context(None),
                ticket_summary="! Urgent: Grid down",
            ),
        )

        text = fake_telegram_send.calls[0]["text"]
        assert text.startswith("🔴 ")
        assert "⚡ Live output: unavailable" in text

    async def test_warning_promoted_to_urgent_root_cause_uses_live_output(
        self, fake_telegram_send
    ):
        from orchestrator.api.app import _deliver_notification

        body = _notify_body(
            text="Multiple components changed",
            alert={"subject": "! Warning: Multiple MPPTs offline"},
        )
        ticket = NotificationTicket(ref="TKT-2", backend="internal")
        delivery = NotificationDelivery(
            ticket=ticket,
            alert_context=build_urgent_alert_context(
                subject="! Warning: Multiple MPPTs offline",
                incoming_severity="warning",
                grid_name="Acme Grid",
                read_output=lambda _grid_name: _return_live_output(3.1),
            ),
            ticket_summary="! Urgent: Acme Grid root cause (grid_off)",
        )

        await _deliver_notification(body, _target(), ticket.ref, delivery)

        text = fake_telegram_send.calls[0]["text"]
        assert text.startswith("🔴 *! Warning: Multiple MPPTs offline*")
        assert "⚡ Live output: 3.1 kW" in text

    async def test_internal_ticket_without_app_url_is_unlinked(
        self, fake_telegram_send, monkeypatch
    ):
        from orchestrator.api.app import _deliver_notification

        monkeypatch.delenv("APP_URL", raising=False)
        body = _notify_body(text="Inverter output stopped", alert={"subject": "Inverter offline"})
        ticket = NotificationTicket(ref="TKT-00101", backend="internal")

        await _deliver_notification(
            body, _target(), ticket.ref, NotificationDelivery(ticket=ticket)
        )

        assert fake_telegram_send.calls[0]["text"].endswith("🎫 Ticket: *TKT-00101*")

    @pytest.mark.parametrize("parse_mode", [None, "HTML"])
    async def test_ticketed_notification_omits_body_and_forces_telegram_markdown(
        self, fake_telegram_send, parse_mode
    ):
        from orchestrator.api.app import _deliver_notification

        body = _notify_body(
            text="See [runbook](https://example.test/runbook) before restarting",
            parse_mode=parse_mode,
            alert={"subject": "Inverter offline"},
        )
        ticket = NotificationTicket(
            ref="OPS-3124", backend="jira", url="https://jira.test/browse/OPS-3124"
        )

        await _deliver_notification(
            body, _target(), ticket.ref, NotificationDelivery(ticket=ticket)
        )

        call = fake_telegram_send.calls[0]
        assert call["parse_mode"] == "Markdown"
        assert "runbook" not in call["text"]
        assert "[OPS-3124](https://jira.test/browse/OPS-3124)" in call["text"]

    async def test_new_ticket_delivery_sends_full_text_no_reply(self, fake_telegram_send):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        body = _notify_body(text="Full alert text")
        delivery = NotificationDelivery(record_message_id_for_ticket_ref="TKT-000001")

        await _deliver_notification(body, _target(), "TKT-000001", delivery)

        assert len(fake_telegram_send.calls) == 1
        call = fake_telegram_send.calls[0]
        assert "Full alert text" in call["text"]
        assert call["reply_to_message_id"] is None

    async def test_amend_delivery_sends_linked_short_text_as_reply(
        self, fake_telegram_send, monkeypatch
    ):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        monkeypatch.setenv("APP_URL", "https://anansi.test")
        body = _notify_body()
        delivery = NotificationDelivery(
            text_override="MPPT A7 also affected (2 components)",
            reply_to_message_id=555,
            ticket=NotificationTicket(ref="TKT-000042", backend="internal"),
        )

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        call = fake_telegram_send.calls[0]
        assert call["text"] == (
            "↻ [TKT-000042](https://anansi.test/tickets/TKT-000042) — "
            "MPPT A7 also affected (2 components)"
        )
        assert call["reply_to_message_id"] == 555

    async def test_duplicate_suppressed_sends_nothing(self, fake_telegram_send):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        body = _notify_body()
        delivery = NotificationDelivery(suppress=True)

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        assert fake_telegram_send.calls == []

    async def test_rollup_delivery_sends_linked_reply(self, fake_telegram_send, monkeypatch):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        monkeypatch.setenv("APP_URL", "https://anansi.test")
        body = _notify_body()
        delivery = NotificationDelivery(
            text_override="still firing — 10 occurrences",
            reply_to_message_id=555,
            ticket=NotificationTicket(ref="TKT-000042", backend="internal"),
        )

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        assert len(fake_telegram_send.calls) == 1
        assert fake_telegram_send.calls[0]["text"] == (
            "↻ [TKT-000042](https://anansi.test/tickets/TKT-000042) — "
            "still firing — 10 occurrences"
        )
        assert fake_telegram_send.calls[0]["reply_to_message_id"] == 555

    async def test_escalation_delivery_is_top_level_not_a_reply(self, fake_telegram_send):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        body = _notify_body()
        delivery = NotificationDelivery(
            text_override="4 MPPTs in Kudi affected (A3, A5, A6, A7)",
            reply_to_message_id=555,  # present but must be ignored -- top_level wins
            top_level=True,
            alert_context=_live_context(None),
            ticket=NotificationTicket(
                ref="OPS-42", backend="jira", url="https://jira.test/browse/OPS-42"
            ),
        )

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        call = fake_telegram_send.calls[0]
        assert call["text"] == (
            "🔴 [OPS-42](https://jira.test/browse/OPS-42) — "
            "4 MPPTs in Kudi affected (A3, A5, A6, A7)\n"
            "⚡ Live output: unavailable"
        )
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

    async def test_edit_message_id_success_skips_new_send(self, fake_telegram_send, monkeypatch):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        edit_calls: List[Dict[str, Any]] = []

        async def _edit(bot_token, chat_id, message_id, text, *, parse_mode=None):
            edit_calls.append(
                {
                    "bot_token": bot_token,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": parse_mode,
                }
            )
            return True

        monkeypatch.setattr("shared.utils.telegram_send.edit_telegram_message", _edit)
        body = _notify_body()
        delivery = NotificationDelivery(
            text_override="TKT-000042: MPPT A7 also affected",
            edit_message_id=555,
            reply_to_message_id=555,
            ticket=NotificationTicket(ref="TKT-000042", backend="internal"),
        )

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        assert len(edit_calls) == 1
        assert edit_calls[0]["message_id"] == 555
        assert fake_telegram_send.calls == []

    async def test_edit_message_id_failure_falls_back_to_send(
        self, fake_telegram_send, monkeypatch
    ):
        from orchestrator.api.app import NotificationDelivery, _deliver_notification

        edit_calls: List[Dict[str, Any]] = []

        async def _edit(bot_token, chat_id, message_id, text, *, parse_mode=None):
            edit_calls.append({"message_id": message_id})
            return False

        monkeypatch.setattr("shared.utils.telegram_send.edit_telegram_message", _edit)

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
        delivery = NotificationDelivery(
            text_override="TKT-000042: MPPT A7 also affected",
            edit_message_id=555,
            reply_to_message_id=555,
            record_message_id_for_ticket_ref="TKT-000042",
            ticket=NotificationTicket(ref="TKT-000042", backend="internal"),
        )

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        assert len(edit_calls) == 1
        assert len(fake_telegram_send.calls) == 1
        assert fake_telegram_send.calls[0]["reply_to_message_id"] == 555
        # Fallback send behaves exactly as an edit-less delivery would --
        # the new message still gets recorded as the ticket's tracked id.
        assert recorded == [("TKT-000042", 999)]


def test_amend_delivery_names_new_component_and_distinct_total():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="TKT-000042",
        confidence=0.9,
        decided_by="llm",
        reason="same issue",
        affected_key={"kind": "mppt", "key": "A7", "label": "MPPT A7"},
        root_cause_kind=None,
        update_message="ignored LLM wording",
        amended_summary="",
        candidate_refs=["TKT-000042"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="TKT-000042",
        decision="amend",
        escalated=False,
        affected_keys_count=2,
        occurrence_count=3,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
        component_added=True,
        rendered_summary="TKT-000042: MPPT A3, MPPT A7 affected (2 components)",
    )
    ticket = NotificationTicket(ref="TKT-000042", backend="internal")

    delivery = _amend_delivery(decision, amendment, ticket)

    assert delivery.ticket == ticket
    assert delivery.text_override == amendment.rendered_summary
    assert delivery.text_override != "Added MPPT A7 (2 affected components)"
    assert delivery.reply_to_message_id == 555
    assert delivery.edit_message_id == 555


def test_amend_delivery_falls_back_to_added_label_phrasing_when_rendered_summary_blank():
    """The Jira-only-seed path can hand back an ``AmendmentResult`` without a
    full rendered ticket summary (``rendered_summary`` defaults to ``""``).
    In that case ``_amend_delivery`` must keep the older "Added X (...)"
    phrasing instead of posting/editing to blank text."""
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="TKT-000042",
        confidence=0.9,
        decided_by="llm",
        reason="same issue",
        affected_key={"kind": "mppt", "key": "A7", "label": "MPPT A7"},
        root_cause_kind=None,
        update_message="ignored LLM wording",
        amended_summary="",
        candidate_refs=["TKT-000042"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="TKT-000042",
        decision="amend",
        escalated=False,
        affected_keys_count=2,
        occurrence_count=3,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
        component_added=True,
        rendered_summary="",
    )
    ticket = NotificationTicket(ref="TKT-000042", backend="internal")

    delivery = _amend_delivery(decision, amendment, ticket)

    assert delivery.text_override == "Added MPPT A7 (2 affected components)"
    assert delivery.edit_message_id == 555


def test_amend_delivery_is_silent_when_no_component_was_added():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3352",
        confidence=0.9,
        decided_by="llm",
        reason="same root cause",
        affected_key={"kind": "mppt", "key": "J47M", "label": "MPPT J47M"},
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3352"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3352",
        decision="amend",
        escalated=False,
        affected_keys_count=16,
        occurrence_count=42,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3352", backend="jira")
    )

    assert delivery.suppress is True
    assert delivery.text_override is None


def test_amend_delivery_never_announces_a_nameless_component():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3353",
        confidence=0.9,
        decided_by="llm",
        reason="grid level",
        affected_key=None,
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3353"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3353",
        decision="amend",
        escalated=False,
        affected_keys_count=0,
        occurrence_count=9,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3353", backend="jira")
    )

    assert delivery.suppress is True


def test_amend_delivery_posts_escalation_without_a_component_add():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3353",
        confidence=0.9,
        decided_by="llm",
        reason="urgent now",
        affected_key=None,
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3353"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3353",
        decision="amend",
        escalated=True,
        affected_keys_count=0,
        occurrence_count=9,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3353", backend="jira")
    )

    assert delivery.suppress is False
    assert delivery.top_level is True
    assert delivery.text_override == "Escalated to urgent"


def test_amend_delivery_escalation_moves_the_edit_target_to_the_new_post():
    """An escalation posts a brand-new top-level message. A future amend's
    edit must target *that* new message, not the stale original -- so the
    escalation branch has to set record_message_id_for_ticket_ref, the same
    way a freshly-filed ticket does."""
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3353",
        confidence=0.9,
        decided_by="llm",
        reason="urgent now",
        affected_key=None,
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3353"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3353",
        decision="amend",
        escalated=True,
        affected_keys_count=0,
        occurrence_count=9,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
        component_added=False,
        rendered_summary="🔴 Escalated summary",
    )
    ticket = NotificationTicket(ref="OPS-3353", backend="jira")

    delivery = _amend_delivery(decision, amendment, ticket)

    assert delivery.top_level is True
    assert delivery.record_message_id_for_ticket_ref == ticket.ref


def test_duplicate_delivery_is_silent():
    from orchestrator.api.app import _duplicate_delivery

    amendment = AmendmentResult(
        ticket_ref="OPS-42",
        decision="duplicate",
        escalated=False,
        affected_keys_count=1,
        occurrence_count=10,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
    )

    delivery = _duplicate_delivery(
        amendment, NotificationTicket(ref="OPS-42", backend="jira")
    )

    assert delivery.suppress is True
