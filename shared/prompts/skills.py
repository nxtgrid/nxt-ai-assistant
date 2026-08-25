"""User-designed skills' catalog: what an LLM sees about available skills.

Phase 3 of docs/superpowers/plans/2026-08-06-user-designed-skills.md.

Deliberately separate from shared/prompts/knowledge.py rather than folded
into it. A skill answers "what can I do" (a procedure); a knowledge module
answers "what do I know" (a document) -- they read nothing alike to an
author, and per the plan's Phase 3, they must render as visibly separate
blocks so a model is never choosing between a document and a procedure in
one flat list. What they share is the *shape* of "small store, cheap
catalog line, full body fetched on demand" -- this module mirrors
knowledge.py's KnowledgeStore/render_inlined pattern deliberately, without
importing from it.

This catalog itself has no per-prompt pinning or geographic/org scope
selection the way knowledge modules do (see knowledge.py's
select_for_prompt): every active, non-staff-only skill is potentially
relevant to every conversation, so the only gate here is staff_only vs. the
request's is_staff -- the same convention command_registry.py already uses
for slash commands. A skill's *own* knowledge (what it draws on when it
runs, as opposed to whether it's listed in this catalog) is a separate
concern, pinned via skill_prompt_id() into the exact same
prompt_knowledge_overrides table a prompt's knowledge modules use -- see
orchestrator/experts/skill_runner.py's _resolve_skill_system_instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Separate from skill_runner.py's SKILL_EXPERT_PREFIX (routes
# matched_expert_id in a different table, for a different purpose) even
# though both happen to be "skill:" -- this one is the key convention for
# prompt_knowledge_overrides.prompt_id, defined here in shared/ because
# anansi_app has no `orchestrator` package to import skill_runner from.
SKILL_PIN_PREFIX = "skill:"


def skill_prompt_id(skill_id: str) -> str:
    """The prompt_knowledge_overrides.prompt_id key for a skill's pins."""
    return f"{SKILL_PIN_PREFIX}{skill_id}"


@dataclass(frozen=True)
class Skill:
    """Catalog-relevant fields only -- not the full row (no steps/inputs).

    Fetching a skill's steps is a Phase 4/5 (builder, runner) concern; the
    catalog only ever needs enough to render one line and decide visibility.
    """

    id: str
    slug: str
    title: str
    summary: str
    staff_only: bool
    # Included so a caller with active_only=False (the knowledge-module
    # picker, which must be able to pin a draft skill before it goes live)
    # can still show status. Defaults to "active" so every existing caller
    # (active_only=True, which never selects this column) keeps working
    # unchanged.
    status: str = "active"


def select_skills_for_context(skills: List[Skill], is_staff: bool) -> List[Skill]:
    """Skills visible in context for this request.

    Mirrors command_registry.py's `if cmd_def.staff_only and not is_staff:`
    gate -- a customer-org request never sees a staff_only skill's title or
    summary, not even its existence.
    """
    return [s for s in skills if not (s.staff_only and not is_staff)]


def render_skill_catalog(skills: List[Skill]) -> Optional[str]:
    """Titles and one-liners only -- never step bodies.

    Its own '# Available Skills' block, deliberately not merged with
    knowledge.render_inlined's '# Technical Knowledge' output -- see this
    module's docstring. Note those differ in kind now: knowledge modules are
    inlined in full, while skills genuinely are a catalog to pick from.

    Shows `title`, not `slug` -- unlike knowledge modules (admin-curated and
    slug-addressed), a skill's title is the name its author chose
    and edits directly (see the plan's Phase 3/4), so it's the identifier
    that should actually appear in context. `slug` stays available on the
    Skill object for a future by-name invocation path (not built yet).
    """
    if not skills:
        return None
    lines = [f"- **{s.title}** — {s.summary}" for s in sorted(skills, key=lambda s: s.title)]
    return "# Available Skills\n\n" + "\n".join(lines)


class SkillCatalogStore:
    """Reads the `skills` table for catalog rendering.

    Degrades to "no skills" whenever the table is absent or unreachable -- a
    system prompt must still render. Mirrors
    shared.prompts.knowledge.KnowledgeStore's shape and cache lifecycle
    deliberately (see this module's docstring for why it isn't the same
    class).
    """

    def __init__(self, client=None, ttl_seconds: int = 300) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._cache: Dict[bool, List[Skill]] = {}
        self._expires: Dict[bool, float] = {}

    @classmethod
    def from_env(cls) -> "SkillCatalogStore":
        from shared.config.db_credentials import chat_db_service_key, chat_db_url

        url, key = chat_db_url(), chat_db_service_key()
        if not (url and key):
            return cls(client=None)
        try:
            from supabase import create_client

            return cls(client=create_client(url, key))
        except Exception:
            LOGGER.opt(exception=True).warning("Could not build the skill catalog store client")
            return cls(client=None)

    def invalidate(self) -> None:
        self._cache = {}
        self._expires = {}

    def all_skills(self, *, active_only: bool = True) -> List[Skill]:
        """Active skills by default. active_only=False includes draft/
        disabled/unusable too -- for the knowledge-module picker, which must
        let an operator pin a module to a skill before it goes live.

        Cached separately per active_only value: they're different result
        sets, and a naive shared cache slot would return the wrong one
        within the TTL window.
        """
        import time

        if active_only in self._cache and time.time() < self._expires.get(active_only, 0):
            return self._cache[active_only]
        if not self._client:
            return []
        try:
            query = self._client.table("skills").select(
                "id, slug, title, summary, staff_only, status"
            )
            if active_only:
                query = query.eq("status", "active")
            result = query.execute()
            self._cache[active_only] = [Skill(**row) for row in (result.data or [])]
        except Exception:
            LOGGER.opt(exception=True).warning("Skill catalog fetch failed; continuing without")
            self._cache[active_only] = []
        self._expires[active_only] = time.time() + self._ttl
        return self._cache[active_only]


# Module-level singleton, matching shared.prompts.core's `PROMPTS = _build_default_library()`
# pattern: constructed once at import time so its 5-minute TTL cache is actually
# useful across calls, rather than every caller building (and never reusing) its
# own store. Degrades to client=None at construction when credentials are absent
# (e.g. in tests) -- same as KnowledgeStore.from_env(), safe to build eagerly.
SKILL_CATALOG = SkillCatalogStore.from_env()


__all__ = [
    "SKILL_CATALOG",
    "SKILL_PIN_PREFIX",
    "Skill",
    "SkillCatalogStore",
    "render_skill_catalog",
    "select_skills_for_context",
    "skill_prompt_id",
]
