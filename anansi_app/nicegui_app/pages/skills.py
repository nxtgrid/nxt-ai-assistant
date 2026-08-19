"""Skills list: every skill, its status, and its schedule.

Replaces /skill-builder as the entry point. The builder itself becomes a
section inside this page's edit modal (Phase 3) so an in-progress build
survives navigation as a draft rather than being lost.
"""

from __future__ import annotations

from typing import Any, Dict, List

from nicegui import ui

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


def _open_editor(row, service, refresh, user_email) -> None:
    """Phase 3 replaces this with the full builder modal."""
    ui.notify("The skill editor arrives in Phase 3.", type="info")
