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
from typing import Any, Callable, List

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


# How many not-yet-pinned rows rows_to_display fills the empty-query view
# with, on top of whatever's already pinned. Fixed and small on purpose: it
# must never grow with the candidate pool -- render_entity_picker can face
# dozens of candidates (every registered prompt id today, every skill ever
# created tomorrow), and a default view whose size scales with that pool is
# exactly the shape of thing that risks pushing a single websocket update
# past NiceGUI's own message-size limit. A fixed cap keeps the default view
# safely small forever, however large the underlying catalog grows.
UNPINNED_PREVIEW_LIMIT = 8


def rows_to_display(rows: List[Any], selected: "set[str]", query: str) -> List[PickerRow]:
    """Which rows render_entity_picker shows right now.

    Typing a query always searches every candidate (filter_picker_rows,
    matching slug/title/summary) regardless of what's pinned -- a new pin
    must stay discoverable, and a search result is never truncated.

    An empty query shows every currently-pinned row -- never capped; an
    operator must always be able to see and unpin everything already
    pinned -- topped up with a small, fixed-size, alphabetical sample of
    not-yet-pinned rows (UNPINNED_PREVIEW_LIMIT) when there's room. Without
    that sample, a module with nothing pinned yet (every module, the first
    time this shipped) rendered a picker that looked completely inert: a
    plain text box with no rows and nothing to suggest there was anything
    to find, unlike the native dropdowns elsewhere in the same dialog that
    visibly pop open the moment you click them. The sample is capped, not
    proportional to the candidate pool, so it can't reintroduce the
    scaling hazard described above.

    ``rows`` may be the raw ``PickerRow``s passed into render_entity_picker
    (whose own ``checked`` is a snapshot from dialog-open time) -- this
    always re-derives ``checked`` from ``selected``, the live, mutated set,
    so a row just ticked or unticked shows correctly without needing rows
    itself rebuilt.
    """
    current = [
        PickerRow(slug=r.slug, title=r.title, chars=r.chars, checked=(r.slug in selected), summary=r.summary)
        for r in rows
    ]
    if query.strip():
        return filter_picker_rows(current, query)
    checked = [r for r in current if r.checked]
    remaining = UNPINNED_PREVIEW_LIMIT - len(checked)
    if remaining <= 0:
        return checked
    sample = [r for r in current if not r.checked][:remaining]
    return checked + sample


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


def render_entity_picker(
    rows: List[PickerRow],
    *,
    label: str,
    search_placeholder: str = "Search…",
    on_change: "Callable[[], None] | None" = None,
) -> "Callable[[], List[str]]":
    """Search + tick which entities (prompts, or skills) use ONE module --
    the reverse direction from render_module_picker. No budget footer: a
    module's own size is shown elsewhere on the same form.

    `on_change`, if given, fires after every tick/untick -- for a caller
    that needs to react live to the selection (e.g. knowledge_modules.py's
    audience warning, which used to re-check on the native ui.select's own
    on_value_change).

    Returns a zero-argument getter for the currently-ticked slugs/ids,
    rather than a Save button -- knowledge_modules.py must union this
    picker's selection with a second one (skills) before writing once (see
    resolve_pins_to_save; two separate saves would have the second call's
    diff delete the first call's pins).

    Shows every pinned row plus a small fixed-size sample of everything
    else until the operator types a search -- see rows_to_display's own
    docstring for why a *fixed* sample (not "everything"): with dozens of
    candidates, rendering all of them unfiltered on open risks pushing a
    single websocket message past NiceGUI's size limit and blanking the
    whole dialog.
    """
    selected: "set[str]" = {r.slug for r in rows if r.checked}

    ui.label(label).classes("text-caption text-bold")
    picked_label = ui.label().classes("text-caption")
    search = ui.input(placeholder=search_placeholder).classes("w-full").props("clearable dense")
    options = ui.column().classes("w-full gap-0").style("max-height: 260px; overflow-y: auto")

    def _update_picked_label() -> None:
        # A tick/untick here has no other feedback -- no toast, no Save
        # button of its own (the caller saves once, later, after unioning
        # this with a second picker) -- so this is the only visible
        # confirmation that a click actually registered.
        picked_label.text = f"{len(selected)} selected" if selected else "Nothing selected yet"

    def redraw() -> None:
        options.clear()
        query = search.value or ""
        visible = rows_to_display(rows, selected, query)
        hidden = len(rows) - len(visible) if not query.strip() else 0
        with options:
            if not visible:
                ui.label(
                    "No matches." if query.strip() else "Nothing to pin yet."
                ).classes("text-italic text-caption")
            else:
                if hidden > 0:
                    ui.label(
                        f"Showing {len(visible)} of {len(rows)} — type to search the rest."
                    ).classes("text-caption text-grey")
                for r in visible:
                    def toggle(e, slug=r.slug) -> None:
                        if e.value:
                            selected.add(slug)
                        else:
                            selected.discard(slug)
                        _update_picked_label()
                        if on_change:
                            on_change()

                    with ui.row().classes("items-center no-wrap w-full"):
                        ui.checkbox(value=r.checked, on_change=toggle).props("dense")
                        ui.label(f"{r.title}  ·  {r.summary}" if r.summary else r.title)

    search.on_value_change(redraw)
    _update_picked_label()
    redraw()
    return lambda: sorted(selected)


__all__ = [
    "PickerRow",
    "UNPINNED_PREVIEW_LIMIT",
    "build_picker_rows",
    "filter_picker_rows",
    "render_entity_picker",
    "render_module_picker",
    "rows_to_display",
]
