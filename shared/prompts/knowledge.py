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
