"""AlertCorrelator -- the /notify smart-ticketing decision pipeline.

Decides whether an incoming alert (see ``alert_facts.AlertFacts``) is a
brand-new issue on a grid, an amend of an already-open ticket (a different
affected component of the same underlying problem), or an exact re-fire
("duplicate") of one already recorded. Read-only with respect to tickets --
this module never creates, comments on, or closes a ticket; it only decides
and records the decision as an audit event. Executing the decision (filing
the new ticket, or amending the existing one) is ``correlation_render.py``'s
job (Task 8), driven by the ``/notify`` handler (Task 9).

Decision pipeline (cheapest/safest first -- most alerts never reach the LLM):

0. ``dedup_key`` already recorded -> replay the prior decision, no new I/O.
1. ``ALERT_CORRELATION_ENABLED`` off -> "new" (the kill switch can never
   drop an alert -- it only turns off grouping).
2. No open candidates for the grid -> "new".
3. An open candidate already has this alert's exact ``(signature,
   component_key)`` pair -> deterministic "duplicate", never touches the LLM.
4. For an alert with no identifiable component, an open candidate already
   carrying this alert's exact ``signature`` -> deterministic "duplicate",
   never touches the LLM (there is no component key that could make it a
   distinct affected component).
5. An open candidate already carries this alert's exact ``signature`` but
   not yet this ``component_key`` -> deterministic "amend", never touches
   the LLM. This is what collapses an N-device storm (one fault, many
   MPPTs) onto a single ticket -- see ``_find_signature_amend``.
6. Otherwise, ask the LLM, then run its response through
   ``_apply_guardrails`` -- which can force "new" (unknown ref, low
   confidence, unparseable response), downgrade "duplicate" to "amend" (no
   signature overlap and not a confident "same_issue"), or flag
   ``needs_root_cause_ticket`` when the model says this is a symptom of a
   grid-level root cause that no open candidate itself represents.

Every failure mode here -- LLM timeout, transport error, malformed JSON, a
correlation-store outage -- degrades to "new" (``decided_by="fallback"``),
never a raised exception: every alert must result in a ticket.

The judgment path (``judge`` + ``to_legacy_correlation_decision``, reached
when ``ALERT_LLM_JUDGMENT_ENABLED`` is on, which is the default) replaces
rungs 3-6's *decision-making* with one typed model call, and feeds the
deterministic rungs in as evidence instead. It applies the same four
guardrails as rung 6 -- see ``to_legacy_correlation_decision``. They are
what keeps "one ticket per causally related failure" true rather than
degrading to "one ticket per grid".
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from orchestrator.config.settings import get_settings
from shared.config import flag_registry as fr
from shared.llm import GenerationOptions, LLMMessage
from shared.utils.logging import get_logger

from . import correlation_rules
from .alert_facts import AlertFacts, derive_severity, same_component
from .alert_judgment import (
    AlertJudgmentResult,
    DeterministicFinding,
    TicketAction,
    TicketRelationship,
    parse_alert_judgment,
)
from .alert_judgment_context import AlertJudgmentContext

if TYPE_CHECKING:
    from .backend import TicketStatus

LOGGER = get_logger(__name__)

_ROOT_CAUSE_KINDS_REQUIRING_PARENT = ("grid_off", "grid_isolated")


def effective_candidate_severity(candidate: CandidateSummary) -> str:
    """The candidate's severity for guardrail/signature-rung comparison,
    with fallbacks for a candidate whose correlation row has never recorded
    one -- most commonly a Jira-discovered ticket just adopted via
    ``TicketRepository.adopt_external``, which carries no stored severity at
    all. A blank severity must never read as "not urgent" when the ticket
    plainly already is one (an escalated "🔴 " summary, or a summary whose
    own "! Urgent:"/"! Warning:" marker says so) -- that's exactly what
    would let a stale candidate masquerade as a fresh warning->urgent
    transition on every subsequent alert.

    Order: stored ``severity`` field, else derived from the summary's
    "! Urgent:"/"! Warning:" marker, else "urgent" when the summary starts
    with the escalated-ticket "🔴 " marker, else "" (genuinely unknown).
    """
    if candidate.severity:
        return candidate.severity
    derived = derive_severity(candidate.summary)
    if derived:
        return derived
    if candidate.summary.strip().startswith("🔴"):
        return "urgent"
    return ""


class CandidateSummary(BaseModel):
    """A candidate ticket offered to the correlator, merged from
    ``CorrelationStore`` (backend-agnostic, has signatures/affected_keys)
    and ``TicketService.find_open_by_grid`` (catches human-filed or
    pre-cutover tickets the correlation layer never recorded)."""

    ref: str
    ticket_id: Optional[str] = None
    backend: str = ""
    summary: str = ""
    description: str = ""
    age_hours: Optional[float] = None
    root_cause_kind: Optional[str] = None
    affected_keys: List[Dict[str, Any]] = Field(default_factory=list)
    occurrence_count: int = 1
    status: str = ""
    signatures: List[str] = Field(default_factory=list)
    severity: str = ""


@dataclass(frozen=True)
class CorrelationDecision:
    """The correlator's verdict for one incoming alert."""

    decision: str  # "new" | "amend" | "duplicate"
    ticket_ref: Optional[str]
    confidence: Optional[float]
    decided_by: str  # "replay"|"flag_off"|"no_candidates"|"signature"|"llm"|"fallback"
    # Two more are produced outside this module, both in app.py, and both
    # mirroring this class's "signature" rung in a context it cannot reach:
    # "fallback_signature" from the lock-free grid-lock-timeout fallback
    # (``_attempt_lock_free_signature_correlation``), and
    # "signature_backstop" from the LLM-judgment path, where an exact
    # signature match overrules a judgment of "new"
    # (``_resolve_notify_ticket_llm_judgment``). Both are worth telling apart
    # from a lock-held "signature" in the audit trail.
    reason: str
    affected_key: Optional[Dict[str, str]]
    root_cause_kind: Optional[str]
    update_message: str
    amended_summary: str
    candidate_refs: List[str]
    llm_raw: Optional[str]
    needs_root_cause_ticket: bool = False
    ticket_severity: str = ""
    ticket_id: Optional[str] = None
    description_addition: str = ""
    title_change_requested: bool = False


