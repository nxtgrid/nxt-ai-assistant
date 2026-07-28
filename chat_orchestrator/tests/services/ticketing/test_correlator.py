"""Tests for AlertCorrelator -- the /notify smart-ticketing decision pipeline.

Covers, per the plan's explicit required-test list
(docs/superpowers/plans/2026-07-27-smart-alert-correlation-notify.md, Task 7):
valid amend, valid duplicate, unknown ref -> new, low confidence -> new,
unparseable JSON -> new, timeout -> new, a "duplicate" without signature
overlap at confidence 0.8 downgraded to amend, and a grid_off amend with no
root-cause candidate -> parent-first. No network -- the LLM gateway is a
fake throughout.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing import correlator as correlator_module
from orchestrator.services.ticketing.alert_facts import AlertFacts, enrich_alert_facts
from orchestrator.services.ticketing.backend import TicketStatus, TicketSummary
from orchestrator.services.ticketing.correlation_rules import CorrelationPolicy
from orchestrator.services.ticketing.correlator import (
    AlertCorrelator,
    CandidateSummary,
    _apply_guardrails,
    _parse_llm_response,
)

# ---------------------------------------------------------------------------
# Pure-function tests: JSON parsing + guardrails
# ---------------------------------------------------------------------------


class TestParseLlmResponse:
    def test_parses_plain_json(self):
        raw = json.dumps({"decision": "new", "confidence": 0.9})
        assert _parse_llm_response(raw) == {"decision": "new", "confidence": 0.9}

    def test_strips_code_fences(self):
        raw = "```json\n" + json.dumps({"decision": "new"}) + "\n```"
        assert _parse_llm_response(raw) == {"decision": "new"}

    def test_none_on_garbage(self):
        assert _parse_llm_response("not json at all {{{") is None

    def test_none_on_empty(self):
        assert _parse_llm_response("") is None


def _candidate(ref="TKT-1", **overrides) -> CandidateSummary:
    defaults = dict(
        ref=ref,
        backend="internal",
        summary="MPPT issue",
        age_hours=1.0,
        root_cause_kind=None,
        affected_keys=[],
        occurrence_count=1,
        status="open",
        signatures=["sig-a"],
    )
    defaults.update(overrides)
    return CandidateSummary(**defaults)


class TestApplyGuardrails:
    def test_valid_amend_passes_through(self):
        parsed = {
            "decision": "amend",
            "ticket_ref": "TKT-1",
            "confidence": 0.9,
            "amended_summary": "4 MPPTs affected",
            "affected_key": {"kind": "mppt", "key": "A7", "label": "MPPT A7"},
            "root_cause_kind": "component",
            "update_message": "TKT-1: MPPT A7 also affected",
            "reason": "same root cause",
        }
        decision = _apply_guardrails(parsed, [_candidate()], min_confidence=0.75, llm_raw="{}")

        assert decision.decision == "amend"
        assert decision.ticket_ref == "TKT-1"
        assert decision.decided_by == "llm"
        assert decision.confidence == 0.9
        assert decision.amended_summary == "4 MPPTs affected"

    def test_valid_duplicate_with_signature_overlap_passes_through(self):
        """Signature overlap alone is sufficient -- relationship/confidence-0.85
        aren't required when the alert's own signature is already recorded
        on the candidate (a looser relationship + confidence is fine here)."""
        parsed = {
            "decision": "duplicate",
            "ticket_ref": "TKT-1",
            "confidence": 0.8,
            "relationship": "same_root_cause",
            "reason": "exact re-fire",
        }
        candidates = [_candidate(signatures=["sig-a"])]
        decision = _apply_guardrails(
            parsed, candidates, min_confidence=0.75, llm_raw="{}", alert_signature="sig-a"
        )

        assert decision.decision == "duplicate"
        assert decision.ticket_ref == "TKT-1"

    def test_urgent_refire_of_warning_candidate_becomes_material_amend(self):
        parsed = {
            "decision": "duplicate",
            "ticket_ref": "TKT-1",
            "confidence": 0.9,
            "relationship": "same_issue",
        }
        decision = _apply_guardrails(
            parsed,
            [_candidate(severity="warning", signatures=["sig-a"])],
            min_confidence=0.75,
            llm_raw="{}",
            alert_signature="sig-a",
            alert_severity="urgent",
        )

        assert decision.decision == "amend"
        assert "severity" in decision.reason

    def test_unknown_ref_forces_new(self):
        parsed = {"decision": "amend", "ticket_ref": "TKT-999", "confidence": 0.9}
        decision = _apply_guardrails(parsed, [_candidate(ref="TKT-1")], min_confidence=0.75, llm_raw="{}")

        assert decision.decision == "new"
        assert decision.decided_by == "fallback"

    def test_low_confidence_forces_new(self):
        parsed = {"decision": "amend", "ticket_ref": "TKT-1", "confidence": 0.5}
        decision = _apply_guardrails(parsed, [_candidate()], min_confidence=0.75, llm_raw="{}")

        assert decision.decision == "new"
        assert decision.decided_by == "fallback"

    def test_duplicate_without_signature_overlap_or_same_issue_downgrades_to_amend(self):
        """confidence 0.8 (above the amend bar) but no signature overlap and
        relationship != 'same_issue' -- per the guardrail, duplicate must
        never be accepted on confidence alone."""
        parsed = {
            "decision": "duplicate",
            "ticket_ref": "TKT-1",
            "confidence": 0.8,
            "relationship": "same_root_cause",
            "reason": "looks similar",
        }
        candidates = [_candidate(signatures=["sig-other"])]
        decision = _apply_guardrails(
            parsed, candidates, min_confidence=0.75, llm_raw="{}", alert_signature="sig-a"
        )

        assert decision.decision == "amend"
        assert decision.ticket_ref == "TKT-1"

    def test_duplicate_with_same_issue_and_high_confidence_but_no_signature_overlap_accepted(self):
        parsed = {
            "decision": "duplicate",
            "ticket_ref": "TKT-1",
            "confidence": 0.9,
            "relationship": "same_issue",
        }
        candidates = [_candidate(signatures=["sig-other"])]
        decision = _apply_guardrails(
            parsed, candidates, min_confidence=0.75, llm_raw="{}", alert_signature="sig-a"
        )

        assert decision.decision == "duplicate"

    def test_missing_decision_field_forces_new(self):
        decision = _apply_guardrails({}, [_candidate()], min_confidence=0.75, llm_raw="{}")
        assert decision.decision == "new"
        assert decision.decided_by == "fallback"

    def test_amend_without_ticket_ref_forces_new(self):
        parsed = {"decision": "amend", "confidence": 0.9}
        decision = _apply_guardrails(parsed, [_candidate()], min_confidence=0.75, llm_raw="{}")
        assert decision.decision == "new"

    def test_amended_summary_capped_and_sanitized(self):
        long_summary = "x" * 400 + "\nsecond line"
        parsed = {
            "decision": "amend",
            "ticket_ref": "TKT-1",
            "confidence": 0.9,
            "amended_summary": long_summary,
        }
        decision = _apply_guardrails(parsed, [_candidate()], min_confidence=0.75, llm_raw="{}")
        assert "\n" not in decision.amended_summary
        assert len(decision.amended_summary) <= 240

    def test_root_cause_first_when_no_candidate_is_root_cause_ticket(self):
        parsed = {
            "decision": "amend",
            "ticket_ref": "TKT-1",
            "confidence": 0.9,
            "root_cause_kind": "grid_off",
            "affected_key": {"kind": "mppt", "key": "A7", "label": "MPPT A7"},
            "reason": "grid has been off",
        }
        candidates = [_candidate(ref="TKT-1", root_cause_kind=None)]
        decision = _apply_guardrails(parsed, candidates, min_confidence=0.75, llm_raw="{}")

        assert decision.decision == "amend"
        assert decision.needs_root_cause_ticket is True
        assert decision.ticket_ref is None

    def test_no_root_cause_first_when_candidate_already_is_root_cause_ticket(self):
        parsed = {
            "decision": "amend",
            "ticket_ref": "TKT-1",
            "confidence": 0.9,
            "root_cause_kind": "grid_off",
            "affected_key": {"kind": "mppt", "key": "A7", "label": "MPPT A7"},
        }
        candidates = [_candidate(ref="TKT-1", root_cause_kind="grid_off")]
        decision = _apply_guardrails(parsed, candidates, min_confidence=0.75, llm_raw="{}")

        assert decision.needs_root_cause_ticket is False
        assert decision.ticket_ref == "TKT-1"

    def test_new_decision_never_triggers_root_cause_first(self):
        parsed = {"decision": "new", "confidence": 0.9, "root_cause_kind": "grid_off"}
        decision = _apply_guardrails(parsed, [_candidate()], min_confidence=0.75, llm_raw="{}")
        assert decision.decision == "new"
        assert decision.needs_root_cause_ticket is False


# ---------------------------------------------------------------------------
# AlertCorrelator.decide() integration tests (fakes only, no network)
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.correlations: List[Dict[str, Any]] = []
        self.events: Dict[str, Dict[str, Any]] = {}
        self.mark_closed_calls: List[str] = []

    async def get_by_dedup_key(self, dedup_key: str) -> Optional[Dict[str, Any]]:
        return self.events.get(dedup_key)

    async def get_correlation(self, ticket_ref: str) -> Optional[Dict[str, Any]]:
        return next(
            (row for row in self.correlations if row["ticket_ref"] == ticket_ref),
            None,
        )

    async def open_candidates_for_grid(self, grid_name, since_iso, limit=15):
        return [
            row
            for row in self.correlations
            if row["grid_name"] == grid_name and row.get("status", "open") == "open"
        ][:limit]

    async def mark_closed(self, ticket_ref: str) -> bool:
        self.mark_closed_calls.append(ticket_ref)
        return True

    async def record_event(self, **kwargs):
        if kwargs.get("dedup_key"):
            self.events[kwargs["dedup_key"]] = {
                "decision": kwargs["decision"],
                "ticket_ref": kwargs.get("ticket_ref"),
                "decided_by": kwargs["decided_by"],
                "confidence": kwargs.get("confidence"),
                "reason": kwargs.get("reason"),
            }
        return True

    async def record_event_ticket_ref(self, dedup_key: str, ticket_ref: str) -> bool:
        if dedup_key in self.events:
            self.events[dedup_key]["ticket_ref"] = ticket_ref
            return True
        return False


class _FakeTicketService:
    def __init__(self) -> None:
        self.open_by_grid: List[TicketSummary] = []
        self.statuses: Dict[str, TicketStatus] = {}

    async def find_open_by_grid(self, grid_name, limit=20, backend_override=None):
        return self.open_by_grid

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        return self.statuses.get(ref)


class _FakeGateway:
    def __init__(self, text: Optional[str] = None, raise_exc: Optional[Exception] = None, delay: float = 0.0):
        self.text = text
        self.raise_exc = raise_exc
        self.delay = delay
        self.calls: List[Any] = []

    async def generate(self, messages, options, **kwargs):
        self.calls.append((messages, options))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc:
            raise self.raise_exc

        class _Result:
            text = self.text

        return _Result()


def _make_correlator(
    store=None, ticket_service=None, gateway=None, min_confidence=0.75, timeout_seconds=5, lookback_hours=168, max_candidates=15
) -> tuple[AlertCorrelator, _FakeStore, _FakeTicketService, _FakeGateway]:
    store = store or _FakeStore()
    ticket_service = ticket_service or _FakeTicketService()
    gateway = gateway if gateway is not None else _FakeGateway(text=json.dumps({"decision": "new"}))
    correlator = AlertCorrelator(
        store=store,
        ticket_service=ticket_service,
        gateway=gateway,
        model="fake-model",
        min_confidence=min_confidence,
        timeout_seconds=timeout_seconds,
        lookback_hours=lookback_hours,
        max_candidates=max_candidates,
        get_correlation_instructions=lambda: {"system_instructions": "rules"},
        get_rag_context=_no_rag,
        get_grid_operational_context=_no_grid_facts,
    )
    return correlator, store, ticket_service, gateway


async def _no_rag(query, limit=None):
    return []


async def _no_grid_facts(grid_name):
    return {}


def _mppt_alert(subject="! Warning: MPPT A3 in Kudi seems to perform lower !", **overrides) -> AlertFacts:
    alert = AlertFacts(subject=subject, details=overrides.pop("details", "mppt A3 [Kudi]"), **overrides)
    return enrich_alert_facts(alert, grid_name="Kudi")


class TestVersionedPolicy:
    @pytest.mark.asyncio
    async def test_injected_policy_limits_the_candidates_sent_to_the_model(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        store = _FakeStore()
        ticket_service = _FakeTicketService()
        gateway = _FakeGateway(text=json.dumps({"decision": "new", "confidence": 0.9}))
        for ref in ("TKT-1", "TKT-2"):
            store.correlations.append(
                {
                    "ticket_ref": ref,
                    "grid_name": "Kudi",
                    "status": "open",
                    "signatures": [],
                    "affected_keys": [],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            ticket_service.statuses[ref] = TicketStatus(summary=ref, is_done=False)

        correlator = AlertCorrelator(
            store=store,
            ticket_service=ticket_service,
            gateway=gateway,
            model="fake-model",
            policy=CorrelationPolicy(
                confidence_floor=0.75,
                llm_timeout_seconds=5,
                open_candidate_window_hours=24,
                maximum_candidate_count=1,
            ),
            get_correlation_instructions=lambda: {"system_instructions": "rules"},
            get_rag_context=_no_rag,
            get_grid_operational_context=_no_grid_facts,
        )

        await correlator.decide("Kudi", _mppt_alert())

        prompt_messages, _options = gateway.calls[0]
        prompt_text = "\n".join(message.text or "" for message in prompt_messages)
        assert "TKT-1" in prompt_text
        assert "TKT-2" not in prompt_text

    @pytest.mark.asyncio
    async def test_default_model_comes_from_application_settings(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        monkeypatch.setattr(
            correlator_module,
            "get_settings",
            lambda: type(
                "_Settings",
                (),
                {"gemini": type("_Gemini", (), {"model": "application-primary"})()},
            )(),
        )
        store = _FakeStore()
        ticket_service = _FakeTicketService()
        gateway = _FakeGateway(text=json.dumps({"decision": "new", "confidence": 0.9}))
        correlator = AlertCorrelator(
            store=store,
            ticket_service=ticket_service,
            gateway=gateway,
            get_correlation_instructions=lambda: {"system_instructions": "rules"},
            get_rag_context=_no_rag,
            get_grid_operational_context=_no_grid_facts,
        )
        store.correlations.append(
            {
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [],
                "affected_keys": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ticket_service.statuses["TKT-1"] = TicketStatus(summary="TKT-1", is_done=False)

        await correlator.decide("Kudi", _mppt_alert())

        _messages, options = gateway.calls[0]
        assert options.model == "application-primary"


class TestDedupReplay:
    @pytest.mark.asyncio
    async def test_replays_prior_decision_without_new_io(self):
        correlator, store, _ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "severity": "urgent",
            }
        )
        store.events["dk-1"] = {
            "decision": "amend",
            "ticket_ref": "TKT-1",
            "decided_by": "llm",
            "confidence": 0.9,
            "reason": "prior decision",
        }

        decision = await correlator.decide("Kudi", _mppt_alert(), dedup_key="dk-1")

        assert decision.decision == "amend"
        assert decision.ticket_ref == "TKT-1"
        assert decision.decided_by == "replay"
        assert decision.ticket_severity == "urgent"
        assert gateway.calls == []  # no LLM call for a replay

    @pytest.mark.asyncio
    async def test_backfilled_ticket_ref_is_returned_on_replay(self):
        """End-to-end regression test for the delivery-idempotency gap: a
        "new" decision's event row is recorded with ticket_ref=None (there's
        nothing to reference until the ticket is actually created by
        app.py), the post-creation backfill lands via
        ``record_event_ticket_ref`` (simulating what
        ``_resolve_notify_ticket_auto`` now does right after
        ``_create_notify_ticket``), and a later replay of the same
        dedup_key must come back with the backfilled ``ticket_ref`` --
        that's what lets the /notify replay guard's ``decision.ticket_ref``
        truthiness check actually suppress the duplicate-ticket case instead
        of silently falling through to file a second ticket."""
        correlator, store, _ts, gateway = _make_correlator()

        first = await correlator.decide("Kudi", _mppt_alert(), dedup_key="dk-2")
        assert first.decision == "new"
        assert first.ticket_ref is None
        assert store.events["dk-2"]["ticket_ref"] is None

        backfilled = await store.record_event_ticket_ref("dk-2", "TKT-99")
        assert backfilled is True

        replay = await correlator.decide("Kudi", _mppt_alert(), dedup_key="dk-2")

        assert replay.decided_by == "replay"
        assert replay.decision == "new"
        assert replay.ticket_ref == "TKT-99"


class TestFlagOff:
    @pytest.mark.asyncio
    async def test_returns_new_without_any_io(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "false")
        correlator, store, ts, gateway = _make_correlator()
        store.correlations.append(
            {"ticket_ref": "TKT-1", "grid_name": "Kudi", "status": "open", "signatures": [], "affected_keys": []}
        )

        decision = await correlator.decide("Kudi", _mppt_alert())

        assert decision.decision == "new"
        assert decision.decided_by == "flag_off"
        assert gateway.calls == []


class TestNoCandidates:
    @pytest.mark.asyncio
    async def test_returns_new_when_no_open_tickets(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        correlator, _store, _ts, gateway = _make_correlator()

        decision = await correlator.decide("Kudi", _mppt_alert())

        assert decision.decision == "new"
        assert decision.decided_by == "no_candidates"
        assert gateway.calls == []


class TestSignatureDuplicate:
    @pytest.mark.asyncio
    async def test_exact_signature_and_key_match_is_silent_duplicate(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert()
        correlator, store, ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [alert.signature],
                "affected_keys": [{"kind": "mppt", "key": "A3", "label": "MPPT A3"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", alert)

        assert decision.decision == "duplicate"
        assert decision.ticket_ref == "TKT-1"
        assert decision.decided_by == "signature"
        assert gateway.calls == []  # deterministic rung -- never reaches the LLM

    @pytest.mark.asyncio
    async def test_urgent_refire_of_warning_ticket_is_a_material_amend(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert(
            subject="! Urgent: MPPT A3 in Kudi seems to perform lower !",
            severity="urgent",
        )
        correlator, store, ticket_service, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "severity": "warning",
                "signatures": [alert.signature],
                "affected_keys": [
                    {"kind": "mppt", "key": "A3", "label": "MPPT A3"}
                ],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ticket_service.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", alert)

        assert decision.decision == "amend"
        assert decision.ticket_severity == "warning"
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_urgent_refire_after_ticket_is_urgent_is_silent_duplicate(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert(
            subject="! Urgent: MPPT A3 in Kudi seems to perform lower !",
            severity="urgent",
        )
        correlator, store, ticket_service, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "severity": "urgent",
                "signatures": [alert.signature],
                "affected_keys": [
                    {"kind": "mppt", "key": "A3", "label": "MPPT A3"}
                ],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ticket_service.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", alert)

        assert decision.decision == "duplicate"
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_same_signature_different_key_is_not_a_signature_duplicate(self, monkeypatch):
        """Same normalized subject, different MPPT key -- must fall through
        to the LLM rung as an amend candidate, not a deterministic duplicate."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert(
            subject="! Warning: MPPT A7 in Kudi seems to perform lower !", details="mppt A7 [Kudi]"
        )
        gateway = _FakeGateway(
            text=json.dumps(
                {
                    "decision": "amend",
                    "ticket_ref": "TKT-1",
                    "confidence": 0.9,
                    "affected_key": {"kind": "mppt", "key": "A7", "label": "MPPT A7"},
                }
            )
        )
        correlator, store, ts, _gw = _make_correlator(gateway=gateway)
        store.correlations.append(
            {
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [alert.signature],  # same signature (A3 vs A7 collide)
                "affected_keys": [{"kind": "mppt", "key": "A3", "label": "MPPT A3"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", alert)

        assert decision.decision == "amend"
        assert decision.decided_by == "llm"
        assert len(gateway.calls) == 1

    @pytest.mark.asyncio
    async def test_live_facts_are_added_only_when_correlation_calls_the_llm(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        gateway = _FakeGateway(text=json.dumps({"decision": "new", "confidence": 0.9}))
        correlator, store, ts, _gw = _make_correlator(gateway=gateway)
        store.correlations.append(
            {
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [],
                "affected_keys": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="Existing issue", is_done=False)
        calls = 0

        async def get_live_facts():
            nonlocal calls
            calls += 1
            return {"live_inverter_output_kw": 2.4}

        await correlator.decide("Kudi", _mppt_alert(), get_live_facts=get_live_facts)

        prompt_messages, _options = gateway.calls[0]
        prompt_text = "\n".join(message.text or "" for message in prompt_messages)
        assert '"live_inverter_output_kw": 2.4' in prompt_text
        assert calls == 1

    @pytest.mark.asyncio
    async def test_exact_duplicate_does_not_fetch_live_facts(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert()
        correlator, store, ts, _gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [alert.signature],
                "affected_keys": [{"kind": "mppt", "key": "A3", "label": "MPPT A3"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="Existing issue", is_done=False)

        async def unexpected_live_facts():
            raise AssertionError("silent duplicate must not fetch live telemetry")

        decision = await correlator.decide("Kudi", alert, get_live_facts=unexpected_live_facts)

        assert decision.decision == "duplicate"

    @pytest.mark.asyncio
    async def test_case_differing_stored_key_still_matches(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert()
        correlator, store, _ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "OPS-42",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [alert.signature],
                "affected_keys": [{"kind": "MPPT", "key": "a3", "label": "MPPT a3"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        decision = await correlator.decide("Kudi", alert)

        assert decision.decision == "duplicate"
        assert decision.decided_by == "signature"
        assert gateway.calls == []


class TestKeylessSignatureDuplicate:
    @pytest.mark.asyncio
    async def test_identical_keyless_alert_is_a_duplicate_without_the_llm(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = enrich_alert_facts(
            AlertFacts(
                subject=(
                    "! Urgent: Turn off Combiner: ALERT - 'Okpokunou': "
                    "'#26 - Charger terminal overheated' on 'Combiner Box 4' !"
                ),
                severity="urgent",
            ),
            grid_name="Okpokunou",
        )
        correlator, store, _ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "OPS-3363",
                "grid_name": "Okpokunou",
                "status": "open",
                "severity": "urgent",
                "signatures": [alert.signature],
                "affected_keys": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        decision = await correlator.decide("Okpokunou", alert)

        assert decision.decision == "duplicate"
        assert decision.ticket_ref == "OPS-3363"
        assert decision.decided_by == "signature"
        assert gateway.calls == []
        # No component was involved in this match -- the audit reason must
        # not claim a component match happened.
        assert "component" not in decision.reason.lower()

    @pytest.mark.asyncio
    async def test_keyless_signature_match_still_escalates_on_urgency(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = enrich_alert_facts(
            AlertFacts(subject="! Urgent: Grid outage in Okpokunou !", severity="urgent"),
            grid_name="Okpokunou",
        )
        correlator, store, _ts, _gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "OPS-3363",
                "grid_name": "Okpokunou",
                "status": "open",
                "severity": "warning",
                "signatures": [alert.signature],
                "affected_keys": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        decision = await correlator.decide("Okpokunou", alert)

        assert decision.decision == "amend"
        assert decision.ticket_ref == "OPS-3363"


class TestLiveStatusConfirmation:
    @pytest.mark.asyncio
    async def test_done_candidate_dropped_and_store_corrected(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        correlator, store, ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [],
                "affected_keys": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="s", is_done=True)

        decision = await correlator.decide("Kudi", _mppt_alert())

        assert decision.decision == "new"
        assert decision.decided_by == "no_candidates"
        assert store.mark_closed_calls == ["TKT-1"]

    @pytest.mark.asyncio
    async def test_unavailable_status_preserves_stored_exact_duplicate(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert()
        correlator, store, _ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "OPS-42",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [alert.signature],
                "affected_keys": [{"kind": "mppt", "key": "A3", "label": "MPPT A3"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        decision = await correlator.decide("Kudi", alert)

        assert decision.decision == "duplicate"
        assert decision.ticket_ref == "OPS-42"
        assert store.mark_closed_calls == []
        assert gateway.calls == []


class TestBackendOnlyCandidates:
    @pytest.mark.asyncio
    async def test_ticket_service_candidates_merged_and_offered_to_llm(self, monkeypatch):
        """A ticket filed by a human directly in Jira (or by n8n before
        cutover) -- tracked by TicketService.find_open_by_grid but absent
        from ticket_correlations -- must still be offered as an LLM
        candidate."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        gateway = _FakeGateway(
            text=json.dumps({"decision": "new", "confidence": 0.9})
        )
        correlator, _store, ts, _gw = _make_correlator(gateway=gateway)
        ts.open_by_grid = [
            TicketSummary(ref="OPS-1", backend="jira", summary="Pre-existing human ticket", status="Open")
        ]
        ts.statuses["OPS-1"] = TicketStatus(summary="Pre-existing human ticket", is_done=False)

        await correlator.decide("Kudi", _mppt_alert())

        prompt_messages, _options = gateway.calls[0]
        prompt_text = "\n".join(m.text or "" for m in prompt_messages)
        assert "OPS-1" in prompt_text


class TestLlmFailureModes:
    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_new(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        gateway = _FakeGateway(text=json.dumps({"decision": "amend"}), delay=1.0)
        correlator, store, ts, _gw = _make_correlator(gateway=gateway, timeout_seconds=0.05)
        store.correlations.append(
            {"ticket_ref": "TKT-1", "grid_name": "Kudi", "status": "open", "signatures": [], "affected_keys": [], "created_at": datetime.now(timezone.utc).isoformat()}
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", _mppt_alert())

        assert decision.decision == "new"
        assert decision.decided_by == "fallback"

    @pytest.mark.asyncio
    async def test_transport_error_falls_back_to_new(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        gateway = _FakeGateway(raise_exc=RuntimeError("network down"))
        correlator, store, ts, _gw = _make_correlator(gateway=gateway)
        store.correlations.append(
            {"ticket_ref": "TKT-1", "grid_name": "Kudi", "status": "open", "signatures": [], "affected_keys": [], "created_at": datetime.now(timezone.utc).isoformat()}
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", _mppt_alert())

        assert decision.decision == "new"
        assert decision.decided_by == "fallback"

    @pytest.mark.asyncio
    async def test_unparseable_response_falls_back_to_new(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        gateway = _FakeGateway(text="not json at all")
        correlator, store, ts, _gw = _make_correlator(gateway=gateway)
        store.correlations.append(
            {"ticket_ref": "TKT-1", "grid_name": "Kudi", "status": "open", "signatures": [], "affected_keys": [], "created_at": datetime.now(timezone.utc).isoformat()}
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", _mppt_alert())

        assert decision.decision == "new"
        assert decision.decided_by == "fallback"


class TestRecordEventIsCalled:
    @pytest.mark.asyncio
    async def test_decide_records_an_audit_event(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        correlator, store, ts, gateway = _make_correlator()

        await correlator.decide("Kudi", _mppt_alert(), dedup_key="dk-99")

        assert "dk-99" in store.events
        assert store.events["dk-99"]["decision"] == "new"
