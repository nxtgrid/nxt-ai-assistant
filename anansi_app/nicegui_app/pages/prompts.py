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
from typing import Any, Dict, List, Optional, Tuple

from nicegui import ui

from nicegui_app import branding
from shared.prompts import PROMPTS
from shared.prompts.access import can_edit_prompt, can_publish_prompt, can_view_prompt
from shared.prompts.components import COMPONENT_LABELS, COMPONENT_ORDER, UNCATEGORIZED
from shared.prompts.gdoc import LEGACY_DOC_ENV_VARS, legacy_doc_id_for
from shared.prompts.knowledge import INLINE_BUDGET_CHARS
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
    tier: str
    component: str = UNCATEGORIZED
    # doc_id falls back to the legacy env var when no UI binding exists, so
    # the row reflects what doc_id_for() would actually resolve to -- not
    # just what's in prompt_doc_bindings. doc_override is False whenever
    # there's no explicit binding row (a legacy-env-var-only doc has no
    # override concept; see OverrideStore.doc_override_for).
    doc_id: "str | None" = None
    doc_override: bool = False
    # Whether the bound doc is the prompt's resolved body right now (i.e.
    # library.resolve() actually returned PromptSource.GDOC) -- independent
    # of `source` below, which folds an *active* override (doc_override=True
    # while the doc is live) into "Overridden" so the badge doesn't need a
    # third state. build_rows sets this from the raw resolution, before that
    # relabeling; _render_row uses it to spot a dormant binding (a doc_id set
    # but something else currently live).
    doc_is_live: bool = False


@dataclass(frozen=True)
class KnowledgeTabRow:
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


