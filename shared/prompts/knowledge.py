"""Knowledge modules — curated context composed into prompts.

A module is selected for a prompt by explicit per-prompt pin, not by tag:
an operator picks which modules a prompt uses in the admin UI, and that
choice is stored in ``prompt_knowledge_overrides``. Two tiers. Pinned modules
are inlined in full, ordered so the most specific survives when the budget
binds. On-demand modules contribute one catalog line each; the model fetches
a body through the knowledge MCP tool when it needs one, which keeps the long
tail out of a window an agent loop re-sends every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from shared.prompts.types import RequestScope
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

PINNED_BUDGET_CHARS = 20000

# Sources whose body needs async, per-request resolution (permission-filtered
# database reads) via JitContextResolver. `gdoc` is deliberately excluded: it
# resolves synchronously inside PromptLibrary via a TTL-cached fetch, the
# same way prompt-level doc overrides already do.
JIT_SOURCES: Tuple[str, ...] = ("graph", "directory", "episodic")


@dataclass(frozen=True)
class KnowledgeModule:
    id: str
    slug: str
    title: str
    summary: str
    body: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    scope: str = "sector"
    mode: str = "pinned"
    source: str = "manual"
    source_ref: Optional[str] = None

    @property
    def is_site_scoped(self) -> bool:
        return self.scope.startswith("site:")

    @property
    def is_jit(self) -> bool:
        """Whether this module's body needs async resolution.

        Only sources needing per-request permission filtering against the
        database are JIT; see JIT_SOURCES.
        """
        return self.source in JIT_SOURCES


def select_for_prompt(
    modules: List[KnowledgeModule],
    pins: Dict[str, bool],
    scope: Optional[RequestScope] = None,
) -> List[KnowledgeModule]:
    """Modules this prompt pins, that the request's scope admits.

    Selection is explicit: an operator picks modules per prompt in the admin
    UI and that choice is stored in ``prompt_knowledge_overrides``. Scope is a
    separate, per-request gate -- a ``site:ABC`` module stays out of a
    conversation about another site even when the prompt pins it.
    """
    scope = scope or RequestScope()
    return sorted(
        (m for m in modules if pins.get(m.slug) and scope.matches(m.scope)),
        key=lambda m: m.slug,
    )


def diff_prompt_pins(current: "set[str]", selected: "set[str]") -> "tuple[set[str], set[str]]":
    """(to_add, to_remove) to reconcile a module's pinned prompts to ``selected``."""
    return selected - current, current - selected


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
        # An unresolved provider body costs nothing here -- its real size is
        # only known once JitContextResolver runs, outside this budget.
        size = len(module.body or "")
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
    # A JIT module whose body hasn't resolved yet (or failed to) contributes
    # nothing here rather than crashing on a None body.
    parts = [f"## {m.title}\n\n{m.body.strip()}" for m in modules if m.body]
    if not parts:
        return None
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
                .select(
                    "id, slug, title, summary, body, tags, scope, mode, source, source_ref"
                )
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

    def prompts_pinning(self, module_id: str) -> List[str]:
        """Prompt ids that currently force this module on (pinned=True)."""
        if not self._client:
            return []
        try:
            result = (
                self._client.table("prompt_knowledge_overrides")
                .select("prompt_id")
                .eq("module_id", module_id)
                .eq("pinned", True)
                .execute()
            )
        except Exception:
            LOGGER.warning(f"Prompt-pin fetch failed for module '{module_id}'", exc_info=True)
            return []
        return [row["prompt_id"] for row in (result.data or [])]

    def set_prompt_pins(self, module_id: str, prompt_ids: List[str], actor: str) -> None:
        """Reconcile this module's pinned prompts to exactly ``prompt_ids``.

        This is the module-authoring counterpart to the per-prompt Knowledge
        tab's checkboxes: both edit the same ``prompt_knowledge_overrides``
        row, from opposite ends of the relationship.
        """
        if not self._client:
            return
        current = set(self.prompts_pinning(module_id))
        to_add, to_remove = diff_prompt_pins(current, set(prompt_ids))
        for prompt_id in to_add:
            self._client.table("prompt_knowledge_overrides").upsert(
                {
                    "prompt_id": prompt_id,
                    "module_id": module_id,
                    "pinned": True,
                    "updated_by": actor,
                }
            ).execute()
        for prompt_id in to_remove:
            self._client.table("prompt_knowledge_overrides").delete().eq(
                "prompt_id", prompt_id
            ).eq("module_id", module_id).execute()

    def set_prompt_modules(self, prompt_id: str, slugs: List[str], actor: str) -> None:
        """Reconcile this prompt's pinned modules to exactly ``slugs``.

        The prompt-editor counterpart to ``set_prompt_pins``: both write the
        same ``prompt_knowledge_overrides`` row, from opposite ends of the
        relationship.
        """
        if not self._client:
            return
        by_slug = {m.slug: m.id for m in self.all_modules()}
        current = set(self.overrides_for(prompt_id))
        to_add, to_remove = diff_prompt_pins(current, set(slugs))
        for slug in sorted(to_add):
            if slug not in by_slug:
                continue
            self._client.table("prompt_knowledge_overrides").upsert(
                {
                    "prompt_id": prompt_id,
                    "module_id": by_slug[slug],
                    "pinned": True,
                    "updated_by": actor,
                }
            ).execute()
        for slug in sorted(to_remove):
            if slug not in by_slug:
                continue
            self._client.table("prompt_knowledge_overrides").delete().eq(
                "prompt_id", prompt_id
            ).eq("module_id", by_slug[slug]).execute()
