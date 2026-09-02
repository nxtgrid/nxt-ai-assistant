"""Read-only canonical Tickets page.

Lists every Anansi ticket from the database's ``ticket_list_view`` projection;
there is no live Jira query or capped Python merge. Responding to a ticket
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

from nicegui_app.page_context import PageContext, set_page_context
from nicegui_app.services_access import get_reader
from shared.config import flag_registry as fr

_STATUS_LABELS = {"open": "open", "in_progress": "in-progress", "done": "done"}
_STATUS_COLORS = {"open": "green", "in_progress": "orange", "done": "grey"}
_PAGE_SIZE = 25

# Default view: open + in-progress only, so a closed backlog doesn't bury
# what actually needs attention. "All statuses" still shows everything.
_OPEN_STATUSES = ["open", "in_progress"]
_DEFAULT_STATUS_FILTER = "open_active"


def _status_filter_value(selected: str) -> "list[str] | str | None":
    """Map the status dropdown's selection to a list_ticket_page ``status`` arg."""
    if selected == "all":
        return None
    if selected == _DEFAULT_STATUS_FILTER:
        return _OPEN_STATUSES
    return selected


# Same three vars JiraTicketBackend.has_credentials() gates on -- kept in
# sync with that all-or-nothing check rather than re-deriving it here,
# since anansi_app can't import chat_orchestrator's JiraTicketBackend.
_JIRA_CREDENTIAL_ENV_VARS = ("JIRA_BASE_URL", "JIRA_USERNAME", "JIRA_API_TOKEN")


def _default_backend_filter() -> str:
    """Best-effort default for the backend filter dropdown.

    Mirrors TicketService.resolve_backend()'s TICKET_BACKEND_OVERRIDE
    handling so the page opens already scoped to whichever backend is
    actually in use -- without a live Jira health probe (this is just a
    page-load default; the dropdown can always be changed). "auto" falls
    back to checking whether Jira looks configured rather than a real
    ``is_available()`` call, since that's an async network probe this
    synchronous page-load default has no business making.
    """
    override = (fr.get("TICKET_BACKEND_OVERRIDE") or "auto").strip().lower()
    if override == "internal":
        return "all"
    if override == "jira":
        return "jira"
    # "auto" (and any unrecognized value).
    if all(os.getenv(var) for var in _JIRA_CREDENTIAL_ENV_VARS):
        return "jira"
    return "all"

_COMMENT_SOURCE_LABELS = {
    "customer": "👤 Customer",
    "staff": "🧑‍💼 Staff",
    "notify": "🔔 Notify",
    "jira": "🎫 Jira",
    "escalation": "🧑‍💼 Staff",
}

