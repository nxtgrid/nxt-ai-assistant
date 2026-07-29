"""Versioned alert-correlation policy and supporting operational context.

The LLM instructions and safety bounds in this module ship with the
application. Deployments may disable correlation with the kill switch, but
cannot silently substitute different grouping rules, confidence bounds, or
prompt limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

_MINIMAL_FALLBACK_INSTRUCTIONS = (
    "You are grouping incoming infrastructure alerts against a grid's already-open "
    "tickets. Classify each incoming alert as 'new', 'amend', or 'duplicate'. Only "
    "choose 'duplicate' when the alert is clearly the exact same issue re-firing on "
    "the exact same component. When uncertain between 'amend' and 'new', prefer "
    "'amend' if a plausibly-related open ticket exists -- it still surfaces the "
    "alert either way. Never suppress an alert entirely."
)


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
    """Load the bundled correlation rules, with a minimal packaging fallback.

    There is intentionally no document id or other deployment override: rule
    changes are reviewed and versioned with the application.
    """
    from orchestrator.services.instructions_provider import _load_fallback_instructions

    sections: Optional[Dict[str, str]] = None
    try:
        sections = _load_fallback_instructions("alert_correlation_instructions.md")
    except Exception:
        LOGGER.warning(
            "Failed to load bundled alert_correlation_instructions.md", exc_info=True
        )

    if not sections:
        sections = {"system_instructions": _MINIMAL_FALLBACK_INSTRUCTIONS}

    return sections


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
        LOGGER.warning(
            "Failed to fetch grid operational facts for {!r}", grid_name, exc_info=True
        )
        return {}
