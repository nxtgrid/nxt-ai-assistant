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
    _log_notification_to_chat_db,
    _resolve_notify_ticket,
    _resolve_notify_ticket_full,
    handle_notify,
)
from orchestrator.services.ticketing.alert_facts import AlertFacts
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
        self.create_result = TicketResult(
            ref="TKT-000001", backend="internal", url=None, ticket_id="ticket-000001"
        )
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


@pytest.mark.asyncio
async def test_auto_routes_to_llm_judgment_resolver_when_enabled(monkeypatch):
    """The rollout flag changes only the auto-correlation resolver boundary."""
    from orchestrator.api import app as app_module

    monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
    monkeypatch.setenv("ALERT_LLM_JUDGMENT_ENABLED", "true")
    captured: dict[str, Any] = {}
    expected = ("OPS-1234", None, {"send_decision": "send"}, NotificationDelivery())

    async def fake_llm_resolver(*args: Any):
        captured["args"] = args
        return expected

    monkeypatch.setattr(app_module, "_resolve_notify_ticket_llm_judgment", fake_llm_resolver)
    body = _notify_body(
        ticket_id="auto",
        alert=AlertFacts(subject="! Warning: inverter communication lost"),
    )

    result = await app_module._resolve_notify_ticket_auto(
        body, _target(), "internal", _live_context(None)
    )

    assert result == expected
    assert captured["args"][0] is body
    assert captured["args"][1].grid_name == "Acme Grid"


def _live_context(output_kw: Optional[float], battery_voltage_v: Optional[float] = None):
    async def read_telemetry(_grid_name: str) -> Dict[str, Optional[float]]:
        return {"output_kw": output_kw, "battery_voltage_v": battery_voltage_v}

    return build_urgent_alert_context(
        subject="! Urgent: Grid down",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_telemetry=read_telemetry,
    )


async def _return_live_telemetry(
    output_kw: Optional[float], battery_voltage_v: Optional[float] = None
) -> Dict[str, Optional[float]]:
    return {"output_kw": output_kw, "battery_voltage_v": battery_voltage_v}


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
        ref="TKT-000001", backend="internal", url=None, ticket_id="ticket-000001"
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
    # The TestResolveNotifyTicketAuto* suites below assert the deterministic
    # ladder's own new/amend/duplicate/replay decisions. ALERT_LLM_JUDGMENT_ENABLED
    # now defaults on, which routes `auto` to the judgment resolver instead --
    # a different path, covered by test_auto_routes_to_llm_judgment_resolver_
    # when_enabled (which re-enables it in-test, overriding this) and by the
    # fail-open storm test in test_notify_alert_storm.py. Pinned off here so
    # these suites keep testing the ladder they were written for, which is
    # still what runs whenever judgment is disabled.
    monkeypatch.setenv("ALERT_LLM_JUDGMENT_ENABLED", "false")


@pytest.fixture(autouse=True)
def _reset_correlation_store_failure_counter():
    """Several tests in this file exercise the *real* CorrelationStore
    against no live database (no client faked in), which now feeds the
    module-level failure counter behind /health and /chat/notify's
    ``correlation_degraded`` flag. Reset it around every test here so one
    test's incidental network failure can't flip another's response shape."""
    import orchestrator.services.ticketing.correlation_store as store_module

    store_module._failure_counts.clear()
    store_module._failure_last_logged_at.clear()
    yield
    store_module._failure_counts.clear()
    store_module._failure_last_logged_at.clear()


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
    # 2, not 1, since Phase 5 of docs/superpowers/plans/2026-08-06-user-designed-skills.md
    # added a second background task (dispatch_skill_alert_trigger) queued
    # alongside the pre-existing _deliver_notification -- the response body
    # this test is actually about stays byte-identical either way.
    assert len(background_tasks.tasks) == 2


async def test_handle_notify_surfaces_correlation_degraded_when_the_store_is_failing(
    monkeypatch,
):
    """A degraded correlation store must be visible to the caller (n8n), not
    only in logs -- see the 2026-08-10 incident this exists to catch."""
    import orchestrator.services.ticketing.correlation_store as store_module

    monkeypatch.setattr(
        "shared.auth.get_auth_service", lambda: _FakeAuthService(_target())
    )
    store_module._record_failure("upsert_correlation", RuntimeError("column does not exist"))
    request = _FakeRequest(headers={"X-Notify-Secret": "test-secret"})
    body = _notify_body(ticket_id=None)
    background_tasks = BackgroundTasks()

    response = await handle_notify(request, body, background_tasks)  # type: ignore[arg-type]

    import json

    assert json.loads(response.body) == {"ok": True, "correlation_degraded": True}


async def test_health_check_reports_correlation_store_failures():
    import orchestrator.services.ticketing.correlation_store as store_module
    from orchestrator.api.app import health_check

    result = await health_check()
    assert result["correlation_store_failures_last_hour"] == 0

    store_module._record_failure("get_correlation", RuntimeError("down"))

    result = await health_check()
    assert result["correlation_store_failures_last_hour"] == 1


