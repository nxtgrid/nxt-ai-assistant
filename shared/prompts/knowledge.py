"""Knowledge modules — curated context composed into prompts.

A module is selected for a prompt by explicit per-prompt attachment, not by
tag: an operator picks which modules a prompt uses in the admin UI, and that
choice is stored in ``prompt_knowledge_overrides``. One tier -- every attached
module is inlined in full, ordered so the most specific survives when the
budget binds.

There used to be a second tier ('on_demand') that contributed a summary line
to a catalog the model could fetch from with get_knowledge_module. It was
removed: attaching a module and having its content actually reach the prompt
were two different things, which is not what operators attaching a module
expect. The `mode` column still exists in the database (see
db/migrations/0006_prompt_library.sql) but nothing reads it -- 0029 backfills
it so it stops disagreeing with behaviour. get_knowledge_module survives as a
by-name lookup tool; it just is not fed a catalog any more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from shared.prompts.types import RequestScope
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

INLINE_BUDGET_CHARS = 20000

# Sources whose body is produced per-request rather than stored. All of them
# need the caller's identity: graph/directory/episodic filter database rows by
# permission, and gdoc checks the caller against the document's Drive ACL.
# PromptLibrary.render() is synchronous and carries no identity, so these
# resolve through JitContextResolver instead.
JIT_SOURCES: Tuple[str, ...] = ("gdoc", "graph", "directory", "episodic")

# Sources the codebase itself defines and resolves, not something an operator
# can author -- there is no "add a provider module" control anywhere (see
# knowledge_modules.py's source_select, which only ever offers manual/gdoc),
# because there is no way to add a new provider through a text box. Exactly
# one row of each should exist: code-defined, always visible, non-deletable
# (see knowledge_modules.py's SINGLETON_SOURCES import) and non-creatable via
# the admin UI. gdoc is JIT but explicitly excluded here -- many gdoc modules
# can exist, one per attached document, so it is not a singleton.
SINGLETON_SOURCES: Tuple[str, ...] = ("directory", "graph", "episodic")

# Slug/title/summary for a freshly-bootstrapped singleton row (see
# KnowledgeStore.ensure_singleton_modules). directory/episodic's slug matches
# their source; graph's stays 'entity-graph' -- the name scripts/
# seed_context_provider_modules.py (P1) and the P4 rollout checklist
# (docs/superpowers/plans/2026-08-23-p4-hybrid-agentic-retrieval.md) already
# document, so this doesn't orphan either. Selection is by source, not slug
# (see ensure_singleton_modules), so the mismatch is cosmetic only.
_SINGLETON_MODULE_DEFAULTS: Dict[str, Dict[str, str]] = {
    "directory": {
        "slug": "directory",
        "title": "Known Grids, Organizations and People",
        "summary": "The grids, organizations and team members this caller "
                    "may see. Use to disambiguate a name mentioned in a message.",
    },
    "graph": {
        "slug": "entity-graph",
        "title": "Knowledge Graph Overview",
        "summary": "Entity types, relationship types and example entities in "
                    "the knowledge graph. Use to decide what to search for "
                    "before querying the graph.",
    },
    "episodic": {
        "slug": "episodic",
        "title": "Episodic Memory",
        "summary": "Distilled lessons from prior conversations for the "
                    "grid or organization in scope.",
    },
}


@dataclass(frozen=True)
class KnowledgeModule:
    id: str
    slug: str
    title: str
    summary: str
    body: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    scope: str = "global"
    source: str = "manual"
    source_ref: Optional[str] = None
    source_tab: Optional[str] = None
    # Only meaningful for source='gdoc'. 'acl_mirror' resolves the body only
    # for a caller who can read the file in Drive; 'published' resolves for
    # everyone the prompt serves. None for every other source.
    doc_audience: Optional[str] = None
    doc_audience_set_by: Optional[str] = None

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


def budget_inlined(
    modules: List[KnowledgeModule], limit: int = INLINE_BUDGET_CHARS
) -> Tuple[List[KnowledgeModule], List[KnowledgeModule]]:
    """Fit a prompt's attached modules into the budget by dropping whole ones.

    Site-scoped material is kept first: it is the most specific and the least
    replaceable. Nothing is ever cut mid-document.

    Every module a prompt attaches is inlined in full, so this is the only
    thing standing between an over-attached prompt and an oversized render.
    A drop is logged here and surfaced in the Context tab's character
    counter, which turns red past the budget.
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
            f"Attached knowledge exceeded the {limit}-char budget; dropped "
            f"{len(dropped)} module(s): {', '.join(m.slug for m in dropped)}"
        )
    return kept, dropped