_DELIVERY_LABELS = {
    "escalation": "Escalation message",
    "notification": "Notification message",
    "update": "Update message",
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


def _origin_label(created_via: str) -> str:
    return {
        "escalation": "🆘 Customer escalation",
        "notification": "🔔 Operational notification",
        "adopted": "↗ Adopted",
        "legacy": "◷ Legacy",
    }.get(created_via, "Unknown origin")


def _escalation_context_label(has_escalation: bool) -> str:
    return "🆘 Escalation" if has_escalation else "🔔 No escalation"


def _status_badge(status: str) -> None:
    label = _STATUS_LABELS.get(status, status or "—")
    color = _STATUS_COLORS.get(status, "blue-grey")
    ui.badge(label, color=color).props("outline").tooltip("Status as of last sync")


def _escalation_chat_id() -> str:
    return os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")


# ── chat page context ─────────────────────────────────────────────────────────
# What the chat widget attaches when the operator asks about this page. Summary
# only: identifiers plus a handful of lines, and a hint naming how to go deeper.
# The bot already has ticket tools -- shipping full rows here would spend tokens
# on data it can fetch on demand.
_MAX_LISTED_REFS = 10


def build_ticket_page_context(detail: dict[str, Any]) -> PageContext:
    """One ticket, as chat context."""
    ref = detail.get("ticket_ref") or "—"
    identifiers: dict[str, str] = {"ticket_ref": str(ref)}
    if detail.get("grid_name"):
        identifiers["grid_name"] = str(detail["grid_name"])
    if detail.get("organization_id") is not None:
        identifiers["organization_id"] = str(detail["organization_id"])

    lines = [
        f"Summary: {detail.get('summary') or '—'}",
        f"Status: {detail.get('status') or '—'}",
        f"Backend: {detail.get('backend') or '—'}",
        f"Origin: {_origin_label(detail.get('created_via') or '')}",
        f"Grid: {detail.get('grid_name') or '—'}",
        f"Organization: {detail.get('org_hashtag') or detail.get('organization_id') or '—'}",
        # Masked, not raw: this string is sent to the model and saved to
        # chat_messages, so it gets the same treatment the page gives it.
        f"Customer: {_mask_customer(detail)}",
        f"Opened: {_format_time_ago(detail.get('created_at'))}",
    ]
    if detail.get("reason"):
        lines.append(f"Reason: {detail['reason']}")
    if detail.get("root_cause_kind"):
        lines.append(f"Root cause: {detail['root_cause_kind']}")
    if (detail.get("affected_count") or 0) > 1:
        lines.append(f"Correlated tickets affected: {detail['affected_count']}")

    return PageContext(
        kind="ticket",
        label=f"Ticket {ref}",
        identifiers=identifiers,
        summary_lines=lines,
        detail_hint=(
            "Use the ticket tools with this ticket_ref for comments, attachments "
            "and correlation detail."
        ),
    )


def build_ticket_list_page_context(
    tickets: list[dict[str, Any]], total: int, status: str
) -> PageContext:
    """The visible page of the ticket list, as chat context."""
    refs = [str(t.get("ticket_ref") or "—") for t in tickets[:_MAX_LISTED_REFS]]
    lines = [
        f"Status filter: {status}",
        f"{total} tickets match; {len(tickets)} shown on this page.",
    ]
    if refs:
        lines.append("Refs on screen: " + ", ".join(refs))
        if len(tickets) > len(refs):
            lines.append(f"(first {len(refs)} of {len(tickets)} listed)")

    return PageContext(
        kind="ticket_list",
        label=f"Tickets ({total})",
        summary_lines=lines,
        detail_hint=(
            "Ask about any ticket_ref above -- the ticket tools can fetch its "
            "full detail on demand."
        ),
    )


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

    # /tickets/{ref} — dedicated canonical ticket detail view. The route
    # parameter is the canonical ticket id; references are display-only.
    if ref:
        await _render_single_detail(db, ref)
        return

    default_backend = _default_backend_filter()
    state: dict[str, Any] = {
        "status": _DEFAULT_STATUS_FILTER,
        "backend": default_backend,
        "created_via": "all",
        "has_escalation": "all",
        "search": "",
        "page": 1,
    }

    # ── filter controls (all read-only) ───────────────────────────────────────
    with ui.row().classes("items-center gap-4 w-full flex-wrap"):
        ui.select(
            {
                _DEFAULT_STATUS_FILTER: "Open (open + in-progress)",
                "all": "All statuses",
                **_STATUS_LABELS,
            },
            value=_DEFAULT_STATUS_FILTER,
        ).bind_value(state, "status").on_value_change(lambda: _reload())
        ui.select(
            {"all": "All backends", "jira": "🎫 Jira", "internal": "🗂 Internal"},
            value=default_backend,
        ).bind_value(state, "backend").on_value_change(lambda: _reload())
        ui.select(
            {
                "all": "All origins",
                "escalation": "🆘 Customer escalation",
                "notification": "🔔 Operational notification",
                "adopted": "↗ Adopted",
                "legacy": "◷ Legacy",
            },
            value="all",
        ).bind_value(state, "created_via").on_value_change(lambda: _reload())
        ui.select(
            {"all": "All ticket context", "yes": "🆘 Has escalation", "no": "No escalation"},
            value="all",
        ).bind_value(state, "has_escalation").on_value_change(lambda: _reload())

    with ui.row().classes("items-center gap-4 w-full flex-wrap"):
        search_input = ui.input("🔍 Search ref / summary / customer").classes("w-72")
        search_input.on("keydown.enter", lambda: _apply(search_input, "search"))

    pager = ui.row().classes("items-center gap-2 w-full")
    list_container = ui.column().classes("w-full gap-2")

    async def _apply(widget, field: str) -> None:
        state[field] = widget.value or ""
        state["page"] = 1
        await _reload()

    async def _reload() -> None:
        list_container.clear()
        pager.clear()
        result = await run.io_bound(
            lambda: db.list_ticket_page(
                page=state["page"],
                page_size=_PAGE_SIZE,
                status=_status_filter_value(state["status"]),
                backend=None if state["backend"] == "all" else state["backend"],
                created_via=(
                    None if state["created_via"] == "all" else state["created_via"]
                ),
                has_escalation=(
                    None
                    if state["has_escalation"] == "all"
                    else state["has_escalation"] == "yes"
                ),
                search=state["search"] or None,
            )
        )
        tickets = result.items
        set_page_context(
            build_ticket_list_page_context(tickets, result.total, state["status"])
        )
        with list_container:
            if not tickets:
                ui.label("No tickets match the current filters.").classes("text-caption")
            else:
                for ticket in tickets:
                    _ticket_row(db, ticket)

        with pager:
            ui.button("← Prev", on_click=_prev).props("flat dense").set_enabled(
                state["page"] > 1
            )
            ui.label(f"Page {state['page']} · {result.total} tickets").classes("text-caption")
            ui.button("Next →", on_click=_next).props("flat dense").set_enabled(
                state["page"] * _PAGE_SIZE < result.total
            )

    async def _prev() -> None:
        state["page"] = max(1, state["page"] - 1)
        await _reload()

    async def _next() -> None:
        state["page"] += 1
        await _reload()

    await _reload()


def _ticket_row(db, ticket: dict) -> None:
    ref = ticket.get("ticket_ref") or "—"
    summary = ticket.get("summary") or ""
    summary_short = summary if len(summary) <= 70 else summary[:69] + "…"
    affected_count = ticket.get("affected_count") or 0
    correlation_suffix = f"  ·  🔗 {affected_count} affected" if affected_count > 1 else ""
    escalated_prefix = "🔴 " if ticket.get("escalated") else ""
    header = (
        f"{escalated_prefix}{_backend_chip(ticket.get('backend'))}  ·  {ref}  ·  {summary_short}"
        f"  ·  {_format_time_ago(ticket.get('created_at'))}"
        f"  ·  💬 {ticket.get('activity_count', 0)}"
        f"{correlation_suffix}"
    )

    exp = ui.expansion(header).classes("w-full").style(
        "border: 1px solid #e2e8f0; border-radius: 6px;"
    )
    loaded = {"done": False}
    with exp:
        # Summary chips row (status badge + org/grid/customer), always visible.
        with ui.row().classes("items-center gap-3 flex-wrap"):
            _status_badge(ticket.get("status"))
            ui.label(_origin_label(ticket.get("created_via") or "")).classes("text-caption")
            ui.label(_escalation_context_label(bool(ticket.get("has_escalation")))).classes(
                "text-caption"
            )
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
            if ticket.get("root_cause_kind"):
                ui.label(f"🧭 root cause: {ticket['root_cause_kind']}").classes("text-caption")
        body = ui.column().classes("w-full gap-2")

    async def _on_toggle() -> None:
        # Lazy-load the detail the first time the row is expanded. Keying off the
        # element's own value (rather than the raw event payload) is robust across
        # NiceGUI/Quasar event shapes.
        if not exp.value or loaded["done"]:
            return
        loaded["done"] = True
        ticket_id = ticket.get("id")
        detail = await run.io_bound(
            lambda: db.get_canonical_ticket_detail(ticket_id) if ticket_id else None
        )
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

        deliveries = detail.get("deliveries") or []
        if deliveries:
            ui.label("Recorded message deliveries").classes("text-bold q-mt-sm")
            with ui.column().classes("gap-1"):
                for delivery in deliveries:
                    label = _DELIVERY_LABELS.get(delivery.get("purpose"), "Message delivery")
                    timestamp = _format_time_ago(delivery.get("sent_at"))
                    if delivery.get("message_url"):
                        ui.link(f"↗ {label} · {timestamp}", delivery["message_url"], new_tab=True)
                    else:
                        ui.label(f"{label} · {timestamp}").classes("text-caption")

        _correlation_section(detail)

        attachments = detail.get("attachments") or []
        if attachments:
            ui.label(f"Attachments ({len(attachments)})").classes("text-bold q-mt-sm")
            with ui.column().classes("gap-1"):
                for attachment in attachments:
                    _attachment_card(attachment)

        ui.label("Comment timeline (read-only)").classes("text-bold q-mt-sm")
        comments = detail.get("comments") or []
        if not comments:
            ui.label("No comments recorded for this ticket.").classes("text-caption")
            return
        for comment in comments:
            _comment_card(comment)


_DECISION_LABELS = {"new": "🆕 New", "amend": "✏️ Amend", "duplicate": "🔁 Duplicate"}
_DECIDED_BY_LABELS = {
    "replay": "replay (dedup)",
    "flag_off": "correlation disabled",
    "no_candidates": "no open candidates",
    "signature": "exact signature match",
    "llm": "LLM decision",
    "fallback": "fallback (error/timeout)",
}


def _correlation_section(detail: dict) -> None:
    """Read-only surfacing of alert-correlation state (Task 11) -- affected
    components, occurrence count, root cause, and the decision audit trail
    from ticket_correlation_events. Renders nothing when the correlator never
    touched this ticket (filed by a human, or before this feature existed)."""
    correlation = detail.get("correlation")
    events = detail.get("correlation_events") or []
    if not correlation and not events:
        return

    ui.label("Alert correlation").classes("text-bold q-mt-sm")
    with ui.row().classes("items-center gap-3 flex-wrap"):
        if correlation:
            ui.label(f"🔁 Occurrences: {correlation.get('occurrence_count') or 1}").classes(
                "text-caption"
            )
            if correlation.get("root_cause_kind"):
                ui.label(f"🧭 Root cause: {correlation['root_cause_kind']}").classes(
                    "text-caption"
                )
            if correlation.get("escalated_at"):
                ui.label(f"🔴 Escalated {_format_time_ago(correlation['escalated_at'])}").classes(
                    "text-caption"
                )

    affected_keys = (correlation or {}).get("affected_keys") or []
    if affected_keys:
        with ui.column().classes("gap-1 q-mt-xs"):
            ui.label(f"Affected components ({len(affected_keys)})").classes(
                "text-caption text-bold"
            )
            for entry in affected_keys:
                label = entry.get("label") or f"{entry.get('kind', '')} {entry.get('key', '')}"
                count = entry.get("count", 1)
                ui.label(
                    f"• {label} — last seen {_format_time_ago(entry.get('last_seen'))} ({count}×)"
                ).classes("text-caption")

    if events:
        with ui.column().classes("gap-1 q-mt-xs"):
            ui.label("Decision history").classes("text-caption text-bold")
            for event in events:
                decision_label = _DECISION_LABELS.get(
                    event.get("decision"), event.get("decision") or "—"
                )
                decided_by = _DECIDED_BY_LABELS.get(
                    event.get("decided_by"), event.get("decided_by") or ""
                )
                confidence = event.get("confidence")
                confidence_text = f" ({confidence:.0%})" if isinstance(confidence, (int, float)) else ""
                reason = event.get("reason") or ""
                ui.label(
                    f"{_format_time_ago(event.get('created_at'))} — {decision_label} "
                    f"via {decided_by}{confidence_text}"
                    + (f": {reason}" if reason else "")
                ).classes("text-caption")


def _attachment_card(attachment: dict) -> None:
    with ui.card().classes("w-full q-pa-sm").style("border: 1px solid #e2e8f0"):
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label(f"📎 {attachment.get('media_type', 'file')}").classes("text-caption text-bold")
            if attachment.get("size_bytes"):
                kb = attachment["size_bytes"] / 1024
                ui.label(f"{kb:.0f} KB").classes("text-caption text-grey")
            ui.space()
            ui.label(_format_time_ago(attachment.get("created_at"))).classes(
                "text-caption text-grey"
            )
        if attachment.get("signed_url"):
            if (attachment.get("media_type") or "").startswith("image"):
                ui.image(attachment["signed_url"]).classes("q-mt-xs").style("max-width: 300px")
            else:
                ui.link("↗ View attachment", attachment["signed_url"], new_tab=True)
        else:
            ui.label("(attachment unavailable)").classes("text-caption text-grey")


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


async def _render_single_detail(db, ticket_id: str) -> None:
    ui.link("← Back to Tickets", "/tickets").classes("q-mb-sm")
    detail = await run.io_bound(lambda: db.get_canonical_ticket_detail(ticket_id))
    if detail is None:
        ui.label("Ticket was not found.").classes("text-negative")
        return
    set_page_context(build_ticket_page_context(detail))
    ref = detail.get("ticket_ref") or ticket_id
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
