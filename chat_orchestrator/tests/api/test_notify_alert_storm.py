"""B7 (plan docs/superpowers/plans/2026-08-11-ticketing-noise-and-correlation-cutover.md):
burst regression test, built from the real 2026-08-08 GridY "No BMS" storm
(finding 1) plus the component-less GridX/GridY Solar Charger miss
(finding 2). Six MPPT devices and one bracket-only Solar Charger device, same
fault, same grid, arriving back-to-back -- must collapse onto one ticket with
one edited-in-place Telegram message, not seven tickets/seven posts.

This drives the *real* AlertCorrelator, alert_facts normalization, and
apply_amendment end to end through the /notify HTTP-level entry points
(_resolve_notify_ticket_full + _deliver_notification) -- only the storage
layer (CorrelationStore, TicketService, DeliveryRepository) and the Telegram
transport are faked, and the LLM gateway is wired to explode if ever called,
so a regression that makes B1-B3's deterministic ladder fall through to the
LLM fails loudly rather than silently passing via a fake LLM decision.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.api.app import NotifyRequest, _resolve_notify_ticket_full
from orchestrator.services.ticketing.backend import (
    TicketCreateOutcome,
    TicketResult,
    TicketStatus,
)
from orchestrator.services.ticketing.correlation_store import AffectedKeyMerge
from shared.auth.auth_service import GridNotificationTarget

GRID = "GridY"

# The real 2026-08-08 storm subjects (plan finding 1's table), reconstructed
# in the full VRM alert shape ("ALERT - '<grid>': '<fault>' on '<device>'")
# that finding 1 traces the signature bug through, plus finding 2's
# component-less Solar Charger device on the same fault.
_SUBJECTS = [
    "! Urgent: ALERT - 'GridY': '#67 - No BMS' on "
    "'Solar Charger - MPPT KBUA ARTN4.4/-176/5 Cabin [5]' !",
    "! Urgent: ALERT - 'GridY': '#67 - No BMS' on "
    "'Solar Charger - MPPT 65SQ ARTN4.4/-141/32 House [0]' !",
    "! Urgent: ALERT - 'GridY': '#67 - No BMS' on "
    "'Solar Charger - MPPT JD65 ARTN4.4/-176/5 Cabin [3]' !",
    "! Urgent: ALERT - 'GridY': '#67 - No BMS' on "
    "'Solar Charger - MPPT RH2W ARTN4.4/-176/5 Cabin [6]' !",
    "! Urgent: ALERT - 'GridY': '#67 - No BMS' on "
    "'Solar Charger - MPPT QI11 ARTN4.4/+27/24 Church [2]' !",
    "! Urgent: ALERT - 'GridY': '#67 - No BMS' on "
    "'Solar Charger - MPPT LQLA ARTN4.4/27/24 Church [1]' !",
    # finding 2: no "MPPT" word at all -- component-less before B2.
    "! Urgent: ALERT - 'GridY': '#67 - No BMS' on "
    "'Solar Charger - VT6Y ARTN4.4/-141/32 House 4 [8]' !",
]

_EXPECTED_KEYS = {"KBUA#5", "65SQ#0", "JD65#3", "RH2W#6", "QI11#2", "LQLA#1", "VT6Y#8"}

# The real 2026-08-28 storm: seven underperforming-MPPT warnings in one
# minute, split across two tickets -- ids carrying a digit onto one, letters-only
# ids onto the other -- because normalize_subject lowercased before masking,
# which left _looks_like_component_id's all-caps branch permanently false. This
# subject shape has no "on '<device>'" clause, so unlike _SUBJECTS above it is
# not rescued by the wholesale device-clause mask.
_STORM_GRID = "GridZ"
_STORM_DEVICES = ["Q4NR", "QWQJ", "TFPA", "XZAE", "6RJA", "73ZC", "9YRN"]
_STORM_SUBJECTS = [
    f"! Warning: MPPT {device} in GridZ seems to perform lower than other MPPTs !"
    for device in _STORM_DEVICES
]


class _ExplodingGateway:
    """Any call means the deterministic ladder (B1-B3) failed to group this
    storm and fell through to the LLM -- fail loudly, not silently."""

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "LLM must not be called -- this storm is fully deterministic "
            "once B1 (signature normalization) and B2 (Solar Charger "
            "detection) are in place"
        )


class _FakeTicketService:
    """Combines the correlator-facing lookups (find_open_by_grid,
    get_ref_by_id, get_backend_name, get_status) with ticket creation
    (create_ticket_with_internal_fallback) and update (update_ticket,
    add_comment). Shares ``tickets`` with the store fake so
    open_candidates_for_grid can join on grid_name the way the real
    two-step query does.
    """

    def __init__(self, tickets: Dict[str, Dict[str, Any]]) -> None:
        self._tickets = tickets
        self._next_id = 1
        self.create_ticket_calls: List[Any] = []
        self.update_ticket_calls: List[Dict[str, Any]] = []
        self.add_comment_calls: List[tuple] = []

    async def resolve_backend(self, override: Optional[str] = None) -> Any:
        class _Backend:
            name = "internal"

        return _Backend()

    async def create_ticket_with_internal_fallback(
        self, req: Any, backend_override: Optional[str] = None
    ) -> TicketCreateOutcome:
        self.create_ticket_calls.append(req)
        ticket_id = f"tid-{self._next_id}"
        self._next_id += 1
        ref = f"OPS-{ticket_id}"
        self._tickets[ticket_id] = {
            "id": ticket_id,
            "ref": ref,
            "backend": "internal",
            "grid_name": req.grid_name,
            "status": "open",
            "summary": req.summary,
        }
        result = TicketResult(ref=ref, backend="internal", url=None, ticket_id=ticket_id)
        return TicketCreateOutcome(result=result, error=None, fallback_used=False)

    async def find_open_by_grid(
        self, grid_name: str, limit: int = 20, backend_override: Optional[str] = None
    ) -> List[Any]:
        # Every candidate in this storm is already tracked by the store --
        # nothing to discover via backend search.
        return []

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        for ticket in self._tickets.values():
            if ticket["ref"] == ref:
                return TicketStatus(summary=ticket["summary"], is_done=ticket["status"] == "done")
        return None

    async def get_ref_by_id(self, ticket_id: str) -> Optional[str]:
        ticket = self._tickets.get(ticket_id)
        return ticket["ref"] if ticket else None

    async def get_backend_name(self, ref: str) -> str:
        return "internal"

    async def update_ticket(
        self, ref: str, summary=None, description=None, priority_id=None
    ) -> bool:
        self.update_ticket_calls.append(
            {"ref": ref, "summary": summary, "description": description, "priority_id": priority_id}
        )
        for ticket in self._tickets.values():
            if ticket["ref"] == ref:
                ticket["summary"] = summary
        return True

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        self.add_comment_calls.append((ref, body, public))
        return True


class _FakeStore:
    """Minimal in-memory CorrelationStore, stateful across the whole storm
    (not reset between /notify calls) -- shares ``tickets`` with the ticket
    service fake so open_candidates_for_grid can merge ticket fields the way
    the real two-step query does (see correlation_store.py's docstring)."""

    def __init__(self, tickets: Dict[str, Dict[str, Any]]) -> None:
        self._tickets = tickets
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.events_by_dedup: Dict[str, Dict[str, Any]] = {}
        self.record_event_calls: List[Dict[str, Any]] = []

    async def get_by_dedup_key(self, dedup_key: str) -> Optional[Dict[str, Any]]:
        return self.events_by_dedup.get(dedup_key)

    async def get_correlation(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        return self.rows.get(ticket_id)

    async def open_candidates_for_grid(
        self, grid_name: str, since_iso: str, limit: int = 15
    ) -> List[Dict[str, Any]]:
        results = []
        for ticket_id, ticket in self._tickets.items():
            if ticket.get("grid_name") != grid_name or ticket.get("status") != "open":
                continue
            row = self.rows.get(ticket_id)
            if row is None:
                continue
            results.append(
                {
                    **row,
                    "ticket_ref": ticket["ref"],
                    "ticket_backend": ticket["backend"],
                    "summary_current": ticket["summary"],
                    "status": ticket["status"],
                    "grid_name": grid_name,
                }
            )
        return results[:limit]

    async def upsert_correlation(
        self,
        *,
        ticket_id: str,
        root_cause_kind,
        primary_signature: str,
        signatures,
        affected_keys,
        summary_base: str,
        description_base: str,
        severity: str,
    ) -> bool:
        self.rows[ticket_id] = {
            "ticket_id": ticket_id,
            "root_cause_kind": root_cause_kind,
            "primary_signature": primary_signature,
            "signatures": list(signatures),
            "affected_keys": list(affected_keys),
            "summary_base": summary_base,
            "description_base": description_base,
            "severity": severity,
            "occurrence_count": 1,
            "escalated_at": None,
        }
        return True

    async def merge_affected_key(
        self, ticket_id: str, *, kind: str, key: str, label: str, occurred_at=None, signature=None
    ) -> Optional[AffectedKeyMerge]:
        row = self.rows.get(ticket_id)
        if row is None:
            return None
        existing = list(row.get("affected_keys") or [])
        already = any(
            e.get("kind") == kind and str(e.get("key", "")).casefold() == key.casefold()
            for e in existing
        )
        if not already:
            existing.append(
                {"kind": kind, "key": key, "label": label, "first_seen": "t", "last_seen": "t", "count": 1}
            )
            row["affected_keys"] = existing
        if signature and signature not in (row.get("signatures") or []):
            row["signatures"] = list(row.get("signatures") or []) + [signature]
        return AffectedKeyMerge(affected_keys=existing, added=not already)

    async def bump_occurrence(self, ticket_id: str, occurred_at=None) -> bool:
        row = self.rows.get(ticket_id)
        if row is None:
            return False
        row["occurrence_count"] = int(row.get("occurrence_count") or 1) + 1
        return True

    async def record_amendment(self, ticket_id: str, *, severity=None, escalated: bool = False) -> bool:
        row = self.rows.get(ticket_id)
        if row is None:
            return False
        if severity:
            row["severity"] = severity
        if escalated:
            row["escalated_at"] = "now"
        return True

    async def record_event(self, **kwargs: Any) -> bool:
        self.record_event_calls.append(kwargs)
        dedup_key = kwargs.get("dedup_key")
        if dedup_key:
            self.events_by_dedup[dedup_key] = {
                "decision": kwargs["decision"],
                "ticket_id": kwargs.get("ticket_id"),
                "decided_by": kwargs["decided_by"],
                "confidence": kwargs.get("confidence"),
                "reason": kwargs.get("reason"),
            }
        return True

    async def record_event_ticket_id(self, dedup_key: str, ticket_id: str) -> bool:
        if dedup_key in self.events_by_dedup:
            self.events_by_dedup[dedup_key]["ticket_id"] = ticket_id
            return True
        return False


class _FakeDeliveryRepository:
    """Shared instance across the whole storm (not reset per-call) -- record()
    updates exactly what latest_for_ticket() reads, so the anchor set by the
    first alert's real send is what every subsequent amend's edit targets."""

    def __init__(self, get_client=None) -> None:
        pass

    anchors: Dict[str, Dict[str, Any]] = {}

    async def latest_for_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        return _FakeDeliveryRepository.anchors.get(ticket_id)

    async def record(self, **kwargs: Any) -> None:
        _FakeDeliveryRepository.anchors[kwargs["ticket_id"]] = {
            "external_chat_id": kwargs["external_chat_id"],
            "external_topic_id": kwargs["external_topic_id"],
            "external_message_id": kwargs["external_message_id"],
        }


class _FakeTelegramTransport:
    def __init__(self) -> None:
        self.send_calls: List[Dict[str, Any]] = []
        self.edit_calls: List[Dict[str, Any]] = []
        self._next_message_id = 1000

    async def send(
        self,
        bot_token,
        chat_id,
        text,
        reply_markup=None,
        parse_mode=None,
        topic_id=None,
        reply_to_message_id=None,
    ) -> int:
        self._next_message_id += 1
        self.send_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "topic_id": topic_id,
                "reply_to_message_id": reply_to_message_id,
                "message_id": self._next_message_id,
            }
        )
        return self._next_message_id

    async def edit(self, bot_token, chat_id, message_id, text, parse_mode=None) -> bool:
        self.edit_calls.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return True


