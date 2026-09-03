"""Gateway session resolution.

The fail-closed case is the important one: AuthService returns empty
organization_ids rather than raising when an email is absent from
public.accounts, so a permissive gateway would forward organization_id=None
to servers that never filter by org.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.session import GatewaySession, SessionDenied, resolve_session


class _FakeAuth:
    def __init__(self, permissions, grid_names=None):
        self._permissions = permissions
        self._grid_names = grid_names or []
        self.grid_call = None

    async def get_user_permissions(self, email, user_id=None):
        return self._permissions

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        self.grid_call = (organization_id, include_all)
        return list(self._grid_names)


class _Perms:
    def __init__(self, organization_ids, is_staff=False, user_id="u1", email="a@example.com"):
        self.organization_ids = organization_ids
        self.is_staff = is_staff
        self.user_id = user_id
        self.email = email
        self.organization_short_name = "testorg"


@pytest.mark.asyncio
async def test_resolve_session_builds_allowed_grid_set():
    auth = _FakeAuth(_Perms(["4"]), grid_names=["Alpha Site", "Beta Site"])

    session = await resolve_session("a@example.com", auth)

    assert isinstance(session, GatewaySession)
    assert session.organization_id == "4"
    assert session.grid_names == frozenset({"Alpha Site", "Beta Site"})
    assert session.is_staff is False
    assert auth.grid_call == ("4", False)


@pytest.mark.asyncio
async def test_staff_session_requests_all_grids():
    auth = _FakeAuth(_Perms(["1"], is_staff=True), grid_names=["Alpha Site"])

    session = await resolve_session("staff@example.com", auth)

    assert session.is_staff is True
    assert auth.grid_call == ("1", True)


@pytest.mark.asyncio
async def test_unknown_email_is_denied_not_unscoped():
    auth = _FakeAuth(_Perms([]))

    with pytest.raises(SessionDenied):
        await resolve_session("stranger@example.com", auth)
