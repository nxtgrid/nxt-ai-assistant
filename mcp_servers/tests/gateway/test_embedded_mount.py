"""Whether the gateway still works when MOUNTED inside another ASGI app.

The gateway used to be its own DigitalOcean service, with nothing in front of
it. It now runs mounted inside chat_orchestrator's FastAPI app (see
orchestrator/api/app.py's _mount_mcp_gateway), which means every request
reaches it through that app's middleware stack instead of hitting a bare
Starlette router. Three things change, and each of them can break /mcp in a
way no other test in this package would catch:

1.  A mounted sub-app never receives the ASGI "lifespan" scope - Starlette's
    Router dispatches it and returns before matching any route - so the
    StreamableHTTPSessionManager task group that every /mcp request needs is
    never started unless the host app enters gateway_lifespan() itself.
2.  Streamable HTTP answers POST /mcp with an SSE stream by default, and it
    now has to travel back out through BaseHTTPMiddleware (what FastAPI's
    @app.middleware("http") builds), which proxies the response through an
    anyio memory stream rather than passing ASGI messages straight through.
3.  The two RFC 8414 / RFC 9728 discovery documents live ABOVE the mount
    prefix (/.well-known/oauth-authorization-server/mcp-gateway), so the
    mount cannot serve them at all and the host app must register them on its
    own root router.

The app built here mirrors chat_orchestrator's middleware shape deliberately
(CORSMiddleware + an @app.middleware("http") response-header middleware), so
these tests fail if that stack ever becomes incompatible with the transport -
which is the whole risk of folding a streaming endpoint into the bot service.
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
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from gateway.app import build_asgi_app, gateway_lifespan, well_known_routes
from gateway.tokens import issue_token

BASE_URL = "https://host.example.com/mcp-gateway"
SECRET = "test-secret-not-a-real-key"  # pragma: allowlist secret


# Deliberately self-contained rather than imported from test_app: there is no
# conftest.py under mcp_servers/tests/, and several files here rely on
# collection-order side effects for sys.path, so a cross-module test import
# can resolve when the whole suite runs and fail when this file is targeted
# on its own.
class _FakeAuth:
    async def get_user_permissions(self, email, user_id=None):
        raise AssertionError("no test here should reach auth_service")

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        raise AssertionError("no test here should reach auth_service")


async def _exploding_list_tools(server_name):
    raise AssertionError("no test here should reach the registry")


async def _exploding_call_tool(server_name, tool_name, arguments):
    raise AssertionError("no test here should reach the registry")


@asynccontextmanager
async def _running_lifespan(app):
    """Drives the ASGI lifespan protocol around `app` by hand.

    httpx's ASGITransport never sends lifespan events on its own (it only ever
    builds an http-type scope), so any test needing a host app's real startup
    has to do this itself.
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


def _host_app():
    """A FastAPI app with chat_orchestrator's middleware shape, hosting the
    gateway exactly the way orchestrator/api/app.py does.
    """
    gateway = build_asgi_app(
        secret=SECRET,
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url=BASE_URL,
    )

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://host.example.com"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["X-Api-Key", "Content-Type"],
    )

    @app.middleware("http")
    async def _hsts(request: Request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/health")
    async def _health():
        return {"status": "ok"}

    app.mount("/mcp-gateway", gateway, name="mcp-gateway")
    app.router.routes.extend(well_known_routes(BASE_URL))
    return app, gateway


@pytest.mark.asyncio
async def test_mcp_initialize_round_trips_through_the_host_middleware_stack():
    """The load-bearing test for the fold-in.

    A full initialize round trip at the PUBLIC path, through CORS and a
    BaseHTTPMiddleware response wrapper, with the session manager started via
    the host app's lifespan rather than the gateway's own. Asserts real
    serverInfo, not merely a non-error status: a 404 or an empty 200 would
    satisfy anything weaker, and a weaker assertion is exactly what let an
    earlier broken /mcp route pass its own test (see test_app.py's
    test_post_to_mcp_without_a_trailing_slash_initializes_successfully).
    """
    app, gateway = _host_app()
    token = issue_token("user@example.com", SECRET)

    async with gateway_lifespan(gateway):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=False
        ) as client:
            response = await client.post(
                "/mcp-gateway/mcp",
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
    # The host's middleware really did run over the mounted response - if it
    # hadn't, this test would be proving less than it claims to.
    assert response.headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.asyncio
async def test_mcp_without_a_token_still_returns_401_when_mounted():
    """The bearer gate is inside the mount, so nothing about being hosted may
    weaken it - and the WWW-Authenticate header must still name the PUBLIC
    resource-metadata URL, not a container-internal one.
    """
    app, gateway = _host_app()

    async with gateway_lifespan(gateway):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/mcp-gateway/mcp",
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"
    assert BASE_URL in response.headers["www-authenticate"]


@pytest.mark.asyncio
async def test_discovery_documents_are_served_above_the_mount_prefix():
    """RFC 8414 puts an issuer's metadata ABOVE its own path, so these two
    paths can never be reached through a mount at /mcp-gateway. If
    well_known_routes stopped being registered on the host's root router, a
    client would get the host app's 404 (in production, DO's ingress catch-all
    - a 307 to anansi-app's login page) instead of metadata.
    """
    app, _ = _host_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resource = await client.get("/.well-known/oauth-protected-resource/mcp-gateway")
        server = await client.get("/.well-known/oauth-authorization-server/mcp-gateway")

    assert resource.status_code == 200
    assert resource.json() == {
        "resource": f"{BASE_URL}/mcp",
        "authorization_servers": [BASE_URL],
    }
    assert server.status_code == 200
    assert server.json()["issuer"] == BASE_URL
    assert server.json()["authorization_endpoint"] == f"{BASE_URL}/oauth/authorize"
    assert server.json()["registration_endpoint"] == f"{BASE_URL}/oauth/register"


@pytest.mark.asyncio
async def test_mounting_the_gateway_does_not_disturb_the_host_apps_own_routes():
    """The bot's own endpoints must be untouched by the mount. FastAPI is
    constructed with no app-wide `dependencies`, so neither app inherits the
    other's auth - this pins that in both directions: /health needs no bearer
    token, and it does not start answering as the gateway.
    """
    app, _ = _host_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        # Bare /mcp must NOT exist on the host root - only under the mount.
        stray = await client.post("/mcp", json={})

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert stray.status_code == 404


@pytest.mark.asyncio
async def test_a_mounted_gateway_gets_no_lifespan_from_its_host():
    """Why gateway_lifespan() has to exist at all.

    Running the HOST app's full lifespan leaves the mounted gateway's session
    manager uninitialised - Starlette returns on the "lifespan" scope before
    it matches any route, so Mount.handle() never sees it. This asserts the
    failure directly, so nobody deletes the explicit
    startup-enters-gateway_lifespan wiring believing the mount handles it.
    """
    app, _ = _host_app()
    token = issue_token("user@example.com", SECRET)

    async with _running_lifespan(app):  # the HOST's lifespan, not the gateway's
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with pytest.raises(Exception):
                await client.post(
                    "/mcp-gateway/mcp",
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


def test_well_known_routes_are_empty_for_a_path_less_issuer():
    """A gateway deployed at a bare origin needs no root-level extras -
    build_asgi_app already serves the unsuffixed forms, and returning routes
    here would register the identical paths twice.
    """
    assert well_known_routes("https://gateway.example.com") == []
    assert len(well_known_routes("https://host.example.com/mcp-gateway")) == 2
