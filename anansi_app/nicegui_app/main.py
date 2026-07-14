"""NiceGUI admin app entry point — successor to the retired Streamlit ``app.py``.

App shell, Google OAuth, RBAC-gated nav/routing, `/healthz`, and every page.

Run locally: ``python -m nicegui_app.main`` from `anansi_app/` (see start.sh for
the production launch, which also co-launches the broadcast scheduler).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Request
from fastapi.responses import JSONResponse
from grid_app.lib import perms
from nicegui import app, ui

from nicegui_app import auth, layout

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
app.add_static_files("/assets", str(ASSETS_DIR))

app.add_middleware(auth.AuthMiddleware)
auth.register(app)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@ui.page("/login")
def login_page(request: Request) -> None:
    if auth.current_user():
        ui.navigate.to("/")
        return

    error = request.query_params.get("error", "")
    denied = request.query_params.get("denied", "")

    with ui.column().classes("absolute-center items-center gap-3"):
        logo_path = ASSETS_DIR / "anansi_logo_nobg.png"
        if logo_path.exists():
            ui.image("/assets/anansi_logo_nobg.png").classes("w-24")
        ui.label("Anansi").classes("text-h4")
        ui.label("Please sign in with your Google account to access Anansi.")
        if denied:
            ui.label(f"Access denied: {denied} is not authorized.").classes("text-negative")
        elif error == "unconfigured":
            ui.label("Google OAuth is not configured on this server.").classes("text-negative")
        elif error:
            ui.label("Sign-in failed. Please try again.").classes("text-negative")
        ui.button("🔐 Sign in with Google", on_click=lambda: ui.navigate.to("/auth/login")).props(
            "color=primary"
        )


# NB: the admin chat page is served at /conversations, NOT /chat — the DO ingress
# routes the /chat prefix to the anansi-bot orchestrator (its Telegram webhook),
# so a /chat page here 405s. See DEPLOY_NICEGUI.md.
@ui.page("/conversations")
async def chat_route() -> None:
    user = auth.current_user()
    if not user:
        ui.navigate.to("/login")
        return
    with layout.frame(user, "/conversations"):
        if not perms.can_view_bot_admin(user["email"]):
            layout.access_denied()
            return
        from nicegui_app.pages import chat

        await chat.render(user)


@ui.page("/documents")
async def documents_route() -> None:
    user = auth.current_user()
    if not user:
        ui.navigate.to("/login")
        return
    with layout.frame(user, "/documents"):
        if not perms.can_view_bot_admin(user["email"]):
            layout.access_denied()
            return
        from nicegui_app.pages import documents

        await documents.render()


@ui.page("/agents")
async def agents_route() -> None:
    user = auth.current_user()
    if not user:
        ui.navigate.to("/login")
        return
    with layout.frame(user, "/agents"):
        if not perms.can_view_bot_admin(user["email"]):
            layout.access_denied()
            return
        from nicegui_app.pages import agents

        await agents.render()


@ui.page("/settings")
async def settings_route() -> None:
    user = auth.current_user()
    if not user:
        ui.navigate.to("/login")
        return
    with layout.frame(user, "/settings"):
        if not perms.can_view_bot_admin(user["email"]):
            layout.access_denied()
            return
        from nicegui_app.pages import settings

        await settings.render()


@ui.page("/grid/{bare}")
async def grid_page(bare: str, request: Request) -> None:
    user = auth.current_user()
    if not user:
        ui.navigate.to("/login")
        return
    with layout.frame(user, f"/grid/{bare}"):
        from nicegui_app.pages import grid

        await grid.render(user, bare, dict(request.query_params))


@ui.page("/")
def index_page() -> None:
    user = auth.current_user()
    if not user:
        ui.navigate.to("/login")
        return
    can_admin = perms.can_view_bot_admin(user["email"])
    can_grid = perms.can_view_grid(user["email"])
    with layout.frame(user, "/"):
        if can_grid:
            ui.label("⚡ Grid Designs & BOMs").classes("text-h5")
            ui.label("Select a table from the sidebar to begin.")
        elif can_admin:
            ui.navigate.to("/conversations")
        else:
            layout.access_denied()


def create_app() -> None:
    """Entry point for ``python main.py`` (dev) and the prod launcher."""
    ui.run(
        title="Anansi",
        favicon=str(ASSETS_DIR / "favicon-32.png"),
        host=os.getenv("HOST", "0.0.0.0"),  # bind all interfaces for the container
        port=int(os.getenv("PORT", "8501")),
        storage_secret=auth.cookie_secret(),
        reload=os.getenv("NICEGUI_RELOAD", "false").lower() == "true",
        show=False,  # never try to open a browser in a headless container
    )


if __name__ in {"__main__", "__mp_main__"}:
    create_app()
