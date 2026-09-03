"""Two self-contained, HMAC-signed values that need no server-side storage
to validate on their own:

CorrelationState  passed as Google's own `state` parameter across the
                  /oauth/authorize -> Google -> /oauth/google-callback hop.
                  Carries everything the callback needs to resume the
                  client's original request.

IssuedCode        the gateway's own authorization code, returned to the
                  client's redirect_uri after the Google leg completes.
                  Single-use enforcement (db/migrations/0032) is the one
                  thing a signature alone can never provide - everything
                  else about validity (tamper-evidence, expiry, which
                  email, which code_challenge) is checked from the value
                  itself.
"""

from __future__ import annotations

import secrets as _secrets
import time
from dataclasses import dataclass
from typing import Optional

import jwt

_ALGORITHM = "HS256"
_CORRELATION_TTL_SECONDS = 600      # 10 minutes - covers a slow Google login
_CODE_TTL_SECONDS = 60              # short-lived, matching OAuth best practice


class CorrelationStateInvalid(Exception):
    """The state round-tripped through Google does not verify."""


class IssuedCodeInvalid(Exception):
    """The authorization code presented at /oauth/token does not verify."""


@dataclass(frozen=True)
class CorrelationState:
    client_redirect_uri: str
    client_state: str
    code_challenge: str


@dataclass(frozen=True)
class IssuedCode:
    code: str
    code_id: str


@dataclass(frozen=True)
class DecodedIssuedCode:
    email: str
    code_challenge: str
    code_id: str


def encode_correlation_state(
    *,
    client_redirect_uri: str,
    client_state: str,
    code_challenge: str,
    secret: str,
    issued_at: Optional[float] = None,
    ttl_seconds: int = _CORRELATION_TTL_SECONDS,
) -> str:
    now = time.time() if issued_at is None else issued_at
    return jwt.encode(
        {
            "client_redirect_uri": client_redirect_uri,
            "client_state": client_state,
            "code_challenge": code_challenge,
            "iat": int(now),
            "exp": int(now + ttl_seconds),
        },
        secret,
        algorithm=_ALGORITHM,
    )


def decode_correlation_state(encoded: str, secret: str) -> CorrelationState:
    try:
        claims = jwt.decode(encoded, secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise CorrelationStateInvalid(str(exc)) from exc

    try:
        return CorrelationState(
            client_redirect_uri=claims["client_redirect_uri"],
            client_state=claims["client_state"],
            code_challenge=claims["code_challenge"],
        )
    except KeyError as exc:
        raise CorrelationStateInvalid(f"Missing claim: {exc}") from exc


def issue_authorization_code(
    *,
    email: str,
    code_challenge: str,
    secret: str,
    issued_at: Optional[float] = None,
    ttl_seconds: int = _CODE_TTL_SECONDS,
) -> IssuedCode:
    now = time.time() if issued_at is None else issued_at
    code_id = _secrets.token_urlsafe(24)
    code = jwt.encode(
        {
            "email": email,
            "code_challenge": code_challenge,
            "code_id": code_id,
            "iat": int(now),
            "exp": int(now + ttl_seconds),
        },
        secret,
        algorithm=_ALGORITHM,
    )
    return IssuedCode(code=code, code_id=code_id)


def decode_issued_code(code: str, secret: str) -> DecodedIssuedCode:
    try:
        claims = jwt.decode(code, secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise IssuedCodeInvalid(str(exc)) from exc

    try:
        return DecodedIssuedCode(
            email=claims["email"],
            code_challenge=claims["code_challenge"],
            code_id=claims["code_id"],
        )
    except KeyError as exc:
        raise IssuedCodeInvalid(f"Missing claim: {exc}") from exc
