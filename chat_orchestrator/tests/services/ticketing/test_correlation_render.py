"""Tests for correlation_render.py: render_summary/render_description (pure
functions) and apply_amendment (the amend/duplicate execution orchestration
that runs after AlertCorrelator decides).

``correlation`` dicts used throughout only carry the columns
``ticket_correlations`` actually has post-0005b (see
db/migrations/0005b_ticket_schema_validate_and_contract.sql) -- current
ticket ref/backend/summary/status/grid live on ``tickets`` instead, and
Telegram delivery coordinates live on ``message_deliveries``, neither of
which this module reads or writes.
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
        ticket_id="ticket-1",
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
    )
    defaults.update(overrides)
    return defaults


class TestRenderSummary:
    def test_single_key_keeps_llm_summary(self):
        correlation = _correlation()
        alert = AlertFacts(subject="! Warning: MPPT A3 in Kudi !")

        result = render_summary(correlation, alert, llm_summary="! Warning: custom text !", grid_name="Kudi")

        assert result == "! Warning: custom text !"

    def test_multiple_keys_uses_aggregate_template(self):
        correlation = _correlation(
            affected_keys=[
                {"kind": "mppt", "key": "A3", "label": "MPPT A3", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "mppt", "key": "A7", "label": "MPPT A7", "first_seen": "t", "last_seen": "t", "count": 1},
            ]
        )
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !")

        result = render_summary(correlation, alert, llm_summary="ignored for multi-key", grid_name="Kudi")

        assert "2" in result
        assert "A3" in result and "A7" in result
        assert "Kudi" in result
        assert result.startswith("! Warning:")

    def test_grid_name_renders_without_a_blank_gap(self):
        """Regression: ticket_correlations dropped its own grid_name column
        (0005b) -- a caller that forgot to pass the real grid_name used to
        silently render "N MPPTs in  affected" (a blank, double-spaced grid
        name) because render_summary fell back to reading a now-nonexistent
        correlation["grid_name"]. grid_name is now a required parameter, so
        this asserts the happy path never regresses to that blank gap."""
        correlation = _correlation(
            affected_keys=[
                {"kind": "mppt", "key": "0", "label": "MPPT 0", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "mppt", "key": "5", "label": "MPPT 5", "first_seen": "t", "last_seen": "t", "count": 1},
            ]
        )
        alert = AlertFacts(subject="! Urgent: MPPT 5 in GridY !", severity="urgent")

        result = render_summary(correlation, alert, llm_summary="", grid_name="GridY")

        assert "in GridY affected" in result
        assert "in  affected" not in result

    def test_urgent_severity_marker_preserved(self):
        correlation = _correlation(
            summary_base="! Urgent: Inverter Fault reported in Kudi !",
            affected_keys=[
                {"kind": "dcu", "key": "1", "label": "DCU 1", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "dcu", "key": "2", "label": "DCU 2", "first_seen": "t", "last_seen": "t", "count": 1},
            ],
        )
        alert = AlertFacts(subject="! Urgent: DCU 2 down !")

        result = render_summary(correlation, alert, llm_summary="", grid_name="Kudi")

        assert result.startswith("! Urgent:")

    def test_urgent_severity_increase_replaces_warning_marker(self):
        correlation = _correlation(
            affected_keys=[
                {"kind": "mppt", "key": "A3", "label": "MPPT A3"},
                {"kind": "mppt", "key": "A7", "label": "MPPT A7"},
            ],
        )
        alert = AlertFacts(component_kind="mppt", severity="urgent")

        result = render_summary(correlation, alert, llm_summary="", grid_name="Kudi")

        assert result.startswith("! Urgent:")

    def test_truncates_long_key_list(self):
        keys = [
            {"kind": "mppt", "key": f"A{i}", "label": f"MPPT A{i}", "first_seen": "t", "last_seen": "t", "count": 1}
            for i in range(8)
        ]
        correlation = _correlation(affected_keys=keys)
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !")

        result = render_summary(correlation, alert, llm_summary="", grid_name="Kudi")

        assert "+2 more" in result
        assert "8" in result  # total count still shown


class TestRenderSummaryCascade:
    """C5: affected_keys spanning more than one component kind is a
    power-chain cascade merge -- render_summary must stay root-cause-led
    instead of picking one dominant kind and silently dropping the other."""

    @staticmethod
    def _cascade_correlation(**overrides: Any) -> Dict[str, Any]:
        defaults: Dict[str, Any] = dict(
            summary_base="! Warning: BMS communication lost",
            affected_keys=[
                {
                    "kind": "battery",
                    "key": "BMS1",
                    "label": "BMS1",
                    "first_seen": "2026-08-08T10:27:00Z",
                    "last_seen": "2026-08-08T10:27:00Z",
                    "count": 1,
                },
                {
                    "kind": "inverter",
                    "key": "INV1",
                    "label": "Inverter INV1",
                    "first_seen": "2026-08-08T10:31:00Z",
                    "last_seen": "2026-08-08T10:31:00Z",
                    "count": 1,
                },
            ],
            severity="warning",
        )
        defaults.update(overrides)
        return _correlation(**defaults)

    def test_headline_is_the_root_summary_base_not_the_llm_amended_summary(self):
        correlation = self._cascade_correlation()
        alert = AlertFacts(component_kind="inverter", severity="warning")

        result = render_summary(
            correlation, alert, llm_summary="ignored cascade text", grid_name="GridX"
        )

        assert result.startswith("! Warning: BMS communication lost")
        assert "ignored cascade text" not in result

    def test_names_the_dependent_kind_and_count(self):
        correlation = self._cascade_correlation()
        alert = AlertFacts(component_kind="inverter", severity="warning")

        result = render_summary(correlation, alert, llm_summary="", grid_name="GridX")

        assert "+1 dependent alert (Inverter)" in result

    def test_pluralizes_multiple_dependent_alerts_of_the_same_kind(self):
        correlation = self._cascade_correlation(
            affected_keys=[
                {
                    "kind": "battery", "key": "BMS1", "label": "BMS1",
                    "first_seen": "2026-08-08T10:27:00Z", "last_seen": "2026-08-08T10:27:00Z", "count": 1,
                },
                {
                    "kind": "inverter", "key": "INV1", "label": "Inverter INV1",
                    "first_seen": "2026-08-08T10:31:00Z", "last_seen": "2026-08-08T10:40:00Z", "count": 3,
                },
                {
                    "kind": "inverter", "key": "INV2", "label": "Inverter INV2",
                    "first_seen": "2026-08-08T10:33:00Z", "last_seen": "2026-08-08T10:33:00Z", "count": 1,
                },
            ]
        )
        alert = AlertFacts(component_kind="inverter", severity="warning")

        result = render_summary(correlation, alert, llm_summary="", grid_name="GridX")

        assert "+2 dependent alerts (Inverter)" in result

    def test_root_kind_is_whichever_arrived_first_not_alphabetical_order(self):
        """Proves the tiebreak is first_seen, not kind-name order: 'inverter'
        sorts after 'battery' alphabetically but arrived first here, so it
        must still win as root -- and 'battery', despite recurring, stays
        the dependent symptom."""
        correlation = self._cascade_correlation(
            summary_base="! Warning: Inverter cycling",
            affected_keys=[
                {
                    "kind": "inverter", "key": "INV1", "label": "Inverter INV1",
                    "first_seen": "2026-08-08T09:00:00Z", "last_seen": "2026-08-08T09:00:00Z", "count": 1,
                },
                {
                    "kind": "battery", "key": "BMS1", "label": "BMS1",
                    "first_seen": "2026-08-08T09:05:00Z", "last_seen": "2026-08-08T09:05:00Z", "count": 1,
                },
            ],
        )
        alert = AlertFacts(component_kind="battery", severity="warning")

        result = render_summary(correlation, alert, llm_summary="", grid_name="GridX")

        assert result.startswith("! Warning: Inverter cycling")
        assert "+1 dependent alert (Battery)" in result

    def test_stays_a_warning_when_nothing_is_urgent(self):
        correlation = self._cascade_correlation()
        alert = AlertFacts(component_kind="inverter", severity="warning")

        result = render_summary(correlation, alert, llm_summary="", grid_name="GridX")

        assert result.startswith("! Warning:")

    def test_incoming_urgent_alert_upgrades_the_marker(self):
        correlation = self._cascade_correlation()
        alert = AlertFacts(component_kind="inverter", severity="urgent")

        result = render_summary(correlation, alert, llm_summary="", grid_name="GridX")

        assert result.startswith("! Urgent:")

    def test_a_prior_urgent_symptom_keeps_the_marker_urgent_even_if_this_alert_is_not(self):
        """The stored correlation severity already ratcheted to urgent from
        an earlier fold (apply_amendment's effective_severity) --
        summary_base itself never changes, so the stored severity is the
        only way "any folded symptom is urgent" can still be seen here."""
        correlation = self._cascade_correlation(severity="urgent")
        alert = AlertFacts(component_kind="inverter", severity="warning")

        result = render_summary(correlation, alert, llm_summary="", grid_name="GridX")

        assert result.startswith("! Urgent:")

    def test_lists_every_dependent_kind_label_when_three_kinds_are_present(self):
        correlation = self._cascade_correlation(
            affected_keys=[
                {
                    "kind": "battery", "key": "BMS1", "label": "BMS1",
                    "first_seen": "2026-08-08T10:27:00Z", "last_seen": "2026-08-08T10:27:00Z", "count": 1,
                },
                {
                    "kind": "inverter", "key": "INV1", "label": "Inverter INV1",
                    "first_seen": "2026-08-08T10:31:00Z", "last_seen": "2026-08-08T10:31:00Z", "count": 1,
                },
                {
                    "kind": "grid", "key": "", "label": "Grid outage",
                    "first_seen": "2026-08-08T10:32:00Z", "last_seen": "2026-08-08T10:32:00Z", "count": 1,
                },
            ]
        )
        alert = AlertFacts(component_kind="grid", severity="warning")

        result = render_summary(correlation, alert, llm_summary="", grid_name="GridX")

        assert "+2 dependent alerts (Grid, Inverter)" in result


class TestRenderDescription:
    def test_includes_marker_block_and_affected_keys(self):
        correlation = _correlation()

        result = render_description(correlation)

        assert MARKER_START in result
        assert MARKER_END in result
        assert "MPPT A3" in result
        assert "Affected components (1):" in result

    def test_marker_block_leads_the_description(self):
        """B5: the affected-equipment list is the first thing an operator
        reads, not buried after the original alert text."""
        correlation = _correlation()

        result = render_description(correlation)

        assert result.startswith(MARKER_START)
        assert result.index(MARKER_END) < result.index("Please check VRM.")

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

    def test_no_affected_keys_keeps_a_bare_description(self):
        """B5: a grid-level alert with no identifiable component keeps a
        bare description -- an empty "Affected components (0):" block with
        nothing listed under it is noise, not information."""
        correlation = _correlation(affected_keys=[])

        result = render_description(correlation)

        assert result == "Please check VRM."
        assert MARKER_START not in result
        assert MARKER_END not in result

    def test_no_affected_keys_and_no_base_is_empty(self):
        correlation = _correlation(affected_keys=[], description_base="")

        result = render_description(correlation)

        assert result == ""


# ---------------------------------------------------------------------------
# apply_amendment
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, correlation: Optional[Dict[str, Any]] = None) -> None:
        self.correlation = correlation
        self.bump_occurrence_calls: List[str] = []
        self.merge_calls: List[Dict[str, Any]] = []
        self.record_amendment_calls: List[Dict[str, Any]] = []
        self.upsert_calls: List[Dict[str, Any]] = []

    async def bump_occurrence(self, ticket_id: str, occurred_at=None) -> bool:
        self.bump_occurrence_calls.append(ticket_id)
        if self.correlation is not None:
            self.correlation["occurrence_count"] = int(self.correlation.get("occurrence_count") or 1) + 1
        return True

    async def merge_affected_key(self, ticket_id, *, kind, key, label, occurred_at=None, signature=None):
        from orchestrator.services.ticketing.correlation_store import AffectedKeyMerge

        self.merge_calls.append({"ticket_id": ticket_id, "kind": kind, "key": key, "label": label})
        if self.correlation is None:
            return None
        affected = list(self.correlation.get("affected_keys") or [])
        added = not any(e["kind"] == kind and e["key"] == key for e in affected)
        if added:
            affected.append({"kind": kind, "key": key, "label": label, "first_seen": "t", "last_seen": "t", "count": 1})
            self.correlation["affected_keys"] = affected
        return AffectedKeyMerge(affected_keys=affected, added=added)

    async def get_correlation(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        return self.correlation

    async def append_description_evidence(self, ticket_id: str, addition: str) -> bool:
        if self.correlation is None or not addition.strip():
            return False
        base = str(self.correlation.get("description_base") or "").rstrip()
        if addition.strip() not in base:
            self.correlation["description_base"] = (
                f"{base}\n\n{addition.strip()}" if base else addition.strip()
            )
        return True

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
        self.upsert_calls.append(
            {
                "ticket_id": ticket_id,
                "root_cause_kind": root_cause_kind,
                "primary_signature": primary_signature,
                "signatures": signatures,
                "summary_base": summary_base,
                "description_base": description_base,
                "severity": severity,
            }
        )
        self.correlation = {
            "ticket_id": ticket_id,
            "root_cause_kind": root_cause_kind,
            "primary_signature": primary_signature,
            "signatures": signatures,
            "affected_keys": affected_keys,
            "summary_base": summary_base,
            "description_base": description_base,
            "severity": severity,
            "occurrence_count": 1,
            "escalated_at": None,
        }
        return True

    async def record_amendment(
        self,
        ticket_id: str,
        *,
        severity: Optional[str] = None,
        escalated: bool = False,
    ) -> bool:
        self.record_amendment_calls.append(
            {
                "ticket_id": ticket_id,
                "severity": severity,
                "escalated": escalated,
            }
        )
        if self.correlation is not None and severity:
            self.correlation["severity"] = severity
        if self.correlation is not None and escalated:
            self.correlation["escalated_at"] = "now"
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
        ticket_id="ticket-1",
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
        ticket_severity="warning",
    )
    defaults.update(overrides)
    return CorrelationDecision(**defaults)


class TestApplyAmendmentAmend:
    @pytest.mark.asyncio
    async def test_judged_title_and_description_change_preserve_existing_content(self):
        correlation = _correlation(description_base="Original ticket description")
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=AlertFacts(subject="! Warning: MPPT A7 in Kudi !", severity="warning"),
            decision=_amend_decision(
                amended_summary="Grid outage following BMS loss",
                title_change_requested=True,
                description_addition=(
                    "Inverter shut down nine minutes after BMS communication was lost."
                ),
            ),
            raw_text="raw notify text",
            grid_name="Kudi",
        )

        assert result is not None
        assert ticket_service.update_calls[0]["summary"] == "Grid outage following BMS loss"
        assert "[anansi:affected-start]" in ticket_service.update_calls[0]["description"]
        assert "Original ticket description" in ticket_service.update_calls[0]["description"]
        assert "Inverter shut down nine minutes" in ticket_service.update_calls[0]["description"]

    @pytest.mark.asyncio
    async def test_new_affected_equipment_updates_ticket_without_count_escalation(self):
        correlation = _correlation(
            affected_keys=[
                {
                    "kind": "mppt",
                    "key": key,
                    "label": f"MPPT {key}",
                    "first_seen": "t",
                    "last_seen": "t",
                    "count": 1,
                }
                for key in ("A3", "A4", "A5")
            ]
        )
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !", severity="warning")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(),
            raw_text="raw notify text",
            grid_name="Kudi",
        )

        assert result is not None
        assert result.decision == "amend"
        assert result.escalated is False
        assert result.affected_keys_count == 4
        assert not ticket_service.update_calls[0]["summary"].startswith("🔴")
        assert ticket_service.update_calls[0]["priority_id"] is None
        assert result.rendered_summary == ticket_service.update_calls[0]["summary"]
        assert result.rendered_summary != ""
        assert store.record_amendment_calls == [
            {
                "ticket_id": "ticket-1",
                "severity": "warning",
                "escalated": False,
            }
        ]

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
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(),
            raw_text="raw notify text",
            grid_name="Kudi",
        )

        assert result is not None
        assert result.decision == "amend"
        assert store.bump_occurrence_calls == ["ticket-1"]
        assert store.merge_calls == [{"ticket_id": "ticket-1", "kind": "mppt", "key": "A7", "label": "MPPT A7"}]
        assert len(ticket_service.update_calls) == 1
        assert ticket_service.update_calls[0]["description"] is not None
        assert ticket_service.comment_calls == [
            {"ref": "TKT-1", "body": "raw notify text", "public": False}
        ]
        assert store.record_amendment_calls[0]["escalated"] is False

    @pytest.mark.asyncio
    async def test_first_urgent_severity_increase_escalates_to_highest(self):
        correlation = _correlation()
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Urgent: MPPT A7 in Kudi !", severity="urgent")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(ticket_severity="warning"),
            raw_text="raw text",
            grid_name="Kudi",
        )

        assert result.escalated is True
        assert ticket_service.update_calls[0]["summary"].startswith("🔴")
        assert ticket_service.update_calls[0]["priority_id"] == "highest"
        assert store.record_amendment_calls[0]["escalated"] is True
        assert store.record_amendment_calls[0]["severity"] == "urgent"
        assert correlation["severity"] == "urgent"

    @pytest.mark.asyncio
    async def test_already_urgent_ticket_keeps_marker_without_priority_update(self):
        correlation = _correlation(
            severity="urgent",
            escalated_at="2026-01-01T00:00:00Z",
            affected_keys=[
                {"kind": "mppt", "key": "A3", "label": "MPPT A3", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "mppt", "key": "A5", "label": "MPPT A5", "first_seen": "t", "last_seen": "t", "count": 1},
                {"kind": "mppt", "key": "A6", "label": "MPPT A6", "first_seen": "t", "last_seen": "t", "count": 1},
            ],
        )
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Urgent: MPPT A7 in Kudi !", severity="urgent")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(ticket_severity="urgent"),
            raw_text="raw text",
            grid_name="Kudi",
        )

        assert result.escalated is False
        assert ticket_service.update_calls[0]["summary"].startswith("🔴")
        assert ticket_service.update_calls[0]["priority_id"] is None

    @pytest.mark.asyncio
    async def test_replayed_persisted_urgent_amendment_is_silent(self):
        correlation = _correlation(
            severity="urgent",
            escalated_at="2026-01-01T00:00:00Z",
        )
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Urgent: MPPT A7 in Kudi !", severity="urgent")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(
                decided_by="replay",
                ticket_severity="urgent",
            ),
            raw_text="same retried alert",
            grid_name="Kudi",
        )

        assert result is not None
        assert result.decision == "duplicate"
        assert store.bump_occurrence_calls == []
        assert store.merge_calls == []
        assert store.record_amendment_calls == []
        assert ticket_service.update_calls == []
        assert ticket_service.comment_calls == []

    @pytest.mark.asyncio
    async def test_urgent_increase_is_silent_when_escalated_at_already_set(self):
        """B4: a row already carrying ``escalated_at`` (a prior escalation --
        here a "legacy" one whose severity field lagged behind, but the
        current design applies this uniformly) must never fire *another*
        escalation notification, even when this alert's own severity
        comparison would otherwise call it a fresh warning->urgent increase.
        The Highest-priority backend push still happens (idempotent,
        harmless) -- only the Telegram announcement is suppressed. A
        deliberate de-escalate-then-re-escalate is accepted as silent by
        this same rule -- see the plan's Known Limitations."""
        correlation = _correlation(
            severity="warning",
            escalated_at="2026-01-01T00:00:00Z",
        )
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Urgent: MPPT A7 in Kudi !", severity="urgent")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(ticket_severity="warning"),
            raw_text="raw text",
            grid_name="Kudi",
        )

        assert result.escalated is False
        # The backend write is unaffected -- still idempotently promoted.
        assert ticket_service.update_calls[0]["priority_id"] == "highest"

    @pytest.mark.asyncio
    async def test_escalation_notification_suppressed_when_persist_fails(self):
        """B4: state we could not persist must not be announced -- or it
        will be announced again on the next alert. The backend ticket update
        still happens (best-effort, independent of our own store's health);
        only the returned ``escalated`` flag (what drives the Telegram
        notification) is gated on ``record_amendment``'s real return value."""

        class _PersistFailingStore(_FakeStore):
            async def record_amendment(self, ticket_id: str, *, severity=None, escalated=False) -> bool:
                self.record_amendment_calls.append(
                    {"ticket_id": ticket_id, "severity": severity, "escalated": escalated}
                )
                return False

        correlation = _correlation()
        store = _PersistFailingStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Urgent: MPPT A7 in Kudi !", severity="urgent")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(ticket_severity="warning"),
            raw_text="raw text",
            grid_name="Kudi",
        )

        assert result.escalated is False
        # The backend priority push and comment still happened -- only the
        # notification signal is suppressed.
        assert ticket_service.update_calls[0]["priority_id"] == "highest"
        assert store.record_amendment_calls[0]["escalated"] is True

    @pytest.mark.asyncio
    async def test_seeds_a_correlation_row_when_none_exists_yet(self):
        """A ticket_id the correlator just resolved (e.g. via
        TicketRepository.adopt_external for a candidate discovered only
        through backend search) may never have had correlation state
        written. apply_amendment now seeds one from this alert and proceeds
        through the ordinary amend flow, rather than special-casing into an
        unconditional escalation the way the deleted "correlation row
        missing" branch used to."""
        store = _FakeStore(correlation=None)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !", severity="warning")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="OPS-42",
            ticket_id="ticket-ops-42",
            alert=alert,
            decision=_amend_decision(
                ticket_ref="OPS-42",
                ticket_id="ticket-ops-42",
                ticket_severity="",
                amended_summary="MPPT A7 in Kudi",
            ),
            raw_text="raw notify text",
            grid_name="Kudi",
        )

        assert result is not None
        assert result.decision == "amend"
        # A non-urgent first alert on a freshly-adopted candidate must not
        # force-escalate -- the deleted branch used to only ever reach this
        # scenario via an urgent alert and always escalated unconditionally.
        assert result.escalated is False
        assert ticket_service.update_calls[0]["priority_id"] is None
        assert not ticket_service.update_calls[0]["summary"].startswith("🔴")
        assert store.upsert_calls == [
            {
                "ticket_id": "ticket-ops-42",
                "root_cause_kind": None,
                "primary_signature": "",
                "signatures": [],
                "summary_base": "MPPT A7 in Kudi",
                "description_base": "raw notify text",
                "severity": "warning",
            }
        ]

    @pytest.mark.asyncio
    async def test_urgent_first_alert_on_freshly_adopted_candidate_escalates(self):
        store = _FakeStore(correlation=None)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="Inverter outage in Kudi", severity="urgent")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="OPS-42",
            ticket_id="ticket-ops-42",
            alert=alert,
            decision=_amend_decision(
                ticket_ref="OPS-42",
                ticket_id="ticket-ops-42",
                ticket_severity="",
                amended_summary="Kudi inverter outage",
            ),
            raw_text="urgent raw text",
            grid_name="Kudi",
        )

        assert result is not None
        assert result.escalated is True
        assert ticket_service.update_calls == [
            {
                "ref": "OPS-42",
                "summary": "🔴 ! Urgent: Kudi inverter outage",
                "description": ticket_service.update_calls[0]["description"],
                "priority_id": "highest",
            }
        ]
        assert ticket_service.comment_calls == [
            {"ref": "OPS-42", "body": "urgent raw text", "public": False}
        ]
        assert result.rendered_summary == "🔴 ! Urgent: Kudi inverter outage"

    @pytest.mark.asyncio
    async def test_returns_none_when_seeding_the_missing_row_fails(self):
        class _FailingSeedStore(_FakeStore):
            async def upsert_correlation(self, **_kwargs) -> bool:
                self.upsert_calls.append(_kwargs)
                return False

        store = _FailingSeedStore(correlation=None)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="x")

        result = await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(),
            raw_text="raw text",
            grid_name="Kudi",
        )

        assert result is None
        assert ticket_service.update_calls == []


