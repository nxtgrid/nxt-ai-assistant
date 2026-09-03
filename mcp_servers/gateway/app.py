"""ASGI wiring for the gateway's MCP protocol endpoint.

This is the one layer that touches a real Starlette Request and the mcp
package's Server/RequestContext types directly, so it's the layer transport.py
was deliberately built to keep everything else out of. build_asgi_app is a
factory, not a module-level singleton, so a test CAN inject fakes for every
external dependency (auth_service, registry_list_tools, registry_call_tool)
and drive it over real ASGI via httpx's ASGITransport (see test_app.py) — the
one thing that stays genuinely untested at the unit level is the module-level
run_gateway() below, which wires the REAL AuthService, the REAL
server_registry, and a secret read from the environment.

Not in this file, deliberately (see the plan's Deferred section):
  - The HTTP sign-in route (Google OAuth callback -> mint_token_for_email).
    An MCP client is handed a token some other way for now; this file only
    serves the MCP protocol endpoint itself.
  - DO App Platform ingress. This app is servable by any ASGI host; nothing
    here assumes a particular deployment target.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import mcp.types as types
import uvicorn
from gateway.oauth_metadata import authorization_server_metadata, protected_resource_metadata
from gateway.tiers import ALLOWED_SERVERS
from gateway.transport import (
    RegistryCallTool,
    RegistryListTools,
    call_tool_for_request,
    list_tools_for_request,
)
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


def build_asgi_app(
    secret: str,
    auth_service: Any,
    registry_list_tools: RegistryListTools,
    registry_call_tool: RegistryCallTool,
    allowed_servers: Optional[List[str]] = None,
    base_url: str = "http://localhost:8080",
) -> Starlette:
    """Build the gateway's ASGI app.

    A factory so every external dependency is injectable — real production
    wiring lives only in run_gateway() below, never baked in here.
    """
    servers = list(allowed_servers) if allowed_servers is not None else list(ALLOWED_SERVERS)
    server = Server("nxt-mcp-gateway")

    @server.list_tools()
    async def _list_tools() -> List[types.Tool]:
        request = server.request_context.request
        headers = dict(request.headers) if request is not None else {}
        tool_dicts = await list_tools_for_request(
            headers, secret, auth_service, registry_list_tools, servers
        )
        return [
            types.Tool(
                name=t["name"],
                description=t.get("description", ""),
                inputSchema=t.get("inputSchema") or {"type": "object", "properties": {}},
            )
            for t in tool_dicts
        ]

    # validate_input=False: the SDK's own validation resolves a tool's schema
    # via its internal per-name cache, populated from list_tools' last
    # result. Our tool set is session-scoped and re-fetched every call, not
    # a fixed set the SDK can usefully cache against - dispatch_tool_call
    # (via _find_tool) already raises ToolDenied for an unknown name, and
    # apply_scope_guard already governs every scope-bearing argument, so
    # the SDK's own schema check would be redundant even where it did apply.
    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
        request = server.request_context.request
        headers = dict(request.headers) if request is not None else {}
        result = await call_tool_for_request(
            headers,
            secret,
            auth_service,
            registry_list_tools,
            registry_call_tool,
            servers,
            name,
            arguments,
        )

        # A raised exception here (TokenInvalid, SessionDenied, ToolDenied,
        # ScopeViolation) is NOT caught locally - Server.call_tool's own
        # wrapper catches any exception from this handler and turns it into
        # a proper MCP error result automatically. This branch instead
        # covers server_registry.call_tool's OWN caught-and-sanitized
        # failures, which return success=False rather than raising.
        if not result.get("success", False):
            return [
                types.TextContent(
                    type="text", text=f"Error: {result.get('error', 'tool call failed')}"
                )
            ]

        return [
            types.TextContent(type=item.get("type", "text"), text=item.get("text", ""))
            for item in result.get("result", [])
        ]

    # stateless=True: a fresh transport per request, no session tracking
    # between requests. Combined with transport.py never caching a
    # GatewaySession, this is what makes "revoking a user takes effect on
    # their next request" true rather than aspirational - there is no
    # connection-lifetime state anywhere for a revocation to lag behind.
    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    async def healthz(request):
        """Unauthenticated on purpose — see the plan's safe DO-rollout
        sequence: an unauthenticated health check is what proves the ingress
        rule reaches this service at all, before any auth wiring is trusted.
        """
        return JSONResponse({"status": "ok"})

    async def protected_resource_metadata_route(request):
        return JSONResponse(protected_resource_metadata(base_url))

    async def authorization_server_metadata_route(request):
        return JSONResponse(authorization_server_metadata(base_url))

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata_route),
            Route("/.well-known/oauth-authorization-server", authorization_server_metadata_route),
            Mount("/mcp", app=session_manager.handle_request),
        ],
        lifespan=lifespan,
    )


def run_gateway() -> None:  # pragma: no cover — real production wiring, no fakes
    """Entrypoint for local/dev running. Reads real config from the
    environment and wires the real AuthService and server_registry — the one
    piece of this module that cannot be exercised by a unit test.
    """
    from server_registry import call_tool as real_call_tool
    from server_registry import list_tools as real_list_tools

    from shared.auth.auth_service import get_auth_service

    secret = os.environ["MCP_GATEWAY_TOKEN_SECRET"]
    app = build_asgi_app(
        secret=secret,
        auth_service=get_auth_service(),
        registry_list_tools=real_list_tools,
        registry_call_tool=real_call_tool,
        allowed_servers=list(ALLOWED_SERVERS),
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    run_gateway()
