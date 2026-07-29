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
(amend only), and escalate the first urgent severity increase. Telegram
delivery itself is Task 10's concern -- this only returns the correlation's
stored Telegram targets so the caller (the ``/notify`` handler) can act on
them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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
_SEVERITY_PREFIX = re.compile(
    r"^\s*!\s*(?:urgent|warning)\s*:\s*", re.IGNORECASE
)


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


def _apply_incoming_severity(summary: str, severity: str) -> str:
    if severity.strip().casefold() != "urgent":
        return summary
    if _SEVERITY_PREFIX.match(summary):
        return _SEVERITY_PREFIX.sub("! Urgent: ", summary, count=1)
    return f"! Urgent: {summary}".rstrip()


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
        summary = (llm_summary or correlation.get("summary_base") or "").strip()
        return _apply_incoming_severity(summary, alert.severity)

    kind_groups: Dict[str, List[str]] = {}
    for entry in affected_keys:
        kind_groups.setdefault(entry.get("kind", ""), []).append(entry.get("key", ""))

    dominant_kind = alert.component_kind or max(kind_groups, key=lambda k: len(kind_groups[k]))
    keys = sorted({k for k in kind_groups.get(dominant_kind, []) if k})
    if not keys:
        return (llm_summary or correlation.get("summary_base") or "").strip()

    severity = (
        "! Urgent: "
        if alert.severity.strip().casefold() == "urgent"
        else _severity_marker(correlation.get("summary_base") or "")
    )
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
    component_added: bool = False


