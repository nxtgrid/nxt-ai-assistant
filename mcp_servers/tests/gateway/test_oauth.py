"""The three OAuth routes, tested as plain async functions - not over ASGI.
Real Starlette Request/Response handling is app.py's job (see its own
docstring on why that layer stays thin and largely untested at unit level);
everything decidable without touching a real HTTP request is tested here.
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.oauth import (
    AuthorizeResult,
    GoogleCallbackResult,
    TokenResult,
    build_authorize_redirect,
    handle_google_callback,
    handle_token_request,
)
from gateway.oauth_codes import (
    decode_correlation_state,
    encode_correlation_state,
    issue_authorization_code,
)
from gateway.oauth_single_use import CodeAlreadyRedeemed
from gateway.pkce import verifier_to_challenge
from gateway.signin import SignInRejected
from gateway.tokens import verify_token

SECRET = "test-secret-not-a-real-key"
BASE_URL = "https://mcp.example.com"


class _FakeGoogleOAuth:
    """Stands in for authlib's Google client."""

    def __init__(self, email="user@example.com"):
        self.email = email
        self.authorize_redirect_args = None

    def build_authorize_url(self, redirect_uri, state):
        self.authorize_redirect_args = (redirect_uri, state)
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    async def fetch_verified_email(self, callback_query):
        return self.email


class _FakeSingleUseStore:
    def __init__(self):
        self.redeemed = set()

    async def try_redeem(self, code_id, expires_at=None):
        if code_id in self.redeemed:
            return False
        self.redeemed.add(code_id)
        return True


class _FakeAuth:
    async def get_user_permissions(self, email, user_id=None):
        class _P:
            organization_ids = ["4"]
            is_staff = False
            user_id = "u1"
            organization_short_name = "testorg"

        return _P()

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        return ["Alpha Site"]


# --- /oauth/authorize --------------------------------------------------------


def test_authorize_redirects_to_google_with_signed_state():
    google = _FakeGoogleOAuth()

    result = build_authorize_redirect(
        client_redirect_uri="http://127.0.0.1:54321/callback",
        client_state="opaque-client-value",
        code_challenge="challenge123",
        base_url=BASE_URL,
        secret=SECRET,
        google_oauth=google,
    )

    assert isinstance(result, AuthorizeResult)
    assert result.redirect_url.startswith("https://accounts.google.com/")
    redirect_uri, state = google.authorize_redirect_args
    assert redirect_uri == f"{BASE_URL}/oauth/google-callback"

    decoded = decode_correlation_state(state, SECRET)
    assert decoded.client_redirect_uri == "http://127.0.0.1:54321/callback"
    assert decoded.client_state == "opaque-client-value"
    assert decoded.code_challenge == "challenge123"


# --- /oauth/google-callback ---------------------------------------------------


@pytest.mark.asyncio
async def test_google_callback_issues_a_code_and_redirects_to_the_client():
    google = _FakeGoogleOAuth(email="user@example.com")
    correlation_state = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:54321/callback",
        client_state="opaque-client-value",
        code_challenge="challenge123",
        secret=SECRET,
    )

    result = await handle_google_callback(
        state=correlation_state,
        callback_query={},
        secret=SECRET,
        google_oauth=google,
        is_authorized=lambda email: True,
        auth_service=_FakeAuth(),
    )

    assert isinstance(result, GoogleCallbackResult)
    parsed = urlparse(result.redirect_url)
    assert parsed.netloc == "127.0.0.1:54321"
    assert parsed.path == "/callback"
    query = parse_qs(parsed.query)
    assert query["state"] == ["opaque-client-value"]  # the CLIENT's own state, unchanged
    assert "code" in query


@pytest.mark.asyncio
async def test_google_callback_rejects_a_tampered_state():
    google = _FakeGoogleOAuth()
    correlation_state = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:1/callback",
        client_state="s",
        code_challenge="c",
        secret=SECRET,
    )

    with pytest.raises(Exception):
        await handle_google_callback(
            state=correlation_state,
            callback_query={},
            secret="different-secret",
            google_oauth=google,
            is_authorized=lambda email: True,
            auth_service=_FakeAuth(),
        )


@pytest.mark.asyncio
async def test_google_callback_rejects_an_unauthorized_email():
    google = _FakeGoogleOAuth(email="stranger@example.com")
    correlation_state = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:1/callback",
        client_state="s",
        code_challenge="c",
        secret=SECRET,
    )

    with pytest.raises(SignInRejected):
        await handle_google_callback(
            state=correlation_state,
            callback_query={},
            secret=SECRET,
            google_oauth=google,
            is_authorized=lambda email: False,
            auth_service=_FakeAuth(),
        )


# --- /oauth/token -------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_exchange_succeeds_with_correct_verifier():
    verifier = "test-verifier-abc"
    challenge = verifier_to_challenge(verifier)
    issued = issue_authorization_code(email="user@example.com", code_challenge=challenge, secret=SECRET)
    store = _FakeSingleUseStore()

    result = await handle_token_request(
        code=issued.code,
        code_verifier=verifier,
        secret=SECRET,
        single_use_store=store,
        auth_service=_FakeAuth(),
    )

    assert isinstance(result, TokenResult)
    assert verify_token(result.access_token, SECRET) == "user@example.com"
    assert result.token_type == "Bearer"


@pytest.mark.asyncio
async def test_token_exchange_rejects_wrong_verifier():
    challenge = verifier_to_challenge("correct-verifier")
    issued = issue_authorization_code(email="user@example.com", code_challenge=challenge, secret=SECRET)
    store = _FakeSingleUseStore()

    with pytest.raises(Exception):
        await handle_token_request(
            code=issued.code,
            code_verifier="wrong-verifier",
            secret=SECRET,
            single_use_store=store,
            auth_service=_FakeAuth(),
        )


@pytest.mark.asyncio
async def test_token_exchange_rejects_a_replayed_code():
    verifier = "test-verifier-abc"
    challenge = verifier_to_challenge(verifier)
    issued = issue_authorization_code(email="user@example.com", code_challenge=challenge, secret=SECRET)
    store = _FakeSingleUseStore()

    await handle_token_request(
        code=issued.code, code_verifier=verifier, secret=SECRET,
        single_use_store=store, auth_service=_FakeAuth(),
    )

    with pytest.raises(CodeAlreadyRedeemed):
        await handle_token_request(
            code=issued.code, code_verifier=verifier, secret=SECRET,
            single_use_store=store, auth_service=_FakeAuth(),
        )
