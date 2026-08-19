"""Google Drive-backed context module bodies.

Synchronous by design: this resolves inside PromptLibrary._compose_knowledge,
which is sync, and mirrors the TTL-cached GDocStore that already backs
prompt-level doc overrides. It is not a ContextProvider in the async sense --
see the plan's "sync/async boundary" note.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_TTL_SECONDS = 300


class GDocProvider:
    """Resolves the `gdoc` source by Drive file id."""

    source = "gdoc"

    def __init__(
        self,
        fetch: Optional[Callable[[str], Optional[str]]] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._fetch = fetch or _default_fetch
        self._ttl = ttl_seconds
        self._cache: Dict[str, Tuple[float, Optional[str]]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def body_for(self, module: KnowledgeModule) -> Optional[str]:
        """The doc's text, or None. Never raises."""
        doc_id = module.source_ref
        if not doc_id:
            LOGGER.warning(f"Module '{module.slug}' is gdoc-sourced but has no source_ref")
            return None

        hit = self._cache.get(doc_id)
        if hit and hit[0] > time.time():
            return hit[1]

        try:
            body = self._fetch(doc_id)
        except Exception:
            LOGGER.warning(f"Google Doc fetch failed for module '{module.slug}'", exc_info=True)
            return None

        body = body.strip() if body else None
        self._cache[doc_id] = (time.time() + self._ttl, body or None)
        return body or None


def _default_fetch(doc_id: str) -> Optional[str]:
    """Export a Drive file as text via the existing service-account plumbing."""
    from shared.prompts.gdoc import fetch_doc_text

    return fetch_doc_text(doc_id)


__all__ = ["GDocProvider"]
