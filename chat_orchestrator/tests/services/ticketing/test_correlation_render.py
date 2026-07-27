"""Tests for correlation_render.py: render_summary/render_description (pure
functions) and apply_amendment (the amend/duplicate execution orchestration
that runs after AlertCorrelator decides).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing.alert_facts import AlertFacts
from orchestrator.services.ticketing.correlation_render import (
    MARKER_END,
    MARKER_START,
    apply_amendment,
    render_description,
    render_summary,
)
from orchestrator.services.ticketing.correlator import CorrelationDecision


def _correlation(**overrides: Any) -> Dict[str, Any]:
    defaults: Dict[str, Any] = dict(
        ticket_ref="TKT-1",
        grid_name="Kudi",
        summary_base="! Warning: MPPT A3 in Kudi seems to perform lower than other MPPTs !",
        description_base="Please check VRM.",
        affected_keys=[
            {
                "kind": "mppt",
                "key": "A3",
                "label": "MPPT A3",
                "first_seen": "2026-01-01T00:00:00Z",
                "last_seen": "2026-01-01T00:00:00Z",
                "count": 1,
            }
        ],
        occurrence_count=1,
        root_cause_kind=None,
        escalated_at=None,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=999,
    )
    defaults.update(overrides)
    return defaults


class TestRenderSummary:
    def test_single_key_keeps_llm_summary(self):
        correlation = _correlation()
        alert = AlertFacts(subject="! Warning: MPPT A3 in Kudi !")

        result = render_summary(correlation, alert, llm_summary="! Warning: custom text !")

        assert result == "! Warning: custom text !"

    def test_multiple_keys_uses_aggregate_template(self):
        correlation = _correlation(
            affected_keys=[
                {"kind": "mppt", "key": "A3", "label": "MPPT A3", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "mppt", "key": "A7", "label": "MPPT A7", "first_seen": "t", "last_seen": "t", "count": 1},
            ]
        )
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !")

        result = render_summary(correlation, alert, llm_summary="ignored for multi-key")

        assert "2" in result
        assert "A3" in result and "A7" in result
        assert "Kudi" in result
        assert result.startswith("! Warning:")

    def test_urgent_severity_marker_preserved(self):
        correlation = _correlation(
            summary_base="! Urgent: Inverter Fault reported in Kudi !",
            affected_keys=[
                {"kind": "dcu", "key": "1", "label": "DCU 1", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "dcu", "key": "2", "label": "DCU 2", "first_seen": "t", "last_seen": "t", "count": 1},
            ],
        )
        alert = AlertFacts(subject="! Urgent: DCU 2 down !")

        result = render_summary(correlation, alert, llm_summary="")

        assert result.startswith("! Urgent:")

    def test_truncates_long_key_list(self):
        keys = [
            {"kind": "mppt", "key": f"A{i}", "label": f"MPPT A{i}", "first_seen": "t", "last_seen": "t", "count": 1}
            for i in range(8)
        ]
        correlation = _correlation(affected_keys=keys)
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !")

        result = render_summary(correlation, alert, llm_summary="")

        assert "+2 more" in result
        assert "8" in result  # total count still shown


class TestRenderDescription:
    def test_includes_marker_block_and_affected_keys(self):
        correlation = _correlation()

        result = render_description(correlation)

        assert result.startswith("Please check VRM.")
        assert MARKER_START in result
        assert MARKER_END in result
        assert "MPPT A3" in result
        assert "Affected components (1):" in result

    def test_idempotent_same_input_same_output(self):
        correlation = _correlation()

        first = render_description(correlation)
        second = render_description(correlation)

        assert first == second
        assert first.count(MARKER_START) == 1
        assert first.count(MARKER_END) == 1

    def test_includes_root_cause_kind_when_present(self):
        correlation = _correlation(root_cause_kind="grid_off")

        result = render_description(correlation)

        assert "Root cause: grid_off" in result

    def test_omits_root_cause_line_when_absent(self):
        correlation = _correlation(root_cause_kind=None)

        result = render_description(correlation)

        assert "Root cause:" not in result

    def test_occurrence_count_shown(self):
        correlation = _correlation(occurrence_count=7)

        result = render_description(correlation)

        assert "Occurrences: 7" in result

    def test_multiple_affected_keys_all_listed(self):
        correlation = _correlation(
            affected_keys=[
                {"kind": "mppt", "key": "A3", "label": "MPPT A3", "first_seen": "t1", "last_seen": "t2", "count": 2},
                {"kind": "mppt", "key": "A7", "label": "MPPT A7", "first_seen": "t3", "last_seen": "t3", "count": 1},
            ]
        )

        result = render_description(correlation)

        assert "MPPT A3" in result
        assert "MPPT A7" in result
        assert "Affected components (2):" in result

    def test_no_base_description_still_renders_block(self):
        correlation = _correlation(description_base="")

        result = render_description(correlation)

        assert result.startswith(MARKER_START)


# ---------------------------------------------------------------------------
# apply_amendment
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, correlation: Optional[Dict[str, Any]] = None) -> None:
        self.correlation = correlation
        self.bump_occurrence_calls: List[str] = []
        self.merge_calls: List[Dict[str, Any]] = []
        self.record_amendment_calls: List[Dict[str, Any]] = []

    async def bump_occurrence(self, ticket_ref: str, occurred_at=None) -> bool:
        self.bump_occurrence_calls.append(ticket_ref)
        return True

    async def merge_affected_key(self, ticket_ref, *, kind, key, label, occurred_at=None, signature=None):
        self.merge_calls.append({"ticket_ref": ticket_ref, "kind": kind, "key": key, "label": label})
        if self.correlation is not None:
            affected = list(self.correlation.get("affected_keys") or [])
            if not any(e["kind"] == kind and e["key"] == key for e in affected):
                affected.append({"kind": kind, "key": key, "label": label, "first_seen": "t", "last_seen": "t", "count": 1})
                self.correlation["affected_keys"] = affected
        return self.correlation.get("affected_keys") if self.correlation else None

    async def get_correlation(self, ticket_ref: str) -> Optional[Dict[str, Any]]:
        return self.correlation

    async def record_amendment(self, ticket_ref: str, *, summary_current: str, escalated: bool = False) -> bool:
        self.record_amendment_calls.append(
            {"ticket_ref": ticket_ref, "summary_current": summary_current, "escalated": escalated}
        )
        return True


class _FakeTicketService:
    def __init__(self) -> None:
        self.update_calls: List[Dict[str, Any]] = []
        self.comment_calls: List[Dict[str, Any]] = []

    async def update_ticket(self, ref, summary=None, description=None, priority_id=None) -> bool:
        self.update_calls.append(
            {"ref": ref, "summary": summary, "description": description, "priority_id": priority_id}
        )
        return True

    async def add_comment(self, ref, body, public=False) -> bool:
        self.comment_calls.append({"ref": ref, "body": body, "public": public})
        return True


def _amend_decision(**overrides: Any) -> CorrelationDecision:
    defaults: Dict[str, Any] = dict(
        decision="amend",
        ticket_ref="TKT-1",
        confidence=0.9,
        decided_by="llm",
        reason="same root cause",
        affected_key={"kind": "mppt", "key": "A7", "label": "MPPT A7"},
        root_cause_kind=None,
        update_message="TKT-1: MPPT A7 also affected",
        amended_summary="",
        candidate_refs=["TKT-1"],
        llm_raw="{}",
        needs_root_cause_ticket=False,
    )
    defaults.update(overrides)
    return CorrelationDecision(**defaults)


class TestApplyAmendmentAmend:
    @pytest.mark.asyncio
    async def test_merges_key_updates_ticket_and_comments(self):
        correlation = _correlation()
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !", signature="sig-x")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            alert=alert,
            decision=_amend_decision(),
            raw_text="raw notify text",
            escalate_after=5,
        )

        assert result is not None
        assert result.decision == "amend"
        assert store.bump_occurrence_calls == ["TKT-1"]
        assert store.merge_calls == [{"ticket_ref": "TKT-1", "kind": "mppt", "key": "A7", "label": "MPPT A7"}]
        assert len(ticket_service.update_calls) == 1
        assert ticket_service.update_calls[0]["description"] is not None
        assert ticket_service.comment_calls == [
            {"ref": "TKT-1", "body": "raw notify text", "public": False}
        ]
        assert store.record_amendment_calls[0]["escalated"] is False

    @pytest.mark.asyncio
    async def test_escalates_when_threshold_crossed(self):
        correlation = _correlation(
            affected_keys=[
                {"kind": "mppt", "key": "A3", "label": "MPPT A3", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "mppt", "key": "A5", "label": "MPPT A5", "first_seen": "t", "last_seen": "t", "count": 1},
            ]
        )
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            alert=alert,
            decision=_amend_decision(),
            raw_text="raw text",
            escalate_after=3,  # merge brings count from 2 -> 3, crossing the threshold
            escalated_priority_id="prio-escalated",
        )

        assert result.escalated is True
        assert ticket_service.update_calls[0]["summary"].startswith("🔴")
        assert ticket_service.update_calls[0]["priority_id"] == "prio-escalated"
        assert store.record_amendment_calls[0]["escalated"] is True

    @pytest.mark.asyncio
    async def test_no_re_escalation_once_already_escalated(self):
        correlation = _correlation(
            escalated_at="2026-01-01T00:00:00Z",
            affected_keys=[
                {"kind": "mppt", "key": "A3", "label": "MPPT A3", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "mppt", "key": "A5", "label": "MPPT A5", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "mppt", "key": "A6", "label": "MPPT A6", "first_seen": "t", "last_seen": "t", "count": 1},
            ],
        )
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            alert=alert,
            decision=_amend_decision(),
            raw_text="raw text",
            escalate_after=3,
        )

        assert result.escalated is False
        assert not ticket_service.update_calls[0]["summary"].startswith("🔴")

    @pytest.mark.asyncio
    async def test_returns_none_when_correlation_missing(self):
        store = _FakeStore(correlation=None)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="x")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            alert=alert,
            decision=_amend_decision(),
            raw_text="raw text",
        )

        assert result is None
        assert ticket_service.update_calls == []


class TestApplyAmendmentDuplicate:
    @pytest.mark.asyncio
    async def test_duplicate_only_bumps_occurrence_no_ticket_mutation(self):
        correlation = _correlation()
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Warning: MPPT A3 in Kudi !")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            alert=alert,
            decision=_amend_decision(decision="duplicate", affected_key=None),
            raw_text="raw text",
        )

        assert result is not None
        assert result.decision == "duplicate"
        assert store.bump_occurrence_calls == ["TKT-1"]
        assert store.merge_calls == []
        assert ticket_service.update_calls == []
        assert ticket_service.comment_calls == []

    @pytest.mark.asyncio
    async def test_duplicate_returns_telegram_targets_for_rollup(self):
        correlation = _correlation()
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Warning: MPPT A3 in Kudi !")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            alert=alert,
            decision=_amend_decision(decision="duplicate", affected_key=None),
            raw_text="raw text",
        )

        assert result.telegram_chat_id == "-100555"
        assert result.telegram_topic_id == "42"
        assert result.telegram_message_id == 999
