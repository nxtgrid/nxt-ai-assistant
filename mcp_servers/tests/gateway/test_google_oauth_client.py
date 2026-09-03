"""The gateway's real Google OAuth 2.0 client for the gateway<->Google leg
(hop 2 in the design doc's two-hop diagram). Deliberately not authlib's
Starlette-session-coupled client - see the module docstring for why - so
this is plain URL-building plus two injectable external calls (the token
exchange POST, and Google's own id_token signature verification), each
faked here rather than touching the network.
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.google_oauth_client import GoogleOAuthClient, GoogleOAuthError


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _client(http_post=None, verify_id_token=None, **kwargs):
    return GoogleOAuthClient(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="https://mcp.example.com/oauth/google-callback",
        http_post=http_post or (lambda *a, **k: _FakeResponse(200, {"id_token": "fake-id-token"})),
        verify_id_token=verify_id_token or (lambda token, audience: {"email": "user@example.com", "email_verified": True}),
        **kwargs,
    )


# --- build_authorize_url -----------------------------------------------------


def test_build_authorize_url_points_at_google_with_the_right_params():
    client = _client()
    url = client.build_authorize_url(redirect_uri="https://mcp.example.com/oauth/google-callback", state="signed-state")

    parsed = urlparse(url)
    assert parsed.netloc == "accounts.google.com"
    query = parse_qs(parsed.query)
    assert query["client_id"] == ["test-client-id"]
    assert query["redirect_uri"] == ["https://mcp.example.com/oauth/google-callback"]
    assert query["state"] == ["signed-state"]
    assert query["response_type"] == ["code"]


# --- fetch_verified_email ----------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_verified_email_returns_the_verified_claim():
    calls = []

    def fake_post(url, data, timeout):
        calls.append((url, data))
        return _FakeResponse(200, {"id_token": "fake-id-token", "access_token": "fake-access"})

    client = _client(http_post=fake_post)
    email = await client.fetch_verified_email({"code": "auth-code-123"})

    assert email == "user@example.com"
    url, data = calls[0]
    assert url == "https://oauth2.googleapis.com/token"
    assert data["code"] == "auth-code-123"
    assert data["redirect_uri"] == "https://mcp.example.com/oauth/google-callback"
    assert data["grant_type"] == "authorization_code"


@pytest.mark.asyncio
async def test_fetch_verified_email_rejects_a_declined_consent_screen():
    client = _client()
    with pytest.raises(GoogleOAuthError):
        await client.fetch_verified_email({"error": "access_denied"})


@pytest.mark.asyncio
async def test_fetch_verified_email_rejects_a_failed_token_exchange():
    client = _client(http_post=lambda *a, **k: _FakeResponse(400, {"error": "invalid_grant"}))
    with pytest.raises(GoogleOAuthError):
        await client.fetch_verified_email({"code": "auth-code-123"})


@pytest.mark.asyncio
async def test_fetch_verified_email_rejects_an_unverified_email():
    client = _client(verify_id_token=lambda token, audience: {"email": "user@example.com", "email_verified": False})
    with pytest.raises(GoogleOAuthError):
        await client.fetch_verified_email({"code": "auth-code-123"})


@pytest.mark.asyncio
async def test_fetch_verified_email_rejects_a_failed_id_token_verification():
    def raising_verify(token, audience):
        raise ValueError("signature mismatch")

    client = _client(verify_id_token=raising_verify)
    with pytest.raises(GoogleOAuthError):
        await client.fetch_verified_email({"code": "auth-code-123"})