async def test_handle_notify_create_ticket_returns_ref_in_response(monkeypatch):
    monkeypatch.setattr(
        "shared.auth.get_auth_service", lambda: _FakeAuthService(_target())
    )
    # A real (unfaked) CorrelationStore would fail its network call in this
    # test environment, which would legitimately flip on correlation_degraded
    # -- faked here so this test's exact response-shape assertion stays about
    # what it's actually testing (ticket_ref/decision fields).
    _patch_recording_store(monkeypatch)
    request = _FakeRequest(headers={"X-Notify-Secret": "test-secret"})
    body = _notify_body(ticket_id="")
    background_tasks = BackgroundTasks()

    response = await handle_notify(request, body, background_tasks)  # type: ignore[arg-type]

    assert response.status_code == 202
    import json

    content = json.loads(response.body)
    # A blank ticket_id now runs through the same correlation path as "auto"
    # (ALERT_CORRELATION_ENABLED is off by default here, so it fails open to
    # a plain "new" ticket via decided_by="flag_off") -- the ticket_ref is
    # still what matters for existing callers.
    assert content == {
        "ok": True,
        "ticket_ref": "TKT-000001",
        "decision": "new",
        "decided_by": "flag_off",
        "confidence": None,
        "correlated_with": None,
    }


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
        ticket_id=None,
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

    async def record_event_ticket_id(self, dedup_key: str, ticket_id: str) -> bool:
        self.backfill_calls.append((dedup_key, ticket_id))
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


class _FakeDeliveryRepository:
    """Fake DeliveryRepository for tests exercising
    ``_finalize_correlation_decision``'s reply/edit-anchor resolution.

    Post-0005b ``AmendmentResult``/``CorrelationStore`` no longer carry a
    Telegram message id -- ``_finalize_correlation_decision`` resolves the
    anchor itself via ``DeliveryRepository.latest_for_ticket(ticket_id)``.
    ``anchors_by_ticket_id`` is a class attribute (same pattern as
    ``_FakeCorrelator.decision_to_return``) so a test can seed it before the
    repository gets constructed fresh inside that function.
    """

    instances: List["_FakeDeliveryRepository"] = []
    anchors_by_ticket_id: Dict[str, Dict[str, Any]] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.record_calls: List[Dict[str, Any]] = []
        _FakeDeliveryRepository.instances.append(self)

    async def latest_for_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        return _FakeDeliveryRepository.anchors_by_ticket_id.get(ticket_id)

    async def record(self, **kwargs: Any) -> None:
        self.record_calls.append(kwargs)


def _patch_delivery_repository(monkeypatch) -> None:
    monkeypatch.setattr(
        "orchestrator.services.ticketing.delivery_repository.DeliveryRepository",
        _FakeDeliveryRepository,
    )


@pytest.fixture(autouse=True)
def _reset_fake_delivery_repository():
    _FakeDeliveryRepository.instances = []
    _FakeDeliveryRepository.anchors_by_ticket_id = {}
    yield
    _FakeDeliveryRepository.instances = []
    _FakeDeliveryRepository.anchors_by_ticket_id = {}


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
        # ticket_id carried on the delivery is what lets _deliver_notification's
        # DeliveryRepository receipt (keyed by ticket.ticket_id) stand in for
        # the deleted record_message_id_for_ticket_ref plumbing.
        assert delivery.ticket is not None
        assert delivery.ticket.ticket_id == "ticket-000001"
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
        assert [u["ticket_id"] for u in upserts] == ["ticket-000001"]
        backfills = [
            b for s in _RecordingCorrelationStore.instances for b in s.backfill_calls
        ]
        assert backfills == [("alert-flagoff-1", "ticket-000001")]


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
        assert delivery.ticket is not None
        assert delivery.ticket.ticket_id == "ticket-000001"
        assert delivery.suppress is False
        assert delivery.reply_to_message_id is None

    async def test_new_ticket_with_a_component_seeds_the_equipment_list_at_creation(
        self, monkeypatch
    ):
        """B5: a ticket's description must not change shape between its
        first and second alert -- so a fresh ticket whose alert already
        names a component is created WITH the affected-equipment block
        leading the description, not with bare raw text that only grows a
        marker block on the next (amend) alert."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _FakeCorrelator.decision_to_return = _decision(
            decision="new", decided_by="no_candidates", confidence=None
        )
        subject = "! Warning: MPPT A3 in Acme Grid seems to perform lower !"
        body = _notify_body(
            ticket_id="auto", text=subject, alert={"subject": subject, "details": "mppt A3 [Acme Grid]"}
        )

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        svc = _FakeTicketService.instances[-1]
        req, _backend_override = svc.create_ticket_calls[0]
        assert req.description.startswith("[anansi:affected-start]")
        assert "MPPT A3" in req.description
        assert subject in req.description  # the raw alert text still appears, trailing

    async def test_new_ticket_without_a_component_keeps_a_bare_description(self, monkeypatch):
        """The bare-description counterpart of the test above -- a
        grid-level alert with no identifiable component must not grow an
        empty "Affected components (0):" block."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _FakeCorrelator.decision_to_return = _decision(
            decision="new", decided_by="no_candidates", confidence=None
        )
        body = _notify_body(ticket_id="auto", text="Meter offline\n\ndetails")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        svc = _FakeTicketService.instances[-1]
        req, _backend_override = svc.create_ticket_calls[0]
        assert req.description == "Meter offline\n\ndetails"
        assert "[anansi:affected-start]" not in req.description


