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
    RedirectUriInvalid,
    TokenResult,
    build_authorize_redirect,
    handle_client_registration,
    handle_google_callback,
    handle_token_request,
    parse_redirect_allowlist,
    validate_client_redirect_uri,
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


# --- redirect_uri validation (RFC 8252 loopback) -----------------------------
#
# /oauth/authorize took the client's redirect_uri verbatim and faithfully
# delivered the authorization code there after a successful Google login,
# with no validation. That is a code-exfiltration hole, not a cosmetic gap:
# an attacker crafts an authorize URL with their own redirect_uri AND their
# own code_challenge, gets an already-authorized user to click it, and
# receives a code they can redeem (PKCE does not help - the attacker chose
# the challenge, so they hold the verifier). The design spec called for
# "any loopback address for a public client using PKCE (RFC 8252)"; only
# the permissive half was implemented.


def test_authorize_accepts_a_loopback_ip_redirect_uri():
    google = _FakeGoogleOAuth()
    result = build_authorize_redirect(
        client_redirect_uri="http://127.0.0.1:54321/callback",
        client_state="s",
        code_challenge="c",
        base_url=BASE_URL,
        secret=SECRET,
        google_oauth=google,
    )
    assert result.redirect_url.startswith("https://accounts.google.com/")


def test_authorize_accepts_a_localhost_redirect_uri():
    google = _FakeGoogleOAuth()
    result = build_authorize_redirect(
        client_redirect_uri="http://localhost:1410/oauth/callback",
        client_state="s",
        code_challenge="c",
        base_url=BASE_URL,
        secret=SECRET,
        google_oauth=google,
    )
    assert result.redirect_url.startswith("https://accounts.google.com/")


def test_authorize_accepts_an_ipv6_loopback_redirect_uri():
    google = _FakeGoogleOAuth()
    result = build_authorize_redirect(
        client_redirect_uri="http://[::1]:5000/cb",
        client_state="s",
        code_challenge="c",
        base_url=BASE_URL,
        secret=SECRET,
        google_oauth=google,
    )
    assert result.redirect_url.startswith("https://accounts.google.com/")


def test_authorize_rejects_a_remote_redirect_uri():
    google = _FakeGoogleOAuth()
    with pytest.raises(RedirectUriInvalid):
        build_authorize_redirect(
            client_redirect_uri="https://evil.example/steal",
            client_state="s",
            code_challenge="c",
            base_url=BASE_URL,
            secret=SECRET,
            google_oauth=google,
        )


def test_authorize_rejects_a_host_that_merely_starts_with_the_loopback_ip():
    # The classic prefix-matching trap: 127.0.0.1.evil.example is a REMOTE
    # host, so the check has to parse the hostname, not startswith() a string.
    google = _FakeGoogleOAuth()
    with pytest.raises(RedirectUriInvalid):
        build_authorize_redirect(
            client_redirect_uri="http://127.0.0.1.evil.example/steal",
            client_state="s",
            code_challenge="c",
            base_url=BASE_URL,
            secret=SECRET,
            google_oauth=google,
        )


def test_authorize_accepts_a_hosted_client_redirect_uri_that_is_allow_listed():
    # The MCP_GATEWAY_REDIRECT_ALLOWLIST case: a HOSTED client (Claude
    # Desktop, claude.ai) redirects to its OWN fixed backend, not a loopback
    # address - this is the exact URL confirmed from Anthropic's own docs.
    google = _FakeGoogleOAuth()
    result = build_authorize_redirect(
        client_redirect_uri="https://claude.ai/api/mcp/auth_callback",
        client_state="s",
        code_challenge="c",
        base_url=BASE_URL,
        secret=SECRET,
        google_oauth=google,
        extra_allowed_redirect_uris=frozenset({"https://claude.ai/api/mcp/auth_callback"}),
    )
    assert result.redirect_url.startswith("https://accounts.google.com/")


def test_authorize_still_rejects_a_remote_redirect_uri_not_on_the_allowlist():
    # extra_allowed_redirect_uris must not become a general remote-redirect
    # escape hatch - only its own exact entries pass, everything else still
    # goes through the loopback check and fails it.
    google = _FakeGoogleOAuth()
    with pytest.raises(RedirectUriInvalid):
        build_authorize_redirect(
            client_redirect_uri="https://evil.example/steal",
            client_state="s",
            code_challenge="c",
            base_url=BASE_URL,
            secret=SECRET,
            google_oauth=google,
            extra_allowed_redirect_uris=frozenset({"https://claude.ai/api/mcp/auth_callback"}),
        )


def test_the_allowlist_check_is_an_exact_string_match_not_a_host_match():
    # An attacker on the same allow-listed HOST but a different path must
    # still be rejected - matching on host (or scheme+host) here would widen
    # the hole this whole mechanism exists to close, just to "any path on
    # claude.ai" instead of "any host at all".
    with pytest.raises(RedirectUriInvalid):
        validate_client_redirect_uri(
            "https://claude.ai/some/other/attacker/controlled/path",
            extra_allowed=frozenset({"https://claude.ai/api/mcp/auth_callback"}),
        )


