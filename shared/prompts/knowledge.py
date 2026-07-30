"""Knowledge modules — curated, tagged context composed into prompts.

Two tiers. Pinned modules are inlined in full, ordered so the most specific
survives when the budget binds. On-demand modules contribute one catalog line
each; the model fetches a body through the knowledge MCP tool when it needs
one, which keeps the long tail out of a window an agent loop re-sends every
step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from shared.prompts.types import RequestScope
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

PINNED_BUDGET_CHARS = 20000


@dataclass(frozen=True)
class KnowledgeModule:
    id: str
    slug: str
    title: str
    summary: str
    body: str
    tags: List[str] = field(default_factory=list)
    scope: str = "sector"
    mode: str = "pinned"

    @property
    def is_site_scoped(self) -> bool:
        return self.scope.startswith("site:")


def select_modules(
    modules: List[KnowledgeModule], tags: List[str], scope: RequestScope
) -> List[KnowledgeModule]:
    """Modules sharing a tag with the prompt whose scope matches the request."""
    wanted = set(tags)
    if not wanted:
        return []
    return [m for m in modules if wanted & set(m.tags) and scope.matches(m.scope)]


def apply_overrides(
    selected: List[KnowledgeModule],
    all_modules: List[KnowledgeModule],
    overrides: Dict[str, bool],
) -> List[KnowledgeModule]:
    """Apply per-prompt forced-on / forced-off decisions to a tag selection."""
    by_slug = {m.slug: m for m in all_modules}
    result = {m.slug: m for m in selected if overrides.get(m.slug, True)}
    for slug, pinned in overrides.items():
        if pinned and slug in by_slug:
            result[slug] = by_slug[slug]
    return [by_slug[s] for s in sorted(result)]


def budget_pinned(
    modules: List[KnowledgeModule], limit: int = PINNED_BUDGET_CHARS
) -> Tuple[List[KnowledgeModule], List[KnowledgeModule]]:
    """Fit pinned modules into the budget by dropping whole modules.

    Site-scoped material is kept first: it is the most specific and the least
    replaceable. Nothing is ever cut mid-document.
    """
    ordered = sorted(modules, key=lambda m: (not m.is_site_scoped, m.slug))
    kept: List[KnowledgeModule] = []
    dropped: List[KnowledgeModule] = []
    used = 0
    for module in ordered:
        size = len(module.body)
        if used + size <= limit:
            kept.append(module)
            used += size
        else:
            dropped.append(module)
    if dropped:
        LOGGER.warning(
            f"Pinned knowledge exceeded the {limit}-char budget; dropped "
            f"{len(dropped)} module(s): {', '.join(m.slug for m in dropped)}"
        )
    return kept, dropped


def render_pinned(modules: List[KnowledgeModule]) -> Optional[str]:
    if not modules:
        return None
    parts = [f"## {m.title}\n\n{m.body.strip()}" for m in modules]
    return "# Technical Knowledge\n\n" + "\n\n".join(parts)


def render_catalog(modules: List[KnowledgeModule]) -> Optional[str]:
    """Names and one-liners only — never bodies."""
    if not modules:
        return None
    lines = [f"- `{m.slug}` — {m.summary}" for m in modules]
    return (
        "# Available Knowledge\n\n"
        "Fetch any of these with the `get_knowledge_module` tool when relevant:\n\n"
        + "\n".join(lines)
    )


class KnowledgeStore:
    """Reads knowledge_modules and prompt_knowledge_overrides.

    Degrades to "no knowledge" whenever the tables are absent or unreachable —
    a prompt must still render.
    """

    def __init__(self, client=None, ttl_seconds: int = 300) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._cache: Optional[List[KnowledgeModule]] = None
        self._expires = 0.0

    @classmethod
    def from_env(cls) -> "KnowledgeStore":
        from shared.config.db_credentials import chat_db_service_key, chat_db_url

        url, key = chat_db_url(), chat_db_service_key()
        if not (url and key):
            return cls(client=None)
        try:
            from supabase import create_client

            return cls(client=create_client(url, key))
        except Exception:
            LOGGER.warning("Could not build the knowledge store client", exc_info=True)
            return cls(client=None)

    def invalidate(self) -> None:
        self._cache = None
        self._expires = 0.0

    def all_modules(self) -> List[KnowledgeModule]:
        import time

        if self._cache is not None and time.time() < self._expires:
            return self._cache
        if not self._client:
            return []
        try:
            result = (
                self._client.table("knowledge_modules")
                .select("id, slug, title, summary, body, tags, scope, mode")
                .eq("is_active", True)
                .execute()
            )
            self._cache = [KnowledgeModule(**row) for row in (result.data or [])]
        except Exception:
            LOGGER.warning("Knowledge module fetch failed; continuing without", exc_info=True)
            self._cache = []
        self._expires = time.time() + self._ttl
        return self._cache

    def overrides_for(self, prompt_id: str) -> Dict[str, bool]:
        if not self._client:
            return {}
        try:
            result = (
                self._client.table("prompt_knowledge_overrides")
                .select("module_id, pinned")
                .eq("prompt_id", prompt_id)
                .execute()
            )
        except Exception:
            LOGGER.warning(f"Knowledge overrides fetch failed for '{prompt_id}'", exc_info=True)
            return {}
        by_id = {m.id: m.slug for m in self.all_modules()}
        return {
            by_id[row["module_id"]]: row["pinned"]
            for row in (result.data or [])
            if row["module_id"] in by_id
        }
