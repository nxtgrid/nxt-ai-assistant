"""Chats page (NiceGUI port of app.py `_render_chat_viewer_page`).

Left column: date range, search, conversation list (groups + DMs). Right column:
period stats + the conversation HTML (built by the shared
``rendering.conversation_html`` module, identical to the Streamlit output).

The broadcast modal is deferred to a later migration session; the button opens a
placeholder notice for now.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from nicegui import run, ui
from rendering import conversation_html as chtml

from nicegui_app.services_access import get_reader


def _format_time_ago(dt: datetime) -> str:
    now = datetime.utcnow()
    if dt.tzinfo is not None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        dt = dt.replace(tzinfo=None)
    delta = now - dt
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() / 60)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() / 3600)}h ago"
    if delta < timedelta(days=7):
        return f"{delta.days}d ago"
    return dt.strftime("%b %d")


async def render(user: dict[str, Any]) -> None:
    user_email = user.get("email", "unknown")
    db = get_reader()

    async def _open_broadcast() -> None:
        from nicegui_app.pages import broadcast

        await broadcast.open_dialog(user)

    with ui.row().classes("items-center justify-between w-full"):
        ui.label("💬 Chats").classes("text-h5")
        ui.button("📢 Broadcast", on_click=_open_broadcast).props("outline")

    if not await run.io_bound(db.is_configured):
        ui.label("⚠️ Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY.").classes(
            "text-negative"
        )
        return

    state: dict[str, Any] = {"days_back": 2, "search": "", "selected": None}

    with ui.row().classes("w-full no-wrap gap-4"):
        left = ui.column().classes("gap-2").style("flex: 1; min-width: 260px")
        right = ui.column().classes("gap-2").style("flex: 3")

    with left:
        ui.label("Date Range").classes("text-bold")
        ui.label().bind_text_from(state, "days_back", lambda d: f"Days to look back: {d}")
        ui.slider(min=1, max=31, value=2).bind_value(state, "days_back").on(
            "change", lambda: _reload()
        )
        search_input = ui.input("🔍 Search conversations").classes("w-full")
        search_input.on("keydown.enter", lambda: _apply_search(search_input.value))
        list_container = ui.column().classes("w-full gap-1")

    async def _apply_search(term: str) -> None:
        state["search"] = term or ""
        await _reload()

    async def _reload() -> None:
        list_container.clear()
        right.clear()
        days_back = state["days_back"]
        search = state["search"].strip()

        contexts = await run.io_bound(
            lambda: db.get_chat_contexts(user_email=user_email, days_back=days_back)
        )
        if search:
            content_matches = await run.io_bound(
                lambda: db.search_conversations_by_content(
                    search_term=search, days_back=days_back, user_email=user_email
                )
            )
            search_lower = search.lower()
            title_matches = [c for c in contexts if search_lower in c["display_name"].lower()]
            combined: dict[tuple, dict] = {}
            for ctx in title_matches + content_matches:
                combined[(ctx["chat_id"], ctx.get("group_id"))] = ctx
            contexts = sorted(combined.values(), key=lambda x: x["last_message"], reverse=True)

        groups = [c for c in contexts if c["is_group"]]
        dms = [c for c in contexts if not c["is_group"]]
        for lst in (groups, dms):
            lst.sort(key=lambda c: c.get("last_message", ""), reverse=True)
            lst.sort(key=lambda c: not c.get("is_escalated", False))

        # Stats
        start_date = datetime.utcnow() - timedelta(days=days_back)
        range_stats = await run.io_bound(
            lambda: db.get_period_stats(
                user_email=user_email, start_date=start_date, end_date=datetime.utcnow()
            )
        )
        stats = {
            "days_back": days_back,
            "total_conversations": len(contexts),
            "groups": len(groups),
            "direct_messages": len(dms),
            "total_messages": range_stats.get("messages", 0),
            "unique_users": range_stats.get("users", 0),
            "input_tokens": range_stats.get("input_tokens", 0),
            "output_tokens": range_stats.get("output_tokens", 0),
            "median_response_time": range_stats.get("median_response_time"),
        }

        with list_container:
            if not contexts:
                if search:
                    ui.label(f"No conversations matching '{search}'").classes("text-caption")
                else:
                    ui.label(f"No chat activity in the last {days_back} days").classes(
                        "text-warning"
                    )
            else:
                if groups:
                    ui.label(f"📊 Groups ({len(groups)})").classes("text-bold q-mt-sm")
                    for ctx in groups:
                        _chat_button(ctx, state, _reload)
                if dms:
                    ui.label(f"Direct Messages ({len(dms)})").classes("text-bold q-mt-sm")
                    for ctx in dms:
                        _chat_button(ctx, state, _reload)

        with right:
            _render_stats(stats)
            selected = state["selected"]
            if selected:
                await _render_conversation(db, selected, days_back)

    await _reload()


def _chat_button(ctx: dict, state: dict, reload_cb) -> None:
    last_msg = datetime.fromisoformat(ctx["last_message"])
    time_ago = _format_time_ago(last_msg)
    name = ctx["display_name"]
    if len(name) > 30:
        name = name[:28] + "…"
    tag = "🚨 " if ctx.get("is_escalated") else ""
    label = f"{tag}{name} · {time_ago}"

    selected = state.get("selected")
    is_selected = bool(
        selected
        and selected["chat_id"] == ctx["chat_id"]
        and selected.get("group_id") == ctx.get("group_id")
    )

    async def select() -> None:
        state["selected"] = ctx
        await reload_cb()

    # Selected row is a solid filled button (like the sidebar's active nav item);
    # unselected rows are outlined. "outline" + "color=primary" alone was too
    # subtle a difference to read as "selected" against the page background.
    props = "align=left no-caps color=primary" if is_selected else "outline align=left no-caps"
    btn = ui.button(label, on_click=select).props(props).classes("w-full")
    btn.style("border-radius: 6px; margin-bottom: 2px; justify-content: flex-start")
    if ctx.get("is_escalated"):
        btn.style("border: 2px solid #e74c3c")


def _render_stats(stats: dict) -> None:
    ui.label(f"Chat Stats ({stats['days_back']} days)").classes("text-h6")
    with ui.row().classes("w-full gap-4 flex-wrap"):
        _stat("Conversations", stats["total_conversations"])
        _stat("Groups", stats["groups"])
        _stat("Direct Messages", stats["direct_messages"])
        _stat("Total Messages", stats["total_messages"])
        _stat("Unique Users", stats["unique_users"])
        _stat("Input Tokens", f"{stats['input_tokens']:,}")
        _stat("Output Tokens", f"{stats['output_tokens']:,}")
        median = stats.get("median_response_time")
        _stat("Median Response", f"{median:.1f}s" if median is not None else "--")
    ui.separator()


def _stat(label: str, value: Any) -> None:
    with ui.column().classes("items-center gap-0").style("min-width: 90px"):
        ui.label(str(value)).classes("text-h6")
        ui.label(label).classes("text-caption")


async def _render_conversation(db, context: dict, days_back: int) -> None:
    ui.label(f"Messages: {context['message_count']}").classes("text-bold")

    date_from = datetime.utcnow() - timedelta(days=days_back)
    messages = await run.io_bound(
        lambda: db.get_conversation_messages(
            chat_id=context["chat_id"],
            group_id=context.get("group_id"),
            telegram_chat_id=(context.get("telegram_chat_id") if context.get("is_group") else None),
            telegram_topic_id=context.get("telegram_topic_id"),
            date_from=date_from,
            date_to=datetime.utcnow(),
            limit=500,
        )
    )
    if not messages:
        ui.label("No messages found in this date range").classes("text-caption")
        return

    show_internal = {"value": False}
    caption = ui.label().classes("text-caption")
    html_container = ui.column().classes("w-full")

    # Resolve feedback user names once (batch, off the event loop).
    cache = await _resolve_feedback_names(db, messages)

    def rebuild() -> None:
        html_container.clear()
        visible = sum(1 for m in messages if not chtml.is_internal_message(m))
        if show_internal["value"]:
            caption.text = f"Showing {len(messages)} messages ({visible} conversation)"
        else:
            caption.text = f"Showing {visible} messages"
        inner = chtml.build_conversation_html(messages, show_internal["value"], cache)
        with html_container:
            ui.html(
                chtml.MESSAGES_CONTAINER_CSS
                + '<div class="messages-scroll-container" id="msg-container">'
                + inner
                + "</div>"
            )
            ui.run_javascript(
                "var c=document.getElementById('msg-container'); if(c)c.scrollTop=c.scrollHeight;"
            )

    def toggle(e) -> None:
        show_internal["value"] = e.value
        rebuild()

    ui.switch("Show internal messages to LLM", value=False, on_change=toggle)
    rebuild()


async def _resolve_feedback_names(db, messages: list[dict]) -> dict[str, str]:
    """Batch-look up telegram_user_id -> name for feedback tooltips."""
    user_ids: set[str] = set()
    for msg in messages:
        metadata = msg.get("metadata") or {}
        feedback = metadata.get("feedback", [])
        if isinstance(feedback, dict):
            feedback = [feedback]
        for fb in feedback or []:
            if not fb.get("user_name") and fb.get("telegram_user_id"):
                user_ids.add(fb["telegram_user_id"])
    if not user_ids:
        return {}
    try:
        result = await run.io_bound(
            lambda: asyncio.run(db._batch_lookup_user_names(list(user_ids)))
        )
        return dict(result) if result else {}
    except Exception:
        return {}