def test_an_empty_allowlist_behaves_exactly_like_no_allowlist():
    # Regression guard: the default (frozenset()) must not silently open
    # anything up - loopback-only behavior is unchanged when unset.
    with pytest.raises(RedirectUriInvalid):
        validate_client_redirect_uri("https://evil.example/steal", extra_allowed=frozenset())
    validate_client_redirect_uri("http://127.0.0.1:9/cb", extra_allowed=frozenset())


class TestParseRedirectAllowlist:
    def test_splits_on_commas_and_strips_whitespace(self):
        raw = " https://claude.ai/api/mcp/auth_callback ,https://b.example/cb "
        assert parse_redirect_allowlist(raw) == frozenset(
            {"https://claude.ai/api/mcp/auth_callback", "https://b.example/cb"}
        )

    def test_drops_empty_entries(self):
        assert parse_redirect_allowlist("https://a.example/cb,,  ,") == frozenset(
            {"https://a.example/cb"}
        )

    def test_empty_string_yields_an_empty_set(self):
        assert parse_redirect_allowlist("") == frozenset()
        assert parse_redirect_allowlist("   ") == frozenset()

    def test_a_single_entry_needs_no_comma(self):
        assert parse_redirect_allowlist("https://claude.ai/api/mcp/auth_callback") == frozenset(
            {"https://claude.ai/api/mcp/auth_callback"}
        )


def test_authorize_rejects_a_redirect_uri_with_no_scheme():
    google = _FakeGoogleOAuth()
    with pytest.raises(RedirectUriInvalid):
        build_authorize_redirect(
            client_redirect_uri="127.0.0.1:8080/cb",
            client_state="s",
            code_challenge="c",
            base_url=BASE_URL,
            secret=SECRET,
            google_oauth=google,
        )


# --- RFC 7591 dynamic client registration ------------------------------------
#
# Claude Code's CLI refuses to proceed without it: "Incompatible auth server:
# does not support dynamic client registration" (from its own debug log,
# after discovery had already succeeded). The original design listed DCR as
# a non-goal on the reasoning that a fixed client ID is spec-compliant - true
# for a client that lets you paste one in, which the CLI does not.


def test_registration_issues_a_client_id_without_a_secret():
    result = handle_client_registration({"redirect_uris": ["http://127.0.0.1:54321/callback"]})
    assert result.registration["client_id"]
    # Public client: PKCE is the security boundary, so no secret is issued
    # and none should be expected at the token endpoint.
    assert "client_secret" not in result.registration
    assert result.registration["token_endpoint_auth_method"] == "none"


def test_registration_echoes_the_requested_redirect_uris():
    result = handle_client_registration({"redirect_uris": ["http://127.0.0.1:1/cb"]})
    assert result.registration["redirect_uris"] == ["http://127.0.0.1:1/cb"]


def test_registration_issues_a_distinct_client_id_each_time():
    first = handle_client_registration({"redirect_uris": ["http://127.0.0.1:1/cb"]})
    second = handle_client_registration({"redirect_uris": ["http://127.0.0.1:1/cb"]})
    assert first.registration["client_id"] != second.registration["client_id"]


@pytest.mark.asyncio
async def test_google_callback_re_validates_the_redirect_uri():
    # Defence in depth. The correlation state is HMAC-signed, so a state that
    # passed /oauth/authorize's check cannot be tampered with in flight - but
    # re-checking at the callback makes the guarantee unconditional rather
    # than "as long as every path that signs a state validated first", and it
    # closes the ~10-minute window (the correlation TTL) in which a state
    # signed by a BUILD THAT PREDATES the /authorize check could still be
    # redeemed after this one deploys.
    google = _FakeGoogleOAuth(email="user@example.com")
    hostile_state = encode_correlation_state(
        client_redirect_uri="https://evil.example/steal",
        client_state="s",
        code_challenge="c",
        secret=SECRET,
    )

    with pytest.raises(RedirectUriInvalid):
        await handle_google_callback(
            state=hostile_state,
            callback_query={},
            secret=SECRET,
            google_oauth=google,
            is_authorized=lambda email: True,
            auth_service=_FakeAuth(),
        )


@pytest.mark.asyncio
async def test_google_callback_honours_the_allowlist_on_its_own_re_validation():
    # The re-check above must apply the SAME extra_allowed_redirect_uris as
    # /oauth/authorize - if this call site's re-validation didn't also
    # receive the allowlist, a hosted client's own flow would pass
    # /oauth/authorize only to be rejected here, breaking every hosted
    # connection this feature exists to enable.
    google = _FakeGoogleOAuth(email="user@example.com")
    correlation_state = encode_correlation_state(
        client_redirect_uri="https://claude.ai/api/mcp/auth_callback",
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
        extra_allowed_redirect_uris=frozenset({"https://claude.ai/api/mcp/auth_callback"}),
    )

    assert result.redirect_url.startswith("https://claude.ai/api/mcp/auth_callback?")