class TestResolveNotifyTicketAutoAmend:
    async def test_amend_decision_calls_apply_amendment(
        self, fake_apply_amendment, monkeypatch
    ):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _patch_delivery_repository(monkeypatch)
        _FakeDeliveryRepository.anchors_by_ticket_id["ticket-42"] = {
            "external_message_id": 123
        }
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-000042",
            ticket_id="ticket-42",
            decision="amend",
            escalated=False,
            affected_keys_count=2,
            occurrence_count=3,
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
        assert delivery.ticket == NotificationTicket(
            ref="TKT-000042", backend="internal", ticket_id="ticket-42"
        )

    async def test_replayed_amendment_uses_silent_executor_result(
        self, fake_apply_amendment, monkeypatch
    ):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-000042",
            ticket_id="ticket-42",
            decision="duplicate",
            escalated=False,
            affected_keys_count=2,
            occurrence_count=3,
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
            """Ticket_id-keyed, but single-ticket -- every method ignores
            which id it's called with and operates on the one correlation
            row this test cares about, since only OPS-42/"ops-42-tid" is
            ever in play here."""

            def __init__(self) -> None:
                self.correlation: Optional[Dict[str, Any]] = None

            async def get_correlation(self, ticket_id):
                return self.correlation

            async def bump_occurrence(self, ticket_id, occurred_at=None):
                if self.correlation is not None:
                    self.correlation["occurrence_count"] += 1
                return self.correlation is not None

            async def merge_affected_key(self, *args, **kwargs):
                return None

            async def upsert_correlation(self, **kwargs):
                self.correlation = {**kwargs, "occurrence_count": 1, "escalated_at": None}
                return True

            async def record_amendment(self, ticket_id, *, severity=None, escalated=False):
                assert self.correlation is not None
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
                        ticket_id="ops-42-tid",
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
                correlation = await self.store.get_correlation("ops-42-tid")
                return _decision(
                    decision="amend",
                    ticket_ref="OPS-42",
                    ticket_id="ops-42-tid",
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
        # Unlike the old special-cased "correlation row missing" branch
        # (which left the Jira description untouched, posting description=None),
        # the unified seed-then-amend flow always renders a description --
        # consistent with every other amend, even a freshly-seeded one. This
        # fixture's affected_keys stays [] throughout (merge_affected_key is
        # faked to always return None, i.e. a no-op merge), so B5's
        # bare-description rule applies: no marker block, just the raw text.
        assert update_calls == [
            {
                "ref": "OPS-42",
                "summary": "🔴 ! Urgent: Kudi inverter outage",
                "description": "urgent raw text",
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
            ticket_id="ops-3353-tid",
            decision="amend",
            escalated=True,
            affected_keys_count=0,
            occurrence_count=4,
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
            ticket_id="ops-3363-tid",
            decision="amend",
            escalated=True,
            affected_keys_count=0,
            occurrence_count=2,
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

    async def test_new_ticket_backfills_ticket_id_onto_its_event_row(self, monkeypatch):
        """Regression test for the delivery-idempotency gap a code reviewer
        flagged in this fix: a "new" decision's ``ticket_correlation_events``
        row is written with ``ticket_id=None`` (nothing exists yet at
        decide-time -- see AlertCorrelator._finalize), so without a backfill
        a later replay of the same dedup_key would find ``ticket_id=None``,
        fail the replay guard's truthiness check, and file a duplicate
        ticket. This exercises the actual app.py wiring added in
        _resolve_notify_ticket_auto -- that it calls
        store.record_event_ticket_id(dedup_key, <new ticket's canonical id>)
        immediately after a brand-new ticket is created and correlated."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")

        class _FakeStore:
            def __init__(self) -> None:
                self.upsert_calls: List[Dict[str, Any]] = []
                self.backfill_calls: List[tuple] = []

            async def upsert_correlation(self, **kwargs):
                self.upsert_calls.append(kwargs)
                return True

            async def record_event_ticket_id(self, dedup_key, ticket_id):
                self.backfill_calls.append((dedup_key, ticket_id))
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
        # The new ticket's canonical id must be backfilled onto the
        # dedup_key's event row -- exactly once -- so a later replay's
        # get_by_dedup_key() lookup finds a populated ticket_id and the
        # guard above can suppress correctly.
        assert ref == "TKT-000001"
        assert store.backfill_calls == [("alert-42", "ticket-000001")]


class TestResolveNotifyTicketAutoDuplicate:
    async def test_duplicate_decision_returns_existing_ref_without_new_ticket(
        self, fake_apply_amendment, monkeypatch
    ):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-000042",
            ticket_id="ticket-42",
            decision="duplicate",
            escalated=False,
            affected_keys_count=1,
            occurrence_count=5,
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

    async def test_blank_ticket_id_gets_same_duplicate_treatment_as_auto(
        self, fake_apply_amendment, monkeypatch
    ):
        """A blank ticket_id ("") must run the same correlation pipeline as
        "auto" -- see _resolve_notify_ticket_full's docstring. Regression
        test for the bug where "" silently bypassed correlation entirely
        (no candidate lookup, no ticket_correlations row), so a caller that
        stopped sending "auto" got a fresh Jira ticket per identical alert
        forever instead of an error or a dropped alert."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="OPS-3368",
            ticket_id="ops-3368-tid",
            decision="duplicate",
            escalated=False,
            affected_keys_count=1,
            occurrence_count=2,
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="duplicate",
            ticket_ref="OPS-3368",
            confidence=1.0,
            decided_by="signature",
        )
        body = _notify_body(ticket_id="")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "OPS-3368"
        assert extra["decision"] == "duplicate"
        assert extra["decided_by"] == "signature"
        assert _FakeTicketService.instances[-1].create_ticket_calls == []
        assert delivery is not None


class TestResolveNotifyTicketAutoRootCauseFirst:
    async def test_root_cause_ticket_filed_then_amended(self, fake_apply_amendment, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-000001",  # matches the fake TicketService's create_ticket result
            ticket_id="ticket-000001",
            decision="amend",
            escalated=False,
            affected_keys_count=1,
            occurrence_count=1,
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
        assert delivery.ticket is not None
        assert delivery.ticket.ticket_id == "ticket-000001"
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
        assert delivery.ticket is not None
        assert delivery.ticket.ticket_id == "ticket-000001"

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
        assert [u["ticket_id"] for u in upserts] == ["ticket-000001"]
        backfills = [
            b for s in _RecordingCorrelationStore.instances for b in s.backfill_calls
        ]
        assert backfills == [("alert-decision-none-1", "ticket-000001")]

    async def test_lock_timeout_falls_back_to_plain_create(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _RecordingCorrelationStore.instances = []
        _patch_recording_store(monkeypatch)

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
        assert delivery.ticket is not None
        assert delivery.ticket.ticket_id == "ticket-000001"

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
        assert [u["ticket_id"] for u in upserts] == ["ticket-000001"]
        backfills = [
            b for s in _RecordingCorrelationStore.instances for b in s.backfill_calls
        ]
        assert backfills == [("alert-lock-timeout-1", "ticket-000001")]

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
                "ticket_id": "existing-1-tid",
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
            ticket_id="existing-1-tid",
            decision="duplicate",
            escalated=False,
            affected_keys_count=1,
            occurrence_count=2,
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
        assert recorded[0]["ticket_id"] == "existing-1-tid"
        assert recorded[0]["decided_by"] == "fallback_signature"

        assert delivery is not None
        assert delivery.suppress is True

    async def test_lock_timeout_with_new_component_on_matching_signature_amends(
        self, monkeypatch, fake_apply_amendment
    ):
        """B3 regression: the lock-free path must run the signature-amend
        rung too, not just the two duplicate rungs -- otherwise a storm that
        happens to hit a lock timeout mid-burst reverts to filing a fresh
        ticket per device instead of collapsing onto the one already found
        by find_deterministic_decision under the lock."""
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

        subject = "! Warning: MPPT B9 in Acme Grid seems to perform lower !"
        base_alert = AlertFacts(subject=subject, details="mppt B9 [Acme Grid]")
        computed_alert = enrich_alert_facts(base_alert, grid_name="Acme Grid")
        assert computed_alert.component_kind == "mppt"
        assert computed_alert.component_key == "B9"

        _RecordingCorrelationStore.open_candidates_to_return = [
            {
                "ticket_id": "existing-1-tid",
                "ticket_ref": "TKT-EXISTING-1",
                "grid_name": "Acme Grid",
                "status": "open",
                "severity": "warning",
                "signatures": [computed_alert.signature],
                # A3 already recorded; B9 (this alert) is a *new* component
                # on the same fault signature -- amend, not duplicate.
                "affected_keys": [{"kind": "mppt", "key": "A3", "label": "MPPT A3"}],
                "created_at": "2026-07-20T00:00:00+00:00",
            }
        ]

        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="TKT-EXISTING-1",
            ticket_id="existing-1-tid",
            decision="amend",
            escalated=False,
            affected_keys_count=2,
            occurrence_count=2,
        )

        body = _notify_body(ticket_id="auto", text=subject, alert=base_alert.model_dump())

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "TKT-EXISTING-1"
        assert extra["decided_by"] == "fallback_signature"
        assert extra["decision"] == "amend"
        assert extra["correlated_with"] == "TKT-EXISTING-1"

        assert all(
            not svc.create_ticket_calls for svc in _FakeTicketService.instances
        )
        assert len(calls) == 1
        assert calls[0]["ticket_ref"] == "TKT-EXISTING-1"
        assert calls[0]["decision"] == "amend"

        recorded = [
            e for s in _RecordingCorrelationStore.instances for e in s.record_event_calls
        ]
        assert len(recorded) == 1
        assert recorded[0]["ticket_id"] == "existing-1-tid"
        assert recorded[0]["decided_by"] == "fallback_signature"
        assert recorded[0]["decision"] == "amend"

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
                "ticket_id": "unrelated-1-tid",
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
        assert [u["ticket_id"] for u in upserts] == ["ticket-000001"]

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
        assert [u["ticket_id"] for u in upserts] == ["ticket-000001"]
        backfills = [
            b for s in _RecordingCorrelationStore.instances for b in s.backfill_calls
        ]
        assert backfills == [("alert-outer-exc-1", "ticket-000001")]

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
        # See test_handle_notify_create_ticket_returns_ref_in_response for
        # why the store is faked rather than left to fail against no live DB.
        _patch_recording_store(monkeypatch)
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


class TestLogNotificationToChatDb:
    """Direct tests of _log_notification_to_chat_db, called via the
    module-level reference imported at the top of this file (captured before
    any test runs) -- called without going through _deliver_notification, so
    the file's autouse _stub_chat_db_logging fixture (below, which patches
    app_module's *attribute*, not this captured function object) doesn't
    apply here."""

    async def test_passes_group_id_to_save_messages(self, monkeypatch):
        """B6: without group_id, the bot's own alert posts are invisible to
        chat_messages reads keyed by group_id -- notably
        ChatWatermarkRepository's topic-scoped scroll count."""
        import orchestrator.services.supabase_client as supabase_client_module

        class _Session:
            id = "session-uuid-1"

        class _SavedMessage:
            id = "msg-uuid-1"

        class _FakeClient:
            def __init__(self) -> None:
                self.save_messages_calls: list[dict[str, Any]] = []

            async def get_session_by_chat_id(self, *, source, chat_id, topic_id=None):
                return _Session()

            async def save_messages(self, session_id, messages, **kwargs):
                self.save_messages_calls.append({"session_id": session_id, **kwargs})
                return [_SavedMessage()]

            async def tag_message_as_ticket_comment(self, message_id, ticket_ref):
                return None

        fake_client = _FakeClient()
        monkeypatch.setattr(
            supabase_client_module, "get_supabase_client", lambda: fake_client
        )

        body = _notify_body(text="! Urgent: MPPT A3 down !")
        await _log_notification_to_chat_db(body, "-100555", "42", 999, ticket_ref="OPS-1")

        assert len(fake_client.save_messages_calls) == 1
        call = fake_client.save_messages_calls[0]
        assert call["group_id"] == "-100555"
        assert call["from_chat_id"] == "-100555"


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

    async def test_records_purpose_update_for_an_amend_rollup_reply(
        self, fake_telegram_send, monkeypatch
    ):
        """An amend/roll-up reply is a short update to an already-notified
        ticket (text_override set), not the original alert -- its delivery
        receipt must be purpose="update", not "notification" (see the design
        doc's three delivery-link kinds: Escalation/Notification/Update)."""
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
            _notify_body(),
            _target(),
            ticket.ref,
            NotificationDelivery(ticket=ticket, text_override="Added MPPT A7 (2 affected components)"),
        )

        assert len(calls) == 1
        assert calls[0]["purpose"] == "update"

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
                read_telemetry=lambda _grid_name: _return_live_telemetry(3.1),
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
        delivery = NotificationDelivery(ticket=NotificationTicket(ref="TKT-000001", backend="internal"))

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

        receipts: List[Dict[str, Any]] = []

        class _Deliveries:
            def __init__(self, **_kwargs):
                pass

            async def record(self, **kwargs):
                receipts.append(kwargs)

        monkeypatch.setattr(
            "orchestrator.services.ticketing.delivery_repository.DeliveryRepository",
            _Deliveries,
        )
        body = _notify_body()
        delivery = NotificationDelivery(
            text_override="TKT-000042: MPPT A7 also affected",
            edit_message_id=555,
            reply_to_message_id=555,
            ticket=NotificationTicket(ref="TKT-000042", backend="internal", ticket_id="ticket-42"),
        )

        await _deliver_notification(body, _target(), "TKT-000042", delivery)

        assert len(edit_calls) == 1
        assert len(fake_telegram_send.calls) == 1
        assert fake_telegram_send.calls[0]["reply_to_message_id"] == 555
        # Fallback send behaves exactly as an edit-less delivery would -- the
        # new message still gets recorded as a message_deliveries receipt.
        assert receipts == [
            {
                "ticket_id": "ticket-42",
                "escalation_id": None,
                "purpose": "update",
                "external_chat_id": "-100555",
                "external_topic_id": "42",
                "external_message_id": 999,
            }
        ]


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
        ticket_id="ticket-42",
        decision="amend",
        escalated=False,
        affected_keys_count=2,
        occurrence_count=3,
        component_added=True,
        rendered_summary="TKT-000042: MPPT A3, MPPT A7 affected (2 components)",
    )
    ticket = NotificationTicket(ref="TKT-000042", backend="internal")

    # reply_to_message_id is resolved by the caller (_finalize_correlation_decision,
    # via DeliveryRepository.latest_for_ticket) and passed in -- not read off
    # amendment, which no longer carries a Telegram coordinate.
    delivery = _amend_delivery(decision, amendment, ticket, reply_to_message_id=555)

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
        ticket_id="ticket-42",
        decision="amend",
        escalated=False,
        affected_keys_count=2,
        occurrence_count=3,
        component_added=True,
        rendered_summary="",
    )
    ticket = NotificationTicket(ref="TKT-000042", backend="internal")

    delivery = _amend_delivery(decision, amendment, ticket, reply_to_message_id=555)

    assert delivery.text_override == "Added MPPT A7 (2 affected components)"
    assert delivery.edit_message_id == 555


