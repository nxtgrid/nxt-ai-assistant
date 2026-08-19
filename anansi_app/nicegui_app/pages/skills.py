"""Skills list: every skill, its status, and its schedule.

Replaces /skill-builder as the entry point. The builder itself becomes a
section inside this page's edit modal (Phase 3) so an in-progress build
survives navigation as a draft rather than being lost.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from nicegui import ui

# Must match nicegui_app.pages.broadcast._build_recurrence's REPEAT_OPTIONS,
# minus "Does not repeat" -- that produces no recurrence at all (see
# SkillBuilderService.set_skill_schedule's docstring), so it isn't a
# meaningful choice here: a one-off skill run doesn't need a persistent cron
# row, it's just run from the builder.
REPEAT_OPTIONS = ["Weekly", "Every other week", "Monthly (same date)", "Monthly (same weekday)"]

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


def can_save_as_draft(title: str) -> bool:
    """A draft needs only a name -- that is what makes the modal viable.

    Saving partial, invalid step lists is the point: losing an in-progress
    build on navigation is the current behaviour this replaces.
    """
    return bool((title or "").strip())


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

    ui.label("🧩 Skills").classes("text-h5")
    ui.label(
        "Reusable step-by-step procedures. A draft is saved but never offered to "
        "the assistant; only active skills reach a conversation."
    ).classes("text-sm text-gray-600 mb-4")

    container = ui.column().classes("w-full gap-2")

    async def refresh() -> None:
        skills = await run.io_bound(service.list_skills)
        schedules = await run.io_bound(service.schedule_summaries)
        rows = build_skill_rows(skills, schedules)

        container.clear()
        with container:
            if not rows:
                ui.label("No skills yet. Create one to get started.").classes(
                    "text-gray-500 italic"
                )
                return
            for row in rows:
                _render_row(row, service, refresh, user_email)

    with ui.row().classes("w-full justify-end mb-2"):
        ui.button(
            "New skill",
            icon="add",
            on_click=lambda: _open_editor(None, service, refresh, user_email),
        ).props("color=primary")

    await refresh()


def _render_row(row, service, refresh, user_email) -> None:
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
                    on_click=lambda r=row: _open_editor(r, service, refresh, user_email),
                ).props("flat dense")


async def _open_editor(row, service, refresh, user_email) -> None:
    """New skill (row=None) or edit an existing one's identity/status/schedule.

    The Steps card mounts the same chat-driven builder the standalone
    /skill-builder page uses (render_builder, extracted for exactly this
    reuse) -- but only for a *new* skill. Reopening an existing skill's
    steps for further chat-driven editing is a known gap: render_builder's
    initial_steps parameter exists for this and is not wired up yet (see
    its docstring), so editing an existing skill here covers identity,
    status and schedule only, not its step transcript.
    """
    from nicegui import run

    from nicegui_app.pages.skill_builder import _derive_steps_payload, render_builder

    with ui.dialog().props("persistent maximized") as dialog, ui.card().classes("w-full"):
        ui.label("Edit skill" if row else "New skill").classes("text-h6")

        with ui.column().classes("w-full gap-4"):
            # 1. Identity
            with ui.card().classes("w-full"):
                ui.label("Identity").classes("text-subtitle2")
                title_input = ui.input("Title", value=(row or {}).get("title", "")).classes(
                    "w-full"
                )
                summary_input = (
                    ui.textarea("Summary", value=(row or {}).get("summary", ""))
                    .classes("w-full")
                    .props("autogrow")
                )
                staff_switch = ui.switch(
                    "Staff only", value=(row or {}).get("audience") != "Everyone"
                )
                status_select = ui.select(
                    ["draft", "active", "disabled"],
                    value=(row or {}).get("status", "draft"),
                    label="Status",
                )

            # 2. Steps -- the existing builder, unchanged. New skills only
            # (see docstring); an existing skill's already-saved steps are
            # shown nowhere in this modal yet.
            with ui.card().classes("w-full"):
                ui.label("Steps").classes("text-subtitle2")
                if row:
                    ui.label(
                        "Editing an existing skill's steps isn't supported here yet -- "
                        "this save will keep its current steps unchanged."
                    ).classes("text-xs text-gray-500")
                    state_holder: Dict[str, Any] = {"steps": None}
                else:
                    builder_user_id = f"{user_email}:{uuid.uuid4()}"
                    state_holder = await render_builder(user_email, builder_user_id)

            # 3. Schedule
            with ui.card().classes("w-full"):
                ui.label("Schedule").classes("text-subtitle2")
                ui.label(
                    "A scheduled skill runs once per entity of the chosen type."
                ).classes("text-xs text-gray-500")
                anchor_select = ui.select(
                    {"": "Not scheduled", "grid": "Per grid", "organization": "Per organization"},
                    value="",
                    label="Fan out across",
                )
                first_run = ui.input("First run (YYYY-MM-DD HH:MM)").classes("w-full")
                repeat_select = ui.select(
                    REPEAT_OPTIONS,
                    value=REPEAT_OPTIONS[0],
                    label="Repeat",
                )

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            async def _save() -> None:
                title = title_input.value or ""
                if not can_save_as_draft(title):
                    ui.notify("A title is required.", type="negative")
                    return

                if state_holder.get("steps") is None:
                    # Editing an existing skill: the Steps card above didn't
                    # mount a builder, so nothing to derive -- keep it as is.
                    steps = row.get("steps") or []
                else:
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
                            summary_input.value or "",
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
                            summary_input.value or "",
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

                ui.notify(f"Saved '{title}'.", type="positive")
                dialog.close()
                await refresh()

            ui.button("Save", on_click=_save, color="primary")

    dialog.open()
