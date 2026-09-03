"""RFC 9728 Protected Resource Metadata and RFC 8414 Authorization Server
Metadata - the two static discovery documents a client fetches before ever
talking to /oauth/authorize.
"""

from __future__ import annotations

from typing import Any, Dict


def protected_resource_metadata(base_url: str) -> Dict[str, Any]:
    """Served at /.well-known/oauth-protected-resource.

    Tells a client which authorization server issues tokens valid for this
    MCP server, and the canonical resource URI those tokens must be bound to
    (RFC 8707) - here, the gateway acts as its own authorization server, so
    both fields point at the same base_url.
    """
    return {
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
    }


def authorization_server_metadata(base_url: str) -> Dict[str, Any]:
    """Served at /.well-known/oauth-authorization-server.

    token_endpoint_auth_methods_supported includes "none": this is a public
    client flow (Claude Code holds no client_secret at all - PKCE is the
    security boundary, not a confidential-client secret).
    """
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
