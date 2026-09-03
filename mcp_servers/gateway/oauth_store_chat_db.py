"""The real single-use store for OAuth authorization codes, against the
chat DB via PostgREST.

Replaces an asyncpg store that connected with AUTH_DB_*, mirroring
AuthService's connection pattern. That was wrong twice over, and only
production said so: AUTH_DB is READ-ONLY, so the insert could never have
succeeded, and it is a different database from the one db/migrations/
targets, so 0032's table was never going to exist there. The symptom was an
UndefinedTableError 500 on /oauth/token - the very last step of an
otherwise-complete OAuth flow, after the user had already signed in with
Google successfully.

CHAT_DB_URL and CHAT_DB_SERVICE_KEY are app-level env vars every service
already inherits, and are the same credentials the rest of the repo uses to
write to chat_db - known-good in production rather than assumed, which
matters more than elegance here after several deploy cycles spent on
unverified guesses. (CHAT_DB_POSTGRES_URL exists at app level too and would
allow keeping asyncpg, but nothing in this repo reads it and its value is
encrypted, so it is unverifiable from here.)

Atomicity is unchanged from the asyncpg version: a plain INSERT whose
PRIMARY KEY conflict PostgREST surfaces as a 409. The uniqueness check
happens inside that single statement, so two concurrent redemptions of the
same code can never both be told they claimed it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests

# Retention for a claimed code's row - generous next to the 60-second code
# TTL (gateway/oauth_codes.py's _CODE_TTL_SECONDS). db/migrations/0032's own
# comment is explicit that correctness never depends on cleanup running,
# only on the PRIMARY KEY conflict.
_RETENTION = timedelta(hours=1)


class SingleUseStoreError(Exception):
    """The store could not determine whether this code was already used."""


class ChatDbSingleUseStore:
    """Satisfies gateway.oauth_single_use.SingleUseStore against
    db/migrations/0032's mcp_gateway_oauth_codes table in the chat DB.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        service_key: Optional[str] = None,
        *,
        http_post: Callable[..., Any] = requests.post,
    ) -> None:
        raw_base = base_url if base_url is not None else os.environ.get("CHAT_DB_URL", "")
        self._base_url = raw_base.rstrip("/")
        self._service_key = (
            service_key if service_key is not None else os.environ.get("CHAT_DB_SERVICE_KEY", "")
        )
        self._http_post = http_post

    async def try_redeem(self, code_id: str, expires_at: Optional[datetime] = None) -> bool:
        """True if this call claimed code_id, False if it was already used.

        Raises SingleUseStoreError on anything else rather than guessing:
        returning False would report a legitimate first redemption as a
        replay, and returning True would issue a token while the single-use
        row was never actually written.
        """
        row_expires_at = expires_at or (datetime.now(timezone.utc) + _RETENTION)
        response = self._http_post(
            f"{self._base_url}/rest/v1/mcp_gateway_oauth_codes",
            json={"code_id": code_id, "expires_at": row_expires_at.isoformat()},
            headers={
                "apikey": self._service_key,
                "Authorization": f"Bearer {self._service_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            timeout=10,
        )

        if response.status_code in (200, 201, 204):
            return True
        if response.status_code == 409:
            return False

        raise SingleUseStoreError(
            f"Unexpected status {response.status_code} claiming authorization code: "
            f"{getattr(response, 'text', '')[:200]}"
        )
