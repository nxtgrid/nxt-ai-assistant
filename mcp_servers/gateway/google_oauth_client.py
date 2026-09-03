"""Real Google OAuth 2.0 client for the gateway's own server-to-server leg
(the /oauth/google-callback -> Google's token endpoint hop — hop 2 in the
design doc's two-hop diagram).

Deliberately NOT authlib's Starlette-coupled OAuth client: that API reads
and writes Starlette session state for its own CSRF `state` handling, which
this gateway has no use for — gateway/oauth_codes.py's signed correlation
state already carries everything needed, and the ASGI app is otherwise
stateless throughout (see app.py's own stateless=True comment). Talking to
Google's endpoints directly with plain HTTP keeps that property intact.

ID token verification uses google-auth's own verify_oauth2_token — already
a pinned dependency (used elsewhere in this repo for Docs/Drive
service-account auth) — rather than hand-rolling JWT/JWKS signature
verification here: it fetches and caches Google's current signing keys and
validates signature, issuer and expiry, exactly the part of "verify a
Google-issued token" most worth not reimplementing by hand.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlencode

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token

_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def _env_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("AUTH_CLIENT_ID", "")


def _env_client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET") or os.environ.get("AUTH_CLIENT_SECRET", "")


class GoogleOAuthError(Exception):
    """The Google leg failed: consent was declined, the code exchange was
    rejected, or the returned id_token didn't verify."""


class GoogleOAuthClient:
    """client_id/client_secret default to GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET,
    falling back to AUTH_CLIENT_ID/AUTH_CLIENT_SECRET — the exact same
    precedence anansi_app/nicegui_app/auth.py's own client_credentials()
    already uses for its own (unrelated) OAuth login flow. This lets a
    deployment reuse anansi-app's existing Web-application OAuth client (it
    supports more than one redirect URI) for the gateway's callback too,
    with zero new secret needed, as long as AUTH_CLIENT_ID/AUTH_CLIENT_SECRET
    are declared at APP level in the DO spec rather than scoped to the
    anansi-app service alone — only app-level env vars are visible to
    mcp-gateway's own container. Empty-string (not required) if nothing at
    all is set, so constructing this with no arguments never raises just
    because the env isn't configured — build_authorize_url still produces a
    syntactically valid URL either way. This is what lets app.py
    default-construct one for every build_asgi_app call without every
    caller (including tests that never exercise the Google leg at all)
    needing to supply real credentials.

    redirect_uri is fixed at construction, not threaded through per call:
    it is always base_url + "/oauth/google-callback" — a property of this
    deployment, not of any individual request — and fetch_verified_email
    has no other way to learn it, since gateway/oauth.py's
    handle_google_callback (already committed, its call signature is not
    revisited here) calls fetch_verified_email(callback_query) alone. A
    per-instance attribute set once at construction avoids the alternative
    of stashing it on the client per-call, which would race across
    concurrent sign-ins on this same shared, long-lived client.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        *,
        http_post: Callable[..., Any] = requests.post,
        verify_id_token: Optional[Callable[[str, Optional[str]], Dict[str, Any]]] = None,
    ) -> None:
        self.client_id = client_id if client_id is not None else _env_client_id()
        self.client_secret = client_secret if client_secret is not None else _env_client_secret()
        self._redirect_uri = redirect_uri or ""
        self._http_post = http_post
        self._verify_id_token = verify_id_token or self._real_verify_id_token

    def _real_verify_id_token(self, token: str, audience: Optional[str]) -> Dict[str, Any]:
        return google_id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience=audience)

    def build_authorize_url(self, redirect_uri: str, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "access_type": "online",
                "prompt": "select_account",
            }
        )
        return f"{_AUTHORIZE_ENDPOINT}?{query}"

    async def fetch_verified_email(self, callback_query: Dict[str, Any]) -> str:
        """Exchange the code for tokens, then verify the id_token's signature
        and read its email claim. Raises GoogleOAuthError on any failure —
        declined consent, a rejected code, or a token that doesn't verify —
        so gateway/oauth.py's caller has one exception type to handle.
        """
        if "code" not in callback_query:
            error = callback_query.get("error", "no code returned")
            raise GoogleOAuthError(f"Google callback carried no code: {error}")

        response = self._http_post(
            _TOKEN_ENDPOINT,
            data={
                "code": callback_query["code"],
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        if response.status_code != 200:
            raise GoogleOAuthError(f"Google token exchange failed: {response.status_code} {response.text}")

        token_response = response.json()
        id_token_str = token_response.get("id_token")
        if not id_token_str:
            raise GoogleOAuthError("Google token response carried no id_token")

        try:
            claims = self._verify_id_token(id_token_str, self.client_id)
        except GoogleOAuthError:
            raise
        except Exception as exc:  # google-auth raises several distinct exception types
            raise GoogleOAuthError(f"Google id_token failed verification: {exc}") from exc

        if not claims.get("email_verified", False):
            raise GoogleOAuthError(f"Google email not verified: {claims.get('email')}")

        email = claims.get("email")
        if not email:
            raise GoogleOAuthError("Google id_token carried no email claim")

        return str(email)
