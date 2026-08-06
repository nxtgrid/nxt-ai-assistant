"""Context admin page: CRUD for curated context modules.

A context module is named, addressable content a prompt can deliberately pin
(inlined in full) or leave on-demand (name + summary only, fetched via the
get_knowledge_module MCP tool when the model decides it's relevant). Selection
is explicit per prompt -- see the Context tab on the Prompts page.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, List, Tuple

from nicegui import ui

VALID_MODES = {"pinned", "on_demand"}
VALID_SOURCES = {"manual", "gdoc", "ingested"}

MODE_LABELS = {"pinned": "Pinned", "on_demand": "On-demand"}
MODE_ORDER = ["pinned", "on_demand"]

# Same disclosure-triangle convention as the Prompts and Settings pages:
# pointing right while collapsed, down once expanded.
DISCLOSURE_ICONS = 'expand-icon="keyboard_arrow_right" expanded-icon="keyboard_arrow_down"'


@dataclass(frozen=True)
class ModuleRow:
    slug: str
    title: str
    tags: List[str]
    scope: str
    mode: str
    chars: int


def build_module_rows(modules: List[Any]) -> List[ModuleRow]:
    return [
        ModuleRow(
            slug=m.slug, title=m.title, tags=list(m.tags), scope=m.scope, mode=m.mode,
            chars=len(m.body),
        )
        for m in sorted(modules, key=lambda m: m.slug)
    ]


def group_module_rows(rows: List[ModuleRow]) -> List[Tuple[str, List[ModuleRow]]]:
    """Bucket rows by mode -- pinned, then on-demand -- as ``(label, rows)``.

    Each bucket stays slug-sorted because ``rows`` already is (see
    ``build_module_rows``).
    """
    by_mode: "defaultdict[str, List[ModuleRow]]" = defaultdict(list)
    for row in rows:
        by_mode[row.mode].append(row)

    order = [m for m in MODE_ORDER if m in by_mode]
    order += sorted(m for m in by_mode if m not in MODE_LABELS)

    return [(MODE_LABELS.get(m, m), by_mode[m]) for m in order]


def prompt_option_label(prompt_id: str, description: str, max_len: int = 70) -> str:
    """Dropdown label: the id plus a truncated purpose, not the id alone."""
    description = description.strip()
    if len(description) > max_len:
        description = description[: max_len - 1].rstrip() + "…"
    return f"{prompt_id} — {description}" if description else prompt_id


def validate_module(
    slug: str,
    title: str,
    summary: str,
    body: str,
    scope: str = "sector",
    mode: str = "pinned",
) -> None:
    """Reject a module that would fail silently at render time."""
    if not slug or not title or not body:
        raise ValueError("slug, title and body are required")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
    if mode == "on_demand" and not summary.strip():
        raise ValueError(
            "an on_demand module needs a summary: it is the only thing the model "
            "sees before deciding to fetch the body"
        )
    if scope != "sector" and not (scope.startswith("site:") or scope.startswith("org:")):
        raise ValueError("scope must be 'sector', 'site:<name>' or 'org:<id>'")


async def render(user_email: str) -> None:
    from shared.prompts.knowledge import KnowledgeStore

    ui.label("🧠 Context").classes("text-h5")
    ui.label(
        "Curated facts the bot is told directly — the context it works from. Pinned "
        "modules are inlined into a prompt in full; on-demand modules contribute only "
        "their summary, and the model fetches the body with a tool when it decides "
        "it's relevant. Attach modules to prompts here or from the Context tab of any "
        "prompt."
    ).classes("text-caption")

    store = KnowledgeStore.from_env()
    if not store._client:  # noqa: SLF001 -- readiness check, same as the Prompts page
        ui.label(
            "⚠️ Context storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY). "
            "Modules can't be listed or saved."
        ).classes("text-warning")
        return

    list_container = ui.column().classes("w-full gap-0")

    def refresh() -> None:
        list_container.clear()
        store.invalidate()
        rows = build_module_rows(store.all_modules())
        with list_container:
            with ui.row().classes("justify-end w-full"):
                ui.button(
                    "+ New context module",
                    on_click=lambda: _open_edit_dialog(None, store, refresh, user_email),
                ).props("color=primary")
            if not rows:
                ui.label("No context modules yet. Use /learn in Telegram to add one.").classes(
                    "text-italic"
                )
                return
            for label, group in group_module_rows(rows):
                section = ui.expansion(f"{label}  ·  {len(group)}", value=True).classes(
                    "w-full q-mb-sm"
                )
                section.props(f'header-class="text-h6 text-weight-bold" {DISCLOSURE_ICONS}')
                with section:
                    for row in group:
                        _render_row(row, store, refresh, user_email)

    refresh()


def _render_row(row: ModuleRow, store: Any, refresh, user_email: str) -> None:
    with ui.card().classes("w-full q-my-xs"):
        with ui.row().classes("items-center justify-between w-full no-wrap"):
            with ui.column().classes("gap-0").style("flex: 3"):
                ui.label(row.title).classes("text-bold")
                ui.label(f"{row.slug} · {row.scope} · {row.mode} · {row.chars} chars").classes(
                    "text-caption"
                )
                if row.tags:
                    ui.label(f"tags (legacy): {', '.join(row.tags)}").classes("text-caption")
            ui.button(
                "Edit",
                on_click=lambda: _open_edit_dialog(row.slug, store, refresh, user_email),
            ).props("flat dense")


async def _open_edit_dialog(
    slug: "str | None", store: Any, refresh, user_email: str
) -> None:
    from shared.prompts import PROMPTS

    existing = None
    if slug:
        existing = next((m for m in store.all_modules() if m.slug == slug), None)
    existing_pins = store.prompts_pinning(existing.id) if existing else []

    with ui.dialog() as dialog, ui.card().classes("w-full").style(
        "max-width: 700px; max-height: calc(100dvh - 32px); overflow-y: auto"
    ):
        ui.label("Edit module" if existing else "New module").classes("text-h6")
        slug_input = ui.input("Slug", value=existing.slug if existing else "").classes("w-full")
        slug_input.set_enabled(existing is None)  # slug is the identity; don't let it drift
        title_input = ui.input("Title", value=existing.title if existing else "").classes(
            "w-full"
        )
        summary_input = ui.input("Summary", value=existing.summary if existing else "").classes(
            "w-full"
        )
        scope_input = ui.input(
            "Scope (sector | site:<name> | org:<id>)",
            value=existing.scope if existing else "sector",
        ).classes("w-full")
        mode_select = ui.select(
            sorted(VALID_MODES), value=existing.mode if existing else "pinned", label="Mode"
        ).classes("w-full")
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Body").classes("text-caption")
            # Defaults to Preview -- opening a module is almost always to
            # read it; Edit is one click away for the times it isn't.
            view_toggle = ui.toggle(["Edit", "Preview"], value="Preview").props("dense")
        body_input = ui.codemirror(
            value=existing.body if existing else "",
            language="Markdown",
            theme="vscodeLight",
            line_wrapping=True,
        ).classes("w-full")
        body_input.set_visibility(False)
        body_preview = (
            ui.markdown(existing.body if existing else "")
            .classes("w-full")
            .style("min-height: 16rem; border: 1px solid #e0e0e0; padding: 0.5rem;")
        )

        def _switch_view(e) -> None:
            if e.value == "Preview":
                body_preview.set_content(body_input.value)
            body_input.set_visibility(e.value == "Edit")
            body_preview.set_visibility(e.value == "Preview")

        view_toggle.on_value_change(_switch_view)

        prompt_options = {
            pid: prompt_option_label(pid, PROMPTS.spec(pid).description)
            for pid in sorted(PROMPTS.ids())
        }
        prompts_select = ui.select(
            prompt_options,
            value=list(existing_pins),
            multiple=True,
            label="Used by these prompts",
        ).classes("w-full").props("use-chips")

        async def save() -> None:
            try:
                validate_module(
                    slug=slug_input.value.strip(),
                    title=title_input.value.strip(),
                    summary=summary_input.value.strip(),
                    body=body_input.value,
                    scope=scope_input.value.strip() or "sector",
                    mode=mode_select.value,
                )
            except ValueError as e:
                ui.notify(str(e), type="negative")
                return

            row = {
                "slug": slug_input.value.strip(),
                "title": title_input.value.strip(),
                "summary": summary_input.value.strip(),
                "body": body_input.value,
                "tags": list(existing.tags) if existing else [],
                "scope": scope_input.value.strip() or "sector",
                "mode": mode_select.value,
                "updated_by": user_email,
            }
            try:
                if existing:
                    store._client.table("knowledge_modules").update(row).eq(
                        "slug", row["slug"]
                    ).execute()
                    module_id = existing.id
                else:
                    result = store._client.table("knowledge_modules").insert(row).execute()
                    module_id = result.data[0]["id"]
                store.set_prompt_pins(
                    module_id, list(prompts_select.value or []), actor=user_email
                )
                ui.notify("Saved", type="positive")
                dialog.close()
                refresh()
            except Exception as e:
                ui.notify(f"Save failed: {e}", type="negative")

        with ui.row().classes("justify-end w-full gap-2 q-mt-sm"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save).props("color=primary")

    dialog.open()
