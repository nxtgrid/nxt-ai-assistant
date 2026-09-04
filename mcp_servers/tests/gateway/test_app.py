"""ASGI-level smoke test for the gateway app.

Drives build_asgi_app over REAL ASGI via httpx's ASGITransport — not just an
import check. This is the one thing in this module that can be verified
without a real database: that the factory actually produces a working
Starlette app, the lifespan starts and stops cleanly, and an unauthenticated
health check responds — exactly the property the plan's safe DO-rollout
sequence depends on being provable before any auth wiring is trusted.

Most of what happens once a request reaches /mcp — auth extraction, tool
filtering, the scope guard, dispatch — is covered directly in
test_transport.py and friends without needing ASGI at all. But whether a
request reaches /mcp in the first place (the actual routing: does POST /mcp
match at all, does it redirect, does the app even resolve past the
lifespan) is exactly the kind of thing that only breaks at the ASGI level,
and it did once in production (see test_post_to_mcp_without_a_trailing_
slash_is_not_redirected) — so a full round trip through a real MCP
initialize IS exercised here, via _running_lifespan below.

httpx's ASGITransport never sends ASGI lifespan events on its own (confirmed
by reading its handle_async_request source: it only ever builds an
http-type scope) - StreamableHTTPSessionManager.run()'s task group is never
initialized without one, so any test that needs a real /mcp round trip has
to drive lifespan.startup/shutdown by hand. _running_lifespan is that.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import httpx
import pytest
from gateway.app import build_asgi_app


@asynccontextmanager
async def _running_lifespan(app):
    """Manually drives the ASGI lifespan protocol around `app` so a test
    can make real requests through code paths (like /mcp's session manager)
    that only work once lifespan.startup has actually completed. See this
    module's own docstring for why httpx's ASGITransport can't do this on
    its own.
    """
    startup_complete = asyncio.Event()
    shutdown_requested = asyncio.Event()
    shutdown_complete = asyncio.Event()

    async def receive():
        if not startup_complete.is_set():
            return {"type": "lifespan.startup"}
        await shutdown_requested.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        if message["type"] == "lifespan.startup.complete":
            startup_complete.set()
        elif message["type"] == "lifespan.shutdown.complete":
            shutdown_complete.set()

    task = asyncio.ensure_future(app({"type": "lifespan"}, receive, send))
    await startup_complete.wait()
    try:
        yield
    finally:
        shutdown_requested.set()
        await asyncio.wait_for(shutdown_complete.wait(), timeout=5)
        await task


class _FakeAuth:
    async def get_user_permissions(self, email, user_id=None):
        raise AssertionError("healthz must never touch auth_service")

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        raise AssertionError("healthz must never touch auth_service")


async def _exploding_list_tools(server_name):
    raise AssertionError("healthz must never touch the registry")


async def _exploding_call_tool(server_name, tool_name, arguments):
    raise AssertionError("healthz must never touch the registry")


@pytest.mark.asyncio
async def test_healthz_responds_without_touching_any_dependency():
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_app_lifespan_starts_and_stops_cleanly():
    # ASGITransport only runs the lifespan when explicitly asked to - this
    # proves StreamableHTTPSessionManager.run()'s context manager doesn't
    # raise on startup/shutdown, independent of any request being made.
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with transport:
            response = await client.get("/healthz")
            assert response.status_code == 200


@pytest.mark.asyncio
async def test_post_to_mcp_without_a_trailing_slash_initializes_successfully():
    # Reproduces a real production failure end to end, not just the symptom
    # that first surfaced. A live claude mcp add + /mcp connection attempt
    # showed "mcp-gateway (x) failed"; doctl apps logs showed the real
    # request/response - POST /mcp 307 Temporary Redirect - and curl -D -
    # confirmed the exact Location: a bare /mcp/, missing the /mcp-gateway
    # ingress prefix DO's rule strips before forwarding, landing on a
    # totally different route for anything that followed it (most MCP HTTP
    # clients don't follow redirects on POST at all regardless, so this
    # surfaced as an outright failure, not a slow success).
    #
    # The redirect came from Mount("/mcp", app=...): Mount.matches()'s own
    # path_regex requires a "/" after the mount path to match anything at
    # all, so Starlette's redirect_slashes=True default was the ONLY way
    # bare POST /mcp (no trailing slash - exactly what an MCP client
    # requests) ever worked. A first fix attempt disabled redirect_slashes,
    # which stopped the wrong redirect but then 404'd instead, for the
    # exact same underlying reason (confirmed against the live app both
    # times, not assumed either time) - Mount was never the right primitive
    # for a single-endpoint transport with no sub-paths. The real fix
    # replaced it with a plain Route (see gateway/app.py's own comment on
    # why that needs the _McpASGIApp adapter class).
    #
    # This test asserts a genuine, complete initialize round trip - not
    # just "no redirect" - since that weaker assertion is exactly what let
    # the original Mount-based code pass a "not redirected" check while
    # still being fundamentally broken (a 404 also satisfies "not a
    # redirect"). A valid bearer token is required here too now - see
    # _McpASGIApp's own docstring on why every /mcp request, including
    # initialize, is gated on one; _FakeAuth/_exploding_list_tools are
    # untouched by that gate (verify_token is pure, no DB call) and by
    # initialize itself (a base SDK lifecycle method, not one of this
    # gateway's own _list_tools/_call_tool handlers).
    from gateway.tokens import issue_token

    token = issue_token("user@example.com", "test-secret-not-a-real-key")
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    async with _running_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "protocolVersion": "2026-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0"},
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {token}",
                },
            )

    assert response.status_code == 200
    assert "location" not in response.headers
    assert '"serverInfo"' in response.text
    assert '"nxt-mcp-gateway"' in response.text


@pytest.mark.asyncio
async def test_protected_resource_metadata_is_served():
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    assert response.json()["resource"] == "https://mcp.example.com/mcp"


@pytest.mark.asyncio
async def test_authorization_server_metadata_is_served():
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    assert response.json()["token_endpoint"] == "https://mcp.example.com/oauth/token"


@pytest.mark.asyncio
async def test_authorize_route_redirects_towards_google(monkeypatch):
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get(
            "/oauth/authorize",
            params={
                "redirect_uri": "http://127.0.0.1:54321/callback",
                "state": "client-state",
                "code_challenge": "abc123",
                "code_challenge_method": "S256",
            },
        )

    assert response.status_code in (302, 307)
    assert "accounts.google.com" in response.headers["location"]


def test_www_authenticate_header_names_the_resource_metadata_url():
    from gateway.app import unauthorized_www_authenticate_header

    header = unauthorized_www_authenticate_header("https://mcp.example.com")
    assert header == (
        'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
    )


# --- the /mcp route requires a token before the MCP protocol layer runs ------
#
# Reproduces a real production gap, found via a live claude mcp add + /mcp
# connection: it showed "connected" despite sending NO Authorization header
# at all (confirmed via doctl apps logs mcp-gateway - no /oauth/* request
# ever fired). _call_tool/_list_tools' own resolve_session_from_headers
# checks do reject a missing token, but that rejection happens INSIDE the
# MCP protocol layer - the SDK's Server wraps it into a JSON-RPC-level
# error, which is still HTTP 200. An OAuth-aware client has no way to read
# that as "please authenticate" (the spec's discovery handshake is keyed
# off a real 401 + WWW-Authenticate), so it just used the connection anyway
# with no token, "connected" but not actually signed in to anything.


class _WorkingAuth:
    """Unlike this module's _FakeAuth (which asserts it's never touched -
    correct for the healthz/discovery tests above), this one actually
    resolves a session, for the one test here that needs a valid token's
    request to succeed all the way through list_tools.
    """

    async def get_user_permissions(self, email, user_id=None):
        class _Permissions:
            organization_ids = ["4"]
            is_staff = False
            user_id = "u1"
            organization_short_name = "testorg"

        return _Permissions()

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        return []


async def _empty_list_tools(server_name):
    return []


@pytest.mark.asyncio
async def test_post_to_mcp_without_a_token_returns_401_with_www_authenticate():
    from gateway.app import unauthorized_www_authenticate_header

    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    async with _running_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                headers={"Accept": "application/json, text/event-stream"},
            )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == unauthorized_www_authenticate_header("https://mcp.example.com")


@pytest.mark.asyncio
async def test_post_to_mcp_with_a_valid_token_still_initializes_successfully():
    from gateway.tokens import issue_token

    token = issue_token("user@example.com", "test-secret-not-a-real-key")
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_WorkingAuth(),
        registry_list_tools=_empty_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    async with _running_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "protocolVersion": "2026-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0"},
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {token}",
                },
            )

    assert response.status_code == 200
    assert '"serverInfo"' in response.text


@pytest.mark.asyncio
async def test_tools_list_includes_get_operating_context_over_real_asgi():
    # End to end through the real MCP protocol layer, not just
    # transport.py's own unit tests: a client listing tools sees the
    # synthetic gateway tool alongside whatever the registry returned.
    from gateway.tokens import issue_token

    token = issue_token("user@example.com", "test-secret-not-a-real-key")
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_WorkingAuth(),
        registry_list_tools=_empty_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    async with _running_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": {}},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {token}",
                },
            )

    assert response.status_code == 200
    assert "gateway__get_operating_context" in response.text
    assert "[READ-ONLY]" in response.text


@pytest.mark.asyncio
async def test_calling_get_operating_context_over_real_asgi_returns_real_text():
    # The other end of the same round trip: actually calling it, not just
    # seeing it listed, and confirming a bearer-token-authenticated call
    # gets real content back rather than an error - stateless=True gives
    # each POST its own transport, so no session-id juggling is needed
    # between initialize and this call, unlike a stateful server.
    from gateway.tokens import issue_token

    token = issue_token("user@example.com", "test-secret-not-a-real-key")
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_WorkingAuth(),
        registry_list_tools=_empty_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    async with _running_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "gateway__get_operating_context", "arguments": {}},
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {token}",
                },
            )

    assert response.status_code == 200
    assert "isError" not in response.text or '"isError":false' in response.text
    # _empty_list_tools/_exploding_call_tool prove the real registry was
    # never touched for this call (either would raise/assert if it were),
    # and the response still carries real text - straight from the actual
    # PROMPTS singleton's bundled content, not a stub.
    assert len(response.text) > 500


# --- discovery must work at the RFC 8414 URL, not just the path-suffix one ---
#
# For an issuer WITH a path component (https://host/mcp-gateway), RFC 8414
# builds the metadata URL by inserting the well-known segment between the
# authority and the path - /.well-known/oauth-authorization-server/mcp-gateway
# - NOT by appending it to the path. Only the appended (OIDC-style) form was
# served, so a client following RFC 8414 got the DO ingress catch-all (a 307
# to the anansi-app login page) instead of metadata. Confirmed against the
# live deployment with curl before writing this.


@pytest.mark.asyncio
async def test_authorization_server_metadata_is_served_at_the_rfc8414_url():
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com/mcp-gateway",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-authorization-server/mcp-gateway")

    assert response.status_code == 200
    assert response.json()["issuer"] == "https://mcp.example.com/mcp-gateway"


@pytest.mark.asyncio
async def test_protected_resource_metadata_is_served_at_the_rfc9728_url():
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com/mcp-gateway",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp-gateway")

    assert response.status_code == 200
    assert response.json()["resource"] == "https://mcp.example.com/mcp-gateway/mcp"


@pytest.mark.asyncio
async def test_the_401_body_is_parseable_json():
    # Claude Code's own connectivity probe parses the response body as JSON
    # regardless of status, so an empty-bodied 401 died as
    # "SyntaxError: Failed to parse JSON" before its OAuth challenge handling
    # ever ran - the connector showed "failed", never "needs authentication".
    # Confirmed from its debug log against the live deployment, not guessed.
    # RFC 6750 section 3 defines this body shape for exactly this response.
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    async with _running_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                headers={"Accept": "application/json, text/event-stream"},
            )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_register_route_issues_a_client_id():
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/oauth/register",
            json={"redirect_uris": ["http://127.0.0.1:54321/callback"], "client_name": "test"},
        )

    assert response.status_code == 201
    assert response.json()["client_id"]
    assert response.json()["token_endpoint_auth_method"] == "none"


@pytest.mark.asyncio
async def test_authorize_route_rejects_a_remote_redirect_uri_without_redirecting():
    # The rejection must be rendered, never redirected to the offending URI -
    # redirecting there would perform exactly the hand-off the check exists
    # to prevent.
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get(
            "/oauth/authorize",
            params={
                "redirect_uri": "https://evil.example/steal",
                "state": "client-state",
                "code_challenge": "abc123",
                "code_challenge_method": "S256",
            },
        )

    assert response.status_code == 400
    assert "location" not in response.headers
    assert response.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_authorize_route_redirects_for_a_hosted_client_on_the_allowlist():
    # end-to-end through the real ASGI/route wiring: build_asgi_app's
    # extra_allowed_redirect_uris parameter has to actually reach
    # oauth_authorize_route's call to build_authorize_redirect, not just
    # exist on the factory signature.
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
        extra_allowed_redirect_uris=frozenset({"https://claude.ai/api/mcp/auth_callback"}),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get(
            "/oauth/authorize",
            params={
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "state": "client-state",
                "code_challenge": "abc123",
                "code_challenge_method": "S256",
            },
        )

    assert response.status_code in (302, 307)
    assert "accounts.google.com" in response.headers["location"]


@pytest.mark.asyncio
async def test_authorize_route_still_rejects_an_unlisted_remote_redirect_uri_when_an_allowlist_is_set():
    # A non-empty allowlist must not become a general remote-redirect escape
    # hatch for the whole route - only its own exact entries pass.
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
        extra_allowed_redirect_uris=frozenset({"https://claude.ai/api/mcp/auth_callback"}),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get(
            "/oauth/authorize",
            params={
                "redirect_uri": "https://evil.example/steal",
                "state": "client-state",
                "code_challenge": "abc123",
                "code_challenge_method": "S256",
            },
        )

    assert response.status_code == 400
    assert "location" not in response.headers