def _target(grid: str = GRID) -> GridNotificationTarget:
    return GridNotificationTarget(grid_name=grid, chat_id="-100555", topic_id="42", was_fuzzy=False)


def _body(subject: str, grid: str = GRID) -> NotifyRequest:
    return NotifyRequest(
        source="grafana",
        grid_name=grid,
        text=subject,
        ticket_id="auto",
        dedup_key=f"dedup-{hash(subject)}",
        alert={"subject": subject},
    )


@pytest.fixture(autouse=True)
def _reset_delivery_anchors():
    _FakeDeliveryRepository.anchors = {}
    yield
    _FakeDeliveryRepository.anchors = {}


@pytest.mark.asyncio
async def test_seven_alerts_on_seven_devices_collapse_onto_one_ticket_one_message(monkeypatch):
    monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
    # Pinned off deliberately. ALERT_LLM_JUDGMENT_ENABLED now defaults on, and
    # under it every alert is judged rather than laddered -- a different
    # decision path with its own coverage (see the fail-open storm test
    # below). This test exists to guard the deterministic ladder itself,
    # which is still what runs whenever judgment is disabled, so it has to
    # ask for that path explicitly rather than inherit today's default.
    monkeypatch.setenv("ALERT_LLM_JUDGMENT_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")

    tickets: Dict[str, Dict[str, Any]] = {}
    store = _FakeStore(tickets)
    ticket_service = _FakeTicketService(tickets)
    transport = _FakeTelegramTransport()

    monkeypatch.setattr(
        "orchestrator.services.ticketing.correlation_store.CorrelationStore",
        lambda get_client=None: store,
    )
    monkeypatch.setattr(
        "orchestrator.services.ticketing.service.TicketService",
        lambda get_supabase_client=None: ticket_service,
    )
    monkeypatch.setattr(
        "orchestrator.services.ticketing.delivery_repository.DeliveryRepository",
        _FakeDeliveryRepository,
    )
    monkeypatch.setattr(
        "shared.llm.get_default_generation_gateway",
        lambda default_model=None: _ExplodingGateway(),
    )
    monkeypatch.setattr("shared.utils.telegram_send.send_telegram_message_with_fallback", transport.send)
    monkeypatch.setattr("shared.utils.telegram_send.edit_telegram_message", transport.edit)

    async def _noop_chat_db_log(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("orchestrator.api.app._log_notification_to_chat_db", _noop_chat_db_log)

    import orchestrator.api.app as app_module

    refs: List[Optional[str]] = []
    for subject in _SUBJECTS:
        body = _body(subject)
        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())
        assert error is None, f"unexpected error for {subject!r}: {error}"
        refs.append(ref)
        await app_module._deliver_notification(body, _target(), ref, delivery)

    # One ticket, referenced by every alert.
    assert len(set(refs)) == 1
    assert len(ticket_service.create_ticket_calls) == 1
    (ticket_id,) = tickets.keys()

    # Seven occurrences, all seven components present (VT6Y included -- B2).
    row = store.rows[ticket_id]
    assert row["occurrence_count"] == 7
    assert {entry["key"] for entry in row["affected_keys"]} == _EXPECTED_KEYS
    assert len(row["affected_keys"]) == 7

    # One real Telegram post (the new ticket) and six in-place edits of that
    # same message -- never a second top-level/escalation post, i.e. no
    # repeated escalation noise (operator problem 1) and no storm splitting
    # across tickets (operator problem 2).
    assert len(transport.send_calls) == 1
    assert len(transport.edit_calls) == 6
    original_message_id = transport.send_calls[0]["message_id"]
    assert all(call["message_id"] == original_message_id for call in transport.edit_calls)

    # The equipment list leads the description (B5).
    final_description = ticket_service.update_ticket_calls[-1]["description"]
    assert final_description.startswith("[anansi:affected-start]")
    for key in _EXPECTED_KEYS:
        assert key in final_description

    # Zero LLM calls -- fully deterministic (B1 signature fix + B2 detection
    # + B3 signature-amend rung). _ExplodingGateway raising would have
    # already failed this test with an AssertionError if this weren't true.