def _parse_llm_response(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Strip code fences (```json ... ```) and parse JSON. ``None`` on any failure."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        lines = [
            line
            for line in text.splitlines()
            if not line.strip().startswith("```") and line.strip().lower() != "json"
        ]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _fallback_decision(
    reason: str,
    candidate_refs: List[str],
    llm_raw: Optional[str] = None,
    decided_by: str = "fallback",
) -> CorrelationDecision:
    return CorrelationDecision(
        decision="new",
        ticket_ref=None,
        confidence=None,
        decided_by=decided_by,
        reason=reason,
        affected_key=None,
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=candidate_refs,
        llm_raw=llm_raw,
        needs_root_cause_ticket=False,
    )


def _apply_guardrails(
    parsed: Dict[str, Any],
    candidates: List[CandidateSummary],
    min_confidence: float,
    llm_raw: Optional[str],
    alert_signature: Optional[str] = None,
    alert_severity: str = "",
) -> CorrelationDecision:
    """Turn a parsed LLM response into a safe ``CorrelationDecision``.

    Every branch that can't be trusted at face value forces ``decision="new"``
    rather than raising -- see the module docstring's failure-mode list.
    """
    candidate_refs = [c.ref for c in candidates]
    by_ref = {c.ref: c for c in candidates}

    decision = parsed.get("decision")
    if decision not in ("new", "amend", "duplicate"):
        return _fallback_decision(
            "LLM response missing/invalid 'decision' field", candidate_refs, llm_raw
        )

    raw_confidence = parsed.get("confidence")
    try:
        confidence = float(raw_confidence) if raw_confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    reason = str(parsed.get("reason") or "")
    root_cause_kind = parsed.get("root_cause_kind") or None
    update_message = str(parsed.get("update_message") or "")[:500]
    affected_key = parsed.get("affected_key") if isinstance(parsed.get("affected_key"), dict) else None
    amended_summary = re.sub(r"\s+", " ", str(parsed.get("amended_summary") or "")).strip()[:240]

    if decision == "new":
        return CorrelationDecision(
            decision="new",
            ticket_ref=None,
            confidence=confidence,
            decided_by="llm",
            reason=reason,
            affected_key=affected_key,
            root_cause_kind=root_cause_kind,
            update_message=update_message,
            amended_summary="",
            candidate_refs=candidate_refs,
            llm_raw=llm_raw,
            needs_root_cause_ticket=False,
        )

    # amend / duplicate both require a ticket_ref that's actually a candidate --
    # the model can't invent or hallucinate a ref.
    ticket_ref = parsed.get("ticket_ref")
    if not ticket_ref or ticket_ref not in by_ref:
        return _fallback_decision(
            f"LLM referenced unknown ticket_ref {ticket_ref!r}", candidate_refs, llm_raw
        )

    if confidence is None or confidence < min_confidence:
        return _fallback_decision(
            f"confidence {confidence!r} below minimum {min_confidence}", candidate_refs, llm_raw
        )

    if decision == "duplicate" and _is_urgent_severity_increase(
        alert_severity, effective_candidate_severity(by_ref[ticket_ref])
    ):
        decision = "amend"
        reason = reason or "urgent severity increase makes this alert a material amendment"
    elif decision == "duplicate":
        relationship = parsed.get("relationship")
        candidate = by_ref[ticket_ref]
        signature_overlap = bool(alert_signature) and alert_signature in (candidate.signatures or [])
        same_issue_confident = relationship == "same_issue" and confidence >= 0.85
        if not (signature_overlap or same_issue_confident):
            # Downgrade, never drop -- an amend still surfaces the alert.
            decision = "amend"
            reason = reason or (
                "duplicate downgraded to amend: no signature overlap and not a "
                "confident same_issue relationship"
            )

    if decision == "amend":
        target_kinds = {
            str(entry.get("kind") or "").strip()
            for entry in by_ref[ticket_ref].affected_keys or []
            if entry.get("kind")
        }
        incoming_kind = str((affected_key or {}).get("kind") or "").strip()
        # A ticket the store has never recorded any kind for (e.g. a
        # Jira-discovered candidate adopted with no affected_keys) can't be
        # proven cross-kind either way -- only refuse when we positively
        # know the incoming kind isn't among the ticket's own.
        is_cross_kind = bool(target_kinds and incoming_kind and incoming_kind not in target_kinds)
        if is_cross_kind:
            cascade_allowed = (
                root_cause_kind == "power_chain"
                and fr.get("ALERT_CASCADE_MERGE_ENABLED")
                and confidence >= min_confidence
            )
            if not cascade_allowed:
                # Today's outcome, restored: a cross-kind amend this module
                # has never been able to propose before Phase C must not
                # silently start succeeding just because the model offered
                # one -- it needs the topology reasoning (root_cause_kind),
                # the operator's own kill switch, and the same confidence
                # bar every other amend already clears.
                return CorrelationDecision(
                    decision="new",
                    ticket_ref=None,
                    confidence=confidence,
                    decided_by="llm",
                    reason=reason
                    or (
                        f"cross-kind amend ({incoming_kind!r} onto a ticket carrying "
                        f"{sorted(target_kinds)!r}) rejected: not a permitted "
                        "power-chain cascade"
                    ),
                    affected_key=affected_key,
                    root_cause_kind=root_cause_kind,
                    update_message=update_message,
                    amended_summary="",
                    candidate_refs=candidate_refs,
                    llm_raw=llm_raw,
                    needs_root_cause_ticket=False,
                )

    needs_root_cause_ticket = False
    final_ticket_ref: Optional[str] = ticket_ref
    final_ticket_id: Optional[str] = by_ref[ticket_ref].ticket_id
    # power_chain deliberately does not join this set (unlike grid_off/
    # grid_isolated): a power_chain symptom's parent ticket already exists
    # by construction (the cross-kind guard above only allows it to amend
    # onto a real candidate) -- there is nothing to synthesize.
    if decision == "amend" and root_cause_kind in _ROOT_CAUSE_KINDS_REQUIRING_PARENT:
        any_root_cause_candidate = any(c.root_cause_kind == root_cause_kind for c in candidates)
        if not any_root_cause_candidate:
            needs_root_cause_ticket = True
            final_ticket_ref = None
            final_ticket_id = None

    return CorrelationDecision(
        decision=decision,
        ticket_ref=final_ticket_ref,
        ticket_id=final_ticket_id,
        confidence=confidence,
        decided_by="llm",
        reason=reason,
        affected_key=affected_key,
        root_cause_kind=root_cause_kind,
        update_message=update_message,
        amended_summary=amended_summary,
        candidate_refs=candidate_refs,
        llm_raw=llm_raw,
        needs_root_cause_ticket=needs_root_cause_ticket,
        ticket_severity=effective_candidate_severity(by_ref[ticket_ref]),
    )


