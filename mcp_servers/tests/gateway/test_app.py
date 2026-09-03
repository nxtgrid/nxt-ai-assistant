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
    # redirect").
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
                headers={"Accept": "application/json, text/event-stream"},
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