@pytest.mark.asyncio
async def test_underperforming_mppt_storm_collapses_onto_one_ticket(monkeypatch):
    """The 2026-08-28 underperforming-MPPT storm, end to end. Seven warnings whose only
    difference is the device id -- four ids carry a digit, three do not --
    must reach one ticket and one Telegram message. In production they split
    into two tickets exactly along that digit boundary."""
    monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
    # Same rationale as the GridY storm above: this guards the
    # deterministic ladder, so LLM judgment is pinned off explicitly.
    monkeypatch.setenv("ALERT_LLM_JUDGMENT_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")

    tickets: Dict[str, Dict[str, Any]] = {}
    store = _FakeStore(tickets)
    ticket_service = _FakeTicketService(tickets)
    transport = _FakeTelegramTransport()

    monkeypatch.setattr(
        "orchestrator.services.ticketing.correlation_store.CorrelationStore",
        lambda get_client=None: store,
    )
    monkeypatch.setattr(
        "orchestrator.services.ticketing.service.TicketService",
        lambda get_supabase_client=None: ticket_service,
    )
    monkeypatch.setattr(
        "orchestrator.services.ticketing.delivery_repository.DeliveryRepository",
        _FakeDeliveryRepository,
    )
    monkeypatch.setattr(
        "shared.llm.get_default_generation_gateway",
        lambda default_model=None: _ExplodingGateway(),
    )
    monkeypatch.setattr(
        "shared.utils.telegram_send.send_telegram_message_with_fallback", transport.send
    )
    monkeypatch.setattr("shared.utils.telegram_send.edit_telegram_message", transport.edit)

    async def _noop_chat_db_log(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("orchestrator.api.app._log_notification_to_chat_db", _noop_chat_db_log)

    import orchestrator.api.app as app_module

    target = _target(_STORM_GRID)
    refs: List[Optional[str]] = []
    for subject in _STORM_SUBJECTS:
        body = _body(subject, _STORM_GRID)
        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, target)
        assert error is None, f"unexpected error for {subject!r}: {error}"
        refs.append(ref)
        await app_module._deliver_notification(body, target, ref, delivery)

    # One ticket, not the two the operators actually saw.
    assert len(set(refs)) == 1, f"storm split across tickets: {sorted(set(refs))}"
    assert len(ticket_service.create_ticket_calls) == 1
    (ticket_id,) = tickets.keys()

    # All seven devices folded in as distinct affected components.
    row = store.rows[ticket_id]
    assert row["occurrence_count"] == 7
    assert {entry["key"] for entry in row["affected_keys"]} == set(_STORM_DEVICES)

    # One post, six in-place edits -- one message in the grid's topic.
    assert len(transport.send_calls) == 1
    assert len(transport.edit_calls) == 6
    original_message_id = transport.send_calls[0]["message_id"]
    assert all(call["message_id"] == original_message_id for call in transport.edit_calls)


