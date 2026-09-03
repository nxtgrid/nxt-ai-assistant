"""The three OAuth routes' logic, kept as plain async functions taking a
google_oauth/auth_service/single_use_store dependency each - app.py wires
the real Starlette Request/Response and the real Google client around
these; nothing here touches either directly, matching the DI discipline
transport.py already established for the tool-calling path.
"""

from __future__ import annotations

import secrets as _secrets
from dataclasses import dataclass
from typing import Any, Callable, Dict, List
from urllib.parse import urlencode, urlparse

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

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class RedirectUriInvalid(Exception):
    """The client's redirect_uri is not an acceptable loopback address."""


@dataclass(frozen=True)
class AuthorizeResult:
    redirect_url: str


@dataclass(frozen=True)
class RegistrationResult:
    registration: Dict[str, Any]


def validate_client_redirect_uri(redirect_uri: str) -> None:
    """Raise RedirectUriInvalid unless redirect_uri is a loopback address.

    This is the security boundary the design doc actually specified ("any
    loopback address for a public client using PKCE... the standard
    native-app pattern, RFC 8252") and that the first implementation
    omitted. Without it, /oauth/authorize would deliver the authorization
    code to ANY host the caller named: an attacker crafts an authorize URL
    carrying their own redirect_uri AND their own code_challenge, gets an
    already-authorized user to click it, and collects a code they can
    redeem for that user's token. PKCE cannot help there - the attacker
    chose the challenge, so they hold the verifier - and the victim sees
    nothing but a normal, genuine Google login.

    Loopback only, per RFC 8252 section 7.3, which is all any native/CLI
    client needs (Claude Code included). A future browser-based client
    wanting a remote https redirect would need its redirect_uris actually
    registered and checked against the client_id, which is a deliberate
    change, not something to leave open by default.
    """
    parsed = urlparse(redirect_uri)
    if parsed.scheme not in ("http", "https"):
        raise RedirectUriInvalid(f"redirect_uri must be http(s), got {parsed.scheme!r}")

    # .hostname (not .netloc) parses the real host out, strips any port, and
    # unwraps IPv6 brackets - startswith() on the raw string would accept
    # http://127.0.0.1.evil.example/, a remote host.
    host = parsed.hostname
    if host is None or host.lower() not in _LOOPBACK_HOSTS:
        raise RedirectUriInvalid(f"redirect_uri must be a loopback address, got host {host!r}")


def handle_client_registration(request_body: Dict[str, Any]) -> RegistrationResult:
    """RFC 7591 dynamic client registration.

    Claude Code's CLI refuses to start the flow without this ("Incompatible
    auth server: does not support dynamic client registration"), so the
    original design's fixed-client-ID plan - spec-compliant, but assuming a
    client that lets you paste an ID in - doesn't work for it.

    The issued client_id is deliberately NOT persisted or validated later.
    For a public client it is an identifier, not a credential: it is not
    secret, it authenticates nothing, and OAuth 2.1 puts the security for
    this flow on PKCE plus redirect_uri validation (see
    validate_client_redirect_uri above) - both of which are enforced per
    authorization request, independent of who registered. Storing
    registrations would add a table and an expiry story while changing none
    of the actual gates: Google-verified identity and the
    grid_app.lib.perms whitelist still decide whether any token is issued
    at all. If redirect_uris ever need to be checked against the
    registration that named them (a browser-client change), that is when
    this gains storage.
    """
    redirect_uris: List[str] = list(request_body.get("redirect_uris") or [])
    registration: Dict[str, Any] = {
        "client_id": _secrets.token_urlsafe(24),
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }
    client_name = request_body.get("client_name")
    if client_name:
        registration["client_name"] = client_name
    return RegistrationResult(registration=registration)


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

    Validated BEFORE anything is signed or any Google redirect is built: a
    redirect_uri that reaches the signed correlation state is one the
    callback will faithfully deliver an authorization code to, so this is
    the only place it can be stopped.
    """
    validate_client_redirect_uri(client_redirect_uri)

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

    # Re-checked even though the state is HMAC-signed and /oauth/authorize
    # already validated it: this makes the guarantee unconditional rather
    # than dependent on every path that signs a state having checked first,
    # and it closes the correlation-TTL window in which a state signed by an
    # older build could still be redeemed here.
    validate_client_redirect_uri(correlation.client_redirect_uri)

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
