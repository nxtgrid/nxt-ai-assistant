"""Tests for AlertCorrelator -- the /notify smart-ticketing decision pipeline.

Covers, per the plan's explicit required-test list
(docs/superpowers/plans/2026-07-27-smart-alert-correlation-notify.md, Task 7):
valid amend, valid duplicate, unknown ref -> new, low confidence -> new,
unparseable JSON -> new, timeout -> new, a "duplicate" without signature
overlap at confidence 0.8 downgraded to amend, and a grid_off amend with no
root-cause candidate -> parent-first. No network -- the LLM gateway is a
fake throughout.

Correlation state is keyed by ``ticket_id`` (db/migrations/0005b) -- fixture
rows in ``_FakeStore.correlations`` carry both ``ticket_id`` (what the store
itself is keyed by) and ``ticket_ref`` (what ``_assemble_candidates`` merges
in from a joined ``tickets`` row, mirroring the real
``CorrelationStore.open_candidates_for_grid``). A row missing ``ticket_id``
is dropped by the correlator as unusable -- see
``TestBackendOnlyCandidates``/``TestVersionedPolicy`` for why every fixture
below sets one.
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
from orchestrator.services.ticketing.correlation_rules import (
    DEFAULT_CORRELATION_POLICY,
    CorrelationPolicy,
)
from orchestrator.services.ticketing.correlator import (
    AlertCorrelator,
    CandidateSummary,
    _apply_guardrails,
    _parse_llm_response,
    effective_candidate_severity,
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


class TestEffectiveCandidateSeverity:
    """B4: a blank stored severity (most commonly a Jira-discovered candidate
    just adopted via TicketRepository.adopt_external, which never sets one)
    must not read as "not urgent" when the ticket plainly already is one --
    that's exactly what let a stale candidate masquerade as a fresh
    warning->urgent transition on every subsequent alert."""

    def test_stored_severity_wins_when_present(self):
        candidate = _candidate(severity="warning", summary="! Urgent: ignored !")
        assert effective_candidate_severity(candidate) == "warning"

    def test_falls_back_to_deriving_from_the_summary_marker(self):
        candidate = _candidate(severity="", summary="! Urgent: Inverter Fault !")
        assert effective_candidate_severity(candidate) == "urgent"

    def test_falls_back_to_the_escalated_emoji_when_no_marker(self):
        """render_summary/apply_amendment prefix an escalated ticket's
        summary with "🔴 " without necessarily also carrying a "! Urgent:"
        marker (e.g. a freshly-adopted candidate's summary_base has no
        marker at all) -- this must still read as urgent."""
        candidate = _candidate(severity="", summary="🔴 3 MPPTs in Kudi affected (A3, A7)")
        assert effective_candidate_severity(candidate) == "urgent"

    def test_blank_when_nothing_signals_severity(self):
        candidate = _candidate(severity="", summary="MPPT issue, no marker at all")
        assert effective_candidate_severity(candidate) == ""

    def test_warning_marker_derives_to_warning(self):
        candidate = _candidate(severity="", summary="! Warning: FS delivery low !")
        assert effective_candidate_severity(candidate) == "warning"


def _candidate(ref="TKT-1", **overrides) -> CandidateSummary:
    defaults = dict(
        ref=ref,
        ticket_id=f"tid-{ref}",
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
        assert decision.ticket_id == "tid-TKT-1"
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
        assert decision.ticket_id is None

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
        assert decision.ticket_id == "tid-TKT-1"

    def test_new_decision_never_triggers_root_cause_first(self):
        parsed = {"decision": "new", "confidence": 0.9, "root_cause_kind": "grid_off"}
        decision = _apply_guardrails(parsed, [_candidate()], min_confidence=0.75, llm_raw="{}")
        assert decision.decision == "new"
        assert decision.needs_root_cause_ticket is False


class TestCrossKindAmendGuard:
    """C4: a power-chain cascade is the only way an amend may join a ticket
    whose affected_keys carry a different component kind than the incoming
    alert's own. Everything else about the guardrails above (ref validity,
    confidence floor, root-cause-first) is unchanged and still applies."""

    @staticmethod
    def _cross_kind_parsed(**overrides) -> Dict[str, Any]:
        parsed = {
            "decision": "amend",
            "ticket_ref": "OPS-3456",
            "confidence": 0.9,
            "affected_key": {"kind": "inverter", "key": "INV1", "label": "Inverter INV1"},
            "root_cause_kind": "power_chain",
            "update_message": "Inverter shut down after BMS comms loss",
            "reason": "battery/BMS -> inverter power chain",
        }
        parsed.update(overrides)
        return parsed

    @staticmethod
    def _bms_candidate(**overrides) -> CandidateSummary:
        defaults = dict(
            ref="OPS-3456",
            affected_keys=[{"kind": "battery", "key": "BMS1", "label": "BMS1"}],
        )
        defaults.update(overrides)
        return _candidate(**defaults)

    def test_forced_new_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("ALERT_CASCADE_MERGE_ENABLED", raising=False)
        decision = _apply_guardrails(
            self._cross_kind_parsed(), [self._bms_candidate()], min_confidence=0.75, llm_raw="{}"
        )
        assert decision.decision == "new"
        assert decision.ticket_ref is None

    def test_forced_new_without_a_power_chain_root_cause(self, monkeypatch):
        monkeypatch.setenv("ALERT_CASCADE_MERGE_ENABLED", "true")
        decision = _apply_guardrails(
            self._cross_kind_parsed(root_cause_kind="component"),
            [self._bms_candidate()],
            min_confidence=0.75,
            llm_raw="{}",
        )
        assert decision.decision == "new"

    def test_independent_mppt_and_inverter_fault_with_no_topology_claim_stays_new(self, monkeypatch):
        """The plan's own Example 4: an MPPT-performance ticket and an
        unrelated inverter fault on a grid that's currently on -- the model
        is expected to say "new" (root_cause_kind anything but
        power_chain), and the guard must agree even with the flag on."""
        monkeypatch.setenv("ALERT_CASCADE_MERGE_ENABLED", "true")
        candidate = _candidate(
            ref="TKT-1", affected_keys=[{"kind": "mppt", "key": "A3", "label": "MPPT A3"}]
        )
        parsed = self._cross_kind_parsed(
            ticket_ref="TKT-1",
            affected_key={"kind": "inverter", "key": "", "label": "Inverter Fault"},
            root_cause_kind=None,
            reason="unrelated issue, same grid",
        )
        decision = _apply_guardrails(parsed, [candidate], min_confidence=0.75, llm_raw="{}")
        assert decision.decision == "new"

    def test_accepted_when_flag_on_and_power_chain_and_confident(self, monkeypatch):
        monkeypatch.setenv("ALERT_CASCADE_MERGE_ENABLED", "true")
        decision = _apply_guardrails(
            self._cross_kind_parsed(), [self._bms_candidate()], min_confidence=0.75, llm_raw="{}"
        )
        assert decision.decision == "amend"
        assert decision.ticket_ref == "OPS-3456"
        assert decision.ticket_id == "tid-OPS-3456"
        assert decision.root_cause_kind == "power_chain"

    def test_low_confidence_forces_new_even_with_flag_on(self, monkeypatch):
        """Plan C6: a cross-kind amend at confidence 0.6 (below the 0.75
        floor) must still fail closed -- the power-chain allowance never
        bypasses the ordinary confidence bar."""
        monkeypatch.setenv("ALERT_CASCADE_MERGE_ENABLED", "true")
        decision = _apply_guardrails(
            self._cross_kind_parsed(confidence=0.6),
            [self._bms_candidate()],
            min_confidence=0.75,
            llm_raw="{}",
        )
        assert decision.decision == "new"

    def test_same_kind_amend_is_unaffected_by_the_guard(self, monkeypatch):
        """A same-kind amend (e.g. a second BMS alert onto the same
        battery/BMS ticket) never needed power_chain before Phase C and
        still doesn't -- the guard only fires on a genuine kind mismatch."""
        monkeypatch.delenv("ALERT_CASCADE_MERGE_ENABLED", raising=False)
        parsed = self._cross_kind_parsed(
            affected_key={"kind": "battery", "key": "BMS2", "label": "BMS2"},
            root_cause_kind="component",
        )
        decision = _apply_guardrails(
            parsed, [self._bms_candidate()], min_confidence=0.75, llm_raw="{}"
        )
        assert decision.decision == "amend"

    def test_blank_affected_key_is_not_treated_as_cross_kind(self, monkeypatch):
        """A grid-level alert (no component at all) folding onto a
        component-specific ticket isn't provably cross-kind -- there is no
        incoming kind to compare, so the ordinary pre-Phase-C guardrails
        apply rather than the power-chain gate."""
        monkeypatch.delenv("ALERT_CASCADE_MERGE_ENABLED", raising=False)
        parsed = self._cross_kind_parsed(affected_key=None, root_cause_kind="component")
        decision = _apply_guardrails(
            parsed, [self._bms_candidate()], min_confidence=0.75, llm_raw="{}"
        )
        assert decision.decision == "amend"

    def test_candidate_with_no_recorded_kind_is_not_treated_as_cross_kind(self, monkeypatch):
        """A Jira-discovered candidate adopted with no affected_keys at all
        (the correlator's adopt_external path) can't be proven cross-kind
        either way -- refusing it outright would regress ordinary LLM-amend
        behaviour onto tickets the correlation store never recorded."""
        monkeypatch.delenv("ALERT_CASCADE_MERGE_ENABLED", raising=False)
        decision = _apply_guardrails(
            self._cross_kind_parsed(root_cause_kind="component"),
            [self._bms_candidate(affected_keys=[])],
            min_confidence=0.75,
            llm_raw="{}",
        )
        assert decision.decision == "amend"

    def test_power_chain_never_requires_a_root_cause_parent_ticket(self, monkeypatch):
        """C4: power_chain deliberately does not join
        _ROOT_CAUSE_KINDS_REQUIRING_PARENT -- the parent already exists as a
        real ticket by construction of the cross-kind guard above, so this
        must never set needs_root_cause_ticket."""
        monkeypatch.setenv("ALERT_CASCADE_MERGE_ENABLED", "true")
        candidates = [self._bms_candidate(root_cause_kind=None)]
        decision = _apply_guardrails(
            self._cross_kind_parsed(), candidates, min_confidence=0.75, llm_raw="{}"
        )
        assert decision.needs_root_cause_ticket is False
        assert decision.ticket_ref == "OPS-3456"