@pytest.mark.asyncio
async def test_llm_outage_during_a_storm_still_files_and_posts_every_alert(monkeypatch):
    """Fail-open under the LLM-first default: an internal error must never be
    the reason an alert goes unseen.

    With judgment on (the default) and the gateway dead, each of the seven
    alerts falls back to its own ticket and its own Telegram post. That is
    noisier than the deterministic ladder's single rolled-up message and
    deliberately so -- during an LLM outage the correct failure is too many
    alerts, never a silent one.
    """
    monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")

    tickets: Dict[str, Dict[str, Any]] = {}
    store = _FakeStore(tickets)
    ticket_service = _FakeTicketService(tickets)
    transport = _FakeTelegramTransport()

    monkeypatch.setattr(
        "orchestrator.services.ticketing.correlation_store.CorrelationStore",
        lambda get_client=None: store,
    )
    monkeypatch.setattr(
        "orchestrator.services.ticketing.service.TicketService",
        lambda get_supabase_client=None: ticket_service,
    )
    monkeypatch.setattr(
        "orchestrator.services.ticketing.delivery_repository.DeliveryRepository",
        _FakeDeliveryRepository,
    )
    monkeypatch.setattr(
        "shared.llm.get_default_generation_gateway",
        lambda default_model=None: _ExplodingGateway(),
    )
    monkeypatch.setattr(
        "shared.utils.telegram_send.send_telegram_message_with_fallback", transport.send
    )
    monkeypatch.setattr("shared.utils.telegram_send.edit_telegram_message", transport.edit)

    async def _noop_chat_db_log(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("orchestrator.api.app._log_notification_to_chat_db", _noop_chat_db_log)

    import orchestrator.api.app as app_module

    refs: List[Optional[str]] = []
    send_decisions: List[str] = []
    for subject in _SUBJECTS:
        body = _body(subject)
        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())
        assert error is None, f"unexpected error for {subject!r}: {error}"
        refs.append(ref)
        send_decisions.append((extra or {}).get("send_decision", ""))
        await app_module._deliver_notification(body, _target(), ref, delivery)

    assert None not in refs
    assert len(set(refs)) == len(_SUBJECTS)
    assert len(transport.send_calls) == len(_SUBJECTS)
    assert set(send_decisions) == {"send"}


