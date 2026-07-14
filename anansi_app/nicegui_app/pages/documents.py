"""RAG Knowledgebase page (NiceGUI port of app.py `_render_documents_page`).

Reuses ``services.supabase_reader.SupabaseReader`` unchanged. Paginated document
list with type/procedure filters, inline title edit, a two-step delete, and a
chunk-viewer dialog.
"""

from __future__ import annotations

from typing import Any, Optional

from nicegui import run, ui

from nicegui_app.services_access import get_reader

PAGE_SIZES = [25, 50, 100]


async def render() -> None:
    ui.label("📚 RAG Knowledgebase").classes("text-h5")

    db = get_reader()
    if not await run.io_bound(db.is_configured):
        ui.label("⚠️ Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY.").classes(
            "text-negative"
        )
        return

    state: dict[str, Any] = {
        "page": 0,
        "page_size": 25,
        "doc_type": "All",
        "procedure": "All",
    }

    doc_types = await run.io_bound(db.get_distinct_doc_types)

    # ── Controls ────────────────────────────────────────────────────────────
    with ui.row().classes("items-center gap-4 w-full"):
        type_select = ui.select(["All"] + doc_types, value="All", label="Document Type").classes(
            "w-48"
        )
        proc_select = ui.select(["All"], value="All", label="Procedure").classes("w-48")
        proc_select.set_visibility(False)
        ui.space()
        ui.select(PAGE_SIZES, value=25, label="Per page").classes("w-28").bind_value(
            state, "page_size"
        )

    count_label = ui.label().classes("text-bold")
    list_container = ui.column().classes("w-full gap-0")

    async def on_type_change() -> None:
        state["doc_type"] = type_select.value
        state["page"] = 0
        if type_select.value == "support_example":
            procs = await run.io_bound(db.get_distinct_procedures)
            proc_select.options = ["All"] + procs
            proc_select.value = "All"
            proc_select.update()
            proc_select.set_visibility(True)
        else:
            proc_select.set_visibility(False)
            state["procedure"] = "All"
        await refresh()

    async def on_proc_change() -> None:
        state["procedure"] = proc_select.value
        state["page"] = 0
        await refresh()

    type_select.on_value_change(on_type_change)
    proc_select.on_value_change(on_proc_change)

    async def refresh() -> None:
        list_container.clear()
        doc_type = state["doc_type"] if state["doc_type"] != "All" else None
        proc = state["procedure"] if state["procedure"] != "All" else None
        page_size = state["page_size"]
        offset = state["page"] * page_size
        documents, total = await run.io_bound(
            lambda: db.get_ingested_documents(
                limit=page_size, offset=offset, doc_type=doc_type, procedure_id=proc
            )
        )
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        count_label.text = f"{total} documents in knowledge base"

        with list_container:
            if not documents:
                ui.label(
                    "No documents ingested yet. Use /ingest in Telegram to add documents."
                ).classes("text-italic")
                return
            for doc in documents:
                await _render_doc_row(db, doc, proc, refresh)
            _render_pager(state, total_pages, refresh)

    await refresh()


def _render_pager(state: dict, total_pages: int, refresh) -> None:
    with ui.row().classes("items-center justify-between w-full q-mt-md"):
        prev_btn = ui.button("← Previous", on_click=lambda: _goto_page(state, -1, refresh)).props(
            "flat"
        )
        prev_btn.set_enabled(state["page"] > 0)
        ui.label(f"Page {state['page'] + 1} of {total_pages}")
        next_btn = ui.button("Next →", on_click=lambda: _goto_page(state, 1, refresh)).props("flat")
        next_btn.set_enabled(state["page"] < total_pages - 1)


async def _goto_page(state: dict, delta: int, refresh) -> None:
    state["page"] += delta
    await refresh()