# ---------------------------------------------------------------------------
# AlertCorrelator.decide() integration tests (fakes only, no network)
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.correlations: List[Dict[str, Any]] = []
        self.events: Dict[str, Dict[str, Any]] = {}

    async def get_by_dedup_key(self, dedup_key: str) -> Optional[Dict[str, Any]]:
        return self.events.get(dedup_key)

    async def get_correlation(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        return next(
            (row for row in self.correlations if row["ticket_id"] == ticket_id),
            None,
        )

    async def open_candidates_for_grid(self, grid_name, since_iso, limit=15):
        return [
            row
            for row in self.correlations
            if row["grid_name"] == grid_name and row.get("status", "open") == "open"
        ][:limit]

    async def record_event(self, **kwargs):
        if kwargs.get("dedup_key"):
            self.events[kwargs["dedup_key"]] = {
                "decision": kwargs["decision"],
                "ticket_id": kwargs.get("ticket_id"),
                "decided_by": kwargs["decided_by"],
                "confidence": kwargs.get("confidence"),
                "reason": kwargs.get("reason"),
            }
        return True

    async def record_event_ticket_id(self, dedup_key: str, ticket_id: str) -> bool:
        if dedup_key in self.events:
            self.events[dedup_key]["ticket_id"] = ticket_id
            return True
        return False


class _FakeTicketService:
    def __init__(self) -> None:
        self.open_by_grid: List[TicketSummary] = []
        self.statuses: Dict[str, TicketStatus] = {}
        self.ref_by_id: Dict[str, str] = {}
        self.adopted_calls: List[Dict[str, Any]] = []

    async def find_open_by_grid(self, grid_name, limit=20, backend_override=None):
        return self.open_by_grid

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        return self.statuses.get(ref)

    async def get_ref_by_id(self, ticket_id: str) -> Optional[str]:
        return self.ref_by_id.get(ticket_id)

    async def adopt_external(self, *, ref, backend, summary, grid_name=None):
        self.adopted_calls.append(
            {"ref": ref, "backend": backend, "summary": summary, "grid_name": grid_name}
        )

        class _Adopted:
            id = f"adopted-{ref}"

        return _Adopted()


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
                    "ticket_id": f"tid-{ref}",
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
                "ticket_id": "tid-TKT-1",
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
        correlator, store, ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_id": "tid-TKT-1",
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "severity": "urgent",
            }
        )
        ts.ref_by_id["tid-TKT-1"] = "TKT-1"
        store.events["dk-1"] = {
            "decision": "amend",
            "ticket_id": "tid-TKT-1",
            "decided_by": "llm",
            "confidence": 0.9,
            "reason": "prior decision",
        }

        decision = await correlator.decide("Kudi", _mppt_alert(), dedup_key="dk-1")

        assert decision.decision == "amend"
        assert decision.ticket_ref == "TKT-1"
        assert decision.ticket_id == "tid-TKT-1"
        assert decision.decided_by == "replay"
        assert decision.ticket_severity == "urgent"
        assert gateway.calls == []  # no LLM call for a replay

    @pytest.mark.asyncio
    async def test_backfilled_ticket_id_resolves_to_a_ref_on_replay(self):
        """End-to-end regression test for the delivery-idempotency gap: a
        "new" decision's event row is recorded with ticket_id=None (there's
        nothing to reference until the ticket is actually created by
        app.py), the post-creation backfill lands via
        ``record_event_ticket_id`` (simulating what
        ``_resolve_notify_ticket_auto`` now does right after
        ``_create_notify_ticket``), and a later replay of the same
        dedup_key must come back with the backfilled id resolved to a ref
        (via ``TicketService.get_ref_by_id``) -- that's what lets the
        /notify replay guard's ``decision.ticket_ref`` truthiness check
        actually suppress the duplicate-ticket case instead of silently
        falling through to file a second ticket."""
        correlator, store, ts, gateway = _make_correlator()

        first = await correlator.decide("Kudi", _mppt_alert(), dedup_key="dk-2")
        assert first.decision == "new"
        assert first.ticket_ref is None
        assert first.ticket_id is None
        assert store.events["dk-2"]["ticket_id"] is None

        backfilled = await store.record_event_ticket_id("dk-2", "tid-99")
        assert backfilled is True
        ts.ref_by_id["tid-99"] = "TKT-99"

        replay = await correlator.decide("Kudi", _mppt_alert(), dedup_key="dk-2")

        assert replay.decided_by == "replay"
        assert replay.decision == "new"
        assert replay.ticket_id == "tid-99"
        assert replay.ticket_ref == "TKT-99"