def test_amend_delivery_is_silent_when_no_component_was_added():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-1002",
        confidence=0.9,
        decided_by="llm",
        reason="same root cause",
        affected_key={"kind": "mppt", "key": "AB12", "label": "MPPT AB12"},
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-1002"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-1002",
        ticket_id="ticket-3352",
        decision="amend",
        escalated=False,
        affected_keys_count=16,
        occurrence_count=42,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-1002", backend="jira"), 777
    )

    assert delivery.suppress is True
    # Silent, but not empty. text_override used to be None here, which is what
    # made an override (the downtime floor, or the LLM fail-open gate) repost
    # the whole alert instead of a one-line update -- see _forced_send_delivery.
    # The delivery now says what it *would* say and where it would thread, and
    # `suppress` alone decides whether any of it is used.
    assert delivery.text_override == "still firing on MPPT AB12 (42 occurrences)"
    assert delivery.reply_to_message_id == 777
    assert delivery.edit_message_id is None, "an override notifies; an edit does not"


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
        ticket_id="ticket-3353",
        decision="amend",
        escalated=False,
        affected_keys_count=0,
        occurrence_count=9,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3353", backend="jira")
    )

    assert delivery.suppress is True


def test_amend_delivery_posts_escalation_without_a_component_add():
    """B4: the old contentless "Escalated to urgent" branch is gone -- with
    no rendered_summary and no live ticket_summary fallback available, the
    message degrades to the bare phrase (still non-empty, still posted)."""
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
        ticket_id="ticket-3353",
        decision="amend",
        escalated=True,
        affected_keys_count=0,
        occurrence_count=9,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3353", backend="jira")
    )

    assert delivery.suppress is False
    assert delivery.top_level is True
    assert delivery.text_override == "escalated to urgent"


