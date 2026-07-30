"""Knowledge Modules admin page: CRUD for tagged, scoped knowledge modules.

A module is curated, named, addressable content a prompt can deliberately
pin (in full) or leave on-demand (name + summary only, fetched via the
get_knowledge_module MCP tool when the model decides it's relevant). See
docs/superpowers/specs/2026-07-30-prompt-library-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from nicegui import ui

VALID_MODES = {"pinned", "on_demand"}
VALID_SOURCES = {"manual", "gdoc", "ingested"}


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

    ui.label("🧠 Knowledge Modules").classes("text-h5")
    ui.label(
        "Curated, tagged, scoped context that prompts pin by tag. Pinned modules are "
        "inlined in full; on-demand modules contribute only their summary to a prompt's "
        "context, and the model fetches the body via a tool when it decides it's relevant."
    ).classes("text-caption")

    store = KnowledgeStore.from_env()
    if not store._client:  # noqa: SLF001 -- readiness check, same as the Prompts page
        ui.label(
            "⚠️ Knowledge storage not configured (CHAT_DB_URL / CHAT_DB_SERVICE_KEY). "
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
                    "+ New module", on_click=lambda: _open_edit_dialog(None, store, refresh)
                ).props("color=primary")
            if not rows:
                ui.label("No knowledge modules yet.").classes("text-italic")
                return
            for row in rows:
                _render_row(row, store, refresh)

    refresh()


def _render_row(row: ModuleRow, store: Any, refresh) -> None:
    with ui.card().classes("w-full q-my-xs"):
        with ui.row().classes("items-center justify-between w-full no-wrap"):
            with ui.column().classes("gap-0").style("flex: 3"):
                ui.label(row.title).classes("text-bold")
                ui.label(f"{row.slug} · {row.scope} · {row.mode} · {row.chars} chars").classes(
                    "text-caption"
                )
                if row.tags:
                    ui.label(", ".join(row.tags)).classes("text-caption")
            ui.button(
                "Edit", on_click=lambda: _open_edit_dialog(row.slug, store, refresh)
            ).props("flat dense")


async def _open_edit_dialog(slug: "str | None", store: Any, refresh) -> None:
    existing = None
    if slug:
        existing = next((m for m in store.all_modules() if m.slug == slug), None)

    with ui.dialog() as dialog, ui.card().classes("w-full").style("max-width: 700px"):
        ui.label("Edit module" if existing else "New module").classes("text-h6")
        slug_input = ui.input("Slug", value=existing.slug if existing else "").classes("w-full")
        slug_input.set_enabled(existing is None)  # slug is the identity; don't let it drift
        title_input = ui.input("Title", value=existing.title if existing else "").classes(
            "w-full"
        )
        summary_input = ui.input("Summary", value=existing.summary if existing else "").classes(
            "w-full"
        )
        tags_input = ui.input(
            "Tags (comma-separated)", value=", ".join(existing.tags) if existing else ""
        ).classes("w-full")
        scope_input = ui.input(
            "Scope (sector | site:<name> | org:<id>)",
            value=existing.scope if existing else "sector",
        ).classes("w-full")
        mode_select = ui.select(
            sorted(VALID_MODES), value=existing.mode if existing else "pinned", label="Mode"
        ).classes("w-full")
        body_input = ui.textarea("Body", value=existing.body if existing else "").classes(
            "w-full"
        ).props("rows=10")

        async def save() -> None:
            tags = [t.strip() for t in tags_input.value.split(",") if t.strip()]
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
                "tags": tags,
                "scope": scope_input.value.strip() or "sector",
                "mode": mode_select.value,
                "updated_by": "unknown",
            }
            try:
                if existing:
                    store._client.table("knowledge_modules").update(row).eq(
                        "slug", row["slug"]
                    ).execute()
                else:
                    store._client.table("knowledge_modules").insert(row).execute()
                ui.notify("Saved", type="positive")
                dialog.close()
                refresh()
            except Exception as e:
                ui.notify(f"Save failed: {e}", type="negative")

        with ui.row().classes("justify-end w-full gap-2 q-mt-sm"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save).props("color=primary")

    dialog.open()
