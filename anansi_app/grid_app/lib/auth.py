"""Google OAuth + email allow-list, adapted from anansi_app/components/auth.py.

Differences from anansi:
  * Reads the dedicated ``GRID_DESIGN_ALLOWED_USERS`` env var (so this app has its
    own access list, independently managed).
  * No staff-org DB fallback and no logo asset — pure whitelist gate.

Streamlit's ``st.login()`` wants OAuth config in ``.streamlit/secrets.toml``; DO
App Platform has no secrets files, so (as in anansi) we inject env vars into
``st.secrets`` at import time. ``start.sh`` also writes a secrets.toml at boot —
either path works.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from typing import Any, Dict, Set

import streamlit as st


def _load_auth_secrets_from_env() -> None:
    try:
        if hasattr(st, "secrets") and "auth" in st.secrets:
            return
    except Exception:
        pass

    client_id = os.getenv("AUTH_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("AUTH_CLIENT_SECRET") or os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return

    cookie_secret = (
        os.getenv("AUTH_COOKIE_SECRET") or hashlib.sha256(client_id.encode()).hexdigest()
    )
    redirect_uri = os.getenv("AUTH_REDIRECT_URI", "http://localhost:8501/oauth2callback")
    server_metadata_url = os.getenv(
        "AUTH_SERVER_METADATA_URL",
        "https://accounts.google.com/.well-known/openid-configuration",
    )

    auth_config = {
        "auth": {
            "redirect_uri": redirect_uri,
            "cookie_secret": cookie_secret,
            "client_id": client_id,
            "client_secret": client_secret,
            "server_metadata_url": server_metadata_url,
        }
    }
    try:
        from streamlit.runtime.secrets import secrets_singleton

        if secrets_singleton._secrets is None:
            secrets_singleton._secrets = {}
        secrets_singleton._secrets.update(auth_config)
        for key, value in auth_config.items():
            secrets_singleton._maybe_set_environment_variable(key, value)
    except Exception:
        print("Warning: Failed to load auth secrets from env", file=sys.stderr)


_load_auth_secrets_from_env()


def _parse_email_whitelist(whitelist_str: str) -> Set[str]:
    """Tolerant parse (commas, semicolons, newlines); lowercased for comparison."""
    if not whitelist_str:
        return set()
    emails = re.split(r"[,;\n]+", whitelist_str)
    return {e.strip().lower() for e in emails if e.strip()}


def _is_configured() -> bool:
    try:
        auth = st.secrets.get("auth", {})
        return all(
            auth.get(k) for k in ("client_id", "client_secret", "redirect_uri", "cookie_secret")
        )
    except Exception:
        return False


def require_authentication() -> Dict[str, Any]:
    """Gate page access; returns the authorised user's info or ``st.stop()``s."""
    # Local-dev bypass: set GRID_DESIGN_DEV_NO_AUTH=1 to skip Google OAuth entirely
    # (for localhost testing without an OAuth client). NEVER set this in production —
    # it grants access to anyone who can reach the app.
    if os.getenv("GRID_DESIGN_DEV_NO_AUTH", "").lower() in ("1", "true", "yes"):
        st.sidebar.warning("⚠️ Dev mode: auth bypassed")
        return {"email": "dev@localhost", "name": "Dev User"}

    allowed = _parse_email_whitelist(os.getenv("GRID_DESIGN_ALLOWED_USERS", ""))

    if not _is_configured():
        st.error("⚠️ Google OAuth not configured. Set AUTH_CLIENT_ID and AUTH_CLIENT_SECRET.")
        st.stop()

    if not st.user.is_logged_in:
        st.markdown(
            "<h1 style='text-align:center;'>⚡ Grid Designs &amp; BOMs</h1>", unsafe_allow_html=True
        )
        st.markdown(
            "<p style='text-align:center;'>Please sign in with your Google account.</p>",
            unsafe_allow_html=True,
        )
        if st.button("🔐 Sign in with Google", type="primary", use_container_width=True):
            st.login()
        st.stop()

    email = st.user.email
    if not email:
        st.error("❌ Could not retrieve email from Google account")
        st.stop()

    if email.lower() not in allowed:
        st.error(f"❌ Access denied: {email} is not authorised for this app.")
        st.info("Ask an administrator to add you to GRID_DESIGN_ALLOWED_USERS.")
        if st.button("🚪 Logout"):
            st.logout()
        st.stop()

    return {
        "email": email,
        "name": getattr(st.user, "name", None) or email.split("@")[0],
    }


def logout_button() -> None:
    if st.button("🚪 Logout", key="sidebar_logout"):
        st.logout()