def _find_signature_duplicate(
    candidates: List[CandidateSummary], alert: AlertFacts
) -> Optional[CandidateSummary]:
    """Rung 3: an open candidate whose ``signatures`` already contains this
    alert's exact signature AND whose ``affected_keys`` already has this
    exact component key -- a genuine re-fire, not merely a similar issue."""
    if not alert.signature or not alert.component_kind:
        return None
    for candidate in candidates:
        if alert.signature not in (candidate.signatures or []):
            continue
        for entry in candidate.affected_keys or []:
            if same_component(entry, alert.component_kind, alert.component_key):
                return candidate
    return None


def _find_signature_only_duplicate(
    candidates: List[CandidateSummary], alert: AlertFacts
) -> Optional[CandidateSummary]:
    """Rung 4: for an alert with no identifiable component, an open candidate
    carrying this exact signature *is* the same alert re-firing -- there is no
    component key that could make it a distinct affected component. Without
    this, identical grid-level and unparsed-device alerts depend entirely on an
    LLM judgment that falls back to "new" (a duplicate ticket) on any hiccup."""
    if not alert.signature or alert.component_kind:
        return None
    for candidate in candidates:
        if alert.signature in (candidate.signatures or []):
            return candidate
    return None


def _find_signature_amend(
    candidates: List[CandidateSummary], alert: AlertFacts
) -> Optional[CandidateSummary]:
    """Rung 5: an open candidate already carries this alert's exact
    signature (same fault shape) but not yet this alert's component key --
    a new device hit by an already-known fault. With B1's normalization fix
    this is what actually collapses an N-device storm onto one ticket (plan
    finding 1): each subsequent device matches the first one's signature and
    amends in, deterministically, instead of reaching the LLM.

    Requires a component -- a keyless alert can only ever be a duplicate
    (rung 4, above), never an amend by this rung, since there's no key that
    could make it "a distinct affected component."
    """
    if not alert.signature or not alert.component_kind:
        return None
    for candidate in candidates:
        if alert.signature not in (candidate.signatures or []):
            continue
        already_present = any(
            same_component(entry, alert.component_kind, alert.component_key)
            for entry in candidate.affected_keys or []
        )
        if not already_present:
            return candidate
    return None


def collect_deterministic_findings(
    candidates: List[CandidateSummary], alert: AlertFacts
) -> List[DeterministicFinding]:
    """Return correlation-rule evidence without selecting a ticket action.

    The v2 LLM path receives every applicable record. The legacy deterministic
    ladder remains below for feature-off compatibility until the rollout flags
    enable the judgment path.
    """
    findings: List[DeterministicFinding] = []
    for candidate in candidates:
        signature_matches = bool(alert.signature and alert.signature in candidate.signatures)
        exact_component = bool(
            alert.component_kind
            and any(
                same_component(entry, alert.component_kind, alert.component_key)
                for entry in candidate.affected_keys
            )
        )

        if signature_matches and exact_component:
            findings.append(
                DeterministicFinding(
                    candidate_ref=candidate.ref,
                    kind="exact_signature_component",
                    facts={"signature": alert.signature, "component_key": alert.component_key},
                    explanation="The candidate records this alert signature on the same component.",
                )
            )
        elif signature_matches and not alert.component_kind:
            findings.append(
                DeterministicFinding(
                    candidate_ref=candidate.ref,
                    kind="exact_signature_keyless",
                    facts={"signature": alert.signature},
                    explanation="The candidate records this grid-level alert signature without a component key.",
                )
            )
        elif signature_matches:
            findings.append(
                DeterministicFinding(
                    candidate_ref=candidate.ref,
                    kind="signature_match_new_component",
                    facts={"signature": alert.signature, "component_key": alert.component_key},
                    explanation="The candidate records the same alert signature on a different component.",
                )
            )
            candidate_kinds = {
                str(entry.get("kind") or "").strip()
                for entry in candidate.affected_keys
                if isinstance(entry, dict)
            }
            if alert.component_kind in candidate_kinds:
                findings.append(
                    DeterministicFinding(
                        candidate_ref=candidate.ref,
                        kind="component_kind_match",
                        facts={"component_kind": alert.component_kind},
                        explanation="The candidate already affects the same kind of equipment.",
                    )
                )

        if _is_urgent_severity_increase(alert.severity, effective_candidate_severity(candidate)):
            findings.append(
                DeterministicFinding(
                    candidate_ref=candidate.ref,
                    kind="urgent_severity_increase",
                    facts={
                        "incoming_severity": alert.severity,
                        "candidate_severity": effective_candidate_severity(candidate),
                    },
                    explanation="The incoming alert is urgent while the candidate is not recorded as urgent.",
                )
            )

        if candidate.root_cause_kind:
            findings.append(
                DeterministicFinding(
                    candidate_ref=candidate.ref,
                    kind="root_cause_kind",
                    facts={"root_cause_kind": candidate.root_cause_kind},
                    explanation="The candidate has a recorded root-cause classification.",
                )
            )

    return findings


