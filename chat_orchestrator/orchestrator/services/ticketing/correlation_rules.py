"""Operator-editable alert correlation rules + supporting context lookups.

Feeds the ``AlertCorrelator`` (correlator.py) prompt with three independent
context sources, each following the same "degrade, never fail" contract as
the rest of the correlation pipeline:

- ``get_correlation_instructions()`` -- the rules doc itself (Google Doc,
  falling back to a bundled file, falling back to a minimal built-in string).
- ``get_rag_context()`` -- permission-filtered RAG snippets, opt-in via
  ``ALERT_CORRELATION_RAG_IDENTITY`` (independent of the general
  ``rag__enabled`` flag being *also* on).
- ``get_grid_operational_context()`` -- deterministic grid facts
  (``is_hps_on``, DCU status) from the auth DB -- what lets the model reason
  e.g. "grid has been OFF for 41h -> this MPPT alert is a child".

None of these ever raise: a bad Google Doc, RAG being disabled/misconfigured,
or an auth-DB error all just mean less context in the prompt, never a
failed correlation decision.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from shared.config import flag_registry as fr
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


def get_correlation_instructions(doc_id: Optional[str] = None) -> Dict[str, str]:
    """Operator-editable alert correlation rules, as a sections dict.

    Priority: Google Doc (``ALERT_CORRELATION_DOC_ID``) -> bundled
    ``alert_correlation_instructions.md`` -> a minimal built-in string.
    """
    from orchestrator.services.artifacts_provider import ArtifactsProvider
    from orchestrator.services.instructions_provider import _load_fallback_instructions

    resolved_doc_id = doc_id if doc_id is not None else (fr.get("ALERT_CORRELATION_DOC_ID") or "")
    sections: Optional[Dict[str, str]] = None
    if resolved_doc_id:
        try:
            sections = ArtifactsProvider()._fetch_google_doc_sections(resolved_doc_id)
        except Exception:
            LOGGER.warning(
                "Failed to fetch alert correlation doc %s", resolved_doc_id, exc_info=True
            )
            sections = None

    if not sections:
        try:
            sections = _load_fallback_instructions("alert_correlation_instructions.md")
        except Exception:
            LOGGER.warning(
                "Failed to load bundled alert_correlation_instructions.md", exc_info=True
            )
            sections = None

    if not sections:
        sections = {"system_instructions": _MINIMAL_FALLBACK_INSTRUCTIONS}

    return sections


async def get_rag_context(
    query: str, limit: Optional[int] = None, rag_provider: Any = None
) -> List[str]:
    """Permission-filtered RAG snippets for the correlation prompt.

    No-op (returns ``[]``) when ``rag__enabled`` is false or
    ``ALERT_CORRELATION_RAG_IDENTITY`` is blank -- correlation must work with
    RAG entirely absent.
    """
    if not fr.get("rag__enabled"):
        return []
    identity = (fr.get("ALERT_CORRELATION_RAG_IDENTITY") or "").strip()
    if not identity:
        return []
    if not query.strip():
        return []

    try:
        if rag_provider is None:
            from orchestrator.services.rag_provider import RAGProvider

            rag_provider = RAGProvider()
        effective_limit = limit if limit is not None else fr.get("rag__top_k")
        return await rag_provider.retrieve_as_text(
            query=query, user_email=identity, limit=effective_limit
        )
    except Exception:
        LOGGER.warning("Alert correlation RAG lookup failed", exc_info=True)
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
            "Failed to fetch grid operational facts for %r", grid_name, exc_info=True
        )
        return {}
