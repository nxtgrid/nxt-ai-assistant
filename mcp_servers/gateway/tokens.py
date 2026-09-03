"""Bearer tokens for MCP clients.

The user signs in with Google (reusing anansi_app's existing OAuth client), and
the gateway issues a token they paste into their MCP client config. This buys
the same identity guarantee as full remote-MCP OAuth without standing up an
authorization server that federates to Google — see the spec's Authentication
section for why that is deferred.

The token asserts only a verified email. All authorization is re-resolved from
the database per session, so revoking access in public.accounts takes effect
without reissuing tokens.
"""

from __future__ import annotations

import time
from typing import Optional

import jwt

_ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 30 * 24 * 3600


class TokenInvalid(Exception):
    """The presented token was missing, malformed, expired or missigned."""


def issue_token(
    email: str,
    secret: str,
    issued_at: Optional[float] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Mint a bearer token asserting a Google-verified email."""
    now = time.time() if issued_at is None else issued_at
    return jwt.encode(
        {"email": email, "iat": int(now), "exp": int(now + ttl_seconds)},
        secret,
        algorithm=_ALGORITHM,
    )


def verify_token(token: str, secret: str) -> str:
    """Return the email a valid token asserts, else raise TokenInvalid."""
    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise TokenInvalid(str(exc)) from exc

    email = claims.get("email")
    if not email:
        raise TokenInvalid("Token carries no email claim")

    return str(email)