def test_amend_delivery_escalation_includes_summary_with_no_doubled_emoji():
    """B4: rendered_summary already carries its own leading "🔴 " (apply_amendment
    prefixes an escalated summary) -- _format_ticket_update_notification adds
    its own for an urgent/top-level post downstream, so _amend_delivery must
    strip its copy or the pair doubles up into "🔴 OPS-3428 — 🔴 ! Urgent: …"."""
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3428",
        confidence=0.9,
        decided_by="llm",
        reason="urgent now",
        affected_key=None,
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3428"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3428",
        ticket_id="ticket-3428",
        decision="amend",
        escalated=True,
        affected_keys_count=3,
        occurrence_count=6,
        component_added=False,
        rendered_summary="🔴 ! Urgent: 3 MPPTs in GridY affected (A3, A7, B1) !",
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3428", backend="jira")
    )

    assert delivery.text_override == (
        "escalated to urgent — ! Urgent: 3 MPPTs in GridY affected (A3, A7, B1) !"
    )
    assert delivery.text_override.count("🔴") == 0


def test_amend_delivery_escalation_falls_back_to_live_ticket_summary():
    """B4: when rendered_summary is blank (e.g. a degenerate render), fall
    back to the ticket's live summary passed in by the caller, still
    stripped of any leading escalated-marker emoji."""
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
        ticket_id="ticket-3353",
        decision="amend",
        escalated=True,
        affected_keys_count=0,
        occurrence_count=9,
        component_added=False,
        rendered_summary="",
    )

    delivery = _amend_delivery(
        decision,
        amendment,
        NotificationTicket(ref="OPS-3353", backend="jira"),
        ticket_summary="🔴 Existing live ticket summary",
    )

    assert delivery.text_override == "escalated to urgent — Existing live ticket summary"


