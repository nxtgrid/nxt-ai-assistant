"""Skills list: every skill, its status, and its schedule.

Replaces /skill-builder as the entry point. The builder itself becomes a
section inside this page's edit modal (Phase 3) so an in-progress build
survives navigation as a draft rather than being lost.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from nicegui import ui

# Matches nicegui_app.pages.broadcast._build_recurrence's REPEAT_OPTIONS
# exactly, including "Does not repeat" -- a real one-time run (see
# SkillBuilderService.set_skill_schedule), not just "no schedule at all".
REPEAT_OPTIONS = [
    "Does not repeat",
    "Weekly",
    "Every other week",
    "Monthly (same date)",
    "Monthly (same weekday)",
]

STATUS_COLORS: Dict[str, str] = {
    "draft": "grey",
    "active": "green",
    "disabled": "orange",
    "unusable": "red",
}


def format_schedule(schedule: Dict[str, Any]) -> str:
    """One-line description of a skill's schedule. '—' when unscheduled."""
    cron = (schedule or {}).get("cron_expression")
    if not cron:
        return "—"
    anchor = schedule.get("anchor_entity_type")
    text = f"{cron} per {anchor}" if anchor else cron
    if not schedule.get("is_active", True):
        text = f"{text} (paused)"
    return text


def schedule_form_defaults(schedule: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """anchor / repeat / first_run string values to preselect when opening
    Edit on a workflow that may already have a user_schedules row.

    An inactive row (a one-time run that already completed, or one an
    operator previously removed via remove_skill_schedule) reads identically
    to no schedule at all -- both here and in _open_editor's Save logic,
    which must agree on the same "is this really scheduled" question or it
    will fire a pointless removal call on a workflow that was never actually
    scheduled to begin with.
    """
    if not schedule or not schedule.get("is_active"):
        return {"anchor": "", "repeat": REPEAT_OPTIONS[0], "first_run": ""}

    anchor = schedule.get("anchor_entity_type") or ""

    first_run = ""
    next_run_at = schedule.get("next_run_at")
    if next_run_at:
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(str(next_run_at).replace("Z", "+00:00"))
            first_run = parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            first_run = ""

    # Only has to round-trip what _build_recurrence itself produces --
    # skill schedules have exactly one writer -- not arbitrary hand-written
    # cron.
    schedule_type = schedule.get("schedule_type")
    cron = schedule.get("cron_expression") or ""
    if schedule_type == "biweekly":
        repeat = "Every other week"
    elif schedule_type != "recurring" or not cron:
        repeat = "Does not repeat"
    else:
        fields = cron.split()
        dow = fields[4] if len(fields) > 4 else ""
        dom = fields[2] if len(fields) > 2 else "*"
        if "#" in dow:
            repeat = "Monthly (same weekday)"
        elif dom != "*":
            repeat = "Monthly (same date)"
        else:
            repeat = "Weekly"

    return {"anchor": anchor, "repeat": repeat, "first_run": first_run}


def derive_fallback_title(summary: str) -> str:
    """A workflow's stored title when the author left the /skill name box
    blank (item d) -- falls back to the auto-generated summary (item b),
    trimmed to a list-friendly length. build_skill_rows shows title as the
    primary line, so a raw MAX_SUMMARY_CHARS-length (200-char) summary would
    read oddly there -- same word-boundary-trim shape as
    skill_summary.py's _truncate_at_word_boundary, duplicated rather than
    imported since anansi_app and chat_orchestrator are separately deployed
    packages with no shared import path outside shared/.

    Returns "" for a blank summary -- can_save_as_draft already refuses to
    save when both the name and the summary are blank, so callers must not
    treat "" here as a title to persist.
    """
    text = (summary or "").strip()
    if not text or len(text) <= 60:
        return text
    clipped = text[:60]
    last_space = clipped.rfind(" ")
    if last_space > 36:  # ~60% of 60, same heuristic as the chat_orchestrator sibling
        clipped = clipped[:last_space]
    return clipped.rstrip(" ,.;:-") + "…"


def can_save_as_draft(skill_name: str, summary: str) -> bool:
    """A draft needs something to derive a title from: an explicit /skill
    name, or an auto-generated summary to fall back to (derive_fallback_title)
    -- either makes the modal viable to save.

    Saving partial, invalid step lists is still the point: losing an
    in-progress build on navigation is the behaviour this replaces.
    """
    return bool((skill_name or "").strip()) or bool((summary or "").strip())


def can_promote_to_active(
    steps: List[Dict[str, Any]],
    validation_errors: List[Dict[str, Any]],
    title: str,
) -> "tuple[bool, str]":
    """Whether this skill may go live. Returns (ok, reason_if_not).

    Warnings never block -- validate_skill_steps emits one for a write no
    later step reads, which is often deliberate in a final response step.
    """
    if not (title or "").strip():
        return False, "A skill needs a title before it can be activated."
    if not steps:
        return False, "A skill needs at least one step before it can be activated."

    blocking = [e for e in validation_errors if e.get("severity") == "error"]
    if blocking:
        return False, "; ".join(e.get("message", "invalid step") for e in blocking)

    failed = [s for s in steps if s.get("had_tool_error")]
    if failed:
        names = ", ".join(s.get("name") or f"step {s.get('index', 0) + 1}" for s in failed)
        return False, (
            f"These steps captured a tool error rather than a result: {names}. "
            f"Rewind and re-run them before activating."
        )

    return True, ""


def build_skill_rows(
    skills: List[Dict[str, Any]], schedules: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Table rows for the list. Pure -- all formatting decisions live here."""
    rows = []
    for skill in skills:
        rows.append(
            {
                "id": skill["id"],
                "title": skill["title"],
                "summary": skill.get("summary") or "",
                "step_count": skill.get("step_count", 0),
                "status": skill.get("status", "draft"),
                "audience": "Staff only" if skill.get("staff_only") else "Everyone",
                "schedule": format_schedule(schedules.get(skill["id"], {})),
                "updated_at": skill.get("updated_at") or "",
                "created_by": skill.get("created_by") or "",
            }
        )
    return rows


async def render(user: dict[str, Any]) -> None:
    from nicegui import run

    from nicegui_app.services_access import get_skill_builder_service

    service = get_skill_builder_service()
    user_email = user.get("email", "")

    ui.label("🎬 Workflows").classes("text-h5")
    ui.label(
        "Reusable step-by-step procedures. Saving one as a /skill so the assistant "
        "can offer it in a conversation is optional -- see the editor. A draft is "
        "saved but never offered; only active skills reach a conversation."
    ).classes("text-sm text-gray-600 mb-4")

    container = ui.column().classes("w-full gap-2")

    async def refresh() -> None:
        skills = await run.io_bound(service.list_skills)
        schedules = await run.io_bound(service.schedule_summaries)
        rows = build_skill_rows(skills, schedules)

        container.clear()
        with container:
            if not rows:
                ui.label("No workflows yet. Create one to get started.").classes(
                    "text-gray-500 italic"
                )
                return
            for row in rows:
                _render_row(row, schedules.get(row["id"]), service, refresh, user_email)

    with ui.row().classes("w-full justify-end mb-2"):
        ui.button(
            "New workflow",
            icon="add",
            on_click=lambda: _open_editor(None, None, service, refresh, user_email),
        ).props("color=primary")

    await refresh()


def _render_row(row, schedule, service, refresh, user_email) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label(row["title"]).classes("text-base font-medium")
                ui.label(row["summary"]).classes("text-sm text-gray-600")
                ui.label(
                    f"{row['step_count']} steps · {row['audience']} · "
                    f"{row['schedule']} · {row['created_by']}"
                ).classes("text-xs text-gray-500")
            with ui.row().classes("items-center gap-2"):
                ui.badge(row["status"], color=STATUS_COLORS.get(row["status"], "grey"))
                ui.button(
                    "Edit",
                    on_click=lambda r=row, s=schedule: _open_editor(
                        r, s, service, refresh, user_email
                    ),
                ).props("flat dense")


async def _open_editor(row, schedule, service, refresh, user_email) -> None:
    """New workflow (row=None) or edit an existing one's identity/status/schedule.

    The Workflow card mounts the same chat-driven builder the standalone
    /skill-builder page used to (render_builder, extracted for exactly this
    reuse), for New and Edit alike. Editing seeds it with the workflow's
    stored steps via initial_steps: each renders as an inert "not yet
    re-run" card until the author actually re-sends it, so a re-run is a
    real execution against live tools, not a replay of old transcript --
    see render_builder's docstring for the pending-tail mechanism, and
    _derive_steps_payload for how anything left un-re-run is preserved
    verbatim at Save.

    Sized and structured like the rest of the app's editor modals
    (broadcast.py, knowledge_modules.py, prompts.py: a capped-width card
    that scrolls internally, not `props("maximized")`, which took over the
    full browser viewport) -- see this module's git history for the before.

    Saving a workflow as an invocable "/skill" is optional (item d): the
    Workflow card is the point of this modal and gets the most room, with
    Identity & schedule -- including the small, optional /skill-name box --
    combined below it (item e).
    """
    from nicegui import run

    from nicegui_app.pages.skill_builder import _derive_steps_payload, render_builder

    with ui.dialog() as dialog, ui.card().classes("w-full").style(
        "max-width: 900px; max-height: calc(100dvh - 32px); overflow-y: auto"
    ):
        ui.label("Edit workflow" if row else "New workflow").classes("text-h6")

        with ui.column().classes("w-full gap-4"):
            # 1. Workflow -- the chat-driven builder, the reason this modal
            # exists. A placeholder now, filled in below once summary_input
            # exists (a new workflow's auto-summary callback writes into it
            # as steps come in) -- NiceGUI fills a container from a `with`
            # block wherever it's opened, regardless of when the container
            # itself was created, the same deferred-fill pattern render()'s
            # own `container` above already relies on.
            steps_card = ui.card().classes("w-full")

            # 2. Identity & schedule, combined (item e) -- secondary to the
            # workflow itself, so it sits below and takes only the room it
            # needs.
            with ui.card().classes("w-full gap-2"):
                ui.label("Identity & schedule").classes("text-subtitle2")

                # An existing workflow's /name is fixed at creation
                # (update_skill never writes slug) -- shown read-only rather
                # than as a box implying an edit that wouldn't stick. Its
                # title stays separately editable, as before.
                if row:
                    title_input = ui.input("Title", value=row.get("title", "")).classes(
                        "w-full"
                    )
                    with ui.row().classes("items-center gap-1"):
                        ui.label("/").classes("text-body1 text-gray-500")
                        ui.label(row.get("slug", "")).classes("text-body1 text-gray-600")
                    ui.label("A workflow's /name can't change after it's created.").classes(
                        "text-xs text-gray-500"
                    )
                    skill_name_input = None
                else:
                    title_input = None
                    with ui.row().classes("items-end gap-1"):
                        ui.label("/").classes("text-h6 text-gray-500")
                        skill_name_input = ui.input("Skill name (optional)").classes("w-56")
                    skill_name_hint = ui.label(
                        "Optional -- makes this workflow invocable by the assistant "
                        "as /name. Leave it blank to just save the workflow."
                    ).classes("text-xs text-gray-500")

                    async def _check_skill_name_clash() -> None:
                        raw = skill_name_input.value or ""
                        if not raw.strip():
                            skill_name_hint.text = (
                                "Optional -- makes this workflow invocable by the "
                                "assistant as /name. Leave it blank to just save the "
                                "workflow."
                            )
                            skill_name_hint.classes(
                                remove="text-negative", add="text-gray-500"
                            )
                            return
                        taken, candidate_slug = await run.io_bound(
                            lambda: service.slug_taken(raw)
                        )
                        if taken:
                            skill_name_hint.text = (
                                f"'/{candidate_slug}' is already used by another skill "
                                f"-- choose a different name."
                            )
                            skill_name_hint.classes(
                                remove="text-gray-500", add="text-negative"
                            )
                        else:
                            skill_name_hint.text = f"Will be invocable as /{candidate_slug}."
                            skill_name_hint.classes(
                                remove="text-negative", add="text-gray-500"
                            )

                    skill_name_input.on_value_change(lambda: _check_skill_name_clash())

                summary_input = (
                    ui.textarea("Summary", value=(row or {}).get("summary", ""))
                    .classes("w-full")
                    .props("autogrow")
                )
                if not row:
                    ui.label(
                        "Auto-generated from the workflow's steps as you build -- "
                        "edit freely, your changes stick."
                    ).classes("text-xs text-gray-500")

                with ui.row().classes("items-center gap-4"):
                    staff_switch = ui.switch(
                        "Staff only", value=(row or {}).get("audience") != "Everyone"
                    )
                    status_select = ui.select(
                        ["draft", "active", "disabled"],
                        value=(row or {}).get("status", "draft"),
                        label="Status",
                    )

                ui.separator()
                ui.label(
                    "Schedule -- runs once per entity of the chosen type."
                ).classes("text-xs text-gray-500")
                schedule_defaults = schedule_form_defaults(schedule)
                with ui.row().classes("w-full gap-2"):
                    anchor_select = ui.select(
                        {
                            "": "Not scheduled",
                            "grid": "Per grid",
                            "organization": "Per organization",
                        },
                        value=schedule_defaults["anchor"],
                        label="Fan out across",
                    ).classes("flex-grow")
                    repeat_select = ui.select(
                        REPEAT_OPTIONS,
                        value=schedule_defaults["repeat"],
                        label="Repeat",
                    ).classes("flex-grow")
                first_run = ui.input(
                    "First run (YYYY-MM-DD HH:MM)", value=schedule_defaults["first_run"]
                ).classes("w-full")

            # Guards state_holder["summary_user_edited"] against the
            # programmatic write below setting it -- only a real user edit
            # (guard inactive) should count as "stop auto-updating".
            _summary_sync_guard = {"active": False}

            def _apply_auto_summary(text: str) -> None:
                _summary_sync_guard["active"] = True
                summary_input.value = text
                _summary_sync_guard["active"] = False

            def _on_summary_edited(_e) -> None:
                # No-op for an existing workflow (state_holder["steps"] is
                # None there -- no live builder, nothing auto-updates it
                # anyway) as well as for the guard-flagged programmatic write.
                if not _summary_sync_guard["active"] and state_holder.get("steps") is not None:
                    state_holder["summary_user_edited"] = True

            summary_input.on_value_change(_on_summary_edited)

        # Now fill the Workflow card placeholder created above. Mounted for
        # both New and Edit alike -- render_builder's initial_steps handles
        # the difference (empty for New, the stored steps for Edit; see its
        # docstring and _derive_steps_payload for how a partially-re-run
        # edit is preserved).
        with steps_card:
            ui.label("Workflow").classes("text-subtitle2")
            if row:
                ui.label(
                    "Each step below is re-runnable, one at a time from the top -- "
                    "grey cards haven't been re-run in this session yet and are "
                    "saved unchanged if you don't get to them."
                ).classes("text-xs text-gray-500")
            builder_user_id = f"{user_email}:{uuid.uuid4()}"
            state_holder = await render_builder(
                user_email,
                builder_user_id,
                initial_steps=(row.get("steps") or []) if row else [],
                on_summary_update=_apply_auto_summary,
            )

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            async def _save() -> None:
                summary_text = summary_input.value or ""
                skill_name = (skill_name_input.value or "").strip() if skill_name_input else ""

                if not can_save_as_draft(skill_name, summary_text):
                    ui.notify(
                        "Add a /skill name, or send at least one message so a "
                        "summary can be generated.",
                        type="negative",
                    )
                    return

                if row:
                    title = title_input.value or ""
                elif skill_name:
                    taken, candidate_slug = await run.io_bound(
                        lambda: service.slug_taken(skill_name)
                    )
                    if taken:
                        ui.notify(
                            f"'/{candidate_slug}' is already used by another skill. "
                            f"Choose a different name.",
                            type="negative",
                        )
                        return
                    title = skill_name
                else:
                    title = derive_fallback_title(summary_text)

                steps = _derive_steps_payload(state_holder)
                if not steps:
                    ui.notify("Send at least one message to build a step.", type="negative")
                    return

                if status_select.value == "active":
                    ok, reason = can_promote_to_active(
                        steps, state_holder.get("validation_errors") or [], title
                    )
                    if not ok:
                        ui.notify(reason, type="negative")
                        return

                if row:
                    result = await run.io_bound(
                        lambda: service.update_skill(
                            row["id"],
                            title,
                            summary_text,
                            staff_switch.value,
                            status_select.value,
                            actor=user_email,
                        )
                    )
                    if not result.get("success"):
                        ui.notify(result.get("error") or "Update failed", type="negative")
                        return
                    skill_id = row["id"]
                else:
                    result = await run.io_bound(
                        lambda: service.save_skill(
                            title,
                            summary_text,
                            steps,
                            staff_switch.value,
                            user_email,
                            status=status_select.value,
                        )
                    )
                    if not result.get("success"):
                        ui.notify(result.get("error") or "Save failed", type="negative")
                        return
                    skill_id = result["skill"]["id"]

                if anchor_select.value:
                    schedule_result = await run.io_bound(
                        lambda: service.set_skill_schedule(
                            skill_id,
                            anchor_entity_type=anchor_select.value,
                            first_run=first_run.value,
                            frequency=repeat_select.value,
                            actor=user_email,
                        )
                    )
                    if not schedule_result.get("success"):
                        ui.notify(
                            f"Saved, but scheduling failed: {schedule_result.get('error')}",
                            type="warning",
                        )
                        dialog.close()
                        await refresh()
                        return
                elif schedule is not None and schedule.get("is_active"):
                    # Had an ACTIVE schedule; the author explicitly cleared
                    # it to "Not scheduled" -- remove it rather than
                    # silently leaving the old row running. is_active gates
                    # this the same way schedule_form_defaults gates the
                    # prefill (see its docstring): without it, saving a
                    # workflow whose one-time run already completed would
                    # fire a pointless removal call every single time.
                    removal_result = await run.io_bound(
                        lambda: service.remove_skill_schedule(skill_id, actor=user_email)
                    )
                    if not removal_result.get("success"):
                        ui.notify(
                            f"Saved, but removing the schedule failed: "
                            f"{removal_result.get('error')}",
                            type="warning",
                        )
                        dialog.close()
                        await refresh()
                        return

                ui.notify(f"Saved '{title}'.", type="positive")
                dialog.close()
                await refresh()

            ui.button("Save", on_click=_save, color="primary")

    dialog.open()
