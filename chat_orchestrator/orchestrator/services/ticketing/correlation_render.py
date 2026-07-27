"""Rendering + amend execution for /notify alert correlation.

``render_summary``/``render_description`` are pure functions: given a
``ticket_correlations`` row (a dict, as returned by ``CorrelationStore``),
they recompute the ticket's summary/description **from scratch** every time
-- never by parsing or appending to the ticket's current (live) text. That's
what makes an amend idempotent (render twice from the same state -> the same
output, byte for byte) and what keeps the correlation layer independent of
the ticket backend: nothing here ever reads Jira ADF back.

``apply_amendment`` is the orchestration that runs after ``AlertCorrelator``
decides "amend" or "duplicate": merge the new affected key (amend only),
bump the occurrence counter (both), re-render and push to the ticket backend
(amend only), and auto-escalate once the affected-component count crosses
``ALERT_CORRELATION_ESCALATE_AFTER``. Telegram delivery itself is Task 10's
concern -- this only returns the correlation's stored Telegram targets so
the caller (the ``/notify`` handler) can act on them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.config import flag_registry as fr
from shared.utils.logging import get_logger

from .alert_facts import AlertFacts
from .correlator import CorrelationDecision

LOGGER = get_logger(__name__)

MARKER_START = "[anansi:affected-start]"
MARKER_END = "[anansi:affected-end]"

_KIND_LABELS = {
    "mppt": "MPPT",
    "dcu": "DCU",
    "base_station": "Base Station",
    "inverter": "Inverter",
    "battery": "Battery",
    "combiner": "Combiner",
    "grid": "Grid",
}

_MAX_KEYS_SHOWN = 6


def _pluralize_kind(kind: str, count: int) -> str:
    label = _KIND_LABELS.get(kind) or (kind.replace("_", " ").title() if kind else "component")
    if count == 1:
        return label
    if label == "Battery":
        return "Batteries"
    return f"{label}s"


def _severity_marker(summary_base: str) -> str:
    """"! Urgent: " / "! Warning: " prefix carried over from the ticket's
    original filed summary, or "" if neither marker is present."""
    text = summary_base or ""
    if re.match(r"^\s*!\s*urgent\s*:", text, re.IGNORECASE):
        return "! Urgent: "
    if re.match(r"^\s*!\s*warning\s*:", text, re.IGNORECASE):
        return "! Warning: "
    return ""


def render_summary(correlation: Dict[str, Any], alert: AlertFacts, llm_summary: str) -> str:
    """Recompute a ticket's summary from its current affected-keys state.

    A single affected key keeps the LLM's ``amended_summary`` (or, if blank,
    the ticket's own ``summary_base``) -- there's nothing to aggregate. Two
    or more keys of the dominant component kind switch to an aggregate
    template naming the count and the (possibly truncated) key list, so the
    summary reads as "N components affected" rather than describing only the
    single component that happened to trigger the last amend.
    """
    affected_keys = correlation.get("affected_keys") or []
    total_keys = len(affected_keys)

    if total_keys < 2:
        return (llm_summary or correlation.get("summary_base") or "").strip()

    kind_groups: Dict[str, List[str]] = {}
    for entry in affected_keys:
        kind_groups.setdefault(entry.get("kind", ""), []).append(entry.get("key", ""))

    dominant_kind = alert.component_kind or max(kind_groups, key=lambda k: len(kind_groups[k]))
    keys = sorted({k for k in kind_groups.get(dominant_kind, []) if k})
    if not keys:
        return (llm_summary or correlation.get("summary_base") or "").strip()

    severity = _severity_marker(correlation.get("summary_base") or "")
    grid_name = correlation.get("grid_name", "")
    label = _pluralize_kind(dominant_kind, len(keys))

    shown = keys[:_MAX_KEYS_SHOWN]
    key_list = ", ".join(shown)
    if len(keys) > _MAX_KEYS_SHOWN:
        key_list += f", +{len(keys) - _MAX_KEYS_SHOWN} more"

    return f"{severity}{len(keys)} {label} in {grid_name} affected ({key_list}) !"


def render_description(correlation: Dict[str, Any]) -> str:
    """Recompute a ticket's description: ``description_base`` (the
    description as first filed, never overwritten) plus a freshly-rendered
    "Affected components" block. Idempotent by construction -- this always
    recomputes the whole block from ``affected_keys``/``occurrence_count``/
    ``root_cause_kind`` rather than editing whatever text currently exists
    on the ticket, so calling it twice on the same correlation state
    produces byte-identical output (never two marker blocks).
    """
    base = (correlation.get("description_base") or "").rstrip()
    affected_keys = correlation.get("affected_keys") or []
    occurrence_count = correlation.get("occurrence_count") or 1
    root_cause_kind = correlation.get("root_cause_kind")

    lines: List[str] = [MARKER_START, f"Affected components ({len(affected_keys)}):"]
    for entry in sorted(affected_keys, key=lambda e: (e.get("kind", ""), e.get("key", ""))):
        label = entry.get("label") or f"{entry.get('kind', '')} {entry.get('key', '')}".strip()
        first_seen = entry.get("first_seen", "")
        last_seen = entry.get("last_seen", "")
        count = entry.get("count", 1)
        lines.append(f"- {label} — first seen {first_seen}, last {last_seen} ({count}x)")

    summary_line = f"Occurrences: {occurrence_count} · Grouped by Anansi alert correlation"
    if root_cause_kind:
        summary_line += f" · Root cause: {root_cause_kind}"
    lines.append(summary_line)
    lines.append(MARKER_END)

    managed_block = "\n".join(lines)
    return f"{base}\n\n{managed_block}" if base else managed_block


@dataclass(frozen=True)
class AmendmentResult:
    """What happened when an "amend"/"duplicate" decision was executed --
    enough for the caller (the /notify handler) to decide what, if anything,
    to post to Telegram (Task 10)."""

    ticket_ref: str
    decision: str  # "amend" | "duplicate"
    escalated: bool
    affected_keys_count: int
    occurrence_count: int
    telegram_chat_id: Optional[str]
    telegram_topic_id: Optional[str]
    telegram_message_id: Optional[int]


async def apply_amendment(
    *,
    store: Any,
    ticket_service: Any,
    ticket_ref: str,
    alert: AlertFacts,
    decision: CorrelationDecision,
    raw_text: str,
    escalate_after: Optional[int] = None,
    escalated_priority_id: Optional[str] = None,
) -> Optional[AmendmentResult]:
    """Execute an "amend" or "duplicate" correlation decision against ``ticket_ref``.

    "duplicate" only bumps the occurrence counter -- no ticket mutation, no
    comment -- that's the whole point of treating it as silent noise
    suppression rather than a ticket update. "amend" merges the new affected
    key, re-renders summary/description from the post-merge state, pushes
    that to the ticket backend, appends the raw alert as a comment, and
    auto-escalates (priority bump + a "🔴 " summary prefix) the first time
    the affected-component count reaches ``escalate_after``.

    Returns ``None`` if the correlation row can't be loaded (e.g. a store
    outage between ``AlertCorrelator.decide()`` and here) -- the caller must
    treat that the same as any other correlation failure (log and move on;
    the ticket itself was already filed/exists, so nothing is lost, only the
    amend's side effects are skipped for this alert).
    """
    escalate_after = (
        escalate_after if escalate_after is not None else int(fr.get("ALERT_CORRELATION_ESCALATE_AFTER"))
    )
    escalated_priority_id = (
        escalated_priority_id
        if escalated_priority_id is not None
        else (fr.get("JIRA_ALERT_ESCALATED_PRIORITY_ID") or None)
    )

    await store.bump_occurrence(ticket_ref)

    if decision.decision == "amend" and decision.affected_key:
        kind = decision.affected_key.get("kind", "")
        key = decision.affected_key.get("key", "")
        label = decision.affected_key.get("label") or f"{kind} {key}".strip()
        await store.merge_affected_key(
            ticket_ref, kind=kind, key=key, label=label, signature=alert.signature or None
        )

    correlation = await store.get_correlation(ticket_ref)
    if correlation is None:
        LOGGER.warning(
            "apply_amendment: correlation row for %r not found after merge -- "
            "skipping render/ticket-update side effects",
            ticket_ref,
        )
        return None

    if decision.decision == "duplicate":
        return AmendmentResult(
            ticket_ref=ticket_ref,
            decision="duplicate",
            escalated=False,
            affected_keys_count=len(correlation.get("affected_keys") or []),
            occurrence_count=int(correlation.get("occurrence_count") or 1),
            telegram_chat_id=correlation.get("telegram_chat_id"),
            telegram_topic_id=correlation.get("telegram_topic_id"),
            telegram_message_id=correlation.get("telegram_message_id"),
        )

    new_summary = render_summary(correlation, alert, decision.amended_summary)
    new_description = render_description(correlation)

    affected_count = len(correlation.get("affected_keys") or [])
    escalate_now = affected_count >= escalate_after and not correlation.get("escalated_at")
    final_summary = f"🔴 {new_summary}" if escalate_now else new_summary

    await ticket_service.update_ticket(
        ticket_ref,
        summary=final_summary,
        description=new_description,
        priority_id=escalated_priority_id if escalate_now else None,
    )
    if raw_text:
        await ticket_service.add_comment(ticket_ref, raw_text, public=False)

    await store.record_amendment(ticket_ref, summary_current=final_summary, escalated=escalate_now)

    return AmendmentResult(
        ticket_ref=ticket_ref,
        decision="amend",
        escalated=escalate_now,
        affected_keys_count=affected_count,
        occurrence_count=int(correlation.get("occurrence_count") or 1),
        telegram_chat_id=correlation.get("telegram_chat_id"),
        telegram_topic_id=correlation.get("telegram_topic_id"),
        telegram_message_id=correlation.get("telegram_message_id"),
    )