def find_deterministic_decision(
    candidates: List[CandidateSummary],
    alert: AlertFacts,
    *,
    decided_by: str = "signature",
    reason_suffix: str = "",
) -> Optional[CorrelationDecision]:
    """The three deterministic, LLM-free rungs, in order: exact
    signature+component duplicate, keyless signature-only duplicate, then
    signature-amend. Returns ``None`` when nothing matches -- the caller
    falls through to the LLM (``AlertCorrelator.decide()``) or to filing a
    plain new ticket (app.py's lock-free grid-lock-timeout fallback).

    Shared between ``AlertCorrelator.decide()`` and
    ``_attempt_lock_free_signature_correlation`` (app.py) so a grid-lock
    timeout still groups a storm instead of reverting to one-LLM-call per
    alert. ``decided_by``/``reason_suffix`` let the lock-free caller record
    that it matched without holding the lock -- see
    ``CorrelationDecision.decided_by``'s docstring for the
    "signature"/"fallback_signature" distinction.
    """
    duplicate = _find_signature_duplicate(candidates, alert) or _find_signature_only_duplicate(
        candidates, alert
    )
    if duplicate is not None:
        duplicate_severity = effective_candidate_severity(duplicate)
        severity_increased = _is_urgent_severity_increase(alert.severity, duplicate_severity)
        # alert.component_kind is truthy only for matches found via
        # _find_signature_duplicate (component-keyed); a keyless match can
        # only have come from _find_signature_only_duplicate, which requires
        # component_kind to be empty.
        keyed_match = bool(alert.component_kind)
        if keyed_match:
            reason = (
                "urgent severity increase on an exact signature+component match"
                if severity_increased
                else "exact signature+component match against an open ticket"
            )
        else:
            reason = (
                "urgent severity increase on an exact signature match "
                "(grid-level alert, no equipment key)"
                if severity_increased
                else "exact signature match against an open ticket "
                "(grid-level alert, no equipment key)"
            )
        return CorrelationDecision(
            decision="amend" if severity_increased else "duplicate",
            ticket_ref=duplicate.ref,
            ticket_id=duplicate.ticket_id,
            confidence=1.0,
            decided_by=decided_by,
            reason=f"{reason}{reason_suffix}",
            affected_key={
                "kind": alert.component_kind,
                "key": alert.component_key,
                "label": alert.component_label,
            },
            root_cause_kind=duplicate.root_cause_kind,
            update_message="",
            amended_summary=alert.subject if severity_increased else "",
            candidate_refs=[c.ref for c in candidates],
            llm_raw=None,
            needs_root_cause_ticket=False,
            ticket_severity=duplicate_severity,
        )

    amend_candidate = _find_signature_amend(candidates, alert)
    if amend_candidate is not None:
        return CorrelationDecision(
            decision="amend",
            ticket_ref=amend_candidate.ref,
            ticket_id=amend_candidate.ticket_id,
            confidence=1.0,
            decided_by=decided_by,
            reason=f"same fault signature, new affected component{reason_suffix}",
            affected_key={
                "kind": alert.component_kind,
                "key": alert.component_key,
                "label": alert.component_label,
            },
            root_cause_kind=amend_candidate.root_cause_kind,
            update_message="",
            amended_summary="",  # the renderer recomputes from state
            candidate_refs=[c.ref for c in candidates],
            llm_raw=None,
            needs_root_cause_ticket=False,
            ticket_severity=effective_candidate_severity(amend_candidate),
        )

    return None


def _is_urgent_severity_increase(incoming: str, existing: str) -> bool:
    return (
        incoming.strip().casefold() == "urgent"
        and existing.strip().casefold() != "urgent"
    )