def test_amend_delivery_escalation_moves_the_edit_target_to_the_new_post():
    """An escalation posts a brand-new top-level message. A future amend's
    edit must target *that* new message, not the stale original.
    ``_deliver_notification`` records a fresh ``message_deliveries`` receipt
    for it (keyed by ``ticket.ticket_id``), which the next amend's
    ``DeliveryRepository.latest_for_ticket`` lookup finds as the newest
    anchor -- so the delivery's ``ticket`` must carry the ticket's id for
    that receipt to be recordable at all."""
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
        ticket_id="ticket-3353",
        decision="amend",
        escalated=True,
        affected_keys_count=0,
        occurrence_count=9,
        component_added=False,
        rendered_summary="🔴 Escalated summary",
    )
    ticket = NotificationTicket(ref="OPS-3353", backend="jira", ticket_id="ticket-3353")

    delivery = _amend_delivery(decision, amendment, ticket)

    assert delivery.top_level is True
    assert delivery.ticket == ticket
    assert delivery.ticket.ticket_id == "ticket-3353"
    assert delivery.text_override == "escalated to urgent — Escalated summary"


def test_amend_delivery_cascade_fold_is_not_suppressed_without_a_component_add():
    """C5: a power_chain fold must never be suppressed, even when it added
    no new *keyed* component (e.g. a blank affected_key) -- linking the two
    pings into one thread is the entire point of the rung."""
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3456",
        confidence=0.92,
        decided_by="llm",
        reason="battery/BMS -> inverter power chain",
        affected_key=None,
        root_cause_kind="power_chain",
        update_message="Inverter shut down after BMS comms loss",
        amended_summary="",
        candidate_refs=["OPS-3456"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3456",
        ticket_id="ticket-3456",
        decision="amend",
        escalated=False,
        affected_keys_count=1,
        occurrence_count=2,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3456", backend="jira"), reply_to_message_id=555
    )

    assert delivery.suppress is False
    assert delivery.top_level is False
    assert delivery.text_override == "Inverter shut down after BMS comms loss"
    assert delivery.reply_to_message_id == 555
    assert delivery.edit_message_id == 555


