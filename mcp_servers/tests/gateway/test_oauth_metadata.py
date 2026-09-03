"""RFC 9728 Protected Resource Metadata and RFC 8414 Authorization Server
Metadata - both static JSON, generated from the gateway's own base URL.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from gateway.oauth_metadata import authorization_server_metadata, protected_resource_metadata

BASE_URL = "https://mcp.example.com"


def test_protected_resource_metadata_points_at_the_authorization_server():
    metadata = protected_resource_metadata(BASE_URL)
    assert metadata["resource"] == "https://mcp.example.com/mcp"
    assert metadata["authorization_servers"] == ["https://mcp.example.com"]


def test_authorization_server_metadata_advertises_the_three_endpoints():
    metadata = authorization_server_metadata(BASE_URL)
    assert metadata["issuer"] == "https://mcp.example.com"
    assert metadata["authorization_endpoint"] == "https://mcp.example.com/oauth/authorize"
    assert metadata["token_endpoint"] == "https://mcp.example.com/oauth/token"


def test_authorization_server_metadata_declares_pkce_s256_only():
    metadata = authorization_server_metadata(BASE_URL)
    assert metadata["code_challenge_methods_supported"] == ["S256"]


def test_authorization_server_metadata_declares_no_client_secret_required():
    # Public client, PKCE-secured - matches how Claude Code (loopback,
    # no stored secret) will call this.
    metadata = authorization_server_metadata(BASE_URL)
    assert "none" in metadata["token_endpoint_auth_methods_supported"]


def test_authorization_server_metadata_advertises_dynamic_registration():
    # Claude Code's CLI hard-fails without this: "Incompatible auth server:
    # does not support dynamic client registration" (its own debug log,
    # after discovery had already succeeded).
    metadata = authorization_server_metadata(BASE_URL)
    assert metadata["registration_endpoint"] == "https://mcp.example.com/oauth/register"
