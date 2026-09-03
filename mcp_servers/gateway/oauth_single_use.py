"""Single-use enforcement, DI'd behind a tiny protocol so no real DB
connection is ever constructed in a test.

The real store (wired in gateway/app.py's production path only) does this
with one atomic statement:

    INSERT INTO mcp_gateway_oauth_codes (code_id, expires_at)
    VALUES ($1, $2)
    ON CONFLICT (code_id) DO NOTHING
    RETURNING code_id

- a non-empty result means this call won the race and claimed the code; an
empty result means it was already claimed (by a legitimate first exchange,
or a replay attempt). This is deliberately an INSERT, not the
UPDATE-with-redeemed_at-column shape sketched in the migration comment,
because INSERT ... ON CONFLICT DO NOTHING is atomic under concurrent
callers without needing a transaction or row lock - two simultaneous
redemption attempts for the same code_id can never both succeed.
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
