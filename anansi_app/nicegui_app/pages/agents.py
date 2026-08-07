"""System ops page (NiceGUI port of components/agents_page.py, minus the
persistent-agent instance management that page originally centered on --
removed in docs/superpowers/plans/2026-08-06-user-designed-skills.md Phase 6).

What's left is genuinely agent-independent: LLM run cost across all expert
workflow/skill runs, and visibility into system jobs + user schedules
(ordinary scheduled commands and Phase 5 skill schedules alike). Kept at the
same route/file so no bookmarks or nav wiring beyond the label need to move.
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests
from nicegui import run, ui

from nicegui_app.services_access import get_reader

_DEFAULT_TZ = ZoneInfo(os.getenv("DEFAULT_TIMEZONE", "UTC"))


def _fmt_local(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(_DEFAULT_TZ).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError):
        return str(iso)[:16]


def _cost_display(cost_usd: str | None) -> str:
    """Format a get_run_usage_by_skill cost_usd value for the run-cost table.

    None means "at least one run in this bucket used an unpriced model" (see
    SupabaseReader.get_run_usage_by_skill) -- must render as unknown, never
    as a guessed or partial dollar amount.
    """
    if cost_usd is None:
        return "—"
    return f"${Decimal(cost_usd):.4f}"


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
    ui.label("System Ops").classes("text-h5")

    reader = get_reader()
    if not await run.io_bound(reader.is_configured):
        ui.label("Database not configured. Check CHAT_DB_URL and CHAT_DB_SERVICE_KEY.").classes(
            "text-negative"
        )
        return

    await _render_run_cost_section()
    await _render_scheduled_jobs_section()


async def _render_run_cost_section() -> None:
    """LLM token/cost usage for expert workflow runs, last 7 days.

    Grouped by packet_type (not by expert/agent instance) -- see
    SupabaseReader.get_run_usage_by_skill's docstring for why: this predates
    skills existing as a first-class concept
    (docs/superpowers/plans/2026-08-06-user-designed-skills.md Phase 3), and
    packet_type is the closest stand-in available today. Costs are estimates
    from shared/llm/pricing.py's static price table, not billed amounts.
    """
    with ui.expansion("💰 LLM Run Cost (Last 7 Days)", value=True).classes("w-full"):
        reader = get_reader()
        if not await run.io_bound(reader.is_configured):
            ui.label("Database not configured — cannot load run cost.").classes("text-warning")
            return

        usage = await run.io_bound(reader.get_run_usage_by_skill)
        if not usage:
            ui.label("No workflow runs with LLM steps in the last 7 days.").classes(
                "text-caption"
            )
            return

        ui.label(
            "By packet type for now — will switch to per-skill once skills exist."
        ).classes("text-caption text-italic")

        ui.table(
            columns=[
                {"name": "type", "label": "Type", "field": "type", "align": "left"},
                {"name": "runs", "label": "Runs", "field": "runs", "align": "right"},
                {"name": "failures", "label": "Failures", "field": "failures", "align": "right"},
                {"name": "tokens", "label": "Tokens (in / out)", "field": "tokens", "align": "right"},
                {"name": "cost", "label": "Est. cost (7d)", "field": "cost", "align": "right"},
            ],
            rows=[
                {
                    "type": packet_type.replace("_", " ").title(),
                    "runs": bucket["runs"],
                    "failures": bucket["failures"],
                    "tokens": f"{bucket['input_tokens']:,} / {bucket['output_tokens']:,}",
                    "cost": _cost_display(bucket["cost_usd"]),
                }
                for packet_type, bucket in sorted(usage.items())
            ],
        ).classes("w-full")


async def _render_scheduled_jobs_section() -> None:
    with ui.expansion("🗓️ Scheduled Jobs", value=True).classes("w-full"):
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
