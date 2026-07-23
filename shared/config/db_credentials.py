"""Chat-database credential resolution.

Every service reads the chat database (Supabase) from the same two environment
variables. The pre-rename ``SUPABASE_*`` names are kept as a legacy fallback for
the duration of the migration. Centralising the ``CHAT_DB_* or SUPABASE_*``
precedence here means the fallback order lives in one place instead of being
re-spelled at each Supabase client-construction site.
"""

from __future__ import annotations

import os


def chat_db_url() -> str:
    """Chat-database URL: ``CHAT_DB_URL``, falling back to legacy ``SUPABASE_URL``."""
    return os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")


def chat_db_service_key() -> str:
    """Chat-database service key: ``CHAT_DB_SERVICE_KEY``, falling back to legacy ``SUPABASE_KEY``."""
    return os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