def test_amend_delivery_cascade_prefers_llm_update_message_over_rendered_summary():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3456",
        confidence=0.92,
        decided_by="llm",
        reason="battery/BMS -> inverter power chain",
        affected_key={"kind": "inverter", "key": "INV1", "label": "Inverter INV1"},
        root_cause_kind="power_chain",
        update_message="Inverter shut down after BMS comms loss",
        amended_summary="",
        candidate_refs=["OPS-3456"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3456",
        ticket_id="ticket-3456",
        decision="amend",
        escalated=False,
        affected_keys_count=2,
        occurrence_count=2,
        component_added=True,
        rendered_summary="! Warning: BMS communication lost — +1 dependent alert (Inverter)",
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3456", backend="jira"), reply_to_message_id=555
    )

    assert delivery.text_override == "Inverter shut down after BMS comms loss"


def test_amend_delivery_cascade_falls_back_to_rendered_summary_when_no_update_message():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3456",
        confidence=0.92,
        decided_by="llm",
        reason="battery/BMS -> inverter power chain",
        affected_key={"kind": "inverter", "key": "INV1", "label": "Inverter INV1"},
        root_cause_kind="power_chain",
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3456"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3456",
        ticket_id="ticket-3456",
        decision="amend",
        escalated=False,
        affected_keys_count=2,
        occurrence_count=2,
        component_added=True,
        rendered_summary="! Warning: BMS communication lost — +1 dependent alert (Inverter)",
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3456", backend="jira"), reply_to_message_id=555
    )

    assert delivery.text_override == "! Warning: BMS communication lost — +1 dependent alert (Inverter)"


def test_amend_delivery_cascade_falls_back_to_generic_phrasing_when_both_blank():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3456",
        confidence=0.92,
        decided_by="llm",
        reason="battery/BMS -> inverter power chain",
        affected_key={"kind": "inverter", "key": "INV1", "label": "Inverter INV1"},
        root_cause_kind="power_chain",
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3456"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3456",
        ticket_id="ticket-3456",
        decision="amend",
        escalated=False,
        affected_keys_count=2,
        occurrence_count=2,
        component_added=True,
        rendered_summary="",
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3456", backend="jira"), reply_to_message_id=555
    )

    assert delivery.text_override == "Folded in as a power_chain symptom: Inverter INV1"


def test_amend_delivery_escalated_cascade_still_posts_the_escalation_message():
    """Precedence: an escalation always wins over the cascade-specific
    phrasing -- B4's urgency handling must not regress just because this
    particular amend also happens to be a power_chain fold."""
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3456",
        confidence=0.92,
        decided_by="llm",
        reason="battery/BMS -> inverter power chain",
        affected_key={"kind": "inverter", "key": "INV1", "label": "Inverter INV1"},
        root_cause_kind="power_chain",
        update_message="Inverter shut down after BMS comms loss",
        amended_summary="",
        candidate_refs=["OPS-3456"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3456",
        ticket_id="ticket-3456",
        decision="amend",
        escalated=True,
        affected_keys_count=2,
        occurrence_count=2,
        component_added=True,
        rendered_summary="🔴 ! Urgent: BMS communication lost — +1 dependent alert (Inverter)",
    )

    delivery = _amend_delivery(decision, amendment, NotificationTicket(ref="OPS-3456", backend="jira"))

    assert delivery.top_level is True
    assert delivery.text_override == (
        "escalated to urgent — ! Urgent: BMS communication lost — +1 dependent alert (Inverter)"
    )


def test_duplicate_delivery_is_silent():
    from orchestrator.api.app import _duplicate_delivery

    amendment = AmendmentResult(
        ticket_ref="OPS-42",
        ticket_id="ticket-42",
        decision="duplicate",
        escalated=False,
        affected_keys_count=1,
        occurrence_count=10,
    )

    delivery = _duplicate_delivery(
        amendment, NotificationTicket(ref="OPS-42", backend="jira")
    )

    assert delivery.suppress is True


# ---------------------------------------------------------------------------
# _deliver_notification -- the downtime delivery floor
# ---------------------------------------------------------------------------