def build_knowledge_tab(modules: List[Any], pins: dict) -> List[KnowledgeTabRow]:
    """Every module as a pickable row, flagged with this prompt's current pins.

    Unlike the tag-era version this hides nothing: the picker is how an
    operator discovers modules, so an unpinned module must still be findable.
    """
    return [
        KnowledgeTabRow(
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


def build_rows(
    library: Any,
    email: str,
    doc_bindings: "Optional[Dict[str, Tuple[str, bool]]]" = None,
) -> List[PromptRow]:
    """The list view, filtered to what this user may see.

    ``doc_bindings`` is the batch ``prompt_id -> (doc_id, is_override)`` map
    from ``OverrideStore.all_doc_bindings()`` -- one query for every row,
    fetched once by the caller, rather than each row querying for its own
    binding. Omit it (or pass ``None``) when doc-id display doesn't matter,
    e.g. in tests that predate doc bindings entirely -- every row's
    ``doc_id``/``doc_override`` then falls back to the legacy-env-var-only,
    non-override defaults.
    """
    doc_bindings = doc_bindings or {}
    rows: List[PromptRow] = []
    for prompt_id in sorted(library.ids()):
        spec = library.spec(prompt_id)
        if not can_view_prompt(spec, email):
            continue
        _body, source, version = library.resolve(prompt_id)
        binding = doc_bindings.get(prompt_id)
        doc_id = binding[0] if binding is not None else legacy_doc_id_for(prompt_id)
        doc_override = binding[1] if binding is not None else False
        doc_is_live = source == PromptSource.GDOC
        label = SOURCE_LABELS[source]
        if doc_is_live and doc_override:
            # The toggle deliberately told the doc to win, not just "a doc
            # happened to be the only source available" -- surface it the
            # same as a DB override so the list doesn't need a third state
            # for "not the shipped default, and I control what's live."
            label = SOURCE_LABELS[PromptSource.DB]
        rows.append(
            PromptRow(
                prompt_id=prompt_id,
                description=spec.description,
                owner=spec.owner,
                component=spec.component,
                source=label,
                version=version,
                overridable=spec.overridable,
                can_edit=can_edit_prompt(spec, email),
                can_publish=can_publish_prompt(spec, email),
                tier=spec.model,
                doc_id=doc_id,
                doc_override=doc_override,
                doc_is_live=doc_is_live,
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


def body_action_visibility(dirty: bool, can_edit: bool, can_publish: bool) -> Tuple[bool, bool, bool]:
    """Visibility for (Revert to default, Save draft, Save & Publish).

    All three stay hidden until the body actually differs from what's
    currently live -- otherwise they offer to save or revert nothing, which
    is just clutter on a dialog that was only just opened. Save draft / Save
    & Publish are additionally gated by this user's edit/publish permission,
    the same checks that guarded them before this function existed. Revert
    has no separate permission concept here (also unchanged): dirty alone
    decides it.
    """
    return dirty, dirty and can_edit, dirty and can_publish


async def render(user_email: str) -> None:
    ui.label("📝 Prompts").classes("text-h5")
    ui.label(
        f"Every prompt {branding.PUBLIC_PRODUCT_NAME} sends to a model, in one place. "
        "Overridable prompts can be edited here without a redeploy; locked prompts are reviewed "
        "and shipped with the app."
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
        rows = build_rows(PROMPTS, user_email, store.all_doc_bindings())
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
                # A dormant binding: a doc is attached but isn't the active
                # source. Without this, a bound-but-inactive doc and an
                # unbound prompt look identical. Checks doc_is_live rather
                # than the source label, since an active override now shares
                # the "Overridden" label with a DB-sourced row -- the label
                # alone can no longer tell "doc is live" from "doc is not."
                if row.doc_id and not row.doc_is_live:
                    ui.label(f"📎 doc attached ({row.doc_id}), not live").classes(
                        "text-caption text-grey"
                    )
            ui.badge(row.source, color="primary" if row.source == "Overridden" else "grey")
            ui.label(f"tier:{row.tier}").classes("text-caption text-grey")
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

    with ui.dialog() as dialog, ui.card().classes("w-full").style(
        "max-width: 900px; max-height: calc(100dvh - 32px); overflow-y: auto"
    ):
        ui.label(row.prompt_id).classes("text-h6")
        ui.label(f"Owner: {spec.owner} · Source: {row.source}").classes("text-caption")

        with ui.tabs().classes("w-full") as tabs:
            edit_tab = ui.tab("Edit")
            knowledge_tab = ui.tab("Context")
            diff_tab = ui.tab("Diff vs default")
            history_tab = ui.tab("History")

        with ui.tab_panels(tabs, value=edit_tab).classes("w-full").style(
            "min-height: 0; overflow-y: auto"
        ):
            with ui.tab_panel(edit_tab):
                # Defaults to Preview -- opening a prompt is almost always to
                # read it; Edit is one click away for the times it isn't.
                view_toggle = ui.toggle(["Edit", "Preview"], value="Preview").props("dense")
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
                body_input.set_visibility(False)
                body_preview = (
                    ui.markdown(current_body)
                    .classes("w-full")
                    .style("min-height: 26rem; border: 1px solid #e0e0e0; padding: 0.5rem;")
                )

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

                ui.separator().classes("q-my-sm")
                ui.label("Google Doc").classes("text-caption text-bold")
                with ui.row().classes("items-start gap-4 w-full no-wrap"):
                    doc_id_input = (
                        ui.input("Google Doc ID", value=row.doc_id or "")
                        .classes("flex-grow")
                        .props("dense")
                    )
                    doc_id_input.set_enabled(row.can_edit)
                    override_switch = ui.switch(
                        "Doc overrides saved versions", value=row.doc_override
                    ).props("dense")
                    override_switch.set_enabled(row.can_publish)

                existing_binding = store.all_doc_bindings().get(row.prompt_id)
                if existing_binding is not None:
                    origin = "from binding, set on this page"
                elif row.doc_id:
                    env_var = LEGACY_DOC_ENV_VARS.get(row.prompt_id)
                    origin = f"from {env_var} (no binding row yet)" if env_var else "from env"
                else:
                    origin = None
                if origin:
                    ui.label(origin).classes("text-caption text-grey")

                override_banner = ui.label(
                    "⚠️ Doc override is on: edits saved above will be stored but will NOT go "
                    "live while this stays on. The doc always wins over a saved version."
                ).classes("text-caption text-warning")
                override_banner.bind_visibility_from(override_switch, "value")

                async def save_doc_binding() -> None:
                    try:
                        doc_id = doc_id_input.value.strip()
                        if doc_id:
                            store.set_doc_binding(
                                row.prompt_id,
                                doc_id,
                                is_override=override_switch.value,
                                actor=user_email,
                            )
                            ui.notify("Doc binding saved", type="positive")
                        else:
                            store.clear_doc_binding(row.prompt_id, actor=user_email)
                            ui.notify("Doc binding cleared", type="positive")
                        dialog.close()
                        refresh()
                    except Exception as e:  # noqa: BLE001 -- surfaced to the operator
                        ui.notify(str(e), type="negative")

                with ui.row().classes("justify-end w-full"):
                    ui.button("Save doc settings", on_click=save_doc_binding).props(
                        "flat"
                    ).set_visibility(row.can_edit)

                ui.separator().classes("q-my-sm")
                ui.label("Model Tier").classes("text-caption text-bold")
                tier_select = ui.select(
                    ["thinking", "fast", "lite"], value=spec.model, label="Tier"
                ).props("dense").classes("w-48")
                tier_select.set_enabled(row.can_edit)

                async def save_tier() -> None:
                    try:
                        store.set_model_override(row.prompt_id, tier_select.value, actor=user_email)
                        ui.notify(f"Tier set to {tier_select.value}", type="positive")
                        dialog.close()
                        refresh()
                    except Exception as e:  # noqa: BLE001 -- surfaced to the operator
                        ui.notify(str(e), type="negative")

                async def revert_tier() -> None:
                    try:
                        store.clear_model_override(row.prompt_id, actor=user_email)
                        ui.notify("Tier reverted to bundled default", type="positive")
                        dialog.close()
                        refresh()
                    except Exception as e:  # noqa: BLE001 -- surfaced to the operator
                        ui.notify(str(e), type="negative")

                with ui.row().classes("justify-end w-full gap-2"):
                    ui.button("Revert tier", on_click=revert_tier).props("flat").set_visibility(
                        row.can_edit
                    )
                    ui.button("Save tier", on_click=save_tier).props("flat").set_visibility(
                        row.can_edit
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
                    except Exception as e:  # noqa: BLE001 -- surfaced to the operator
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
                    except Exception as e:  # noqa: BLE001 -- surfaced to the operator
                        ui.notify(str(e), type="negative")

                async def revert() -> None:
                    try:
                        store.revert_to_default(row.prompt_id, actor=user_email)
                        ui.notify("Reverted to the bundled default", type="positive")
                        dialog.close()
                        refresh()
                    except Exception as e:  # noqa: BLE001 -- surfaced to the operator
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
                    revert_button = ui.button("Revert to default", on_click=revert).props(
                        "flat color=warning"
                    )
                    save_draft_button = ui.button("Save draft", on_click=save_draft).props("flat")
                    save_publish_button = ui.button(
                        "Save & Publish", on_click=publish_latest
                    ).props("color=primary")

                def _refresh_body_actions() -> None:
                    revert_visible, save_draft_visible, save_publish_visible = (
                        body_action_visibility(
                            body_input.value != current_body, row.can_edit, row.can_publish
                        )
                    )
                    revert_button.set_visibility(revert_visible)
                    save_draft_button.set_visibility(save_draft_visible)
                    save_publish_button.set_visibility(save_publish_visible)

                body_input.on_value_change(lambda: _refresh_body_actions())
                _refresh_body_actions()

            with ui.tab_panel(knowledge_tab):
                from shared.prompts.knowledge import KnowledgeStore

                k_store = KnowledgeStore.from_env()
                if not k_store._client:  # noqa: SLF001 -- readiness check, as elsewhere on this page
                    ui.label(
                        "⚠️ Context storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY)."
                    ).classes("text-warning")
                else:
                    ui.label(
                        "Context modules this prompt uses. Every module you tick is inlined "
                        "into this prompt in full. Built-in modules resolve per request and "
                        "have no fixed size until they do, so they don't count towards the "
                        "budget below."
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
                        # Every ticked module is inlined, so the counter sums
                        # all of them rather than a subset. Past the budget,
                        # budget_inlined drops whole modules at render time
                        # and only logs it -- this red counter is the one
                        # warning an operator gets before that happens.
                        inlined_chars = sum(r.chars for r in rows if r.checked)
                        over = inlined_chars > INLINE_BUDGET_CHARS
                        picked_label.text = (
                            f"{len(selected)} selected · {inlined_chars} chars "
                            f"of {INLINE_BUDGET_CHARS} budget"
                            + (" · over budget, some will be dropped at render" if over else "")
                        )
                        picked_label.classes(
                            replace="text-caption text-bold "
                            + ("text-negative" if over else "")
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
                                        size_text = "live" if r.is_jit else f"{r.chars} chars"
                                        ui.label(f"{r.title}  ·  {size_text}")
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
