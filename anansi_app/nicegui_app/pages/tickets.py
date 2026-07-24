"""Tickets page (Task 8) — unified, READ-ONLY view across both backends.

Lists tickets from ``internal_tickets`` (🗂 Internal) and Jira-backed
``escalation_mappings`` (🎫 Jira) via ``SupabaseReader.list_tickets`` — no live
Jira fetch on render; statuses are "as of last sync". Responding to a ticket
still happens in the Telegram escalation group; this page only surfaces state
and deep-links out. There are deliberately NO reply / close / edit / mutate
controls anywhere on this page.

Deep-linking to a customer conversation: chat.py has no URL-driven selection
yet, so we take the documented fall-back (option b) — a plain link to
``/conversations`` plus a caption spelling out the customer identity / chat_id /
topic to look for. Revisit if chat.py gains query-param selection.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from nicegui import run, ui

from nicegui_app.services_access import get_reader

# Which statuses are checked by default (open + in-progress), per the spec.
_DEFAULT_STATUSES = {"open": True, "in_progress": True, "done": False}
_STATUS_LABELS = {"open": "open", "in_progress": "in-progress", "done": "done"}
_STATUS_COLORS = {"open": "green", "in_progress": "orange", "done": "grey"}
_PAGE_SIZE = 25

_COMMENT_SOURCE_LABELS = {
    "customer": "👤 Customer",
    "staff": "🧑‍💼 Staff",
    "notify": "🔔 Notify",
    "jira": "🎫 Jira",
    "escalation": "🧑‍💼 Staff",
}


def _build_telegram_msg_link(
    escalation_chat_id: Optional[str], message_id: Optional[int]
) -> Optional[str]:
    """Build a t.me deep-link to a message in the escalation support group.

    Replicated locally (small pure function) rather than importing the private
    helper from chat_orchestrator's escalation_service.py.
    """
    if not escalation_chat_id or not message_id:
        return None
    chat_str = str(escalation_chat_id)
    if not chat_str.startswith("-100"):
        return None
    group_id = chat_str[4:]
    if not group_id.isdigit():
        return None
    return f"https://t.me/c/{group_id}/{message_id}"


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def _format_time_ago(value: Any) -> str:
    dt = _parse_dt(value)
    if dt is None:
        return "—"
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


def _mask_customer(ticket: dict) -> str:
    """Best-effort, privacy-preserving customer label (no full email/id shown)."""
    username = ticket.get("customer_username")
    if username:
        return username if str(username).startswith("@") else f"@{username}"
    email = ticket.get("customer_email")
    if email and "@" in str(email):
        local, _, domain = str(email).partition("@")
        head = local[0] if local else "?"
        return f"{head}***@{domain}"
    chat_id = ticket.get("customer_chat_id")
    if chat_id:
        tail = str(chat_id)[-4:]
        return f"user •••{tail}"
    return "unknown"


def _backend_chip(backend: str) -> str:
    return "🎫 Jira" if backend == "jira" else "🗂 Internal"


def _status_badge(status: str) -> None:
    label = _STATUS_LABELS.get(status, status or "—")
    color = _STATUS_COLORS.get(status, "blue-grey")
    ui.badge(label, color=color).props("outline").tooltip("Status as of last sync")


def _escalation_chat_id() -> str:
    return os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")


async def render(user: dict[str, Any], ref: Optional[str] = None) -> None:
    db = get_reader()

    with ui.row().classes("items-center justify-between w-full"):
        ui.label("🎫 Tickets").classes("text-h5")
        ui.label("Read-only · status as of last sync").classes("text-caption text-grey")

    if not await run.io_bound(db.is_configured):
        ui.label(
            "⚠️ Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY."
        ).classes("text-negative")
        return

    # /tickets/{ref} — dedicated single-ticket detail view.
    if ref:
        await _render_single_detail(db, ref)
        return

    state: dict[str, Any] = {
        "statuses": dict(_DEFAULT_STATUSES),
        "backend": "all",
        "org": "",
        "search": "",
        "offset": 0,
    }

    # ── filter controls (all read-only) ───────────────────────────────────────
    with ui.row().classes("items-center gap-4 w-full flex-wrap"):
        ui.label("Status:").classes("text-bold")
        for key in ("open", "in_progress", "done"):
            ui.checkbox(
                _STATUS_LABELS[key], value=state["statuses"][key]
            ).bind_value(state["statuses"], key).on_value_change(lambda: _reload())
        ui.select(
            {"all": "All backends", "jira": "🎫 Jira", "internal": "🗂 Internal"},
            value="all",
        ).bind_value(state, "backend").on_value_change(lambda: _reload())

    with ui.row().classes("items-center gap-4 w-full flex-wrap"):
        org_input = ui.input("🏢 Org filter (id or #hashtag)").classes("w-64")
        org_input.on("keydown.enter", lambda: _apply(org_input, "org"))
        search_input = ui.input("🔍 Search ref / summary / customer").classes("w-72")
        search_input.on("keydown.enter", lambda: _apply(search_input, "search"))

    pager = ui.row().classes("items-center gap-2 w-full")
    list_container = ui.column().classes("w-full gap-2")

    async def _apply(widget, field: str) -> None:
        state[field] = widget.value or ""
        state["offset"] = 0
        await _reload()

    async def _reload() -> None:
        list_container.clear()
        pager.clear()
        status_filter = [k for k, v in state["statuses"].items() if v]
        backend_filter = None if state["backend"] == "all" else state["backend"]
        tickets = await run.io_bound(
            lambda: db.list_tickets(
                status_filter=status_filter,
                org_filter=state["org"] or None,
                backend_filter=backend_filter,
                search=state["search"] or None,
                limit=_PAGE_SIZE,
                offset=state["offset"],
            )
        )
        with list_container:
            if not tickets:
                ui.label("No tickets match the current filters.").classes("text-caption")
            else:
                for ticket in tickets:
                    _ticket_row(db, ticket)

        with pager:
            page_no = state["offset"] // _PAGE_SIZE + 1
            ui.button("← Prev", on_click=_prev).props("flat dense").set_enabled(
                state["offset"] > 0
            )
            ui.label(f"Page {page_no}").classes("text-caption")
            # A full page implies there may be more; not exact, but cheap.
            ui.button("Next →", on_click=_next).props("flat dense").set_enabled(
                len(tickets) == _PAGE_SIZE
            )

    async def _prev() -> None:
        state["offset"] = max(0, state["offset"] - _PAGE_SIZE)
        await _reload()

    async def _next() -> None:
        state["offset"] += _PAGE_SIZE
        await _reload()

    await _reload()


def _ticket_row(db, ticket: dict) -> None:
    ref = ticket.get("ticket_ref") or "—"
    summary = ticket.get("summary") or ""
    summary_short = summary if len(summary) <= 70 else summary[:69] + "…"
    header = (
        f"{_backend_chip(ticket.get('backend'))}  ·  {ref}  ·  {summary_short}"
        f"  ·  {_format_time_ago(ticket.get('created_at'))}"
        f"  ·  💬 {ticket.get('comment_count', 0)}"
    )

    exp = ui.expansion(header).classes("w-full").style(
        "border: 1px solid #e2e8f0; border-radius: 6px;"
    )
    loaded = {"done": False}
    with exp:
        # Summary chips row (status badge + org/grid/customer), always visible.
        with ui.row().classes("items-center gap-3 flex-wrap"):
            _status_badge(ticket.get("status"))
            org = ticket.get("org_hashtag") or (
                f"org {ticket.get('organization_id')}"
                if ticket.get("organization_id") is not None
                else "—"
            )
            ui.label(f"🏢 {org}").classes("text-caption")
            if ticket.get("grid_name"):
                ui.label(f"⚡ {ticket['grid_name']}").classes("text-caption")
            ui.label(f"👤 {_mask_customer(ticket)}").classes("text-caption")
            if ticket.get("reason"):
                ui.label(f"📌 {ticket['reason']}").classes("text-caption")
        body = ui.column().classes("w-full gap-2")

    async def _on_toggle() -> None:
        # Lazy-load the detail the first time the row is expanded. Keying off the
        # element's own value (rather than the raw event payload) is robust across
        # NiceGUI/Quasar event shapes.
        if not exp.value or loaded["done"]:
            return
        loaded["done"] = True
        detail = await run.io_bound(lambda: db.get_ticket_detail(ticket.get("ticket_ref")))
        _render_detail_body(body, detail or ticket)

    exp.on_value_change(_on_toggle)


def _render_detail_body(container, detail: dict) -> None:
    container.clear()
    with container:
        # Deep-links (read-only). Conversation link is the documented fall-back:
        # /conversations + a caption naming the customer to look for.
        with ui.row().classes("items-center gap-4 flex-wrap"):
            ui.link("💬 View in Chats", "/conversations")
            hints = []
            if detail.get("customer_chat_id"):
                hints.append(f"chat_id {detail['customer_chat_id']}")
            if detail.get("customer_topic_id"):
                hints.append(f"topic {detail['customer_topic_id']}")
            if hints:
                ui.label("(look for " + ", ".join(hints) + ")").classes(
                    "text-caption text-grey"
                )
            tme = _build_telegram_msg_link(
                _escalation_chat_id(), detail.get("escalation_message_id")
            )
            if tme:
                ui.link("↗ Escalation message (Telegram)", tme, new_tab=True)

        description = detail.get("description") or detail.get("summary") or ""
        if description:
            ui.label("Description").classes("text-bold q-mt-sm")
            ui.label(description).classes("text-body2").style("white-space: pre-wrap")

        ui.label("Comment timeline (read-only)").classes("text-bold q-mt-sm")
        comments = detail.get("comments") or []
        if not comments:
            ui.label("No comments recorded for this ticket.").classes("text-caption")
            return
        for comment in comments:
            _comment_card(comment)


def _comment_card(comment: dict) -> None:
    source = comment.get("source") or "staff"
    label = _COMMENT_SOURCE_LABELS.get(source, source)
    visibility = "public" if comment.get("is_public", True) else "internal"
    with ui.card().classes("w-full q-pa-sm").style("border: 1px solid #e2e8f0"):
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label(label).classes("text-caption text-bold")
            if comment.get("author"):
                ui.label(str(comment["author"])).classes("text-caption text-grey")
            ui.space()
            ui.label(visibility).classes("text-caption text-grey")
            ui.label(_format_time_ago(comment.get("created_at"))).classes(
                "text-caption text-grey"
            )
        ui.label(comment.get("body") or "").classes("text-body2").style(
            "white-space: pre-wrap"
        )


async def _render_single_detail(db, ref: str) -> None:
    ui.link("← Back to Tickets", "/tickets").classes("q-mb-sm")
    detail = await run.io_bound(lambda: db.get_ticket_detail(ref))
    if detail is None:
        ui.label(f"Ticket '{ref}' was not found.").classes("text-negative")
        return
    summary = detail.get("summary") or ref
    with ui.row().classes("items-center gap-3 flex-wrap"):
        ui.label(f"{_backend_chip(detail.get('backend'))}  {ref}").classes("text-h6")
        _status_badge(detail.get("status"))
    ui.label(summary).classes("text-subtitle1")
    with ui.row().classes("items-center gap-3 flex-wrap"):
        org = detail.get("org_hashtag") or (
            f"org {detail.get('organization_id')}"
            if detail.get("organization_id") is not None
            else "—"
        )
        ui.label(f"🏢 {org}").classes("text-caption")
        if detail.get("grid_name"):
            ui.label(f"⚡ {detail['grid_name']}").classes("text-caption")
        ui.label(f"👤 {_mask_customer(detail)}").classes("text-caption")
        ui.label(f"🕒 {_format_time_ago(detail.get('created_at'))}").classes("text-caption")
    body = ui.column().classes("w-full gap-2 q-mt-sm")
    _render_detail_body(body, detail)
