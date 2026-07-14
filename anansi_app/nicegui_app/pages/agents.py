"""Persistent Agents page (NiceGUI port of components/agents_page.py).

Reuses ``services.agent_management_service.AgentManagementService`` and
``services.settings_service.SettingsService`` unchanged. Lists agent instances
grouped by expert, with per-instance pause/resume/wake/restart/stop controls,
a scheduled-jobs section, and a create-instance dialog.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from nicegui import run, ui

from nicegui_app.services_access import get_agent_service, get_reader, get_settings_service

_DEFAULT_TZ = ZoneInfo(os.getenv("DEFAULT_TIMEZONE", "UTC"))

_STATUS_COLORS = {
    "active": "positive",
    "executing": "info",
    "paused": "warning",
    "error": "negative",
    "terminated": "grey",
    "initializing": "grey",
}


def _fmt_local(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(_DEFAULT_TZ).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError):
        return str(iso)[:16]


def _wake_ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        if delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() / 60)}m ago"
        if delta.total_seconds() < 86400:
            return f"{int(delta.total_seconds() / 3600)}h ago"
        return f"{delta.days}d ago"
    except (ValueError, TypeError):
        return str(iso)[:16]


def _fetch_system_jobs() -> list[dict]:
    base_url = os.getenv("CHAT_ORCHESTRATOR_URL", "http://localhost:8000/chat").rstrip("/")
    api_key = os.getenv("API_KEY", "")
    try:
        resp = requests.get(f"{base_url}/api/v1/jobs", headers={"X-Api-Key": api_key}, timeout=5)
        resp.raise_for_status()
        return list(resp.json().get("jobs", []))
    except Exception as e:
        return [{"_error": str(e)}]


async def render() -> None:
    ui.label("Persistent Agents").classes("text-h5")

    svc = get_agent_service()
    if not await run.io_bound(svc.is_configured):
        ui.label("Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY.").classes(
            "text-negative"
        )
        return

    await _render_scheduled_jobs_section()

    # Global kill-switch indicator (var lives on anansi-bot; fetch from DO).
    settings_svc = get_settings_service()
    remote_settings = await run.io_bound(
        lambda: settings_svc.get_current_settings(fetch_from_do=True)
    )
    if not remote_settings.get("PERSISTENT_AGENTS_ENABLED", False):
        ui.label(
            "Persistent agents are disabled globally. Enable them in "
            "Settings > Bot Behavior > Persistent Agents."
        ).classes("text-warning")

    body = ui.column().classes("w-full")

    async def refresh() -> None:
        body.clear()
        instances = await run.io_bound(svc.list_instances)
        expert_configs = await run.io_bound(svc.get_persistent_expert_configs)
        config_map = {c["expert_id"]: c for c in expert_configs}

        with body:
            if not instances:
                ui.label("No persistent agent instances found. Create one below.").classes(
                    "text-italic"
                )
            else:
                groups: dict[str, list[dict]] = {}
                for inst in instances:
                    groups.setdefault(inst["expert_id"], []).append(inst)
                for expert_id, group in groups.items():
                    _render_expert_group(
                        svc, expert_id, group, config_map.get(expert_id, {}), refresh
                    )

            ui.separator()
            ui.button(
                "+ Create Agent Instance",
                on_click=lambda: _create_dialog(svc, refresh),
            ).props("color=primary")

    await refresh()


async def _render_scheduled_jobs_section() -> None:
    with ui.expansion("🗓️ Scheduled Jobs (non-agent)").classes("w-full"):
        ui.label("System Jobs").classes("text-bold")
        jobs = await run.io_bound(_fetch_system_jobs)
        if jobs and "_error" in jobs[0]:
            ui.label(f"Could not fetch system jobs: {jobs[0]['_error']}").classes("text-warning")
        elif not jobs:
            ui.label("No system jobs registered (all feature flags may be off).").classes(
                "text-caption"
            )
        else:
            ui.table(
                columns=[
                    {"name": "name", "label": "Name", "field": "name", "align": "left"},
                    {"name": "trigger", "label": "Trigger", "field": "trigger", "align": "left"},
                    {"name": "next", "label": "Next Run", "field": "next", "align": "left"},
                ],
                rows=[
                    {
                        "name": j.get("name", j.get("id", "?")),
                        "trigger": j.get("trigger", "—"),
                        "next": _fmt_local(j.get("next_run_time")),
                    }
                    for j in jobs
                ],
            ).classes("w-full")

        ui.separator()
        ui.label("User Schedules").classes("text-bold")
        reader = get_reader()
        if not await run.io_bound(reader.is_configured):
            ui.label("Database not configured — cannot load user schedules.").classes(
                "text-warning"
            )
            return
        schedules = await run.io_bound(reader.get_all_user_schedules)
        if not schedules:
            ui.label("No user schedules found.").classes("text-caption")
            return
        ui.table(
            columns=[
                {"name": "name", "label": "Name", "field": "name", "align": "left"},
                {"name": "type", "label": "Type", "field": "type", "align": "left"},
                {"name": "next", "label": "Next Run", "field": "next", "align": "left"},
                {"name": "status", "label": "Status", "field": "status", "align": "left"},
                {"name": "by", "label": "Created By", "field": "by", "align": "left"},
            ],
            rows=[
                {
                    "name": s.get("friendly_name") or s.get("command", "")[:40],
                    "type": s.get("schedule_type", "—"),
                    "next": _fmt_local(s.get("next_run_at")),
                    "status": s.get("status", "—"),
                    "by": s.get("created_by_email") or "—",
                }
                for s in schedules
            ],
        ).classes("w-full")


def _render_expert_group(svc, expert_id: str, instances: list, config: dict, refresh) -> None:
    is_user_startable = config.get("is_user_startable", False)
    is_known = bool(config)
    now = datetime.now(timezone.utc)

    filtered = []
    for i in instances:
        if i["status"] != "terminated":
            filtered.append(i)
            continue
        auto = (i.get("created_by") or "").startswith("auto:")
        if is_user_startable or not is_known:
            if auto:
                continue
        elif auto:
            filtered.append(i)
            continue
        updated_at = i.get("updated_at")
        if updated_at:
            delta = now - datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if delta.total_seconds() < 300:
                filtered.append(i)

    if not filtered:
        return

    active = sum(1 for i in filtered if i["status"] == "active")
    paused = sum(1 for i in filtered if i["status"] == "paused")
    errors = sum(1 for i in filtered if i["status"] == "error")
    label = (
        f"{expert_id.replace('_', ' ').title()} ({len(filtered)}) — "
        f"{active} active · {paused} paused · {errors} error"
    )

    with ui.expansion(label, value=(errors > 0 or active > 0)).classes("w-full"):
        with ui.row().classes("gap-2"):
            ui.button(
                "Pause All",
                on_click=lambda: _bulk(svc, expert_id, "paused", refresh),
            ).props("flat dense")
            ui.button(
                "Resume All",
                on_click=lambda: _bulk(svc, expert_id, "active", refresh),
            ).props("flat dense")
        for inst in filtered:
            _render_instance_row(svc, inst, refresh)


async def _bulk(svc, expert_id: str, status: str, refresh) -> None:
    count = await run.io_bound(lambda: svc.update_status_by_expert(expert_id, status))
    ui.notify(f"Updated {count} instance(s)")
    await refresh()


def _render_instance_row(svc, inst: dict, refresh) -> None:
    inst_id = str(inst["id"])
    status = inst["status"]
    with ui.card().classes("w-full q-my-xs"):
        with ui.row().classes("items-center justify-between w-full no-wrap"):
            with ui.column().classes("gap-0").style("flex: 2"):
                ui.label(inst["instance_name"]).classes("text-bold")
                creator = inst.get("created_by") or ""
                suffix = (
                    f" · by {creator}" if creator and inst.get("expert_id") == "user_agent" else ""
                )
                with ui.row().classes("items-center gap-1"):
                    ui.badge(status, color=_STATUS_COLORS.get(status, "grey"))
                    ui.label(
                        f"Wake: {_wake_ago(inst.get('last_woke_at'))} · #{inst.get('wake_count', 0)}{suffix}"
                    ).classes("text-caption")
            with ui.row().classes("gap-1").style("flex: 1; justify-content: flex-end"):
                if status in ("active", "executing"):
                    ui.button(
                        "Pause", on_click=lambda: _set_status(svc, inst_id, "paused", refresh)
                    ).props("flat dense")
                elif status in ("paused", "error"):
                    ui.button(
                        "Resume", on_click=lambda: _set_status(svc, inst_id, "active", refresh)
                    ).props("flat dense")
                if status in ("active", "error"):
                    ui.button("Wake", on_click=lambda: _wake(svc, inst, refresh)).props(
                        "flat dense"
                    )
                ui.button("Restart", on_click=lambda: _restart(svc, inst_id, refresh)).props(
                    "flat dense"
                )
                if status != "terminated":
                    ui.button("Stop", on_click=lambda: _terminate(svc, inst_id, refresh)).props(
                        "flat dense color=negative"
                    )

        with ui.expansion(f"Details: {inst['instance_name']}").classes("w-full"):
            ui.label(f"ID: {inst['id']}").classes("text-caption")
            ui.label(f"Thread: {inst['thread_id']}").classes("text-caption")
            ui.label(f"Entity: {inst['anchor_entity_type']} / {inst['anchor_entity_id']}").classes(
                "text-caption"
            )
            ui.label(f"Schedule: {inst.get('wake_schedule', 'none')}").classes("text-caption")
            if inst.get("error_message"):
                ui.label(f"Error: {inst['error_message']}").classes("text-negative")
            if inst.get("check_prompt"):
                ui.label("Check Prompt").classes("text-bold text-caption")
                ui.label(inst["check_prompt"]).classes("text-caption")
            if inst.get("response_prompt"):
                ui.label("Response Prompt").classes("text-bold text-caption")
                ui.label(inst["response_prompt"]).classes("text-caption")
            metadata = inst.get("metadata", {})
            if metadata:
                ui.json_editor({"content": {"json": metadata}, "readOnly": True}).classes("w-full")


async def _set_status(svc, inst_id: str, status: str, refresh) -> None:
    await run.io_bound(lambda: svc.update_status(inst_id, status))
    await refresh()


async def _wake(svc, inst: dict, refresh) -> None:
    inst_id = str(inst["id"])
    await run.io_bound(lambda: svc.queue_manual_wake(inst_id))
    if inst["status"] == "error":
        await run.io_bound(lambda: svc.update_status(inst_id, "active"))
    ui.notify(f"Queued wake for {inst['instance_name']}")
    await refresh()


async def _restart(svc, inst_id: str, refresh) -> None:
    await run.io_bound(lambda: svc.restart_instance(inst_id))
    await refresh()


async def _terminate(svc, inst_id: str, refresh) -> None:
    await run.io_bound(lambda: svc.terminate_instance(inst_id))
    await refresh()


async def _create_dialog(svc, refresh) -> None:
    expert_configs = await run.io_bound(svc.get_persistent_expert_configs)
    expert_ids = [c["expert_id"] for c in expert_configs]

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Create Agent Instance").classes("text-h6")
        expert_select: Any
        if not expert_ids:
            ui.label("No persistent experts found in expert definitions.").classes("text-warning")
            expert_select = ui.input("Expert ID", value="grid_monitor").classes("w-full")
        else:
            expert_select = ui.select(expert_ids, value=expert_ids[0], label="Expert").classes(
                "w-full"
            )

        name_input = ui.input("Instance Name").classes("w-full")
        entity_input = ui.input("Entity ID").classes("w-full")
        schedule_input = ui.input("Wake Schedule (cron)").classes("w-full")
        org_input = ui.number(
            "Organization ID", value=int(os.getenv("STAFF_ORG_ID", "2")), min=1
        ).classes("w-full")

        async def create() -> None:
            expert_id = expert_select.value
            selected: dict = next((c for c in expert_configs if c["expert_id"] == expert_id), {})
            if not name_input.value or not entity_input.value:
                ui.notify("Instance name and entity are required", type="negative")
                return
            try:
                result = await run.io_bound(
                    lambda: svc.create_instance(
                        expert_id=expert_id,
                        instance_name=name_input.value,
                        anchor_entity_type=selected.get("anchor_entity_type", ""),
                        anchor_entity_id=entity_input.value,
                        anchor_metadata={},
                        organization_id=int(org_input.value),
                        wake_schedule=schedule_input.value or None,
                    )
                )
                ui.notify(
                    f"Created: {result.get('instance_name', name_input.value)}", type="positive"
                )
                dialog.close()
                await refresh()
            except Exception as e:  # noqa: BLE001 - surface to operator
                ui.notify(f"Failed to create: {e}", type="negative")

        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=create).props("color=primary")
    dialog.open()
