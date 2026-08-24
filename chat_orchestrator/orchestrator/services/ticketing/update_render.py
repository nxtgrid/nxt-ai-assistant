"""Rendering and LLM summarisation for ticket update cards.

Split out from ``update_notifier`` so the text logic is unit-testable without
a database or a Telegram token, mirroring how ``correlation_render`` is split
from ``correlator``.

The card is always a *full statement of current state*. The notifier may
either edit the original ticket message in place or post a fresh reply, and
both must read correctly standing alone -- so nothing here may depend on the
content of a previous message.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from shared.llm import GenerationOptions, LLMMessage
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

#: Comments shorter than this are treated as chatter ("ok", "thanks", "+1")
#: and never reach the LLM classifier.
NOISE_FLOOR_CHARS = 20

#: How much of a comment survives into the deterministic fallback summary.
FALLBACK_CHARS = 300

_STATUS_LABELS = {
    "open": "open",
    "in_progress": "in progress",
    "done": "closed",
}

_STATUS_ICONS = {
    "open": "\U0001f7e2",  # green circle
    "in_progress": "\U0001f7e0",  # orange circle
    "done": "✅",  # white heavy check mark
}


def is_probably_noise(body: str) -> bool:
    """True for comments not worth spending an LLM call on."""
    return len(body.strip()) <= NOISE_FLOOR_CHARS


def fallback_summary(comments: List[Dict[str, Any]]) -> str:
    """Deterministic stand-in when the LLM is unavailable: latest comment, truncated.

    ``comments`` is oldest-first (as returned by
    ``TicketRepository.list_comments_by_ref``), so the last element is newest.
    """
    if not comments:
        return ""
    body = (comments[-1].get("body") or "").strip()
    if not body:
        return ""
    if len(body) <= FALLBACK_CHARS:
        return body
    return body[:FALLBACK_CHARS] + "…"


def render_update_card(
    *,
    ticket_ref: str,
    summary: str,
    status: str,
    activity: str,
    url: Optional[str],
) -> str:
    """Render the complete current state of a ticket as Telegram Markdown."""
    icon = _STATUS_ICONS.get(status, "\U0001f4cb")
    label = _STATUS_LABELS.get(status, status or "unknown")
    ref_text = f"[{ticket_ref}]({url})" if url else f"*{ticket_ref}*"

    lines = [f"{icon} {ref_text} — {label}"]
    if summary:
        lines.append(summary)
    text = "\n".join(lines)
    if activity.strip():
        text = f"{text}\n\n{activity.strip()}"
    return text


_SUMMARY_SYSTEM = (
    "You summarise support-ticket activity for a solar mini-grid operations "
    "team on Telegram. Reply with one or two plain sentences, no preamble, no "
    "bullet points, no markdown. State what was actually done or found. If the "
    "comments do not say what happened, say so briefly rather than inventing "
    "detail."
)

_SIGNIFICANCE_SYSTEM = (
    "You triage support-ticket comments for a solar mini-grid operations team. "
    "Decide whether a comment is operationally significant -- a diagnosis, a "
    "root cause, an escalation, a customer impact, a blocker, a schedule "
    "change, or a resolution. Routine acknowledgements, status pings, and "
    "administrative chatter are not significant. Respond with JSON only: "
    '{"significant": true|false, "summary": "<one sentence>"}'
)


def _format_comments(comments: List[Dict[str, Any]]) -> str:
    parts = []
    for comment in comments:
        author = (comment.get("author") or "unknown").strip()
        body = (comment.get("body") or "").strip()
        if body:
            parts.append(f"{author}: {body}")
    return "\n\n".join(parts)


async def summarize_activity(
    gateway: Any,
    model: str,
    comments: List[Dict[str, Any]],
) -> str:
    """One-or-two-sentence summary of recent ticket comments.

    Fails open to ``fallback_summary`` on any error -- a closure notification
    that says a little less is strictly better than one that never arrives.
    """
    if not comments:
        return ""
    formatted = _format_comments(comments)
    if not formatted:
        return ""
    try:
        result = await gateway.generate(
            [
                LLMMessage(role="system", text=_SUMMARY_SYSTEM),
                LLMMessage(role="user", text=formatted),
            ],
            GenerationOptions(model=model, temperature=0.0),
        )
    except Exception:
        LOGGER.opt(exception=True).warning("ticket update: activity summarisation failed")
        return fallback_summary(comments)
    text = (getattr(result, "text", "") or "").strip()
    return text or fallback_summary(comments)


async def classify_significance(
    gateway: Any,
    model: str,
    comment_body: str,
) -> bool:
    """Whether a single comment warrants interrupting a Telegram group.

    Fails *closed* (returns False): the cost of a missed notification is one
    less message, while the cost of a false positive is training the team to
    ignore ticket updates.
    """
    import json

    if is_probably_noise(comment_body):
        return False
    try:
        result = await gateway.generate(
            [
                LLMMessage(role="system", text=_SIGNIFICANCE_SYSTEM),
                LLMMessage(role="user", text=comment_body.strip()),
            ],
            GenerationOptions(model=model, temperature=0.0, response_format="json"),
        )
        parsed = json.loads((getattr(result, "text", "") or "").strip())
    except Exception:
        LOGGER.opt(exception=True).warning("ticket update: significance classification failed")
        return False
    return bool(parsed.get("significant")) if isinstance(parsed, dict) else False
