"""Environment-backed settings.

Mirrors anansi's env var names so this app can share the same Supabase "chat DB"
and slot into the same DigitalOcean App Platform config. Loaded from the process
environment (and a local ``.env`` during development).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load the repo-root .env explicitly so creds resolve regardless of the process
# cwd (Streamlit/preview launchers don't always start in the repo root).
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv()  # also honor a cwd .env / real env (no-op in production)


def _first(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


# ── Shared chat DB (Supabase) ───────────────────────────────────────────────
# Accept the legacy MAIN_SUPABASE_*/SUPABASE_* names too, in the same order the
# grid_design MCP server and anansi's connections.py resolve them. In the
# mcp_servers environment SUPABASE_URL points at the Auth project, so
# MAIN_SUPABASE_URL must be preferred over it.
CHAT_DB_URL: str = _first("CHAT_DB_URL", "MAIN_SUPABASE_URL", "SUPABASE_URL")
CHAT_DB_SERVICE_KEY: str = _first("CHAT_DB_SERVICE_KEY", "MAIN_SUPABASE_KEY", "SUPABASE_KEY")

# ── Access control ──────────────────────────────────────────────────────────
GRID_DESIGN_ALLOWED_USERS: str = os.getenv("GRID_DESIGN_ALLOWED_USERS", "")
# API key for the HTTP API surface (grid_app/api). Unset = open (local dev only).
GRID_DESIGN_API_KEY: str = os.getenv("GRID_DESIGN_API_KEY", "")

# ── Feature flags ────────────────────────────────────────────────────────────
SHOW_FIELD_OPS: bool = os.getenv("GRID_DESIGN_SHOW_FIELD_OPS", "false").lower() in (
    "1",
    "true",
    "yes",
)

# ── External integrations ───────────────────────────────────────────────────
EXCHANGE_RATE_API_URL: str = os.getenv(
    "EXCHANGE_RATE_API_URL", "https://open.er-api.com/v6/latest/USD"
)

# Table prefix for this app's tables inside the shared public schema.
TABLE_PREFIX = "gd_"


def is_db_configured() -> bool:
    return bool(CHAT_DB_URL and CHAT_DB_SERVICE_KEY)