# Synthetic sites and identifiers throughout: the alert *shapes* are what these
# tests pin, and real grid names, ticket refs and device serials are operator
# data that must not travel in a public repo.
#
# The recurring-meter-alert shape, from a site whose generation we do not
# manage: one DCU alert, already on its ticket, re-firing hourly. Nothing new to
# say -- but the fail-open gate said "send" to every one of them.
_UNMANAGED_GRID = "Fairhaven"
_RECURRING_SUBJECT = (
    "! Warning: DCU 900000001 in Fairhaven could have a problem, causing Meter Issues !"
)

# The dark-plant shape: an underperforming-MPPT warning from a managed site
# whose gateway has stopped reporting. Letters-only device id on purpose -- that
# is the token shape the signature normalization has to keep intact.
_DARK_PLANT_GRID = "Riverbend"
_DARK_PLANT_SUBJECT = (
    "! Warning: MPPT WXYZ in Riverbend seems to perform lower than other MPPTs !"
)


class _RepeatAmendment:
    """apply_amendment's result for an alert already recorded on its ticket:
    no new component, no escalation, nothing rendered to say."""

    ticket_ref = "OPS-1001"
    ticket_id = "tid-1"
    decision = "duplicate"
    escalated = False
    component_added = False
    affected_keys_count = 1
    occurrence_count = 9
    rendered_summary = ""