def render_inlined(modules: List[KnowledgeModule]) -> Optional[str]:
    """Every module's body in full, under one heading.

    There is no summary-only variant. A module attached to a prompt is part
    of that prompt; the get_knowledge_module tool remains for looking one up
    by name on demand, but nothing is offered to the model as a catalog it
    has to decide to fetch.
    """
    if not modules:
        return None
    # A JIT module whose body hasn't resolved yet (or failed to) contributes
    # nothing here rather than crashing on a None body.
    parts = [f"## {m.title}\n\n{m.body.strip()}" for m in modules if m.body]
    if not parts:
        return None
    return "# Technical Knowledge\n\n" + "\n\n".join(parts)


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
            LOGGER.opt(exception=True).warning("Could not build the knowledge store client")
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
                    "id, slug, title, summary, body, tags, scope, source, "
                    "source_ref, source_tab, doc_audience, doc_audience_set_by"
                )
                .eq("is_active", True)
                .execute()
            )
            self._cache = [KnowledgeModule(**row) for row in (result.data or [])]
        except Exception:
            LOGGER.opt(exception=True).warning("Knowledge module fetch failed; continuing without")
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
            LOGGER.opt(exception=True).warning(
                f"Knowledge overrides fetch failed for '{prompt_id}'"
            )
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
            LOGGER.opt(exception=True).warning(f"Prompt-pin fetch failed for module '{module_id}'")
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

    def ensure_singleton_modules(self, actor: str) -> Dict[str, str]:
        """Create any missing code-defined singleton rows (see SINGLETON_SOURCES).

        The admin UI has no path to creating one of these (no source picker
        offers directory/graph/episodic -- there's no way to add a new
        provider through a text box) and neither does /learn (hardcodes
        source='manual'). scripts/seed_context_provider_modules.py covered
        directory and graph, but only when a human remembered to run it by
        hand -- it never covered episodic, and per the P4 rollout checklist
        that expected someone to check for it, nothing confirms anyone ever
        did. This makes creation automatic instead of a step to remember:
        called from the Context admin page on every load, cheap (one SELECT
        already needed for the page's own listing, an INSERT only for
        whatever's missing) and idempotent, so a newly registered provider
        just appears next deploy with no manual step at all. The script
        still exists as a CLI-only alternative and now shares this same
        source list, so the two can't drift apart the way SINGLETON_SOURCES
        and this method's row shape once could have.

        Fails open per source, never raises: a CHECK-constraint rejection
        here (most likely migration 0017_context_module_providers.sql not
        yet applied against this database) surfaces as "this one module
        didn't get created", not a broken page. Returns {source: outcome},
        outcome one of "exists", "created", or "failed: <error>", for the
        caller to report.

        A created row is attached to no prompt: prompt_knowledge_overrides
        decides which prompts use it, and this method never touches that
        table. Bootstrapping existence and attaching it to a prompt stay two
        separate, deliberate steps.
        """
        if not self._client:
            return {}
        existing_sources = {m.source for m in self.all_modules()}
        results: Dict[str, str] = {}
        created_any = False
        for source in SINGLETON_SOURCES:
            if source in existing_sources:
                results[source] = "exists"
                continue
            defaults = _SINGLETON_MODULE_DEFAULTS[source]
            row = {
                "slug": defaults["slug"],
                "title": defaults["title"],
                "summary": defaults["summary"],
                "body": None,
                "tags": [],
                "scope": "global",
                "source": source,
                "updated_by": actor,
                "is_active": True,
            }
            try:
                self._client.table("knowledge_modules").insert(row).execute()
                results[source] = "created"
                created_any = True
            except Exception as e:
                LOGGER.warning(f"Could not create the '{source}' singleton module: {e}")
                results[source] = f"failed: {e}"
        if created_any:
            self.invalidate()
        return results

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