async def _render_doc_row(db, doc: dict, proc_filter: Optional[str], refresh) -> None:
    with ui.card().classes("w-full q-my-xs"):
        with ui.row().classes("items-start justify-between w-full no-wrap"):
            # Title + metadata
            with ui.column().classes("gap-1").style("flex: 3"):
                title = doc["title"]
                if doc.get("source_url"):
                    ui.link(title, doc["source_url"], new_tab=True).classes("text-bold")
                else:
                    ui.label(title).classes("text-bold")
                doc_type = doc.get("doc_type", "unknown")
                audience = doc.get("audience", "staff")
                ui.label(f"📄 {doc_type} • 👥 {audience}").classes("text-caption")
                source_id = doc.get("source_id", "")
                src = f"Source: {doc.get('source_type', 'unknown')}"
                if source_id:
                    src += f" · {source_id[:12]}…"
                ui.label(src).classes("text-caption")

            chunk_count = await run.io_bound(lambda: db.get_document_chunks_count(doc["id"]))
            with ui.column().classes("items-center").style("flex: 1"):
                ui.label(str(chunk_count)).classes("text-h6")
                ui.label("chunks").classes("text-caption")

            with ui.column().classes("items-end gap-1").style("flex: 1"):
                ingested = doc.get("ingested_at")
                if ingested:
                    ui.label(f"📅 {str(ingested)[:10]}").classes("text-caption")
                with ui.row().classes("gap-1"):
                    ui.button("✏️", on_click=lambda d=doc: _edit_title_dialog(db, d, refresh)).props(
                        "flat dense"
                    ).tooltip("Edit title")
                    ui.button(
                        "📖", on_click=lambda d=doc: _chunk_viewer_dialog(db, d, proc_filter)
                    ).props("flat dense").tooltip("View chunks")
                    ui.button("🗑️", on_click=lambda d=doc: _delete_dialog(db, d, refresh)).props(
                        "flat dense color=negative"
                    ).tooltip("Delete document")


async def _edit_title_dialog(db, doc: dict, refresh) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Edit title").classes("text-bold")
        title_input = ui.input(value=doc["title"]).classes("w-full")

        async def save() -> None:
            new_title = title_input.value.strip()
            if new_title and new_title != doc["title"]:
                ok = await run.io_bound(lambda: db.update_document_title(doc["id"], new_title))
                if ok:
                    ui.notify("Title updated", type="positive")
                    dialog.close()
                    await refresh()
                    return
                ui.notify("Failed to update title", type="negative")
            else:
                dialog.close()

        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save).props("color=primary")
    dialog.open()


async def _delete_dialog(db, doc: dict, refresh) -> None:
    with ui.dialog() as dialog, ui.card():
        ui.label(f"⛔ Permanently delete “{doc['title']}”?").classes("text-bold")
        ui.label("This cannot be undone.").classes("text-caption")

        async def do_delete() -> None:
            ok = await run.io_bound(lambda: db.delete_document(doc["id"]))
            dialog.close()
            if ok:
                ui.notify(f"Deleted: {doc['title']}", type="positive")
                await refresh()
            else:
                ui.notify("Failed to delete document. Check server logs.", type="negative")

        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("DELETE PERMANENTLY", on_click=do_delete).props("color=negative")
    dialog.open()


async def _chunk_viewer_dialog(db, doc: dict, proc_filter: Optional[str]) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full").style("max-width: 900px"):
        ui.label(doc["title"]).classes("text-h6")
        chunks = await run.io_bound(
            lambda: db.get_document_chunks(doc["id"], procedure_id=proc_filter)
        )
        if not chunks:
            ui.label("No chunks found.")
        else:
            label = f"{len(chunks)} chunk(s)"
            if proc_filter:
                label += f" matching {proc_filter}"
            ui.label(label).classes("text-caption")
            for chunk in chunks:
                idx = chunk.get("chunk_index", 0)
                with ui.expansion(f"Chunk {idx}", value=(idx == 0)).classes("w-full"):
                    ui.label(chunk.get("content", "")).style("white-space: pre-wrap")
                    meta = chunk.get("chunk_metadata") or {}
                    proc_ids = meta.get("procedure_ids", [])
                    if proc_ids:
                        ui.label(f"Procedures: {', '.join(proc_ids)}").classes("text-caption")
        with ui.row().classes("justify-end w-full"):
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()
