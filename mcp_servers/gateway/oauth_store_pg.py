"""Real, asyncpg-backed SingleUseStore (gateway/oauth_single_use.py's
Protocol) — the one piece of the OAuth flow this repo's test suite
deliberately never touches with a real DB connection, matching Task 4's own
explicit choice to test only against a fake (see test_oauth_single_use.py).

A short-lived connection per call, not a long-lived pool: this path fires
once per sign-in (not once per tool call, unlike the hot AuthService path),
so pooling overhead doesn't matter, and a fresh connect()/close() per call
sidesteps the loop-bound-pool lifecycle bug AuthService's own _get_db_pool
docstring documents fighting (asyncpg pools are bound to the event loop
that created them). run_gateway() runs under one long-lived uvicorn loop
for the process's whole life, so that specific bug wouldn't even reproduce
here the way it did for AuthService's poller-daemon callers — but a
short-lived connection is simpler to reason about regardless, and this path
is too infrequent for the extra round-trip to matter.

Reuses AuthService's own AUTH_DB_* env vars and ssl="require" (see
shared/auth/auth_service.py's _get_db_pool) rather than inventing a new
connection-string scheme — same Supabase pooler, same auth story.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

# expires_at retention window for a claimed code's row - generous relative
# to the 60-second code TTL (gateway/oauth_codes.py's _CODE_TTL_SECONDS);
# see oauth_single_use.py's own docstring for why correctness never depends
# on cleanup actually running against this column.
_RETENTION = timedelta(hours=1)


class PgSingleUseStore:  # pragma: no cover — real DB connection, no fakes
    """Satisfies gateway.oauth_single_use.SingleUseStore against the real
    mcp_gateway_oauth_codes table (db/migrations/0032).
    """

    async def try_redeem(self, code_id: str, expires_at: Optional[datetime] = None) -> bool:
        import asyncpg

        row_expires_at = expires_at or (datetime.now(timezone.utc) + _RETENTION)
        conn = await asyncpg.connect(
            host=os.environ["AUTH_DB_HOST"],
            port=int(os.environ.get("AUTH_DB_PORT", "6543")),
            database=os.environ.get("AUTH_DB_NAME", "postgres"),
            user=os.environ["AUTH_DB_USER"],
            password=os.environ["AUTH_DB_PASSWORD"],
            ssl="require",
            statement_cache_size=0,  # disable prepared statements for PgBouncer
        )
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO mcp_gateway_oauth_codes (code_id, expires_at)
                VALUES ($1, $2)
                ON CONFLICT (code_id) DO NOTHING
                RETURNING code_id
                """,
                code_id,
                row_expires_at,
            )
            return row is not None
        finally:
            await conn.close()