async def apply_amendment(
    *,
    store: Any,
    ticket_service: Any,
    ticket_ref: str,
    alert: AlertFacts,
    decision: CorrelationDecision,
    raw_text: str,
    grid_name: str = "",
    telegram_chat_id: Optional[str] = None,
    telegram_topic_id: Optional[str] = None,
) -> Optional[AmendmentResult]:
    """Execute an "amend" or "duplicate" correlation decision against ``ticket_ref``.

    "duplicate" only bumps the occurrence counter -- no ticket mutation, no
    comment -- that's the whole point of treating it as silent noise
    suppression rather than a ticket update. "amend" merges the new affected
    key, re-renders summary/description from the post-merge state, pushes
    that to the ticket backend, appends the raw alert as a comment, and
    escalates (Highest priority + a "🔴 " summary prefix) the first time an
    incoming urgent alert raises the stored ticket's severity.

    Returns ``None`` if a non-urgent amendment's correlation row can't be
    loaded (e.g. a store outage between ``AlertCorrelator.decide()`` and
    here). Urgent amendments still apply their summary and dynamic Highest
    priority directly to the existing backend ticket, so a Jira-discovered
    candidate from before correlation-store cutover cannot lose escalation.
    """
    if (
        decision.decided_by == "replay"
        and alert.severity.strip().casefold() == "urgent"
        and decision.ticket_severity.strip().casefold() == "urgent"
    ):
        correlation = await store.get_correlation(ticket_ref)
        if (
            correlation is not None
            and str(correlation.get("severity") or "").strip().casefold() == "urgent"
        ):
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

    await store.bump_occurrence(ticket_ref)

    component_added = False
    affected_key = decision.affected_key or {}
    kind = str(affected_key.get("kind") or "").strip()
    key = str(affected_key.get("key") or "").strip()
    # A dict of empty strings is truthy, so the old `if decision.affected_key`
    # guard would merge a nameless ("", "") entry for any component-less alert.
    if decision.decision == "amend" and kind and key:
        label = affected_key.get("label") or f"{kind} {key}".strip()
        merge = await store.merge_affected_key(
            ticket_ref, kind=kind, key=key, label=label, signature=alert.signature or None
        )
        component_added = bool(merge is not None and merge.added)

    correlation = await store.get_correlation(ticket_ref)
    if correlation is None:
        if (
            decision.decision == "amend"
            and alert.severity.strip().casefold() == "urgent"
        ):
            summary = (
                decision.amended_summary or alert.subject or decision.update_message
            ).strip()
            urgent_summary = _apply_incoming_severity(summary, alert.severity)
            final_summary = (
                urgent_summary
                if urgent_summary.startswith("🔴")
                else f"🔴 {urgent_summary}".rstrip()
            )
            await ticket_service.update_ticket(
                ticket_ref,
                summary=final_summary,
                description=None,
                priority_id="highest",
            )
            if raw_text:
                await ticket_service.add_comment(ticket_ref, raw_text, public=False)
            seeded_affected_count = 0
            if grid_name:
                affected_key = decision.affected_key or (
                    {
                        "kind": alert.component_kind,
                        "key": alert.component_key,
                        "label": alert.component_label,
                    }
                    if alert.component_kind and alert.component_key
                    else None
                )
                affected_keys = (
                    [
                        {
                            **affected_key,
                            "first_seen": alert.fired_at,
                            "last_seen": alert.fired_at,
                            "count": 1,
                        }
                    ]
                    if affected_key is not None
                    else []
                )
                seeded_affected_count = len(affected_keys)
                try:
                    seeded = await store.upsert_correlation(
                        ticket_ref=ticket_ref,
                        ticket_backend=await ticket_service.get_backend_name(ticket_ref),
                        grid_name=grid_name,
                        organization_id=None,
                        root_cause_kind=decision.root_cause_kind,
                        primary_signature=alert.signature or "",
                        signatures=[alert.signature] if alert.signature else [],
                        affected_keys=affected_keys,
                        summary_base=final_summary,
                        description_base=raw_text,
                        severity="urgent",
                        telegram_chat_id=telegram_chat_id,
                        telegram_topic_id=telegram_topic_id,
                    )
                    if seeded:
                        await store.record_amendment(
                            ticket_ref,
                            summary_current=final_summary,
                            severity="urgent",
                            escalated=True,
                        )
                except Exception:
                    LOGGER.warning(
                        "apply_amendment: failed to seed Jira-only correlation row for %r",
                        ticket_ref,
                        exc_info=True,
                    )
            return AmendmentResult(
                ticket_ref=ticket_ref,
                decision="amend",
                escalated=True,
                affected_keys_count=seeded_affected_count,
                occurrence_count=1,
                telegram_chat_id=telegram_chat_id,
                telegram_topic_id=telegram_topic_id,
                telegram_message_id=None,
                component_added=bool(seeded_affected_count),
            )
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
    severity_increased_to_urgent = (
        alert.severity.strip().casefold() == "urgent"
        and decision.ticket_severity.strip().casefold() != "urgent"
    )
    # Severity is the source of truth. A legacy count-based ``escalated_at``
    # marker must not prevent the first warning-to-urgent priority promotion.
    escalate_now = severity_increased_to_urgent
    remains_escalated = bool(correlation.get("escalated_at"))
    final_summary = f"🔴 {new_summary}" if escalate_now or remains_escalated else new_summary
    effective_severity = (
        "urgent"
        if any(
            severity.strip().casefold() == "urgent"
            for severity in (
                str(correlation.get("severity") or ""),
                decision.ticket_severity,
                alert.severity,
            )
        )
        else (alert.severity or decision.ticket_severity or correlation.get("severity") or "")
    )

    await ticket_service.update_ticket(
        ticket_ref,
        summary=final_summary,
        description=new_description,
        priority_id="highest" if escalate_now else None,
    )
    if raw_text:
        await ticket_service.add_comment(ticket_ref, raw_text, public=False)

    await store.record_amendment(
        ticket_ref,
        summary_current=final_summary,
        severity=effective_severity,
        escalated=escalate_now,
    )

    return AmendmentResult(
        ticket_ref=ticket_ref,
        decision="amend",
        escalated=escalate_now,
        affected_keys_count=affected_count,
        occurrence_count=int(correlation.get("occurrence_count") or 1),
        telegram_chat_id=correlation.get("telegram_chat_id"),
        telegram_topic_id=correlation.get("telegram_topic_id"),
        telegram_message_id=correlation.get("telegram_message_id"),
        component_added=component_added,
    )
