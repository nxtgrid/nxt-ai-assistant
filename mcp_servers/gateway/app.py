"""ASGI wiring for the gateway's MCP protocol endpoint.

This is the one layer that touches a real Starlette Request and the mcp
package's Server/RequestContext types directly, so it's the layer transport.py
was deliberately built to keep everything else out of. build_asgi_app is a
factory, not a module-level singleton, so a test CAN inject fakes for every
external dependency (auth_service, registry_list_tools, registry_call_tool)
and drive it over real ASGI via httpx's ASGITransport (see test_app.py) — the
one thing that stays genuinely untested at the unit level is the module-level
run_gateway() below, which wires the REAL AuthService, the REAL
server_registry, the REAL Google OAuth client, the REAL grid_app.lib.perms
whitelist, and a secret read from the environment.

The three /oauth/* routes and the two /.well-known/* discovery documents ARE
in this file (see gateway/oauth.py and gateway/oauth_metadata.py for their
actual logic) - this module just mounts them alongside the MCP protocol
endpoint on the same ASGI app, matching the design doc's "the gateway acts
as its own authorization server" framing rather than standing up a second
service.

Still not in this file: DO App Platform ingress. This app is servable by any
ASGI host; nothing here assumes a particular deployment target.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional

import mcp.types as types
import uvicorn
from gateway.google_oauth_client import GoogleOAuthClient
from gateway.oauth import build_authorize_redirect, handle_google_callback, handle_token_request
from gateway.oauth_metadata import authorization_server_metadata, protected_resource_metadata
from gateway.tiers import ALLOWED_SERVERS
from gateway.tokens import TokenInvalid, verify_token
from gateway.transport import (
    RegistryCallTool,
    RegistryListTools,
    call_tool_for_request,
    extract_bearer_token,
    list_tools_for_request,
)
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route


def unauthorized_www_authenticate_header(base_url: str) -> str:
    """The header a client parses to discover where to authenticate.

    Per the spec's discovery sequence, this is what turns a bare 401 into
    something a client can act on - without it, a client has a token
    requirement with no way to find out how to satisfy it.

    NOT YET wired onto the MCP Server's own 401 path (TokenInvalid/
    SessionDenied raised from _call_tool/_list_tools below) - that depends
    on exactly how StreamableHTTPSessionManager surfaces a raised exception
    as an HTTP status, unconfirmed against the installed mcp package's
    source as of this writing. The plain Starlette routes above already set
    real status codes directly, so this header is usable on any of those
    today; only the MCP-protocol path still needs that follow-up.
    """
    return f'Bearer resource_metadata="{base_url}/.well-known/oauth-protected-resource"'


def _deny_all(email: str) -> bool:
    """Fail-closed default for is_authorized: a gateway deployed without an
    explicit whitelist function (run_gateway() always supplies the real
    grid_app.lib.perms.has_any_access — see its own module) rejects every
    sign-in rather than silently allowing everyone or crashing on a None
    call.
    """
    return False


class _McpASGIApp:
    """Wraps StreamableHTTPSessionManager.handle_request (a raw ASGI3
    callable, but a BOUND METHOD) so Starlette's Route treats it as a raw
    ASGI app rather than a request_response(request) handler - see the
    Route("/mcp", ...) call site's own comment for the full reasoning.
    Mirrors mcp.server.fastmcp.server's own StreamableHTTPASGIApp exactly.

    ALSO gates every request on a valid bearer token before the MCP
    protocol layer ever runs - found necessary against a real production
    connection: _call_tool/_list_tools' own resolve_session_from_headers
    checks do reject a missing token, but that rejection happens INSIDE
    the MCP protocol layer, which the SDK's Server wraps into a JSON-RPC-
    level error (still HTTP 200) rather than a real HTTP 401. An OAuth-
    aware client has no way to read that as "please authenticate" - the
    spec's discovery handshake is keyed off an actual 401 +
    WWW-Authenticate - so a real Claude Code connection attempt showed
    "connected" despite sending no Authorization header at all (confirmed
    via doctl apps logs: no /oauth/* request ever fired). This checks only
    the token's own signature and expiry (verify_token - pure, no DB call),
    not full session resolution: a missing/malformed/expired token needs
    OAuth (401 is correct), but a validly-signed token for a since-revoked
    org is a DIFFERENT failure (authorized-nothing, not unauthenticated) -
    re-running the OAuth dance wouldn't fix that, so it's left to surface
    as the existing MCP-level SessionDenied error inside list_tools/
    call_tool instead, exactly as it already did before this gate existed.
    """

    def __init__(
        self,
        session_manager: StreamableHTTPSessionManager,
        secret: str,
        base_url: str,
    ) -> None:
        self._session_manager = session_manager
        self._secret = secret
        self._base_url = base_url

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            try:
                token = extract_bearer_token(headers)
                verify_token(token, self._secret)
            except TokenInvalid:
                response = Response(
                    status_code=401,
                    headers={"WWW-Authenticate": unauthorized_www_authenticate_header(self._base_url)},
                )
                await response(scope, receive, send)
                return
        await self._session_manager.handle_request(scope, receive, send)


def build_asgi_app(
    secret: str,
    auth_service: Any,
    registry_list_tools: RegistryListTools,
    registry_call_tool: RegistryCallTool,
    allowed_servers: Optional[List[str]] = None,
    base_url: str = "http://localhost:8080",
    google_oauth_client: Optional[Any] = None,
    is_authorized: Optional[Callable[[str], bool]] = None,
    single_use_store: Optional[Any] = None,
) -> Starlette:
    """Build the gateway's ASGI app.

    A factory so every external dependency is injectable — real production
    wiring lives only in run_gateway() below, never baked in here.

    google_oauth_client defaults to a real GoogleOAuthClient (not a fake)
    when omitted: build_authorize_url is pure string-building with no
    network call, so this is safe to default-construct even for a test that
    never touches the Google leg at all — it's what lets a plain GET to
    /oauth/authorize redirect somewhere real without every caller needing
    to inject a fake. is_authorized defaults to _deny_all (fail-closed);
    single_use_store has no safe default (there is no "fail-closed" store),
    so /oauth/token raises loudly if run_gateway() didn't wire a real one.
    """
    servers = list(allowed_servers) if allowed_servers is not None else list(ALLOWED_SERVERS)
    google_oauth_client = google_oauth_client or GoogleOAuthClient(
        redirect_uri=f"{base_url}/oauth/google-callback"
    )
    is_authorized = is_authorized or _deny_all
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

    async def oauth_authorize_route(request):
        result = build_authorize_redirect(
            client_redirect_uri=request.query_params["redirect_uri"],
            client_state=request.query_params.get("state", ""),
            code_challenge=request.query_params["code_challenge"],
            base_url=base_url,
            secret=secret,
            google_oauth=google_oauth_client,
        )
        return RedirectResponse(result.redirect_url, status_code=302)

    async def oauth_google_callback_route(request):
        result = await handle_google_callback(
            state=request.query_params["state"],
            callback_query=dict(request.query_params),
            secret=secret,
            google_oauth=google_oauth_client,
            is_authorized=is_authorized,
            auth_service=auth_service,
        )
        return RedirectResponse(result.redirect_url, status_code=302)

    async def oauth_token_route(request):
        form = await request.form()
        result = await handle_token_request(
            code=form["code"],
            code_verifier=form["code_verifier"],
            secret=secret,
            single_use_store=single_use_store,
            auth_service=auth_service,
        )
        return JSONResponse(
            {
                "access_token": result.access_token,
                "token_type": result.token_type,
                "expires_in": result.expires_in,
            }
        )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            yield

    asgi_app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata_route),
            Route("/.well-known/oauth-authorization-server", authorization_server_metadata_route),
            Route("/oauth/authorize", oauth_authorize_route),
            Route("/oauth/google-callback", oauth_google_callback_route),
            Route("/oauth/token", oauth_token_route, methods=["POST"]),
            # A plain Route, not Mount("/mcp", app=session_manager.
            # handle_request) - Streamable HTTP is one endpoint, not a tree
            # of sub-paths, so Mount's prefix-matching semantics were never
            # the right primitive here. A first attempt used Mount, which
            # only ever matched bare POST /mcp (no trailing slash - exactly
            # what an MCP client requests) via Starlette's redirect_slashes
            # fallback: Mount.matches()'s own path_regex requires a "/" after
            # the mount path to match at all, so redirect_slashes=True's
            # 307-to-/mcp/ was the ONLY way bare /mcp ever worked - and that
            # redirect's Location is computed relative to what Starlette saw
            # inside the container, with no idea the public URL needs the
            # /mcp-gateway prefix DO's ingress rule strips before forwarding
            # (see .do/app.example.yaml's own comment on why), landing on the
            # ingress catch-all instead. Turning redirect_slashes off headed
            # off the wrong redirect but then 404'd instead, for the exact
            # same underlying reason - confirmed against a real production
            # failure both times (doctl apps logs showing the 307, then curl
            # showing the 404), not assumed either time.
            #
            # session_manager.handle_request is a raw ASGI3 callable but a
            # BOUND METHOD - Starlette's Route.__init__ special-cases
            # inspect.ismethod() endpoints as request_response(request)
            # handlers, the wrong calling convention here. _McpASGIApp exists
            # only to dodge that check: a class instance's __call__ is
            # neither isfunction nor ismethod, so Route treats it as a raw
            # ASGI app instead - exactly the pattern mcp.server.fastmcp.
            # server's own StreamableHTTPASGIApp uses, reimplemented locally
            # rather than imported since this gateway deliberately stays on
            # the low-level Server API throughout, not fastmcp's high-level
            # one (see mcp_servers/requirements.txt's own comment on why).
            Route("/mcp", endpoint=_McpASGIApp(session_manager, secret, base_url)),
        ],
        lifespan=lifespan,
    )
    return asgi_app


def run_gateway() -> None:  # pragma: no cover — real production wiring, no fakes
    """Entrypoint for local/dev running. Reads real config from the
    environment and wires the real AuthService, server_registry, Google
    OAuth client, is_authorized whitelist and single-use store — the one
    piece of this module that cannot be exercised by a unit test.

    MCP_GATEWAY_BASE_URL must be this deployment's real public HTTPS origin
    (e.g. "https://your-app.example.com/mcp-gateway") — it is what gets
    embedded in the discovery documents and is the value Google's redirect_
    uri validation checks against, so a wrong value here fails visibly at
    first sign-in rather than silently.

    is_authorized delegates to grid_app.lib.perms.has_any_access directly
    (not anansi_app.nicegui_app.auth.is_authorized, its NiceGUI-page
    wrapper) — see gateway/Dockerfile's own comment on why only
    anansi_app/grid_app/ is copied into this image, not all of anansi_app/.
    """
    from gateway.oauth_store_pg import PgSingleUseStore
    from grid_app.lib import perms
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
        base_url=os.environ["MCP_GATEWAY_BASE_URL"],
        is_authorized=lambda email: bool(perms.has_any_access(email)),
        single_use_store=PgSingleUseStore(),
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    run_gateway()
