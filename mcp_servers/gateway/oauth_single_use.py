"""Single-use enforcement, DI'd behind a tiny protocol so no real DB
connection is ever constructed in a test.

The real store (gateway/oauth_store_chat_db.py, wired in app.py's
production path only) claims a code with a plain INSERT against
db/migrations/0032's table in the chat DB, over PostgREST. The PRIMARY KEY
conflict is the guarantee: it is evaluated inside that single statement, so
two concurrent redemptions of the same code_id can never both be told they
claimed it - no transaction or row lock needed. PostgREST surfaces that
conflict as a 409, which the store maps to "already redeemed".

An earlier version of that store used asyncpg against AUTH_DB_*, copying
AuthService's connection pattern. That database is read-only AND is not the
one db/migrations/ targets, so the insert could never have worked; it
reached production and failed as UndefinedTableError on the last step of an
otherwise-complete sign-in. Worth remembering that "which database" is a
real design decision here, not a detail to inherit from whatever module was
nearest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol


class CodeAlreadyRedeemed(Exception):
    """This authorization code has already been exchanged for a token."""


class SingleUseStore(Protocol):
    async def try_redeem(self, code_id: str, expires_at: datetime) -> bool:
        """Atomically claim code_id. True if this call claimed it."""
        ...


async def redeem_once(
    code_id: str, store: SingleUseStore, expires_at: Optional[datetime] = None
) -> None:
    """Raise CodeAlreadyRedeemed unless this is the first redemption.

    expires_at is for the row's own cleanup convenience only - it is
    deliberately NOT the issued code's own exp claim (decode_issued_code
    never surfaces that; only email/code_challenge/code_id). The real
    production store should default this to "now + a fixed retention
    window" (an hour is generous given codes live 60 seconds) rather than
    trying to thread the code's actual expiry through - db/migrations/
    0032's own comment is explicit that correctness never depends on
    cleanup running at all, only on the PRIMARY KEY / ON CONFLICT check.
    """
    claimed = await store.try_redeem(code_id, expires_at)
    if not claimed:
        raise CodeAlreadyRedeemed(f"Authorization code {code_id!r} was already redeemed")
