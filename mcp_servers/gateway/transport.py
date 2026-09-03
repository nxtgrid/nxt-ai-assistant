"""Per-request auth extraction and the two MCP-facing request flows.

Deliberately headers-in, plain-values-out: nothing here touches a real
Starlette Request, ASGI scope, or the mcp package's Server/RequestContext
types. That keeps this layer testable with plain dicts and fakes. The thin
wiring in app.py that DOES touch those (pulling headers off the real request,
registering handlers on a real Server) is the only untested layer here by
design - see app.py's own docstring for why.

Every function re-derives the session from headers on every call. There is
no session cache anywhere in this module - combined with app.py's
stateless=True StreamableHTTPSessionManager, a revoked user's next request
re-resolves from the database and gets nothing, with no cache or long-lived
connection to make that latent.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Mapping

from gateway.catalog import list_exposed_tools
from gateway.server import dispatch_tool_call
from gateway.session import GatewaySession, resolve_session
from gateway.tokens import TokenInvalid, verify_token

RegistryListTools = Callable[[str], Awaitable[List[Dict[str, Any]]]]
RegistryCallTool = Callable[[str, str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


def extract_bearer_token(headers: Mapping[str, str]) -> str:
    """Pull the token out of an ``Authorization: Bearer <token>`` header.

    Header lookup is case-insensitive (HTTP header names are), independent
    of whether the mapping passed in already normalised case.
    """
    auth_header = None
    for key, value in headers.items():
        if key.lower() == "authorization":
            auth_header = value
            break

    if not auth_header:
        raise TokenInvalid("Missing Authorization header")

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise TokenInvalid("Authorization header must be 'Bearer <token>'")

    return token.strip()


async def resolve_session_from_headers(
    headers: Mapping[str, str], secret: str, auth_service
) -> GatewaySession:
    """Extract, verify, and resolve — the one path every request goes through.

    Raises before touching auth_service at all if the token is missing or
    malformed, and before touching it again if verify_token rejects it.
    """
    token = extract_bearer_token(headers)
    email = verify_token(token, secret)
    return await resolve_session(email, auth_service)


async def _fetch_tools_by_server(
    registry_list_tools: RegistryListTools, allowed_servers: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch fresh, per-request — no caching, so a newly disabled tool or
    server (ActionFlags) takes effect on the very next call.
    """
    return {server_name: await registry_list_tools(server_name) for server_name in allowed_servers}


async def list_tools_for_request(
    headers: Mapping[str, str],
    secret: str,
    auth_service,
    registry_list_tools: RegistryListTools,
    allowed_servers: List[str],
) -> List[Dict[str, Any]]:
    """Full list_tools flow for one incoming request."""
    session = await resolve_session_from_headers(headers, secret, auth_service)
    tools_by_server = await _fetch_tools_by_server(registry_list_tools, allowed_servers)
    return list_exposed_tools(tools_by_server, session)


async def call_tool_for_request(
    headers: Mapping[str, str],
    secret: str,
    auth_service,
    registry_list_tools: RegistryListTools,
    registry_call_tool: RegistryCallTool,
    allowed_servers: List[str],
    namespaced_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Full call_tool flow for one incoming request."""
    session = await resolve_session_from_headers(headers, secret, auth_service)
    tools_by_server = await _fetch_tools_by_server(registry_list_tools, allowed_servers)
    return await dispatch_tool_call(
        namespaced_name, arguments, session, tools_by_server, registry_call_tool
    )
