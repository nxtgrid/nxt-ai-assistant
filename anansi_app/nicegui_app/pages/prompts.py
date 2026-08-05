"""Prompts admin page: list, edit, diff, publish, revert.

The prompt library ships with the app (``shared/prompts/library/*.prompt``);
this page lets an authorized operator override a prompt live, see a diff
against the shipped default, review version history, and roll back --
without a redeploy.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, List, Tuple

from nicegui import ui

from shared.prompts import PROMPTS
from shared.prompts.access import can_edit_prompt, can_publish_prompt, can_view_prompt
from shared.prompts.components import COMPONENT_LABELS, COMPONENT_ORDER, UNCATEGORIZED
from shared.prompts.knowledge import PINNED_BUDGET_CHARS
from shared.prompts.overrides import OverrideStore
from shared.prompts.types import PromptSource

SOURCE_LABELS = {
    PromptSource.DB: "Overridden",
    PromptSource.GDOC: "Google Doc",
    PromptSource.BUNDLED: "Default",
}

# Same disclosure-triangle convention as the Settings page: pointing right
# while collapsed, down once expanded -- no rotation, two distinct icons.
DISCLOSURE_ICONS = 'expand-icon="keyboard_arrow_right" expanded-icon="keyboard_arrow_down"'


@dataclass(frozen=True)
class PromptRow:
    prompt_id: str
    description: str
    owner: str
    source: str
    version: "int | None"
    overridable: bool
    can_edit: bool
    can_publish: bool
    component: str = UNCATEGORIZED


@dataclass(frozen=True)
class KnowledgeTabRow:
    slug: str
    title: str
    mode: str
    chars: int
    checked: bool
    summary: str = ""


def build_knowledge_tab(modules: List[Any], pins: dict) -> List[KnowledgeTabRow]:
    """Every module as a pickable row, flagged with this prompt's current pins.

    Unlike the tag-era version this hides nothing: the picker is how an
    operator discovers modules, so an unpinned module must still be findable.
    """
    return [
        KnowledgeTabRow(
            slug=module.slug,
            title=module.title,
            mode=module.mode,
            chars=len(module.body),
            checked=bool(pins.get(module.slug)),
            summary=module.summary,
        )
        for module in sorted(modules, key=lambda m: m.slug)
    ]


def filter_module_rows(rows: List[KnowledgeTabRow], query: str) -> List[KnowledgeTabRow]:
    """Case-insensitive substring match over slug, title and summary."""
    needle = query.strip().lower()
    if not needle:
        return list(rows)
    return [
        r
        for r in rows
        if needle in r.slug.lower() or needle in r.title.lower() or needle in r.summary.lower()
    ]


def build_rows(library: Any, email: str) -> List[PromptRow]:
    """The list view, filtered to what this user may see."""
    rows: List[PromptRow] = []
    for prompt_id in sorted(library.ids()):
        spec = library.spec(prompt_id)
        if not can_view_prompt(spec, email):
            continue
        _body, source, version = library.resolve(prompt_id)
        rows.append(
            PromptRow(
                prompt_id=prompt_id,
                description=spec.description,
                owner=spec.owner,
                component=spec.component,
                source=SOURCE_LABELS[source],
                version=version,
                overridable=spec.overridable,
                can_edit=can_edit_prompt(spec, email),
                can_publish=can_publish_prompt(spec, email),
            )
        )
    return rows


def group_rows(rows: List[PromptRow]) -> List[Tuple[str, List[PromptRow]]]:
    """Bucket rows by component, in ``COMPONENT_ORDER``, as ``(label, rows)``.

    A component outside ``COMPONENT_ORDER`` (unset, or a typo in frontmatter)
    lands in a trailing "Uncategorized" bucket rather than being dropped, so a
    bad or missing tag is visible instead of silently disappearing a prompt.
    Each bucket stays prompt-id sorted because ``rows`` already is (see
    ``build_rows``), so no re-sort is needed here.
    """
    by_component: "defaultdict[str, List[PromptRow]]" = defaultdict(list)
    for row in rows:
        by_component[row.component].append(row)

    order = [c for c in COMPONENT_ORDER if c in by_component]
    order += sorted(c for c in by_component if c not in COMPONENT_LABELS)

    return [(COMPONENT_LABELS.get(c, "Uncategorized"), by_component[c]) for c in order]


def diff_lines(default_body: str, current_body: str) -> List[Tuple[str, str]]:
    """Line diff of the shipped default against what is live."""
    result: List[Tuple[str, str]] = []
    for line in difflib.ndiff(
        default_body.strip().splitlines(), current_body.strip().splitlines()
    ):
        marker, text = line[:2], line[2:]
        if marker in ("  ", "- ", "+ "):
            result.append((marker, text))
    return result


async def render(user_email: str) -> None:
    ui.label("📝 Prompts").classes("text-h5")
    ui.label(
        "Every prompt Anansi sends to a model, in one place. Overridable prompts can be "
        "edited here without a redeploy; locked prompts are reviewed and shipped with the app."
    ).classes("text-caption")

    store = OverrideStore.from_env()
    if not store.is_configured():
        ui.label(
            "⚠️ Prompt override storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY). "
            "Prompts are readable below but edits can't be saved."
        ).classes("text-warning")

    search_input = ui.input(placeholder="Search prompts…").classes("w-full")
    list_container = ui.column().classes("w-full gap-0")

    def refresh() -> None:
        list_container.clear()
        rows = build_rows(PROMPTS, user_email)
        query = (search_input.value or "").strip().lower()
        if query:
            rows = [
                r
                for r in rows
                if query in r.prompt_id.lower() or query in r.description.lower()
            ]
        with list_container:
            if not rows:
                ui.label("No prompts match.").classes("text-italic")
                return
            for label, group in group_rows(rows):
                section = ui.expansion(f"{label}  ·  {len(group)}", value=bool(query)).classes(
                    "w-full q-mb-sm"
                )
                section.props(f'header-class="text-h6 text-weight-bold" {DISCLOSURE_ICONS}')
                with section:
                    for row in group:
                        _render_row(row, store, refresh, user_email)

    search_input.on_value_change(lambda: refresh())
    refresh()


def _render_row(row: PromptRow, store: OverrideStore, refresh, user_email: str) -> None:
    with ui.card().classes("w-full q-my-xs"):
        with ui.row().classes("items-center justify-between w-full no-wrap"):
            with ui.column().classes("gap-0").style("flex: 3"):
                ui.label(row.prompt_id).classes("text-bold")
                ui.label(row.description).classes("text-caption")
            ui.badge(row.source, color="primary" if row.source == "Overridden" else "grey")
            if row.version is not None:
                ui.label(f"v{row.version}").classes("text-caption")
            if not row.overridable:
                ui.badge("locked", color="grey")
            ui.button(
                "Open",
                on_click=lambda r=row: _open_detail_dialog(r, store, refresh, user_email),
            ).props("flat dense")


async def _open_detail_dialog(row: PromptRow, store: OverrideStore, refresh, user_email: str) -> None:
    spec = PROMPTS.spec(row.prompt_id)
    current_body, _source, _version = PROMPTS.resolve(row.prompt_id)

    with ui.dialog() as dialog, ui.card().classes("w-full").style("max-width: 900px"):
        ui.label(row.prompt_id).classes("text-h6")
        ui.label(f"Owner: {spec.owner} · Source: {row.source}").classes("text-caption")

        with ui.tabs().classes("w-full") as tabs:
            edit_tab = ui.tab("Edit")
            knowledge_tab = ui.tab("Context")
            diff_tab = ui.tab("Diff vs default")
            history_tab = ui.tab("History")

        with ui.tab_panels(tabs, value=edit_tab).classes("w-full"):
            with ui.tab_panel(edit_tab):
                view_toggle = ui.toggle(["Edit", "Preview"], value="Edit").props("dense")
                body_input = (
                    ui.codemirror(
                        value=current_body,
                        language="Markdown",
                        theme="vscodeLight",
                        line_wrapping=True,
                    )
                    .classes("w-full")
                    .style("height: 26rem")
                )
                body_preview = (
                    ui.markdown("")
                    .classes("w-full")
                    .style("min-height: 26rem; border: 1px solid #e0e0e0; padding: 0.5rem;")
                )
                body_preview.set_visibility(False)

                def _switch_view(e) -> None:
                    if e.value == "Preview":
                        body_preview.set_content(body_input.value)
                    body_input.set_visibility(e.value == "Edit")
                    body_preview.set_visibility(e.value == "Preview")

                view_toggle.on_value_change(_switch_view)

                if spec.variables:
                    ui.label(f"Declared variables: {', '.join(spec.variables)}").classes(
                        "text-caption"
                    )

                async def save_draft() -> None:
                    try:
                        version = PROMPTS.propose(
                            row.prompt_id,
                            body_input.value,
                            note="Edited from the Prompts page",
                            actor=user_email,
                        )
                        ui.notify(f"Saved as v{version} (not yet live)", type="positive")
                        dialog.close()
                        refresh()
                    except PermissionError as e:
                        ui.notify(str(e), type="negative")
                    except RuntimeError as e:
                        ui.notify(str(e), type="negative")

                async def publish_latest() -> None:
                    try:
                        version = PROMPTS.propose(
                            row.prompt_id,
                            body_input.value,
                            note="Published from the Prompts page",
                            actor=user_email,
                        )
                        PROMPTS.publish(row.prompt_id, version, actor=user_email)
                        ui.notify(f"Published v{version}", type="positive")
                        dialog.close()
                        refresh()
                    except PermissionError as e:
                        ui.notify(str(e), type="negative")
                    except RuntimeError as e:
                        ui.notify(str(e), type="negative")

                async def revert() -> None:
                    try:
                        store.revert_to_default(row.prompt_id, actor=user_email)
                        ui.notify("Reverted to the bundled default", type="positive")
                        dialog.close()
                        refresh()
                    except (PermissionError, RuntimeError) as e:
                        ui.notify(str(e), type="negative")

                async def reload_cache() -> None:
                    PROMPTS.reload()
                    PROMPTS.invalidate_doc_cache()
                    store.invalidate()
                    ui.notify("Cache reloaded", type="positive")
                    dialog.close()
                    refresh()

                with ui.row().classes("justify-end w-full gap-2 q-mt-sm"):
                    ui.button("Reload cache", on_click=reload_cache).props("flat")
                    ui.button("Revert to default", on_click=revert).props("flat color=warning")
                    ui.button("Save draft", on_click=save_draft).props("flat").set_visibility(
                        row.can_edit
                    )
                    ui.button("Save & Publish", on_click=publish_latest).props(
                        "color=primary"
                    ).set_visibility(row.can_publish)

            with ui.tab_panel(knowledge_tab):
                from shared.prompts.knowledge import KnowledgeStore

                k_store = KnowledgeStore.from_env()
                if not k_store._client:  # noqa: SLF001 -- readiness check, as elsewhere on this page
                    ui.label(
                        "⚠️ Context storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY)."
                    ).classes("text-warning")
                else:
                    ui.label(
                        "Context modules this prompt uses. Pinned modules are inlined in full; "
                        "on-demand modules contribute only a summary line, and the model fetches "
                        "the body with get_knowledge_module when it decides it's relevant."
                    ).classes("text-caption")

                    all_modules = k_store.all_modules()
                    pins = k_store.overrides_for(row.prompt_id)
                    selected: set[str] = {m.slug for m in all_modules if pins.get(m.slug)}

                    search = ui.input(placeholder="Search modules…").classes("w-full").props(
                        "clearable dense"
                    )
                    picked_label = ui.label().classes("text-caption text-bold")
                    options = ui.column().classes("w-full gap-0").style(
                        "max-height: 340px; overflow-y: auto"
                    )

                    def redraw() -> None:
                        options.clear()
                        rows = filter_module_rows(
                            build_knowledge_tab(all_modules, {s: True for s in selected}),
                            search.value or "",
                        )
                        pinned_chars = sum(
                            r.chars for r in rows if r.checked and r.mode == "pinned"
                        )
                        picked_label.text = (
                            f"{len(selected)} selected · {pinned_chars} pinned chars "
                            f"of {PINNED_BUDGET_CHARS} budget"
                        )
                        picked_label.classes(
                            replace="text-caption text-bold "
                            + ("text-negative" if pinned_chars > PINNED_BUDGET_CHARS else "")
                        )
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
                                        ui.label(f"{r.title}  ·  {r.mode}  ·  {r.chars} chars")
                                        if r.summary:
                                            ui.label(r.summary).classes("text-caption")

                    async def save_pins() -> None:
                        try:
                            k_store.set_prompt_modules(
                                row.prompt_id, sorted(selected), actor=user_email
                            )
                            k_store.invalidate()
                            ui.notify("Context updated", type="positive")
                        except Exception as e:  # noqa: BLE001 -- surfaced to the operator
                            ui.notify(f"Save failed: {e}", type="negative")

                    search.on_value_change(redraw)
                    redraw()
                    with ui.row().classes("justify-end w-full q-mt-sm"):
                        ui.button("Save context", on_click=save_pins).props("color=primary")

            with ui.tab_panel(diff_tab):
                lines = diff_lines(spec.body, current_body)
                if not lines:
                    ui.label("No changes from the shipped default.").classes("text-caption")
                else:
                    with ui.column().classes("gap-0 font-mono"):
                        for marker, text in lines:
                            color = (
                                "text-positive"
                                if marker == "+ "
                                else "text-negative"
                                if marker == "- "
                                else ""
                            )
                            ui.label(f"{marker}{text}").classes(color)

            with ui.tab_panel(history_tab):
                versions = store.versions(row.prompt_id) if store.is_configured() else []
                if not versions:
                    ui.label("No saved versions yet.").classes("text-caption")
                else:
                    for v in versions:
                        with ui.row().classes("items-center gap-2"):
                            ui.label(f"v{v['version']}").classes("text-bold")
                            ui.label(v.get("created_by", "")).classes("text-caption")
                            ui.label(v.get("note", "")).classes("text-caption")

        with ui.row().classes("justify-end w-full"):
            ui.button("Close", on_click=dialog.close).props("flat")

    dialog.open()
