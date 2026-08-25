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

from nicegui import ui


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


def render_module_picker(
    pinning_id: str, store: Any, user_email: str, *, show_budget: bool
) -> None:
    """Search + tick which modules `pinning_id` (a prompt id or a
    skill:<uuid>, both live in prompt_knowledge_overrides) uses, with its
    own independent Save button -- lifted verbatim from the Prompts page's
    original Context tab, generalized to any pinning id.

    `show_budget` is False for the reverse-direction picker's context (a
    module -> which prompts/skills use it); there, a module's own size is
    shown elsewhere on that same form, and summing many different entities'
    inlined-char totals wouldn't mean anything.
    """
    from shared.prompts.knowledge import INLINE_BUDGET_CHARS

    if not store._client:  # noqa: SLF001 -- readiness check, matches every other call site
        ui.label(
            "⚠️ Context storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY)."
        ).classes("text-warning")
        return

    ui.label(
        "Context modules this uses. Every module you tick is inlined "
        "in full. Built-in modules resolve per request and have no fixed "
        "size until they do, so they don't count towards the budget below."
    ).classes("text-caption")

    all_modules = store.all_modules()
    pins = store.overrides_for(pinning_id)
    selected: "set[str]" = {m.slug for m in all_modules if pins.get(m.slug)}

    search = ui.input(placeholder="Search modules…").classes("w-full").props("clearable dense")
    picked_label = ui.label().classes("text-caption text-bold")
    options = ui.column().classes("w-full gap-0").style("max-height: 340px; overflow-y: auto")

    def redraw() -> None:
        options.clear()
        rows = filter_picker_rows(
            build_picker_rows(all_modules, {s: True for s in selected}),
            search.value or "",
        )
        if show_budget:
            inlined_chars = sum(r.chars for r in rows if r.checked)
            over = inlined_chars > INLINE_BUDGET_CHARS
            picked_label.text = (
                f"{len(selected)} selected · {inlined_chars} chars "
                f"of {INLINE_BUDGET_CHARS} budget"
                + (" · over budget, some will be dropped at render" if over else "")
            )
            picked_label.classes(
                replace="text-caption text-bold " + ("text-negative" if over else "")
            )
        else:
            picked_label.text = f"{len(selected)} selected"
        with options:
            if not rows:
                ui.label("No modules match.").classes("text-italic text-caption")
            for r in rows:
                def toggle(e, slug=r.slug) -> None:
                    if e.value:
                        selected.add(slug)
                    else:
                        selected.discard(slug)
                    redraw()

                with ui.row().classes("items-center no-wrap w-full"):
                    ui.checkbox(value=r.checked, on_change=toggle).props("dense")
                    with ui.column().classes("gap-0"):
                        size_text = "live" if r.is_jit else f"{r.chars} chars"
                        ui.label(f"{r.title}  ·  {size_text}")
                        if r.summary:
                            ui.label(r.summary).classes("text-caption")

    async def save_pins() -> None:
        try:
            store.set_prompt_modules(pinning_id, sorted(selected), actor=user_email)
            store.invalidate()
            ui.notify("Context updated", type="positive")
        except Exception as e:  # noqa: BLE001 -- surfaced to the operator
            ui.notify(f"Save failed: {e}", type="negative")

    search.on_value_change(redraw)
    redraw()
    with ui.row().classes("justify-end w-full q-mt-sm"):
        ui.button("Save context", on_click=save_pins).props("color=primary")


__all__ = ["PickerRow", "build_picker_rows", "filter_picker_rows", "render_module_picker"]