class TestApplyAmendmentReportsNovelty:
    @pytest.mark.asyncio
    async def test_new_component_sets_component_added(self):
        store = _FakeStore(correlation=_correlation())
        result = await apply_amendment(
            store=store,
            ticket_service=_FakeTicketService(),
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=AlertFacts(subject="! Warning: MPPT A7 in Kudi !", severity="warning"),
            decision=_amend_decision(),
            raw_text="raw notify text",
            grid_name="Kudi",
        )

        assert result is not None
        assert result.component_added is True

    @pytest.mark.asyncio
    async def test_already_known_component_clears_component_added(self):
        store = _FakeStore(correlation=_correlation())
        decision = _amend_decision(
            affected_key={"kind": "mppt", "key": "A3", "label": "MPPT A3"}
        )

        result = await apply_amendment(
            store=store,
            ticket_service=_FakeTicketService(),
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=AlertFacts(subject="! Warning: MPPT A3 in Kudi !", severity="warning"),
            decision=decision,
            raw_text="raw notify text",
            grid_name="Kudi",
        )

        assert result is not None
        assert result.decision == "amend"
        assert result.component_added is False
        assert result.affected_keys_count == 1

    @pytest.mark.asyncio
    async def test_amend_without_affected_key_is_not_a_component_add(self):
        store = _FakeStore(correlation=_correlation(affected_keys=[]))

        result = await apply_amendment(
            store=store,
            ticket_service=_FakeTicketService(),
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=AlertFacts(subject="! Urgent: Grid outage in Kudi !", severity="warning"),
            decision=_amend_decision(affected_key=None),
            raw_text="raw notify text",
            grid_name="Kudi",
        )

        assert result is not None
        assert result.component_added is False
        assert result.affected_keys_count == 0
        assert store.merge_calls == []


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
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(decision="duplicate", affected_key=None),
            raw_text="raw text",
            grid_name="Kudi",
        )

        assert result is not None
        assert result.decision == "duplicate"
        assert store.bump_occurrence_calls == ["ticket-1"]
        assert store.merge_calls == []
        assert ticket_service.update_calls == []
        assert ticket_service.comment_calls == []


