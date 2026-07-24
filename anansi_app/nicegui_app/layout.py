"""Shared page shell for the NiceGUI admin app: dark sidebar, nav, user row.

Port of the Streamlit sidebar in ``app.py`` (logo + live bot status, user/logout
row, collapsible Bot Admin and Grid Design sections). The ~250 lines of CSS that
fought Streamlit's chrome are unnecessary here — the shell is styled directly.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

from grid_app.lib import perms
from nicegui import run, ui

SIDEBAR_BG = "#141824"
ACCENT = "#4da6ff"
PAGE_BG = "#f0f2f6"

BOT_ADMIN_NAV = [
    ("/tickets", "🎫 Tickets"),
    ("/conversations", "💬 Chats"),
    ("/documents", "📚 RAG Knowledgebase"),
    ("/agents", "🤖 Agents"),
    ("/settings", "⚙️ Settings"),
]

# Group order for the Grid Design nav (mirrors the Streamlit sidebar).
GRID_NAV_GROUP_ORDER = ["Engineering", "Procurement", "Sites", "Catalogue", "Field Ops"]

_STATUS_COLORS = {"live": "#22c55e", "deploying": "#f59e0b", "down": "#ef4444"}
_STATUS_TOOLTIPS = {"live": "Bot is live", "deploying": "Deploying…", "down": "Bot is down"}


def _nav_button(label: str, target: str, selected: bool) -> None:
    btn = (
        ui.button(label, on_click=lambda t=target: ui.navigate.to(t))
        .props("flat align=left no-caps dense")
        .classes("w-full")
    )
    if selected:
        btn.style(
            f"background-color: rgba(77, 166, 255, 0.15); color: #ffffff;"
            f" border-left: 3px solid {ACCENT}; border-radius: 0 6px 6px 0;"
        )
    else:
        btn.style("color: #cbd5e1;")


def _render_bot_admin_nav(current_path: str, expanded: bool) -> None:
    with ui.expansion("🤖 Bot Admin", value=expanded).classes("w-full").style("color: #e2e8f0"):
        for target, label in BOT_ADMIN_NAV:
            _nav_button(label, target, selected=current_path == target)
        langfuse_url = os.getenv("LANGFUSE_DASHBOARD_URL", "").strip()
        if langfuse_url:
            ui.separator().style("background-color: #334155")
            ui.link("📊 LLM Observability", langfuse_url, new_tab=True).style(
                f"color: {ACCENT}; text-decoration: none; padding: 0.25rem 0.75rem;"
            )


def _render_grid_nav(current_path: str, expanded: bool) -> None:
    from grid_app.entities import grouped_entities
    from nicegui import app

    from shared.grid_design import settings as grid_settings

    show_field_ops = app.storage.user.get("show_field_ops", grid_settings.SHOW_FIELD_OPS)

    with ui.expansion("⚡ Grid Design", value=expanded).classes("w-full").style("color: #e2e8f0"):
        groups = grouped_entities()
        for group_name in GRID_NAV_GROUP_ORDER:
            specs = groups.get(group_name)
            if not specs:
                continue
            if group_name == "Field Ops" and not show_field_ops:
                continue
            ui.label(group_name).classes("text-bold").style(
                "color: #94a3b8; font-size: 0.8rem; padding: 0.25rem 0.5rem 0;"
            )
            for spec in specs:
                _nav_button(
                    f"{spec.icon} {spec.label}",
                    f"/grid/{spec.bare}",
                    selected=current_path == f"/grid/{spec.bare}",
                )

        admin_specs = groups.get("Admin", [])
        if admin_specs:
            ui.label("⚙️ Admin").classes("text-bold").style(
                "color: #94a3b8; font-size: 0.8rem; padding: 0.25rem 0.5rem 0;"
            )
            for spec in admin_specs:
                _nav_button(
                    f"{spec.icon} {spec.label}",
                    f"/grid/{spec.bare}",
                    selected=current_path == f"/grid/{spec.bare}",
                )

        def _toggle(e) -> None:
            app.storage.user["show_field_ops"] = e.value
            ui.navigate.reload()

        ui.switch("Show Field Ops", value=show_field_ops, on_change=_toggle).props("dense").style(
            "color: #cbd5e1"
        )


def _render_status_logo() -> None:
    """Logo with a live bot-status dot, refreshed every 30s (was st.fragment)."""
    with ui.row().classes("items-center gap-2").style("padding: 0.75rem 0.75rem 0;"):
        ui.image("/assets/anansi_logo.png").classes("w-9 h-9").props("fit=contain")
        ui.label("Anansi").classes("text-lg text-bold").style("color: #ffffff")
        dot = ui.element("div").style(
            "width: 10px; height: 10px; border-radius: 9999px;"
            f" background-color: {_STATUS_COLORS['down']};"
        )
        tooltip = ui.tooltip(_STATUS_TOOLTIPS["down"])
        with dot:
            tooltip.move(dot)

    async def _refresh() -> None:
        from services.bot_status_service import get_bot_status

        status = await run.io_bound(get_bot_status)
        if status not in _STATUS_COLORS:
            status = "down"
        dot.style(f"background-color: {_STATUS_COLORS[status]}")
        tooltip.text = _STATUS_TOOLTIPS[status]

    ui.timer(30.0, _refresh)
    ui.timer(0.1, _refresh, once=True)


@contextmanager
def frame(user: dict[str, Any], current_path: str):
    """Page shell: dark sidebar with RBAC-gated nav; yields the content area."""
    ui.colors(primary=ACCENT)
    ui.query("body").style(f"background-color: {PAGE_BG}")

    email = user.get("email", "")
    can_admin = perms.can_view_bot_admin(email)
    can_grid = perms.can_view_grid(email)

    is_bot_admin_page = any(current_path == target for target, _ in BOT_ADMIN_NAV)
    is_grid_page = current_path.startswith("/grid")

    with (
        ui.left_drawer(value=True, fixed=True)
        .props("width=240")
        .style(f"background-color: {SIDEBAR_BG}; padding: 0;")
    ):
        _render_status_logo()

        with (
            ui.row()
            .classes("items-center justify-between w-full no-wrap")
            .style("padding: 0 0.75rem;")
        ):
            ui.label(user.get("name", email)).style(
                "color: #cbd5e1; font-size: 0.85rem; overflow: hidden;"
                " text-overflow: ellipsis; white-space: nowrap;"
            )
            ui.button("⏻", on_click=lambda: ui.navigate.to("/logout")).props(
                "flat dense round no-caps"
            ).style("color: #cbd5e1").tooltip("Logout")

        ui.separator().style("background-color: #334155")

        if can_admin:
            _render_bot_admin_nav(current_path, expanded=is_bot_admin_page)
        if can_grid:
            if can_admin:
                ui.separator().style("background-color: #334155")
            _render_grid_nav(current_path, expanded=is_grid_page)

    with ui.column().classes("w-full").style("max-width: 1200px; padding: 1rem 1.5rem;") as content:
        yield content


def access_denied() -> None:
    with ui.column().classes("w-full items-start"):
        ui.label("🔒 You don't have access to this section.").classes("text-lg text-bold")
        ui.label("Contact your administrator if you believe this is a mistake.")
