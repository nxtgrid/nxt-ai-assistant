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
5. Otherwise, ask the LLM, then run its response through
   ``_apply_guardrails`` -- which can force "new" (unknown ref, low
   confidence, unparseable response), downgrade "duplicate" to "amend" (no
   signature overlap and not a confident "same_issue"), or flag
   ``needs_root_cause_ticket`` when the model says this is a symptom of a
   grid-level root cause that no open candidate itself represents.

Every failure mode here -- LLM timeout, transport error, malformed JSON, a
correlation-store outage -- degrades to "new" (``decided_by="fallback"``),
never a raised exception: every alert must result in a ticket.
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
from .alert_facts import AlertFacts, same_component

if TYPE_CHECKING:
    from .backend import TicketStatus

LOGGER = get_logger(__name__)

_ROOT_CAUSE_KINDS_REQUIRING_PARENT = ("grid_off", "grid_isolated")


class CandidateSummary(BaseModel):
    """A candidate ticket offered to the correlator, merged from
    ``CorrelationStore`` (backend-agnostic, has signatures/affected_keys)
    and ``TicketService.find_open_by_grid`` (catches human-filed or
    pre-cutover tickets the correlation layer never recorded)."""

    ref: str
    backend: str = ""
    summary: str = ""
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
    # "fallback_signature" is also produced -- outside this module, by
    # app.py's lock-free grid-lock-timeout fallback, which mirrors this
    # class's "signature" rung without holding the per-grid lock. See
    # ``_attempt_lock_free_signature_correlation`` in app.py.
    reason: str
    affected_key: Optional[Dict[str, str]]
    root_cause_kind: Optional[str]
    update_message: str
    amended_summary: str
    candidate_refs: List[str]
    llm_raw: Optional[str]
    needs_root_cause_ticket: bool = False
    ticket_severity: str = ""


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
        alert_severity, by_ref[ticket_ref].severity
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

    needs_root_cause_ticket = False
    final_ticket_ref: Optional[str] = ticket_ref
    if decision == "amend" and root_cause_kind in _ROOT_CAUSE_KINDS_REQUIRING_PARENT:
        any_root_cause_candidate = any(c.root_cause_kind == root_cause_kind for c in candidates)
        if not any_root_cause_candidate:
            needs_root_cause_ticket = True
            final_ticket_ref = None

    return CorrelationDecision(
        decision=decision,
        ticket_ref=final_ticket_ref,
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
        ticket_severity=by_ref[ticket_ref].severity,
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
        '"root_cause_kind": "grid_off"|"grid_isolated"|"component"|"other", '
        '"update_message": "<one line for the O&M Telegram topic>", '
        '"reason": "<short justification>"}'
    )
    return "\n".join(lines)


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

    async def decide(
        self,
        grid_name: str,
        alert: AlertFacts,
        dedup_key: Optional[str] = None,
        backend_override: Optional[str] = None,
        get_live_facts: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
    ) -> CorrelationDecision:
        if dedup_key:
            prior = await self._store.get_by_dedup_key(dedup_key)
            if prior:
                ticket_ref = prior.get("ticket_ref")
                ticket_severity = prior.get("ticket_severity") or ""
                if ticket_ref:
                    try:
                        correlation = await self._store.get_correlation(ticket_ref)
                    except Exception:
                        LOGGER.warning(
                            "Failed to load durable severity for replayed ticket %r",
                            ticket_ref,
                            exc_info=True,
                        )
                    else:
                        if correlation is not None:
                            ticket_severity = correlation.get("severity") or ticket_severity
                return CorrelationDecision(
                    decision=prior.get("decision", "new"),
                    ticket_ref=ticket_ref,
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
            LOGGER.warning("Candidate assembly failed for grid %r", grid_name, exc_info=True)
            candidates = []

        if not candidates:
            return await self._finalize(
                grid_name,
                alert,
                dedup_key,
                _fallback_decision("no open candidates for grid", [], decided_by="no_candidates"),
            )

        duplicate = _find_signature_duplicate(candidates, alert) or (
            _find_signature_only_duplicate(candidates, alert)
        )
        if duplicate is not None:
            severity_increased = _is_urgent_severity_increase(
                alert.severity, duplicate.severity
            )
            # alert.component_kind is truthy only for matches found via
            # _find_signature_duplicate (component-keyed); a keyless match can
            # only have come from _find_signature_only_duplicate, which
            # requires component_kind to be empty.
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
            decision = CorrelationDecision(
                decision="amend" if severity_increased else "duplicate",
                ticket_ref=duplicate.ref,
                confidence=1.0,
                decided_by="signature",
                reason=reason,
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
                ticket_severity=duplicate.severity,
            )
            return await self._finalize(grid_name, alert, dedup_key, decision)

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

    async def _finalize(
        self,
        grid_name: str,
        alert: AlertFacts,
        dedup_key: Optional[str],
        decision: CorrelationDecision,
    ) -> CorrelationDecision:
        try:
            await self._store.record_event(
                ticket_ref=decision.ticket_ref,
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
            LOGGER.warning(
                "Failed to record correlation event for grid %r", grid_name, exc_info=True
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
            if not ref:
                continue
            store_refs.add(ref)
            by_ref[ref] = CandidateSummary(
                ref=ref,
                backend=row.get("ticket_backend") or "",
                summary=row.get("summary_current") or row.get("summary_base") or "",
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
            by_ref[summary.ref] = CandidateSummary(
                ref=summary.ref,
                backend=summary.backend,
                summary=summary.summary,
                age_hours=_age_hours(getattr(summary, "created_at", None), now),
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
                if candidate.ref in store_refs:
                    await self._store.mark_closed(candidate.ref)
                continue
            if status is None and candidate.ref not in store_refs:
                continue
            if status is None:
                LOGGER.warning("Preserving cached candidate %r: status unavailable", candidate.ref)
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
                LOGGER.warning(
                    "Candidate status lookup raised for %r", candidate.ref, exc_info=True
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
                LOGGER.warning("Live telemetry context failed for grid %r", grid_name, exc_info=True)
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
