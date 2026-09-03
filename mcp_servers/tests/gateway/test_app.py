"""ASGI-level smoke test for the gateway app.

Drives build_asgi_app over REAL ASGI via httpx's ASGITransport — not just an
import check. This is the one thing in this module that can be verified
without a real database: that the factory actually produces a working
Starlette app, the lifespan starts and stops cleanly, and an unauthenticated
health check responds — exactly the property the plan's safe DO-rollout
sequence depends on being provable before any auth wiring is trusted.

The /mcp endpoint itself is NOT exercised here: driving a real MCP
Streamable-HTTP session end-to-end needs an actual mcp.client session on the
other end, which is integration-test territory, not a unit test. What IS
covered — auth extraction, tool filtering, the scope guard, dispatch — is
covered directly in test_transport.py and friends without needing ASGI at
all.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import httpx
import pytest
from gateway.app import build_asgi_app


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
async def test_post_to_mcp_without_a_trailing_slash_is_not_redirected():
    # Starlette's Mount defaults to redirecting POST /mcp -> POST /mcp/
    # (a 307, method-preserving in principle). That's still a real break
    # here: DO's ingress strips /mcp-gateway before forwarding, so the
    # Location Starlette computes is a bare /mcp/ with no idea the public
    # URL needs that prefix back - a client that even follows it lands on
    # a completely different route (the ingress catch-all), and most MCP
    # HTTP clients don't follow redirects on POST at all, so this showed up
    # as an outright failed connection, not a slow one. The client's own
    # configured URL (.../mcp-gateway/mcp, no trailing slash) must work
    # with zero redirect.
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        async with transport:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                headers={"Accept": "application/json, text/event-stream"},
            )

    assert response.status_code not in (307, 308)
    assert "location" not in response.headers


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
