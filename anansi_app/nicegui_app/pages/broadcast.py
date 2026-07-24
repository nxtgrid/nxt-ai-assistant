"""Broadcast dialog (NiceGUI port of components/broadcast_modal.py).

A large dialog opened from the Chats page. Four tabs: Compose (the high-stakes
verify -> send/schedule path), Templates, Scheduled, History. Reuses
``services.broadcast_service.BroadcastService`` and
``services.broadcast_verification_service.BroadcastVerificationService``
unchanged (both are Streamlit-free); sync calls run off the event loop via
``run.io_bound``.

Verification is mandatory before send: a failed LLM judge blocks the send and
asks the operator to edit — same guardrail as the Streamlit modal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from nicegui import run, ui

TIMEZONE_OPTIONS = {
    "UTC": 0,
    "CET (UTC+1)": 1,
    "CEST (UTC+2)": 2,
    "EST (UTC-5)": -5,
    "PST (UTC-8)": -8,
}

REPEAT_OPTIONS = [
    "Does not repeat",
    "Weekly",
    "Every other week",
    "Monthly (same date)",
    "Monthly (same weekday)",
]

MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _build_recurrence(dt_utc: datetime, frequency: str) -> Optional[dict]:
    """Derive a {schedule_type, cron_expression, timezone} from first-send + frequency."""
    if frequency == "Does not repeat":
        return None
    minute, hour = dt_utc.minute, dt_utc.hour
    cron_dow = (dt_utc.weekday() + 1) % 7
    if frequency == "Weekly":
        cron, stype = f"{minute} {hour} * * {cron_dow}", "recurring"
    elif frequency == "Every other week":
        cron, stype = f"{minute} {hour} * * {cron_dow}", "biweekly"
    elif frequency == "Monthly (same date)":
        cron, stype = f"{minute} {hour} {dt_utc.day} * *", "recurring"
    elif frequency == "Monthly (same weekday)":
        nth = ((dt_utc.day - 1) // 7) + 1
        cron, stype = f"{minute} {hour} * * {cron_dow}#{nth}", "recurring"
    else:
        return None
    try:
        from croniter import croniter  # type: ignore[import-untyped]

        if not croniter.is_valid(cron):
            return None
    except ImportError:
        pass
    return {"schedule_type": stype, "cron_expression": cron, "timezone": "UTC"}


async def open_dialog(user: dict) -> None:
    from services.broadcast_service import BroadcastService
    from services.broadcast_verification_service import BroadcastVerificationService

    svc = BroadcastService()
    vsvc = BroadcastVerificationService()

    if not await run.io_bound(svc.is_configured):
        ui.notify(
            "Broadcast service not configured (CHAT_DB_* + TELEGRAM_BOT_TOKEN).",
            type="negative",
        )
        return

    email = user.get("email", "unknown")

    with ui.dialog() as dialog, ui.card().classes("w-full").style(
        "max-width: 900px; max-height: calc(100dvh - 32px); overflow-y: auto"
    ):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("📢 Broadcast Message").classes("text-h6")
            ui.button(icon="close", on_click=dialog.close).props("flat round dense")
        with ui.tabs().classes("w-full") as tabs:
            t_compose = ui.tab("Compose")
            t_templates = ui.tab("Templates")
            t_scheduled = ui.tab("Scheduled")
            t_history = ui.tab("History")
        with ui.tab_panels(tabs, value=t_compose).classes("w-full"):
            with ui.tab_panel(t_compose):
                await _compose_tab(svc, vsvc, email)
            with ui.tab_panel(t_templates):
                await _templates_tab(svc, email)
            with ui.tab_panel(t_scheduled):
                await _scheduled_tab(svc)
            with ui.tab_panel(t_history):
                await _history_tab(svc)
    dialog.open()


async def _compose_tab(svc, vsvc, email: str) -> None:
    groups = await run.io_bound(svc.get_available_groups)
    if not groups:
        ui.label("No customer groups available. Check organization Telegram chat IDs.").classes(
            "text-warning"
        )
        return
    group_options = {g["chat_id"]: g["name"] for g in groups}

    # Per-dialog state.
    state: dict[str, Any] = {"images": [], "template_images": []}

    group_select = (
        ui.select(group_options, multiple=True, label="Target Groups")
        .classes("w-full")
        .props("use-chips")
    )

    templates = await run.io_bound(svc.get_templates)
    message = ui.textarea("Message", value="").classes("w-full").props("maxlength=4096")
    char_label = ui.label("0/4096 characters").classes("text-caption")
    message.on_value_change(
        lambda: char_label.set_text(f"{len(message.value or '')}/4096 characters")
    )

    if templates:
        with ui.row().classes("items-end gap-2 w-full"):
            tmpl_names = ["-- Select template --"] + [t["name"] for t in templates]
            tmpl_select = ui.select(
                tmpl_names, value=tmpl_names[0], label="Insert template"
            ).classes("flex-grow")

            def insert_template() -> None:
                name = tmpl_select.value
                tmpl = next((t for t in templates if t["name"] == name), None)
                if not tmpl:
                    return
                cur = message.value or ""
                message.value = (cur + "\n\n" + tmpl["content"]) if cur else tmpl["content"]
                imgs = _parse_template_images(tmpl)
                if imgs:
                    state["template_images"] = imgs
                    ui.notify(f"Loaded template + {len(imgs)} image(s)")

            ui.button("Insert", on_click=insert_template).props("flat")

    with ui.expansion("Available placeholders").classes("w-full"):
        ui.markdown(
            "- `<org_name>` — organization name\n"
            "- `<org_hashtag>` — hashtag form\n"
            "- `<org_grids>` — comma-separated grids\n\n"
            "Replaced per-recipient. Supports Telegram Markdown."
        )

    # Image upload.
    img_status = ui.label().classes("text-caption")

    def _on_upload(e) -> None:
        if len(state["images"]) >= MAX_IMAGES:
            ui.notify("Maximum 10 images (Telegram limit).", type="negative")
            return
        content = e.content.read()
        if len(content) > MAX_IMAGE_BYTES:
            ui.notify(f"{e.name} exceeds Telegram's 10 MB limit.", type="negative")
            return
        from services.broadcast_service import ImageData

        state["images"].append(
            ImageData(filename=e.name, content_type=e.type or "image/jpeg", data=content)
        )
        total_mb = sum(len(i.data) for i in state["images"]) / (1024 * 1024)
        img_status.set_text(f"{len(state['images'])} image(s), {total_mb:.1f} MB")

    ui.upload(
        label="Attach images (optional)", multiple=True, auto_upload=True, on_upload=_on_upload
    ).props('accept="image/*"').classes("w-full")

    ui.separator()

    # Delivery: immediate vs scheduled.
    delivery = ui.radio(["Send immediately", "Schedule for later"], value="Send immediately").props(
        "inline"
    )
    sched_box = ui.column().classes("w-full gap-2")
    delivery.on_value_change(
        lambda: sched_box.set_visibility(delivery.value == "Schedule for later")
    )
    with sched_box:
        with ui.row().classes("gap-2 w-full"):
            date_in = ui.input(
                "Date", value=(datetime.now().date() + timedelta(days=1)).isoformat()
            ).props("type=date")
            time_in = ui.input("Time", value="09:00").props("type=time")
            tz_in = ui.select(list(TIMEZONE_OPTIONS), value="CET (UTC+1)", label="Timezone")
        repeat_in = ui.select(REPEAT_OPTIONS, value=REPEAT_OPTIONS[0], label="Repeat")
    sched_box.set_visibility(False)

    ui.separator()
    verify_note = (
        "🔍 Message will be verified before sending."
        if await run.io_bound(vsvc.is_enabled)
        else "⚠️ Verification service not configured."
    )
    ui.label(verify_note).classes("text-caption")
    status = ui.column().classes("w-full")
    send_btn = ui.button("Send").props("color=primary")

    def _scheduled_dt() -> Optional[datetime]:
        if delivery.value != "Schedule for later":
            return None
        try:
            local = datetime.fromisoformat(f"{date_in.value}T{time_in.value}")
        except ValueError:
            return None
        offset = TIMEZONE_OPTIONS.get(tz_in.value, 0)
        return (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)

    async def _send() -> None:
        status.clear()
        msg = (message.value or "").strip()
        gids = list(group_select.value or [])
        if not msg or not gids:
            ui.notify("Message and at least one group are required.", type="negative")
            return

        # Step 0: reject unknown placeholders deterministically.
        unknown = await run.io_bound(lambda: svc.find_unknown_placeholders(msg))
        if unknown:
            with status:
                ui.label("Unsupported placeholder(s): " + ", ".join(unknown)).classes(
                    "text-negative"
                )
            return

        send_btn.disable()
        scheduled = _scheduled_dt()
        recurrence = _build_recurrence(scheduled, repeat_in.value) if scheduled else None

        # Step 1: mandatory verification (enriched for first recipient).
        vresult = None
        if await run.io_bound(vsvc.is_enabled):
            to_verify = await run.io_bound(lambda: svc.enrich_message(msg, gids[0]))
            names = [group_options.get(g, g) for g in gids][:5]
            with status:
                ui.spinner()
                ui.label("Verifying message…")
            vresult = await run.io_bound(
                lambda: vsvc.verify_broadcast(message=to_verify, target_groups=names)
            )
            status.clear()
            if not vresult.passed:
                with status:
                    ui.label(f"⚠️ Verification failed: {vresult.feedback}").classes("text-negative")
                    if vresult.categories:
                        ui.label(f"Issues: {', '.join(vresult.categories)}").classes("text-caption")
                    ui.label("Please edit your message and try again.").classes("text-caption")
                send_btn.enable()
                return

        # Step 2: send/schedule.
        images = list(state["template_images"]) + list(state["images"]) or None
        with status:
            ui.spinner()
            ui.label("Scheduling…" if scheduled else "Sending broadcast…")
        result = await run.io_bound(
            lambda: svc.send_broadcast(
                message=msg,
                group_ids=gids,
                created_by=email,
                scheduled_for=scheduled,
                verification_passed=vresult.passed if vresult else None,
                verification_feedback=vresult.feedback if vresult else None,
                images=images,
                recurrence=recurrence,
            )
        )
        status.clear()
        send_btn.enable()
        with status:
            if scheduled:
                ui.label(
                    f"✅ Scheduled for {scheduled.strftime('%Y-%m-%d %H:%M')} UTC "
                    f"to {result.total} group(s)."
                ).classes("text-positive")
            elif result.failed == 0:
                ui.label(f"✅ Sent to {result.successful} group(s).").classes("text-positive")
            else:
                ui.label(
                    f"Completed: {result.successful}/{result.total} delivered, {result.failed} failed."
                ).classes("text-warning")
                for err in result.errors[:5]:
                    ui.label(err).classes("text-caption text-negative")

    send_btn.on_click(_send)


def _parse_template_images(template: dict) -> list:
    from services.broadcast_service import ImageData

    raw = template.get("image_attachments")
    if not raw:
        return []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(raw, list) or not raw:
        return []
    return [ImageData.from_dict(d) for d in raw]


async def _templates_tab(svc, email: str) -> None:
    body = ui.column().classes("w-full")

    async def refresh() -> None:
        body.clear()
        templates = await run.io_bound(svc.get_templates)
        with body:
            with ui.expansion("➕ Add new template", value=not templates).classes("w-full"):
                name_in = ui.input("Template name").classes("w-full")
                content_in = ui.textarea("Content").classes("w-full")

                async def save() -> None:
                    if not name_in.value.strip() or not content_in.value.strip():
                        ui.notify("Name and content are required.", type="negative")
                        return
                    ok, msg = await run.io_bound(
                        lambda: svc.save_template(
                            name=name_in.value.strip(),
                            content=content_in.value.strip(),
                            created_by=email,
                            images=None,
                        )
                    )
                    ui.notify(msg, type="positive" if ok else "negative")
                    if ok:
                        await refresh()

                ui.button("Save template", on_click=save).props("color=primary")

            if not templates:
                ui.label("No templates yet.").classes("text-caption")
                return
            ui.label(f"{len(templates)} template(s)").classes("text-caption")
            for tmpl in templates:
                with ui.expansion(tmpl["name"]).classes("w-full"):
                    content_edit = ui.textarea("Content", value=tmpl["content"]).classes("w-full")
                    imgs = _parse_template_images(tmpl)
                    if imgs:
                        ui.label(f"{len(imgs)} image(s) attached").classes("text-caption")
                    with ui.row().classes("gap-2"):

                        async def update(t=tmpl, ce=content_edit) -> None:
                            ok, msg = await run.io_bound(
                                lambda: svc.update_template(
                                    template_id=t["id"], content=ce.value, images=None
                                )
                            )
                            ui.notify(
                                "Updated" if ok else msg, type="positive" if ok else "negative"
                            )

                        async def delete(t=tmpl) -> None:
                            ok, msg = await run.io_bound(lambda: svc.delete_template(t["id"]))
                            ui.notify(
                                "Deleted" if ok else msg, type="positive" if ok else "negative"
                            )
                            if ok:
                                await refresh()

                        ui.button("Update", on_click=update).props("flat")
                        ui.button("Delete", on_click=delete).props("flat color=negative")

    await refresh()


async def _scheduled_tab(svc) -> None:
    body = ui.column().classes("w-full")

    async def refresh() -> None:
        body.clear()
        scheduled = await run.io_bound(svc.get_scheduled_broadcasts)
        with body:
            if not scheduled:
                ui.label("No scheduled broadcasts.").classes("text-caption")
                return
            for b in scheduled:
                stype = b.get("schedule_type")
                is_recurring = stype in ("recurring", "biweekly")
                when = str(b.get("scheduled_for", "Unknown"))[:16]
                title = f"{'🔁 Recurring' if is_recurring else 'Scheduled'} — next {when}"
                with ui.expansion(title).classes("w-full"):
                    ui.label(f"Recipients: {b.get('total_recipients', 0)} groups").classes(
                        "text-caption"
                    )
                    ui.label(b.get("message", "")[:200]).style("white-space: pre-wrap")

                    async def cancel(bid=b["id"], rec=is_recurring) -> None:
                        ok, msg = await run.io_bound(lambda: svc.cancel_scheduled_broadcast(bid))
                        ui.notify(
                            ("Series cancelled" if rec else "Cancelled") if ok else msg,
                            type="positive" if ok else "negative",
                        )
                        if ok:
                            await refresh()

                    ui.button("Cancel series" if is_recurring else "Cancel", on_click=cancel).props(
                        "flat color=negative"
                    )

    await refresh()


async def _history_tab(svc) -> None:
    history = await run.io_bound(lambda: svc.get_broadcast_history(limit=20))
    if not history:
        ui.label("No broadcast history yet.").classes("text-caption")
        return
    for b in history:
        status = b.get("status", "unknown")
        created = str(b.get("created_at", "Unknown"))[:16]
        successful = b.get("successful_sends", 0)
        total = b.get("total_recipients", 0)
        preview = b.get("message", "")[:50]
        with ui.expansion(f"[{status.upper()}] {created} — {preview}…").classes("w-full"):
            ui.label(f"Delivered: {successful}/{total}").classes("text-caption")
            ui.label(f"Created by: {b.get('created_by', 'Unknown')}").classes("text-caption")
            ui.label(b.get("message", "")[:500]).style("white-space: pre-wrap")