class TestApplyAmendmentFoldedComment:
    """C5: a power_chain amend's raw-alert comment is prefixed so the
    folded-in repair stays legible on the shared ticket -- the mitigation
    for the one real cost of merging a cascade over cross-linking it."""

    @pytest.mark.asyncio
    async def test_power_chain_amend_prefixes_the_comment(self):
        correlation = _correlation(
            affected_keys=[
                {
                    "kind": "battery", "key": "BMS1", "label": "BMS1",
                    "first_seen": "t", "last_seen": "t", "count": 1,
                }
            ],
        )
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(
            component_kind="inverter", component_key="INV1", component_label="Inverter INV1",
            severity="warning",
        )

        await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="OPS-3456",
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(
                ticket_ref="OPS-3456",
                affected_key={"kind": "inverter", "key": "INV1", "label": "Inverter INV1"},
                root_cause_kind="power_chain",
            ),
            raw_text="RESTART FAILED - Inverter Off while battery Ok >52V",
            grid_name="GridX",
        )

        assert ticket_service.comment_calls == [
            {
                "ref": "OPS-3456",
                "body": (
                    "Folded in as a power_chain symptom:\n\n"
                    "RESTART FAILED - Inverter Off while battery Ok >52V"
                ),
                "public": False,
            }
        ]

    @pytest.mark.asyncio
    async def test_ordinary_amend_comment_is_not_prefixed(self):
        """Regression: only a power_chain decision gets the prefix -- an
        everyday same-kind amend's comment must stay exactly the raw alert
        text, as it always has."""
        correlation = _correlation()
        store = _FakeStore(correlation=correlation)
        ticket_service = _FakeTicketService()
        alert = AlertFacts(subject="! Warning: MPPT A7 in Kudi !", component_kind="mppt")

        await apply_amendment(
            store=store,
            ticket_service=ticket_service,
            ticket_ref="TKT-1",
            ticket_id="ticket-1",
            alert=alert,
            decision=_amend_decision(root_cause_kind=None),
            raw_text="MPPT A7 low output",
            grid_name="Kudi",
        )

        assert ticket_service.comment_calls[0]["body"] == "MPPT A7 low output"
