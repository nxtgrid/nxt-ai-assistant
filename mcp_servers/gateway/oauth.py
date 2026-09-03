"""The three OAuth routes' logic, kept as plain async functions taking a
google_oauth/auth_service/single_use_store dependency each - app.py wires
the real Starlette Request/Response and the real Google client around
these; nothing here touches either directly, matching the DI discipline
transport.py already established for the tool-calling path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict
from urllib.parse import urlencode

from gateway.oauth_codes import (
    decode_correlation_state,
    decode_issued_code,
    encode_correlation_state,
    issue_authorization_code,
)
from gateway.oauth_single_use import redeem_once
from gateway.pkce import verify_challenge
from gateway.session import SessionDenied, resolve_session
from gateway.signin import SignInRejected
from gateway.tokens import issue_token


@dataclass(frozen=True)
class AuthorizeResult:
    redirect_url: str


@dataclass(frozen=True)
class GoogleCallbackResult:
    redirect_url: str


@dataclass(frozen=True)
class TokenResult:
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 30 * 24 * 3600  # matches gateway.tokens.DEFAULT_TTL_SECONDS


def build_authorize_redirect(
    *,
    client_redirect_uri: str,
    client_state: str,
    code_challenge: str,
    base_url: str,
    secret: str,
    google_oauth: Any,
) -> AuthorizeResult:
    """Start the Google leg. The CLIENT's redirect_uri (Claude Code's own
    loopback address) is never sent to Google at all - only encoded into the
    signed correlation state Google faithfully round-trips back to us.
    """
    state = encode_correlation_state(
        client_redirect_uri=client_redirect_uri,
        client_state=client_state,
        code_challenge=code_challenge,
        secret=secret,
    )
    redirect_url = google_oauth.build_authorize_url(
        redirect_uri=f"{base_url}/oauth/google-callback",
        state=state,
    )
    return AuthorizeResult(redirect_url=redirect_url)


async def handle_google_callback(
    *,
    state: str,
    callback_query: Dict[str, Any],
    secret: str,
    google_oauth: Any,
    is_authorized: Callable[[str], bool],
    auth_service: Any,
) -> GoogleCallbackResult:
    """Google's own redirect target. Verifies the correlation state, gets
    the verified email from Google, checks the same two gates signin.py's
    mint_token_for_email checks (whitelist + resolvable session) - but
    doesn't call that function directly, since it issues a long-lived
    access token immediately; here we only issue a short-lived
    authorization CODE, matching the authorization_code grant shape.
    """
    correlation = decode_correlation_state(state, secret)
    email = await google_oauth.fetch_verified_email(callback_query)

    if not is_authorized(email):
        raise SignInRejected(f"{email} is not authorized for this application")

    try:
        await resolve_session(email, auth_service)
    except SessionDenied as exc:
        raise SignInRejected(f"{email} maps to no organization") from exc

    issued = issue_authorization_code(
        email=email, code_challenge=correlation.code_challenge, secret=secret
    )

    query = urlencode({"code": issued.code, "state": correlation.client_state})
    return GoogleCallbackResult(redirect_url=f"{correlation.client_redirect_uri}?{query}")


async def handle_token_request(
    *,
    code: str,
    code_verifier: str,
    secret: str,
    single_use_store: Any,
    auth_service: Any,
) -> TokenResult:
    """Exchange a code for the gateway's own long-lived access token.

    Order matters: decode (tamper/expiry check) and verify PKCE BEFORE
    touching the single-use store, so a malformed or forged code can never
    burn a legitimate code_id's single-use slot.
    """
    decoded = decode_issued_code(code, secret)
    verify_challenge(code_verifier, decoded.code_challenge)

    await redeem_once(decoded.code_id, single_use_store)

    access_token = issue_token(decoded.email, secret)
    return TokenResult(access_token=access_token)
