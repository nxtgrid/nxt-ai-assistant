"""Role-based access control for the grid app — single source of truth.

Four independent email whitelists (env vars), parsed tolerantly (commas,
semicolons, newlines; case-insensitive):

  ALLOWED_VIEWER_EMAILS      anansi admins — Bot Admin + grid VIEW (no grid edit)
  GRID_DESIGN_ALLOWED_USERS  grid VIEW-only
  GRID_DESIGN_EDITORS        edit every grid table EXCEPT ``purchases``
  GRID_PROCUREMENT_EDITORS   edit ONLY ``purchases`` (BoS)

Grid VIEW is granted to the *union* of all four lists; a user in none of them
has no access. Edit rights are strictly separated by list (see ``can_edit``).

The current user's email is resolved from ``st.session_state["grid_user_email"]``
(set by the app entry point right after auth), falling back to ``st.user.email``.
``GRID_DESIGN_DEV_NO_AUTH=1`` is a LOCAL-ONLY bypass that grants everything — it
must never be set in production.
"""

from __future__ import annotations

import os
import re

import streamlit as st

# The one table whose edit rights are gated by GRID_PROCUREMENT_EDITORS rather
# than GRID_DESIGN_EDITORS.
PROCUREMENT_TABLE = "purchases"

# Sentinel email used by the dev bypass; every check short-circuits to allow.
_DEV_EMAIL = "dev@localhost"


def _parse(env_name: str) -> set[str]:
    """Tolerant whitelist parse (commas, semicolons, newlines); lowercased."""
    raw = os.getenv(env_name, "")
    if not raw:
        return set()
    return {e.strip().lower() for e in re.split(r"[,;\n]+", raw) if e.strip()}


def _dev_bypass() -> bool:
    return os.getenv("GRID_DESIGN_DEV_NO_AUTH", "").lower() in ("1", "true", "yes")


# ── whitelist readers (parsed fresh each call so env changes take effect) ──────
def admins() -> set[str]:
    return _parse("ALLOWED_VIEWER_EMAILS")


def viewers() -> set[str]:
    return _parse("GRID_DESIGN_ALLOWED_USERS")


def design_editors() -> set[str]:
    return _parse("GRID_DESIGN_EDITORS")


def proc_editors() -> set[str]:
    return _parse("GRID_PROCUREMENT_EDITORS")


def _grid_union() -> set[str]:
    return admins() | viewers() | design_editors() | proc_editors()


# ── current user ──────────────────────────────────────────────────────────────
def current_email() -> str:
    """Resolve the logged-in user's email (lowercased), or '' if unknown."""
    if _dev_bypass():
        return _DEV_EMAIL
    email = st.session_state.get("grid_user_email")
    if not email:
        try:
            email = st.user.email if st.user.is_logged_in else None
        except Exception:
            email = None
    return (email or "").lower()


# ── access checks ─────────────────────────────────────────────────────────────
def can_view_bot_admin(email: str | None = None) -> bool:
    if _dev_bypass():
        return True
    email = (email or current_email()).lower()
    return bool(email) and email in admins()


def can_view_grid(email: str | None = None) -> bool:
    if _dev_bypass():
        return True
    email = (email or current_email()).lower()
    return bool(email) and email in _grid_union()


def can_edit(table_bare: str, email: str | None = None) -> bool:
    """Whether ``email`` may write rows of ``table_bare``.

    Procurement editors edit only ``purchases``; design editors edit everything
    else. Admins and view-only users edit nothing.
    """
    if _dev_bypass():
        return True
    email = (email or current_email()).lower()
    if not email or not can_view_grid(email):
        return False
    if table_bare == PROCUREMENT_TABLE:
        return email in proc_editors()
    return email in design_editors()


def has_any_access(email: str | None = None) -> bool:
    """True if the user may use the app at all (Bot Admin or grid view)."""
    if _dev_bypass():
        return True
    email = (email or current_email()).lower()
    return can_view_bot_admin(email) or can_view_grid(email)