class _RepeatDecision:
    root_cause_kind = "component"
    affected_key = {"label": "DCU 900000001"}
    update_message = ""
    ticket_severity = ""


@pytest.mark.asyncio
async def test_forced_send_of_a_silent_recurrence_is_threaded_not_a_full_repost(monkeypatch):
    """The fail-open gate has to be able to raise its voice without reading the
    whole alert aloud again.

    ``_amend_delivery``/``_duplicate_delivery`` choose ``text_override``, the
    reply anchor and the edit target *together* with ``suppress`` -- a delivery
    built to stay silent carries none of them, because it was never going to
    render. Flipping ``suppress`` alone therefore fell through to
    ``_format_ticket_notification``, which reposts the entire original alert,
    unthreaded, once per alert. Seven MPPT warnings on one storm ticket became
    seven identical top-level messages, and an unmanaged site got the same DCU
    alert re-posted in full every hour.

    A forced send must be a *reply*, never an edit: Telegram edits do not
    notify, and being seen is the entire point of forcing.
    """
    from orchestrator.api.app import (
        NotificationTicket,
        _duplicate_delivery,
        _forced_send_delivery,
    )

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    transport = _FakeTelegramTransport()
    monkeypatch.setattr(
        "shared.utils.telegram_send.send_telegram_message_with_fallback", transport.send
    )
    monkeypatch.setattr("shared.utils.telegram_send.edit_telegram_message", transport.edit)

    async def _noop_chat_db_log(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("orchestrator.api.app._log_notification_to_chat_db", _noop_chat_db_log)

    import orchestrator.api.app as app_module

    ticket = NotificationTicket(ref="OPS-1001", backend="internal", ticket_id="tid-1")
    silent = _duplicate_delivery(
        _RepeatAmendment(),
        ticket,
        reply_to_message_id=4242,
        decision=_RepeatDecision(),
    )
    assert silent.suppress is True, "a recurrence is still silent by default"

    forced = _forced_send_delivery(silent, site_status="unknown")
    body = _body(_RECURRING_SUBJECT, grid=_UNMANAGED_GRID)
    await app_module._deliver_notification(
        body, _target(_UNMANAGED_GRID), "OPS-1001", forced
    )

    assert len(transport.send_calls) == 1
    assert transport.edit_calls == [], "a forced send must notify, so never an edit"

    posted = transport.send_calls[0]
    assert posted["reply_to_message_id"] == 4242, "threaded under the ticket's own message"
    assert "OPS-1001" in posted["text"]
    assert "Site status: Unknown" in posted["text"]
    assert "could have a problem" not in posted["text"], (
        "forcing a silent recurrence must not repost the whole alert"
    )


@pytest.mark.asyncio
async def test_a_dark_plant_says_so_instead_of_relisting_a_device(monkeypatch):
    """When the gateway has stopped reporting, the message an operator needs is
    that the plant is dark -- not "MPPT QWQJ is still firing".

    The device readings behind that line came from the same feed that went
    quiet. Naming the outage once, with a status line that says so, is what
    makes the underperforming-MPPT storm interpretable instead of alarming.
    """
    from datetime import datetime, timezone

    from orchestrator.api.app import NotificationTicket, _duplicate_delivery
    from orchestrator.services.ticketing.alert_judgment_context import AlertTelemetry
    from orchestrator.services.ticketing.downtime_alert_policy import (
        assess_downtime,
        decide_downtime_override,
    )

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    transport = _FakeTelegramTransport()
    monkeypatch.setattr(
        "shared.utils.telegram_send.send_telegram_message_with_fallback", transport.send
    )
    monkeypatch.setattr("shared.utils.telegram_send.edit_telegram_message", transport.edit)

    async def _noop_chat_db_log(*args: Any, **kwargs: Any) -> None:
        return None

    import orchestrator.api.app as app_module

    monkeypatch.setattr(app_module, "_log_notification_to_chat_db", _noop_chat_db_log)

    # Real policy, faked I/O: a plant whose last gateway report is stale.
    dark = AlertTelemetry(
        generation_management="managed",
        grid_status="unknown",
        site_status="unknown",
        unavailable_reason="stale",
        fresh=False,
    )
    state = assess_downtime(dark)
    assert state.reasons == ("plant_comms_down",)

    async def _state(_alert_context: Any) -> Any:
        return state

    async def _override(_grid_name: str, _state_arg: Any) -> Any:
        return decide_downtime_override(
            state, last_downtime_alert_at=None, now=datetime.now(timezone.utc)
        )

    monkeypatch.setattr(app_module, "_live_downtime_state", _state)
    monkeypatch.setattr(app_module, "_downtime_delivery_override", _override)

    ticket = NotificationTicket(ref="OPS-1000", backend="internal", ticket_id="tid-1")
    silent = _duplicate_delivery(
        _RepeatAmendment(), ticket, reply_to_message_id=66225, decision=_RepeatDecision()
    )
    body = _body(_DARK_PLANT_SUBJECT, grid=_DARK_PLANT_GRID)
    await app_module._deliver_notification(
        body, _target(_DARK_PLANT_GRID), "OPS-1000", silent
    )

    assert len(transport.send_calls) == 1
    posted = transport.send_calls[0]["text"]
    assert "plant comms down" in posted
    assert "Site status: Plant comms down" in posted
    assert "seems to perform lower" not in posted, "the storm text is the artefact, not the news"
    assert "still firing" not in posted, "a device line is the wrong thing to say here"
    assert transport.send_calls[0]["reply_to_message_id"] == 66225