class TestDowntimeDeliveryFloor:
    """Correlation may silence equipment noise; it may not silence a dark grid.

    A never-closed ticket on an unrelated component (the 2026-08-28
    incident: a weeks-old MPPT ticket absorbing every later alert) must not stop
    the topic hearing that the grid is down -- once when it goes down, and
    once more for every day it stays down.
    """

    @staticmethod
    def _context(*, site_status: str = "off", fresh: bool = True, phases: float = 0.0):
        async def _read(_grid_name: str) -> Dict[str, Any]:
            return {
                "generation_management": "managed",
                "grid_status": "off" if site_status == "off" else "fs_on",
                "site_status": site_status,
                "output_kw": 0.0,
                "battery_voltage_v": 51.2,
                "l1_voltage_v": phases,
                "l2_voltage_v": phases,
                "l3_voltage_v": phases,
                "observed_at": "2026-08-28T10:00:00+00:00",
                "fresh": fresh,
            }

        return build_urgent_alert_context(
            subject="! Warning: MPPT A3 seems to perform lower !",
            grid_name="Grid A",
            read_telemetry=_read,
        )

    @staticmethod
    def _patch_ledger(monkeypatch, last_sent_at, recorded: List[Dict[str, Any]]):
        class _Repo:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            async def latest_downtime_sent_at(self, _grid_name: str):
                return last_sent_at

            async def record_success(self, **kwargs: Any):
                recorded.append(kwargs)
                return {"id": "row-1"}

        monkeypatch.setattr(
            "orchestrator.services.ticketing.notify_alert_delivery_repository"
            ".NotifyAlertDeliveryRepository",
            _Repo,
        )

    async def test_newly_down_grid_breaks_through_a_suppressed_duplicate(
        self, fake_telegram_send, monkeypatch
    ):
        from orchestrator.api.app import _deliver_notification

        recorded: List[Dict[str, Any]] = []
        self._patch_ledger(monkeypatch, None, recorded)

        await _deliver_notification(
            _notify_body(text="! Warning: MPPT A3 seems to perform lower !"),
            _target(),
            "OPS-1001",
            NotificationDelivery(suppress=True, alert_context=self._context()),
        )

        assert len(fake_telegram_send.calls) == 1
        assert recorded and recorded[0]["downtime"] is True

    async def test_still_down_a_day_later_breaks_through_again(
        self, fake_telegram_send, monkeypatch
    ):
        from datetime import datetime, timedelta, timezone

        from orchestrator.api.app import _deliver_notification

        recorded: List[Dict[str, Any]] = []
        yesterday = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self._patch_ledger(monkeypatch, yesterday, recorded)

        await _deliver_notification(
            _notify_body(), _target(), "OPS-1001",
            NotificationDelivery(suppress=True, alert_context=self._context()),
        )

        assert len(fake_telegram_send.calls) == 1

    async def test_second_downtime_alert_the_same_day_stays_suppressed(
        self, fake_telegram_send, monkeypatch
    ):
        from datetime import datetime, timedelta, timezone

        from orchestrator.api.app import _deliver_notification

        recorded: List[Dict[str, Any]] = []
        earlier_today = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self._patch_ledger(monkeypatch, earlier_today, recorded)

        await _deliver_notification(
            _notify_body(), _target(), "OPS-1001",
            NotificationDelivery(suppress=True, alert_context=self._context()),
        )

        assert fake_telegram_send.calls == []

    async def test_healthy_grid_keeps_todays_suppression_behavior(
        self, fake_telegram_send, monkeypatch
    ):
        from orchestrator.api.app import _deliver_notification

        recorded: List[Dict[str, Any]] = []
        self._patch_ledger(monkeypatch, None, recorded)

        await _deliver_notification(
            _notify_body(), _target(), "OPS-1001",
            NotificationDelivery(
                suppress=True,
                alert_context=self._context(site_status="on", phases=230.0),
            ),
        )

        assert fake_telegram_send.calls == []

    async def test_stale_telemetry_keeps_todays_suppression_behavior(
        self, fake_telegram_send, monkeypatch
    ):
        """Unknowable is not "down": the floor only ever adds a send, so it
        must leave the correlation decision alone when it cannot see."""
        from orchestrator.api.app import _deliver_notification

        recorded: List[Dict[str, Any]] = []
        self._patch_ledger(monkeypatch, None, recorded)

        await _deliver_notification(
            _notify_body(), _target(), "OPS-1001",
            NotificationDelivery(suppress=True, alert_context=self._context(fresh=False)),
        )

        assert fake_telegram_send.calls == []

    async def test_an_ordinary_send_while_down_advances_the_daily_clock(
        self, fake_telegram_send, monkeypatch
    ):
        """A downtime alert that was never suppressed still has to mark the
        ledger, or the floor would re-post the same news minutes later."""
        from orchestrator.api.app import _deliver_notification

        recorded: List[Dict[str, Any]] = []
        self._patch_ledger(monkeypatch, None, recorded)

        await _deliver_notification(
            _notify_body(), _target(), "OPS-1001",
            NotificationDelivery(alert_context=self._context()),
        )

        assert len(fake_telegram_send.calls) == 1
        assert recorded and recorded[0]["downtime"] is True