def _age_hours(created_at: Optional[str], now: datetime) -> Optional[float]:
    if not created_at:
        return None
    try:
        text = str(created_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((now - dt).total_seconds() / 3600.0, 2)
    except Exception:
        return None


def _build_prompt(
    grid_facts: Dict[str, Any],
    rag_snippets: List[str],
    candidates: List[CandidateSummary],
    alert: AlertFacts,
) -> str:
    """Pure prompt assembly -- grid facts, RAG context, candidates, then the
    incoming alert, in that order (see the plan's "Decision pipeline" prompt
    assembly order)."""
    lines: List[str] = []

    lines.append("## Grid operational facts")
    lines.append(json.dumps(grid_facts, default=str) if grid_facts else "(none available)")

    if rag_snippets:
        lines.append("\n## Related context")
        for snippet in rag_snippets:
            lines.append(f"- {snippet}")

    lines.append("\n## Open ticket candidates on this grid")
    if candidates:
        for candidate in candidates:
            lines.append(
                json.dumps(
                    {
                        "ref": candidate.ref,
                        "summary": candidate.summary,
                        "age_hours": candidate.age_hours,
                        "root_cause_kind": candidate.root_cause_kind,
                        "affected_keys": candidate.affected_keys,
                        "occurrence_count": candidate.occurrence_count,
                        "status": candidate.status,
                    },
                    default=str,
                )
            )
    else:
        lines.append("(none)")

    lines.append("\n## Incoming alert")
    lines.append(json.dumps(alert.model_dump(), default=str))

    lines.append(
        "\nRespond with a single JSON object and nothing else:\n"
        '{"decision": "new"|"amend"|"duplicate", "ticket_ref": "<one of the candidate refs above>"|null, '
        '"relationship": "same_issue"|"same_root_cause"|null, "confidence": <0.0-1.0>, '
        '"amended_summary": "<updated ticket summary, only for amend>", '
        '"affected_key": {"kind": "...", "key": "...", "label": "..."}, '
        '"root_cause_kind": "grid_off"|"grid_isolated"|"power_chain"|"component"|"other", '
        '"update_message": "<one line for the O&M Telegram topic>", '
        '"reason": "<short justification>"}'
    )
    return "\n".join(lines)


def _build_judgment_prompt(context: AlertJudgmentContext, alert: AlertFacts) -> str:
    """Serialize every judgment input as separately labeled, untrusted JSON data."""
    sections = {
        "context_availability": context.availability_payload(),
        "deterministic_findings": [finding.model_dump(mode="json") for finding in context.deterministic_findings],
        "open_tickets": [ticket.model_dump(mode="json") for ticket in context.open_tickets],
        "live_telemetry": context.telemetry.model_dump(mode="json"),
        "prior_delivered_alerts": [alert.model_dump(mode="json") for alert in context.prior_alerts],
        "om_topic_messages": [message.model_dump(mode="json") for message in context.om_messages],
        "incoming_alert": alert.model_dump(mode="json"),
    }
    return "\n\n".join(
        f"## {name}\n{json.dumps(value, default=str)}" for name, value in sections.items()
    )


def _with_guardrail_note(reason: str, note: str) -> str:
    """Keep the model's own reasoning and record what overrode it.

    ``_apply_guardrails`` writes ``reason = reason or <note>``, which on this
    path would never fire -- the v2 schema requires a non-empty ``reason`` --
    so a rung that silently changed the decision would leave nothing in
    ``ticket_correlation_events`` explaining why the recorded action differs
    from the one the model asked for.
    """
    return f"{reason.rstrip()} [guardrail: {note}]" if reason.strip() else f"guardrail: {note}"


def _alert_affected_key(alert: Optional[AlertFacts]) -> Optional[Dict[str, str]]:
    """The alert's own component, in the shape ``apply_amendment`` merges.

    ``None`` when there is no identifiable component (a grid-level alert, or
    no alert facts passed at all): a dict of empty strings is truthy, and
    merging one writes a nameless row into the ticket's affected-equipment
    list -- the guard ``apply_amendment`` already carries for exactly this.
    """
    if alert is None or not (alert.component_kind and alert.component_key):
        return None
    return {
        "kind": alert.component_kind,
        "key": alert.component_key,
        "label": alert.component_label,
    }


def to_legacy_correlation_decision(
    result: AlertJudgmentResult,
    candidates: List[CandidateSummary],
    *,
    alert: Optional[AlertFacts] = None,
    min_confidence: Optional[float] = None,
) -> CorrelationDecision:
    """Adapt a validated LLM judgment for legacy ticket execution.

    Runs the same four safety rungs ``_apply_guardrails`` applies on the
    deterministic path. They were originally written there and this adapter
    shipped without them, which quietly made ``ALERT_CASCADE_MERGE_ENABLED``
    inert -- the flag is read in ``_apply_guardrails`` and nowhere else, and
    ``ALERT_LLM_JUDGMENT_ENABLED`` defaults on, so the operator's kill switch
    for cross-equipment merges stopped being consulted for live traffic. The
    observable symptom was a grid-outage alert landing on an unrelated open
    ticket for the same grid, which is the failure the rungs exist to stop:
    the unit of a ticket is one causally related failure, not one grid.

    ``alert``/``min_confidence`` are optional so an existing caller keeps
    working, but a caller that omits them gets the pre-guardrail behaviour for
    the two rungs that need alert facts -- pass both.
    """
    candidate_refs = [candidate.ref for candidate in candidates]
    if not result.valid or result.judgment is None:
        return _fallback_decision(
            result.error_detail or "invalid alert judgment", candidate_refs, result.raw
        )
    ticket = result.judgment.ticket
    if ticket.action is TicketAction.CREATE_NEW:
        decision = "new"
        target = None
    elif ticket.action is TicketAction.UPDATE_EXISTING:
        decision = "amend"
        target = next((candidate for candidate in candidates if candidate.ref == ticket.target_ticket_ref), None)
    else:
        decision = "duplicate"
        target = next((candidate for candidate in candidates if candidate.ref == ticket.target_ticket_ref), None)
    if decision != "new" and target is None:
        return _fallback_decision("judgment target no longer offered", candidate_refs, result.raw)

    floor = (
        min_confidence
        if min_confidence is not None
        else correlation_rules.DEFAULT_CORRELATION_POLICY.confidence_floor
    )
    # The judgment schema has no affected-key field -- the model names a ticket,
    # not a component -- so the component comes from the alert's own facts, the
    # same source ``find_deterministic_decision`` uses. This was hardcoded to
    # None on every return path below, and it is the only input
    # ``apply_amendment`` merges into a ticket's ``affected_keys``: on the live
    # judgment path a ticket's affected-equipment list was therefore seeded once
    # and never grew. Everything downstream that reads that list was reading a
    # frozen value -- the aggregate summary ("N MPPTs in <grid> affected"),
    # ``component_added`` (the only thing that makes an amend speak), and the
    # affected-component count the notification reports.
    affected_key = _alert_affected_key(alert)
    root_cause_kind = ticket.root_cause_kind.value
    reason = ticket.reason
    needs_root_cause_ticket = False

    if target is not None:
        alert_signature = (alert.signature if alert else "") or ""
        alert_severity = (alert.severity if alert else "") or ""

        # Rung 1: an urgent alert arriving on a not-yet-urgent ticket is a
        # material change however the model labelled it -- never a silent
        # re-fire.
        if decision == "duplicate" and _is_urgent_severity_increase(
            alert_severity, effective_candidate_severity(target)
        ):
            decision = "amend"
            reason = _with_guardrail_note(
                reason, "urgent severity increase makes this alert a material amendment"
            )
        elif decision == "duplicate":
            # Rung 2: "this is the same alert again" is only credible when the
            # ticket already carries this fault shape, or the model is
            # confidently claiming the identical issue. Downgrade, never drop.
            signature_overlap = bool(alert_signature) and alert_signature in (
                target.signatures or []
            )
            same_issue_confident = (
                ticket.relationship is TicketRelationship.SAME_ISSUE
                and ticket.confidence >= 0.85
            )
            if not (signature_overlap or same_issue_confident):
                decision = "amend"
                reason = _with_guardrail_note(
                    reason,
                    "duplicate downgraded to amend: no signature overlap and not a "
                    "confident same_issue relationship",
                )

        if decision == "amend":
            # Rung 3: a different kind of equipment is a different failure
            # unless a named power chain says otherwise -- and that allowance
            # stays behind the operator's own kill switch.
            target_kinds = {
                str(entry.get("kind") or "").strip()
                for entry in target.affected_keys or []
                if isinstance(entry, dict) and entry.get("kind")
            }
            incoming_kind = ((alert.component_kind if alert else "") or "").strip()
            is_cross_kind = bool(
                target_kinds and incoming_kind and incoming_kind not in target_kinds
            )
            if is_cross_kind:
                cascade_allowed = (
                    root_cause_kind == "power_chain"
                    and fr.get("ALERT_CASCADE_MERGE_ENABLED")
                    and ticket.confidence >= floor
                )
                if not cascade_allowed:
                    return CorrelationDecision(
                        decision="new",
                        ticket_ref=None,
                        ticket_id=None,
                        confidence=ticket.confidence,
                        decided_by="llm_judgment",
                        reason=_with_guardrail_note(
                            reason,
                            f"cross-kind amend ({incoming_kind!r} onto a ticket carrying "
                            f"{sorted(target_kinds)!r}) rejected: not a permitted "
                            "power-chain cascade",
                        ),
                        affected_key=affected_key,
                        root_cause_kind=root_cause_kind,
                        update_message="",
                        amended_summary="",
                        candidate_refs=candidate_refs,
                        llm_raw=result.raw,
                        needs_root_cause_ticket=False,
                    )

        # Rung 4: a grid-wide root cause groups its symptoms under a ticket
        # that represents the root cause. When no candidate is one, the
        # symptom does not get glued onto whichever ticket happened to be
        # open -- clearing the ref sends it back to file its own, and the
        # deterministic path's parent-first branch does the same from here.
        if decision == "amend" and root_cause_kind in _ROOT_CAUSE_KINDS_REQUIRING_PARENT:
            if not any(
                candidate.root_cause_kind == root_cause_kind for candidate in candidates
            ):
                needs_root_cause_ticket = True
                reason = _with_guardrail_note(
                    reason,
                    f"{root_cause_kind} symptom with no open root-cause ticket: "
                    "filing its own rather than amending an unrelated candidate",
                )
                target = None

    return CorrelationDecision(
        decision=decision,
        ticket_ref=target.ref if target else None,
        ticket_id=target.ticket_id if target else None,
        confidence=ticket.confidence,
        decided_by="llm_judgment",
        reason=reason,
        affected_key=affected_key,
        root_cause_kind=root_cause_kind,
        update_message="",
        amended_summary=ticket.proposed_title if ticket.change_title and ticket.proposed_title else "",
        candidate_refs=candidate_refs,
        llm_raw=result.raw,
        needs_root_cause_ticket=needs_root_cause_ticket,
        ticket_severity=effective_candidate_severity(target) if target else "",
        description_addition=(
            ticket.description_addition
            if ticket.action is TicketAction.UPDATE_EXISTING and ticket.change_description
            and ticket.description_addition
            else ""
        ),
        title_change_requested=(
            ticket.action is TicketAction.UPDATE_EXISTING and ticket.change_title
        ),
    )


class AlertCorrelator:
    """Decides new/amend/duplicate for one incoming alert on one grid.

    All dependencies are injectable for testing; production callers (the
    ``/notify`` handler, Task 9) construct this with real
    ``CorrelationStore``/``TicketService``/generation gateway instances.
    """

    def __init__(
        self,
        store: Any,
        ticket_service: Any,
        gateway: Any = None,
        model: Optional[str] = None,
        policy: correlation_rules.CorrelationPolicy = correlation_rules.DEFAULT_CORRELATION_POLICY,
        min_confidence: Optional[float] = None,
        timeout_seconds: Optional[float] = None,
        lookback_hours: Optional[int] = None,
        max_candidates: Optional[int] = None,
        candidate_status_concurrency: Optional[int] = None,
        get_correlation_instructions: Optional[Callable[[], Dict[str, str]]] = None,
        get_rag_context: Optional[Callable[..., Awaitable[List[str]]]] = None,
        get_grid_operational_context: Optional[Callable[[str], Awaitable[Dict[str, Any]]]] = None,
    ) -> None:
        self._store = store
        self._ticket_service = ticket_service
        self._model = model if model is not None else get_settings().gemini.model

        if gateway is not None:
            self._gateway = gateway
        else:
            from shared.llm import get_default_generation_gateway

            self._gateway = get_default_generation_gateway(default_model=self._model)

        self._min_confidence = (
            min_confidence if min_confidence is not None else policy.confidence_floor
        )
        self._timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else policy.llm_timeout_seconds
        )
        self._lookback_hours = (
            lookback_hours
            if lookback_hours is not None
            else policy.open_candidate_window_hours
        )
        self._max_candidates = (
            max_candidates
            if max_candidates is not None
            else policy.maximum_candidate_count
        )
        self._candidate_status_concurrency = (
            candidate_status_concurrency
            if candidate_status_concurrency is not None
            else policy.candidate_status_concurrency
        )
        self._get_correlation_instructions = (
            get_correlation_instructions or correlation_rules.get_correlation_instructions
        )
        self._get_rag_context = get_rag_context or correlation_rules.get_rag_context
        self._get_grid_operational_context = (
            get_grid_operational_context or correlation_rules.get_grid_operational_context
        )

    async def replay_decision(self, dedup_key: Optional[str]) -> Optional[CorrelationDecision]:
        """Rung 0: the prior decision for a ``dedup_key`` already decided.

        Returns ``None`` when there is nothing to replay (no key, no prior
        row, or a store read that failed) so every caller falls through to
        deciding normally -- an unreadable ledger must not be the reason an
        alert goes undecided.

        Extracted from ``decide()`` so the LLM-judgment path can share it.
        That path used to skip rung 0 entirely: a webhook retry re-ran the
        model and, whenever the second judgment landed on "new", filed a
        second ticket for an alert already decided and already posted.
        """
        if not dedup_key:
            return None
        try:
            prior = await self._store.get_by_dedup_key(dedup_key)
        except Exception:
            LOGGER.opt(exception=True).warning(
                "Dedup-key replay lookup failed for {!r}; deciding afresh", dedup_key
            )
            return None
        if not prior:
            return None

        ticket_id = prior.get("ticket_id")
        ticket_ref: Optional[str] = None
        ticket_severity = ""
        if ticket_id:
            ticket_ref = await self._ticket_service.get_ref_by_id(ticket_id)
            try:
                correlation = await self._store.get_correlation(ticket_id)
            except Exception:
                LOGGER.opt(exception=True).warning(
                    "Failed to load durable severity for replayed ticket {!r}",
                    ticket_id,
                )
            else:
                if correlation is not None:
                    ticket_severity = correlation.get("severity") or ticket_severity
        return CorrelationDecision(
            decision=prior.get("decision", "new"),
            ticket_ref=ticket_ref,
            ticket_id=ticket_id,
            confidence=prior.get("confidence"),
            decided_by="replay",
            reason=prior.get("reason") or "replayed prior decision (dedup_key match)",
            affected_key=None,
            root_cause_kind=None,
            update_message="",
            amended_summary="",
            candidate_refs=[],
            llm_raw=None,
            needs_root_cause_ticket=False,
            ticket_severity=ticket_severity,
        )

    async def decide(
        self,
        grid_name: str,
        alert: AlertFacts,
        dedup_key: Optional[str] = None,
        backend_override: Optional[str] = None,
        get_live_facts: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
    ) -> CorrelationDecision:
        replayed = await self.replay_decision(dedup_key)
        if replayed is not None:
            return replayed

        if not fr.get("ALERT_CORRELATION_ENABLED"):
            return await self._finalize(
                grid_name,
                alert,
                dedup_key,
                _fallback_decision("alert correlation disabled", [], decided_by="flag_off"),
            )

        try:
            candidates = await self._assemble_candidates(grid_name, backend_override=backend_override)
        except Exception:
            LOGGER.opt(exception=True).warning("Candidate assembly failed for grid {!r}", grid_name)
            candidates = []

        if not candidates:
            return await self._finalize(
                grid_name,
                alert,
                dedup_key,
                _fallback_decision("no open candidates for grid", [], decided_by="no_candidates"),
            )

        deterministic_decision = find_deterministic_decision(candidates, alert)
        if deterministic_decision is not None:
            return await self._finalize(grid_name, alert, dedup_key, deterministic_decision)

        candidate_refs = [c.ref for c in candidates]
        try:
            raw = await asyncio.wait_for(
                self._call_llm(grid_name, alert, candidates, get_live_facts=get_live_facts),
                timeout=self._timeout_seconds,
            )
        except Exception as e:
            return await self._finalize(
                grid_name,
                alert,
                dedup_key,
                _fallback_decision(f"LLM call failed: {e}", candidate_refs),
            )

        judgment = parse_alert_judgment(raw, set(candidate_refs), self._min_confidence)
        if judgment.valid:
            return await self._finalize(
                grid_name,
                alert,
                dedup_key,
                to_legacy_correlation_decision(
                    judgment, candidates, alert=alert, min_confidence=self._min_confidence
                ),
            )

        parsed = _parse_llm_response(raw)
        if parsed is None:
            return await self._finalize(
                grid_name,
                alert,
                dedup_key,
                _fallback_decision("unparseable LLM response", candidate_refs, llm_raw=raw),
            )

        decision = _apply_guardrails(
            parsed,
            candidates,
            min_confidence=self._min_confidence,
            llm_raw=raw,
            alert_signature=alert.signature,
            alert_severity=alert.severity,
        )
        return await self._finalize(grid_name, alert, dedup_key, decision)

    async def judge(
        self, grid_name: str, alert: AlertFacts, context: AlertJudgmentContext
    ) -> AlertJudgmentResult:
        """Ask the correlation model exactly once; parsing failure remains fail-open input."""
        del grid_name
        instructions = self._get_correlation_instructions()
        system_instructions = (
            instructions.get("system_instructions", "")
            if isinstance(instructions, dict)
            else str(instructions)
        )
        try:
            response = await asyncio.wait_for(
                self._gateway.generate(
                    [
                        LLMMessage(role="system", text=system_instructions),
                        LLMMessage(role="user", text=_build_judgment_prompt(context, alert)),
                    ],
                    GenerationOptions(
                        model=self._model, temperature=0.0, response_format="json"
                    ),
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            return AlertJudgmentResult(
                valid=False, error_code="timed_out", error_detail="LLM judgment timed out"
            )
        except Exception as exc:
            return AlertJudgmentResult(
                valid=False, error_code="llm_failed", error_detail=type(exc).__name__
            )
        return parse_alert_judgment(
            getattr(response, "text", None),
            {ticket.ref for ticket in context.open_tickets},
            self._min_confidence,
        )

    async def _finalize(
        self,
        grid_name: str,
        alert: AlertFacts,
        dedup_key: Optional[str],
        decision: CorrelationDecision,
    ) -> CorrelationDecision:
        try:
            await self._store.record_event(
                ticket_id=decision.ticket_id,
                grid_name=grid_name,
                source=alert.rule_id or None,
                signature=alert.signature or None,
                dedup_key=dedup_key,
                decision=decision.decision,
                decided_by=decision.decided_by,
                confidence=decision.confidence,
                reason=decision.reason,
                candidate_refs=decision.candidate_refs,
                alert=alert.model_dump(),
                llm_raw=decision.llm_raw,
            )
        except Exception:
            LOGGER.opt(exception=True).warning(
                "Failed to record correlation event for grid {!r}", grid_name
            )
        return decision

    async def _assemble_candidates(
        self, grid_name: str, backend_override: Optional[str] = None
    ) -> List[CandidateSummary]:
        since_iso = (
            datetime.now(timezone.utc) - timedelta(hours=self._lookback_hours)
        ).isoformat()
        store_rows = await self._store.open_candidates_for_grid(
            grid_name, since_iso, limit=self._max_candidates
        )
        backend_summaries = await self._ticket_service.find_open_by_grid(
            grid_name, limit=self._max_candidates, backend_override=backend_override
        )

        now = datetime.now(timezone.utc)
        by_ref: Dict[str, CandidateSummary] = {}
        store_refs = set()
        for row in store_rows:
            ref = row.get("ticket_ref")
            ticket_id = row.get("ticket_id")
            if not ref:
                continue
            if not ticket_id:
                # Cannot happen given a healthy store (ticket_id is
                # ticket_correlations' NOT NULL primary key post-0005b) --
                # guarded anyway since a candidate with no id can't be
                # amended safely.
                LOGGER.warning(
                    "Dropping candidate {!r}: correlation row missing ticket_id", ref
                )
                continue
            store_refs.add(ref)
            by_ref[ref] = CandidateSummary(
                ref=ref,
                ticket_id=ticket_id,
                backend=row.get("ticket_backend") or "",
                summary=row.get("summary_current") or row.get("summary_base") or "",
                description=row.get("description") or "",
                age_hours=_age_hours(row.get("created_at"), now),
                root_cause_kind=row.get("root_cause_kind"),
                affected_keys=row.get("affected_keys") or [],
                occurrence_count=row.get("occurrence_count") or 1,
                status=row.get("status") or "",
                signatures=row.get("signatures") or [],
                severity=row.get("severity") or "",
            )

        for summary in backend_summaries:
            if summary.ref in by_ref:
                continue
            summary_age_hours = _age_hours(getattr(summary, "created_at", None), now)
            if summary_age_hours is not None and summary_age_hours > self._lookback_hours:
                # Store-side candidates are already bounded by
                # `open_candidate_window_hours`; this source was not, so one
                # never-closed ticket stayed an eligible amend/duplicate
                # target forever and silently absorbed every later alert on
                # the grid. Age it out here (before adopting it, which is
                # otherwise pointless I/O) so the window bounds both sources.
                # Undated candidates are kept: only refuse when we positively
                # know, or a missing `created_at` files a duplicate ticket.
                LOGGER.info(
                    "Dropping backend candidate {!r}: {:.0f}h old, outside the {}h "
                    "correlation window",
                    summary.ref,
                    summary_age_hours,
                    self._lookback_hours,
                )
                continue
            try:
                adopted = await self._ticket_service.adopt_external(
                    ref=summary.ref,
                    backend=summary.backend,
                    summary=summary.summary,
                    grid_name=grid_name,
                )
            except Exception:
                LOGGER.opt(exception=True).warning(
                    "Dropping externally-discovered candidate {!r}: could not adopt "
                    "into the canonical ticket table (cannot be amended without a "
                    "ticket_id)",
                    summary.ref,
                )
                continue
            by_ref[summary.ref] = CandidateSummary(
                ref=summary.ref,
                ticket_id=adopted.id,
                backend=summary.backend,
                summary=summary.summary,
                description=summary.description,
                age_hours=summary_age_hours,
                status=summary.status,
            )

        semaphore = asyncio.Semaphore(self._candidate_status_concurrency)
        ordered = list(by_ref.values())
        statuses = await asyncio.gather(
            *(self._confirm_candidate_status(c, semaphore) for c in ordered)
        )

        confirmed: List[CandidateSummary] = []
        for candidate, status in zip(ordered, statuses):
            if status is not None and status.is_done:
                # No separate "mark closed" step needed here -- ticket
                # status lives solely on `tickets` (TicketRepository's
                # table) post-0005b, and open_candidates_for_grid already
                # reads status from there on the next call.
                continue
            if status is None and candidate.ref not in store_refs:
                continue
            if status is None:
                LOGGER.warning("Preserving cached candidate {!r}: status unavailable", candidate.ref)
            confirmed.append(candidate)

        confirmed.sort(key=lambda c: c.age_hours if c.age_hours is not None else 0.0)
        return confirmed[: self._max_candidates]

    async def _confirm_candidate_status(
        self, candidate: CandidateSummary, semaphore: asyncio.Semaphore
    ) -> Optional[TicketStatus]:
        async with semaphore:
            try:
                return await self._ticket_service.get_status(candidate.ref)
            except Exception:
                LOGGER.opt(exception=True).warning(
                    "Candidate status lookup raised for {!r}", candidate.ref
                )
                return None

    async def _call_llm(
        self,
        grid_name: str,
        alert: AlertFacts,
        candidates: List[CandidateSummary],
        get_live_facts: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
    ) -> Optional[str]:
        instructions = self._get_correlation_instructions()
        system_instructions = (
            instructions.get("system_instructions", "")
            if isinstance(instructions, dict)
            else str(instructions)
        )
        grid_facts = await self._get_grid_operational_context(grid_name)
        if get_live_facts is not None:
            try:
                live_facts = await get_live_facts()
            except Exception:
                LOGGER.opt(exception=True).warning(
                    "Live telemetry context failed for grid {!r}", grid_name
                )
                live_facts = {"live_inverter_output": "unavailable"}
            grid_facts = {**grid_facts, "live_telemetry": live_facts}
        rag_query = f"{alert.subject}\n{alert.details}".strip()
        rag_snippets = await self._get_rag_context(rag_query)

        prompt = _build_prompt(grid_facts, rag_snippets, candidates, alert)
        result = await self._gateway.generate(
            [
                LLMMessage(role="system", text=system_instructions),
                LLMMessage(role="user", text=prompt),
            ],
            GenerationOptions(model=self._model, temperature=0.0, response_format="json"),
        )
        return result.text
