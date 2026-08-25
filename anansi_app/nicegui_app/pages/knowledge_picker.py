"""Shared search-and-tick widget: which knowledge modules a prompt or a
skill uses (both are pinning ids in prompt_knowledge_overrides -- see
shared/prompts/knowledge.py and shared/prompts/skills.py's skill_prompt_id).

Moved and generalized from the Prompts page's original Context tab, which
was prompt-specific in name only -- nothing in this logic ever depended on
being a prompt. See test_knowledge_picker.py; the pre-move tests lived in
test_knowledge_modules_page.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List


@dataclass(frozen=True)
class PickerRow:
    slug: str
    title: str
    chars: int
    checked: bool
    summary: str = ""
    # A provider-backed module (see shared/prompts/knowledge.py's is_jit) has
    # no stored body -- chars is 0 (correct for the budget sum below,
    # matching budget_inlined's "unresolved costs nothing" rule) but that
    # reads as "empty" rather than "resolved at request time" unless a row
    # can say which one it is.
    is_jit: bool = False


def build_picker_rows(modules: List[Any], pins: dict) -> List[PickerRow]:
    """Every module as a pickable row, flagged with this entity's current pins.

    Unlike the tag-era version this hides nothing: the picker is how an
    operator discovers modules, so an unpinned module must still be findable.
    """
    return [
        PickerRow(
            slug=module.slug,
            title=module.title,
            # None for a provider-backed module -- len(None) would crash
            # (this hazard is the same one build_module_rows had; see
            # knowledge_modules.py's build_module_rows for the sibling fix).
            chars=len(module.body or ""),
            checked=bool(pins.get(module.slug)),
            summary=module.summary,
            is_jit=getattr(module, "is_jit", False),
        )
        for module in sorted(modules, key=lambda m: m.slug)
    ]


def filter_picker_rows(rows: List[PickerRow], query: str) -> List[PickerRow]:
    """Case-insensitive substring match over slug, title and summary."""
    needle = query.strip().lower()
    if not needle:
        return list(rows)
    return [
        r
        for r in rows
        if needle in r.slug.lower() or needle in r.title.lower() or needle in r.summary.lower()
    ]


__all__ = ["PickerRow", "build_picker_rows", "filter_picker_rows"]
