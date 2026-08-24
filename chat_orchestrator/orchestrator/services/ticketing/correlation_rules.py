"""Versioned alert-correlation policy and supporting operational context.

The safety bounds in this module (``CorrelationPolicy`` below) ship with the
application and cannot be changed without a PR. The grouping-rules prompt
text is different: ops/eng can draft a change from the Prompts admin page,
but only eng can publish one live -- see ``get_correlation_instructions``
and docs/superpowers/specs/2026-08-08-prompt-permissions-design.md.
Deployments may disable correlation entirely with the kill switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.prompts import PROMPTS
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class CorrelationPolicy:
    """Application-versioned safety bounds for one correlation decision."""

    confidence_floor: float = 0.75
    llm_timeout_seconds: float = 12
    # The lock is held across candidate assembly (Jira search + status
    # confirmation) *and* the LLM call, so it must outlast the LLM budget --
    # sharing it made every concurrent alert on a busy grid file its own
    # ticket. A single holder's own worst case (LLM 12s + bounded
    # ticket-backend HTTP calls) can already approach the old 45s bound, and
    # /chat/notify's ticket-resolution step is synchronous in the caller's
    # request cycle (see the comment at app.py's handle_notify -- "Ticket
    # resolution (if requested) is synchronous..."), so a multi-alert burst
    # queued behind one grid's lock could exceed 45s well before the burst
    # itself was unreasonable. 120s keeps that burst queued and correlated
    # rather than bailing out to the (now lock-free, deterministic-only)
    # timeout fallback, while still bounding the caller's worst-case wait to
    # a fixed, documented ceiling.
    grid_lock_timeout_seconds: float = 120
    open_candidate_window_hours: int = 168
    maximum_candidate_count: int = 15
    candidate_status_concurrency: int = 5


DEFAULT_CORRELATION_POLICY = CorrelationPolicy()


def get_correlation_instructions() -> Dict[str, str]:
    """Load the live correlation policy (bundled default, or a published
    override).

    ``ticketing.correlation`` is ``overridable: true`` with
    ``access.edit: [eng, ops]`` and ``access.publish: [eng]``: ops/eng can
    draft a grouping-rule change from the Prompts admin page, but only an
    eng account can publish it live. That is a permission gate, not code
    review -- nothing here re-checks a published change's content, and an
    eng account can draft and publish its own change with nobody else
    looking at it. There is still no document override for this prompt.
    """
    return {"system_instructions": PROMPTS.render("ticketing.correlation").system_text}


async def get_rag_context(
    query: str, limit: Optional[int] = None, rag_provider: Any = None
) -> List[str]:
    """Reserved versioned-policy hook; external RAG context is currently disabled."""
    return []


async def get_grid_operational_context(
    grid_name: str, auth_service: Any = None
) -> Dict[str, Any]:
    """Deterministic grid facts (``is_hps_on``, DCU status roll-up) for the
    correlation prompt. Returns ``{}`` on any failure -- a bad lookup means
    "no extra context", not a failed decision."""
    try:
        if auth_service is None:
            from shared.auth import get_auth_service

            auth_service = get_auth_service()
        return await auth_service.get_grid_operational_facts(grid_name)
    except Exception:
        LOGGER.opt(exception=True).warning(
            "Failed to fetch grid operational facts for {!r}", grid_name
        )
        return {}
