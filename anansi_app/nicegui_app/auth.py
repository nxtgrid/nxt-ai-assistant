"""Google OAuth (Authlib) + whitelist authorization for the NiceGUI admin app.

Replaces Streamlit's built-in ``st.login()`` OIDC. Same env vars as before:

  GOOGLE_CLIENT_ID / AUTH_CLIENT_ID          OAuth client ID
  GOOGLE_CLIENT_SECRET / AUTH_CLIENT_SECRET  OAuth client secret
  AUTH_REDIRECT_URI                          callback URL (default localhost)
  AUTH_COOKIE_SECRET                         session cookie secret (derived from
                                             client_id if unset, as before)
  AUTH_SERVER_METADATA_URL                   OIDC metadata (default Google)
  GRID_DESIGN_DEV_NO_AUTH                    LOCAL-ONLY bypass — never in prod

The callback path stays ``/oauth2callback`` so the existing Google OAuth client
registration keeps working unchanged at cutover.

Authorization (who may log in at all) delegates to ``grid_app.lib.perms``
(union of the four whitelists) — the single RBAC source of truth shared with
the Streamlit app.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Optional, cast

from fastapi import Request
from fastapi.responses import RedirectResponse
from grid_app.lib import perms
from nicegui import app as ng_app
from starlette.middleware.base import BaseHTTPMiddleware

SESSION_USER_KEY = "user"

# Paths reachable without a session. NiceGUI internals (/_nicegui) must be open
# or the login page itself cannot load its assets / websocket.
UNRESTRICTED_PREFIXES = ("/_nicegui", "/assets")
UNRESTRICTED_PATHS = {
    "/login",
    "/auth/login",
    "/oauth2callback",
    "/logout",
    "/healthz",
    "/favicon.ico",
}


def client_credentials() -> tuple[Optional[str], Optional[str]]:
    client_id = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("AUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("AUTH_CLIENT_SECRET")
    return client_id, client_secret


def is_configured() -> bool:
    client_id, client_secret = client_credentials()
    return bool(client_id and client_secret)


def cookie_secret() -> str:
    """Session-cookie signing secret; derivation matches the Streamlit app."""
    secret = os.getenv("AUTH_COOKIE_SECRET")
    if secret:
        return secret
    client_id, _ = client_credentials()
    if client_id:
        return hashlib.sha256(client_id.encode()).hexdigest()
    # Last resort for un-configured local runs (dev bypass): stable dummy.
    return hashlib.sha256(b"anansi-dev").hexdigest()


def redirect_uri() -> str:
    return os.getenv("AUTH_REDIRECT_URI", "http://localhost:8501/oauth2callback")


def dev_bypass() -> bool:
    return os.getenv("GRID_DESIGN_DEV_NO_AUTH", "").lower() in ("1", "true", "yes")


def is_authorized(email: str) -> bool:
    """Whether this email may use the app at all (Bot Admin or any grid list)."""
    return bool(perms.has_any_access(email))


def current_user(request: Optional[Request] = None) -> Optional[dict[str, Any]]:
    """The logged-in, authorized user ({email, name}) or None.

    ``request`` is accepted (unused) so callers written against the earlier
    Starlette-session design don't need to change; NiceGUI's ``app.storage.user``
    is contextvar-backed and needs no explicit request.
    """
    if dev_bypass():
        return {"email": "dev@localhost", "name": "Dev User"}
    return cast(Optional[dict[str, Any]], ng_app.storage.user.get(SESSION_USER_KEY))


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login (NiceGUI-example pattern)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            dev_bypass()
            or path in UNRESTRICTED_PATHS
            or path.startswith(UNRESTRICTED_PREFIXES)
            or ng_app.storage.user.get(SESSION_USER_KEY)
        ):
            return await call_next(request)
        return RedirectResponse("/login")


def register(app) -> None:
    """Attach the OAuth routes to the (FastAPI) app.

    Import of Authlib is local so the module can be imported (e.g. for tests)
    without the dependency installed.
    """
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    client_id, client_secret = client_credentials()
    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=os.getenv(
            "AUTH_SERVER_METADATA_URL",
            "https://accounts.google.com/.well-known/openid-configuration",
        ),
        client_kwargs={"scope": "openid email profile"},
    )

    @app.get("/auth/login")
    async def auth_login(request: Request):
        if not is_configured():
            return RedirectResponse("/login?error=unconfigured")
        return await oauth.google.authorize_redirect(request, redirect_uri())

    @app.get("/oauth2callback")
    async def oauth2callback(request: Request):
        try:
            token = await oauth.google.authorize_access_token(request)
        except Exception:
            return RedirectResponse("/login?error=oauth")
        userinfo = token.get("userinfo") or {}
        email = (userinfo.get("email") or "").lower()
        if not email:
            return RedirectResponse("/login?error=noemail")
        if not is_authorized(email):
            return RedirectResponse(f"/login?denied={email}")
        ng_app.storage.user[SESSION_USER_KEY] = {
            "email": email,
            "name": userinfo.get("name") or email.split("@")[0],
        }
        return RedirectResponse("/")

    @app.get("/logout")
    async def logout():
        ng_app.storage.user.pop(SESSION_USER_KEY, None)
        return RedirectResponse("/login")
