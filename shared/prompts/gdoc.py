"""The single Google Doc adapter for prompt overrides.

Replaces the five separate fetch-and-parse paths that previously existed in
instructions_provider, expert_instructions_provider, artifacts_provider,
procedure_provider and customer_mcp_server. This returns raw markdown; section
splitting belongs to render.py.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, Optional, Tuple

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_TTL_SECONDS = 3600

# Prompt id -> the env var that historically carried its doc id. Kept so
# existing deployments keep working unchanged after the refactor.
LEGACY_DOC_ENV_VARS: Dict[str, str] = {
    "customer.system": "CUSTOMER_SUPPORT_DOC_ID",
    "staff.system": "STAFF_SUPPORT_DOC_ID",
    "experts.definitions": "EXPERT_INSTRUCTIONS_DOC_ID",
    "troubleshooting.procedures": "TROUBLESHOOTING_PROCEDURES_DOC_ID",
    "verification.criteria": "VERIFICATION_DOC_ID",
}


def legacy_doc_id_for(prompt_id: str) -> Optional[str]:
    """Doc id from the historical env var for this prompt, if any.

    The override store layers a ``prompt_doc_bindings`` lookup in front of
    this, so a doc attached from the admin page wins over the legacy env var.
    Keeping the env var as the floor means existing deployments keep working
    untouched.
    """
    env_var = LEGACY_DOC_ENV_VARS.get(prompt_id)
    if not env_var:
        return None
    return os.getenv(env_var, "").strip() or None


def fetch_doc_text(doc_id: str) -> Optional[str]:
    """Export a Drive file as markdown text via the service-account plumbing.

    The single place that knows how to reach Drive for a doc id -- GDocStore
    (prompt-level overrides) and GDocProvider (providers_gdoc.py, knowledge
    module bodies) both call this rather than each setting up their own
    client.
    """
    from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown

    return fetch_google_doc_markdown(doc_id) or ""


class GDocStore:
    """Fetches prompt bodies from Google Docs, with a TTL cache.

    A fetch failure is never fatal: it returns None so the caller falls through
    to the bundled default, and logs the failure with the doc id.
    """

    def __init__(
        self,
        doc_id_for: Callable[[str], Optional[str]] = legacy_doc_id_for,
        fetch: Callable[[str], str] = fetch_doc_text,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._doc_id_for = doc_id_for
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._cache: Dict[str, Tuple[str, float]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def body_for(self, prompt_id: str) -> Optional[str]:
        doc_id = self._doc_id_for(prompt_id)
        if not doc_id:
            return None

        cached = self._cache.get(doc_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        try:
            body = self._fetch(doc_id)
        except Exception:
            LOGGER.warning(
                f"Google Doc fetch failed for prompt '{prompt_id}' (doc {doc_id}); "
                f"falling back to the bundled default",
                exc_info=True,
            )
            return None

        if not body or not body.strip():
            LOGGER.warning(f"Google Doc {doc_id} for prompt '{prompt_id}' is empty")
            return None

        self._cache[doc_id] = (body, time.time() + self._ttl)
        return body