class TestFlagOff:
    @pytest.mark.asyncio
    async def test_returns_new_without_any_io(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "false")
        correlator, store, ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_id": "tid-TKT-1",
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [],
                "affected_keys": [],
            }
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
                "ticket_id": "tid-TKT-1",
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
        assert decision.ticket_id == "tid-TKT-1"
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
                "ticket_id": "tid-TKT-1",
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
                "ticket_id": "tid-TKT-1",
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
    async def test_urgent_refire_of_blank_severity_but_escalated_summary_is_silent_duplicate(
        self, monkeypatch
    ):
        """B4: a candidate whose correlation row never recorded a severity
        (e.g. adopted mid-flight) but whose summary already carries the "🔴 "
        escalated marker must resolve via effective_candidate_severity, not
        the raw blank field -- otherwise every subsequent urgent re-fire
        reads as a fresh warning->urgent transition and re-announces."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert(
            subject="! Urgent: MPPT A3 in Kudi seems to perform lower !",
            severity="urgent",
        )
        correlator, store, ticket_service, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_id": "tid-TKT-1",
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "severity": "",  # never recorded -- e.g. an adopted candidate
                "summary_current": "🔴 3 MPPTs in Kudi affected (A3, A7)",
                "signatures": [alert.signature],
                "affected_keys": [{"kind": "mppt", "key": "A3", "label": "MPPT A3"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ticket_service.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", alert)

        assert decision.decision == "duplicate"
        assert decision.decided_by == "signature"
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_same_signature_different_key_is_a_deterministic_signature_amend(
        self, monkeypatch
    ):
        """B3: same normalized subject, different MPPT key on an open ticket
        is exactly what a multi-device storm looks like (plan finding 1) --
        this must resolve via the deterministic signature-amend rung, never
        touching the LLM, so a burst of N devices collapses onto one ticket
        instead of scattering across an LLM call each."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert(
            subject="! Warning: MPPT A7 in Kudi seems to perform lower !", details="mppt A7 [Kudi]"
        )
        correlator, store, ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_id": "tid-TKT-1",
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
        assert decision.decided_by == "signature"
        assert decision.ticket_ref == "TKT-1"
        assert decision.ticket_id == "tid-TKT-1"
        assert decision.affected_key == {"kind": "mppt", "key": "A7", "label": "MPPT A7"}
        assert decision.amended_summary == ""  # renderer recomputes, not the correlator
        assert decision.confidence == 1.0
        assert gateway.calls == []  # deterministic rung -- never reaches the LLM

    @pytest.mark.asyncio
    async def test_third_device_on_the_same_signature_also_amends_deterministically(
        self, monkeypatch
    ):
        """The storm isn't just two devices -- a third (or Nth) new component
        on the same fault shape must keep resolving deterministically too,
        as long as its own key isn't already in affected_keys."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert(
            subject="! Warning: MPPT B9 in Kudi seems to perform lower !", details="mppt B9 [Kudi]"
        )
        correlator, store, ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_id": "tid-TKT-1",
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [alert.signature],
                "affected_keys": [
                    {"kind": "mppt", "key": "A3", "label": "MPPT A3"},
                    {"kind": "mppt", "key": "A7", "label": "MPPT A7"},
                ],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", alert)

        assert decision.decision == "amend"
        assert decision.decided_by == "signature"
        assert decision.affected_key == {"kind": "mppt", "key": "B9", "label": "MPPT B9"}
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_amend_rung_never_fires_for_a_keyless_alert(self, monkeypatch):
        """A keyless (grid-level) alert can only ever be a duplicate (rung
        4) -- there's no component key that would make it a distinct
        affected component, so the amend rung must not claim it either."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = enrich_alert_facts(
            AlertFacts(subject="! Urgent: Grid outage in Kudi !", severity="urgent"),
            grid_name="Kudi",
        )
        gateway = _FakeGateway(text=json.dumps({"decision": "new", "confidence": 0.9}))
        correlator, store, ts, _gw = _make_correlator(gateway=gateway)
        store.correlations.append(
            {
                "ticket_id": "tid-TKT-1",
                "ticket_ref": "TKT-1",
                "grid_name": "Kudi",
                "status": "open",
                "severity": "urgent",
                "signatures": [],  # deliberately does NOT contain alert.signature
                "affected_keys": [{"kind": "mppt", "key": "A3", "label": "MPPT A3"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        ts.statuses["TKT-1"] = TicketStatus(summary="s", is_done=False)

        decision = await correlator.decide("Kudi", alert)

        # No deterministic rung matches (no signature overlap at all) --
        # falls through to the LLM's "new", confirming the amend rung didn't
        # short-circuit on a keyless alert by mistake.
        assert decision.decided_by == "llm"
        assert len(gateway.calls) == 1

    @pytest.mark.asyncio
    async def test_live_facts_are_added_only_when_correlation_calls_the_llm(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        gateway = _FakeGateway(text=json.dumps({"decision": "new", "confidence": 0.9}))
        correlator, store, ts, _gw = _make_correlator(gateway=gateway)
        store.correlations.append(
            {
                "ticket_id": "tid-TKT-1",
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
                "ticket_id": "tid-TKT-1",
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
                "ticket_id": "tid-OPS-42",
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
                "ticket_id": "tid-OPS-3363",
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
                "ticket_id": "tid-OPS-3363",
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
    async def test_done_candidate_dropped(self, monkeypatch):
        """A candidate the backend now reports as done is excluded from the
        decision set. Post-0005b there is no separate "mark closed" step for
        the correlator to perform -- ticket status lives solely on
        `tickets` (TicketRepository's table), so there is nothing left for
        the correlation layer to write back here."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        correlator, store, ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_id": "tid-TKT-1",
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

    @pytest.mark.asyncio
    async def test_unavailable_status_preserves_stored_exact_duplicate(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert()
        correlator, store, _ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_id": "tid-OPS-42",
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
        assert gateway.calls == []


class TestCandidateStatusConcurrency:
    @pytest.mark.asyncio
    async def test_status_lookups_run_concurrently(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        correlator, store, ticket_service, _gateway = _make_correlator()
        for index in range(8):
            store.correlations.append(
                {
                    "ticket_id": f"tid-OPS-{index}",
                    "ticket_ref": f"OPS-{index}",
                    "grid_name": "Kudi",
                    "status": "open",
                    "signatures": [],
                    "affected_keys": [],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        in_flight = 0
        peak = 0

        async def _slow_status(ref):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return TicketStatus(summary=ref, is_done=False)

        ticket_service.get_status = _slow_status

        candidates = await correlator._assemble_candidates("Kudi")

        assert len(candidates) == 8
        assert peak > 1
        assert peak <= DEFAULT_CORRELATION_POLICY.candidate_status_concurrency


class TestBackendOnlyCandidates:
    @pytest.mark.asyncio
    async def test_ticket_service_candidates_merged_and_offered_to_llm(self, monkeypatch):
        """A ticket filed by a human directly in Jira (or by n8n before
        cutover) -- tracked by TicketService.find_open_by_grid but absent
        from ticket_correlations -- must still be offered as an LLM
        candidate. Since it has no correlation row, it must first be
        adopted into the canonical `tickets` table (so it has a ticket_id
        to be amended by) via TicketService.adopt_external."""
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
        assert ts.adopted_calls == [
            {"ref": "OPS-1", "backend": "jira", "summary": "Pre-existing human ticket", "grid_name": "Kudi"}
        ]

    @pytest.mark.asyncio
    async def test_candidate_dropped_when_adoption_fails(self, monkeypatch):
        """A backend-discovered candidate that cannot be adopted (store
        outage mid-decision) is dropped rather than offered without a
        ticket_id -- it could never be amended safely."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        correlator, _store, ts, gateway = _make_correlator()
        ts.open_by_grid = [
            TicketSummary(ref="OPS-1", backend="jira", summary="Pre-existing human ticket", status="Open")
        ]

        async def _failing_adopt(**_kwargs):
            raise RuntimeError("db down")

        ts.adopt_external = _failing_adopt

        decision = await correlator.decide("Kudi", _mppt_alert())

        assert decision.decision == "new"
        assert decision.decided_by == "no_candidates"
        assert gateway.calls == []


class TestLlmFailureModes:
    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_new(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        gateway = _FakeGateway(text=json.dumps({"decision": "amend"}), delay=1.0)
        correlator, store, ts, _gw = _make_correlator(gateway=gateway, timeout_seconds=0.05)
        store.correlations.append(
            {"ticket_id": "tid-TKT-1", "ticket_ref": "TKT-1", "grid_name": "Kudi", "status": "open", "signatures": [], "affected_keys": [], "created_at": datetime.now(timezone.utc).isoformat()}
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
            {"ticket_id": "tid-TKT-1", "ticket_ref": "TKT-1", "grid_name": "Kudi", "status": "open", "signatures": [], "affected_keys": [], "created_at": datetime.now(timezone.utc).isoformat()}
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
            {"ticket_id": "tid-TKT-1", "ticket_ref": "TKT-1", "grid_name": "Kudi", "status": "open", "signatures": [], "affected_keys": [], "created_at": datetime.now(timezone.utc).isoformat()}
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
        assert store.events["dk-99"]["ticket_id"] is None


class TestPowerChainCascadeDecision:
    """C6: the real 2026-08-08 Ogbinbiri case (plan Example 5) end to end
    through AlertCorrelator.decide() -- a battery/BMS ticket already open,
    an inverter-off alert arriving minutes later. The historical model got
    this wrong (new, 0.9 confidence); this fixture is what right looks
    like."""

    @staticmethod
    def _bms_ticket_row() -> Dict[str, Any]:
        return {
            "ticket_id": "tid-OPS-3456",
            "ticket_ref": "OPS-3456",
            "grid_name": "Ogbinbiri",
            "status": "open",
            "signatures": ["bms-sig"],
            "affected_keys": [{"kind": "battery", "key": "BMS1", "label": "BMS1"}],
            "root_cause_kind": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _inverter_off_alert() -> AlertFacts:
        alert = AlertFacts(
            subject="RESTART FAILED - Inverter Off while battery Ok >52V ... causing Grid outage",
            details="",
            component_kind="inverter",
            component_key="INV1",
            component_label="Inverter INV1",
        )
        return enrich_alert_facts(alert, grid_name="Ogbinbiri")

    @pytest.mark.asyncio
    async def test_amends_onto_the_earlier_bms_ticket_when_the_flag_is_on(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        monkeypatch.setenv("ALERT_CASCADE_MERGE_ENABLED", "true")
        gateway = _FakeGateway(
            text=json.dumps(
                {
                    "decision": "amend",
                    "ticket_ref": "OPS-3456",
                    "confidence": 0.92,
                    "affected_key": {"kind": "inverter", "key": "INV1", "label": "Inverter INV1"},
                    "root_cause_kind": "power_chain",
                    "update_message": "Inverter shut down after BMS comms loss (OPS-3456)",
                    "reason": "battery/BMS -> inverter power chain, within 30 minutes",
                }
            )
        )
        correlator, store, ts, _gw = _make_correlator(gateway=gateway)
        store.correlations.append(self._bms_ticket_row())
        ts.statuses["OPS-3456"] = TicketStatus(summary="BMS comms lost", is_done=False)

        decision = await correlator.decide("Ogbinbiri", self._inverter_off_alert())

        assert decision.decision == "amend"
        assert decision.ticket_ref == "OPS-3456"
        assert decision.ticket_id == "tid-OPS-3456"
        assert decision.root_cause_kind == "power_chain"

    @pytest.mark.asyncio
    async def test_stays_new_when_the_flag_is_off(self, monkeypatch):
        """The historical incident's actual production configuration --
        cascade merging must default to today's behaviour until an operator
        opts in."""
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        monkeypatch.delenv("ALERT_CASCADE_MERGE_ENABLED", raising=False)
        gateway = _FakeGateway(
            text=json.dumps(
                {
                    "decision": "amend",
                    "ticket_ref": "OPS-3456",
                    "confidence": 0.92,
                    "affected_key": {"kind": "inverter", "key": "INV1", "label": "Inverter INV1"},
                    "root_cause_kind": "power_chain",
                    "update_message": "Inverter shut down after BMS comms loss (OPS-3456)",
                    "reason": "battery/BMS -> inverter power chain, within 30 minutes",
                }
            )
        )
        correlator, store, ts, _gw = _make_correlator(gateway=gateway)
        store.correlations.append(self._bms_ticket_row())
        ts.statuses["OPS-3456"] = TicketStatus(summary="BMS comms lost", is_done=False)

        decision = await correlator.decide("Ogbinbiri", self._inverter_off_alert())

        assert decision.decision == "new"
