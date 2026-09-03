"""Per-request bearer-token extraction and the two MCP-facing request flows.

Deliberately headers-in, dicts-out: these functions never touch a real
Starlette Request, so they're testable without spinning up ASGI at all. The
thin wiring in app.py that DOES touch a real Request is the only untested
layer, by design - see app.py's own docstring.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.tokens import TokenInvalid, issue_token
from gateway.transport import (
    call_tool_for_request,
    extract_bearer_token,
    list_tools_for_request,
    resolve_session_from_headers,
)

SECRET = "test-secret-not-a-real-key"


class _FakeAuth:
    def __init__(self, organization_ids=("4",), grid_names=("Alpha Site",)):
        self._organization_ids = list(organization_ids)
        self._grid_names = list(grid_names)

    async def get_user_permissions(self, email, user_id=None):
        class _P:
            organization_ids = self._organization_ids
            is_staff = False
            user_id = "u1"
            organization_short_name = "testorg"

        return _P()

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        return list(self._grid_names)


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


# --- extract_bearer_token -----------------------------------------------


def test_extracts_token_from_authorization_header():
    assert extract_bearer_token({"authorization": "Bearer abc123"}) == "abc123"


def test_header_lookup_is_case_insensitive():
    assert extract_bearer_token({"Authorization": "Bearer abc123"}) == "abc123"


def test_missing_header_raises_token_invalid():
    with pytest.raises(TokenInvalid):
        extract_bearer_token({})


def test_non_bearer_scheme_raises_token_invalid():
    with pytest.raises(TokenInvalid):
        extract_bearer_token({"authorization": "Basic abc123"})


def test_bearer_with_no_token_raises_token_invalid():
    with pytest.raises(TokenInvalid):
        extract_bearer_token({"authorization": "Bearer "})


# --- resolve_session_from_headers ----------------------------------------


@pytest.mark.asyncio
async def test_valid_token_resolves_a_session():
    token = issue_token("user@example.com", SECRET)
    session = await resolve_session_from_headers(_bearer(token), SECRET, _FakeAuth())
    assert session.email == "user@example.com"
    assert session.organization_id == "4"


@pytest.mark.asyncio
async def test_missing_bearer_token_never_reaches_auth_service():
    class _ExplodingAuth:
        async def get_user_permissions(self, *a, **k):
            raise AssertionError("should never be called without a valid token")

    with pytest.raises(TokenInvalid):
        await resolve_session_from_headers({}, SECRET, _ExplodingAuth())


# --- list_tools_for_request ------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_only_returns_customer_visible_tools():
    token = issue_token("user@example.com", SECRET)

    async def fake_registry_list_tools(server_name):
        return {
            "customer": [{"name": "get_status", "visible_to_customer": True}],
            "jira": [{"name": "get_issue", "visible_to_customer": False}],
        }.get(server_name, [])

    tools = await list_tools_for_request(
        _bearer(token), SECRET, _FakeAuth(), fake_registry_list_tools, ["customer", "jira"]
    )

    names = [t["name"] for t in tools]
    assert "customer__get_status" in names
    assert not any(n.startswith("jira__") for n in names)


@pytest.mark.asyncio
async def test_list_tools_with_invalid_token_raises_before_querying_registry():
    async def exploding_list_tools(server_name):
        raise AssertionError("should never be called without a valid token")

    with pytest.raises(TokenInvalid):
        await list_tools_for_request({}, SECRET, _FakeAuth(), exploding_list_tools, ["customer"])


# --- call_tool_for_request --------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_guards_arguments_before_delegating():
    token = issue_token("user@example.com", SECRET)

    async def fake_registry_list_tools(server_name):
        return {"customer": [{"name": "get_status", "visible_to_customer": True}]}.get(server_name, [])

    calls = []

    async def fake_registry_call_tool(server_name, tool_name, arguments):
        calls.append((server_name, tool_name, arguments))
        return {"success": True, "result": [{"type": "text", "text": "ok"}]}

    result = await call_tool_for_request(
        _bearer(token),
        SECRET,
        _FakeAuth(),
        fake_registry_list_tools,
        fake_registry_call_tool,
        ["customer"],
        "customer__get_status",
        {"organization_id": 99},
    )

    assert result["success"] is True
    server_name, tool_name, arguments = calls[0]
    assert (server_name, tool_name) == ("customer", "get_status")
    assert arguments["organization_id"] == 4  # caller's 99 discarded


@pytest.mark.asyncio
async def test_call_tool_with_invalid_token_never_reaches_the_registry():
    async def exploding_list_tools(server_name):
        raise AssertionError("should never be called without a valid token")

    async def exploding_call_tool(server_name, tool_name, arguments):
        raise AssertionError("should never be called without a valid token")

    with pytest.raises(TokenInvalid):
        await call_tool_for_request(
            {}, SECRET, _FakeAuth(), exploding_list_tools, exploding_call_tool, ["customer"], "customer__get_status", {}
        )
