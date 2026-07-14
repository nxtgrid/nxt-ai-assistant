"""Unit tests for the grid-access auth gate (gd_auth.py).

Covers the staff bypass, exact-match grid ownership checks against a mocked
Auth DB connection (asyncpg), the fail-closed None-organization_id case, and
the Chat DB design->grid name resolution helper.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers.servers.grid_design_server import gd_auth

STAFF_ORG_ID = 2
CUSTOMER_ORG_ID = 42

_ENV = {"STAFF_ORG_ID": str(STAFF_ORG_ID)}


def _mock_conn(fetchrow_return):
    """Build a mocked asyncpg connection whose fetchrow/close are AsyncMocks."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.close = AsyncMock(return_value=None)
    return conn


# ── assert_grid_access ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_staff_org_bypasses_db_check():
    with (
        patch.dict(os.environ, _ENV, clear=False),
        patch(
            "mcp_servers.servers.grid_design_server.gd_auth.asyncpg.connect",
            new_callable=AsyncMock,
        ) as mock_connect,
    ):
        await gd_auth.assert_grid_access("AnyGrid", STAFF_ORG_ID)
    mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_non_staff_org_with_matching_grid_passes():
    conn = _mock_conn(fetchrow_return={"?column?": 1})
    with (
        patch.dict(os.environ, _ENV, clear=False),
        patch(
            "mcp_servers.servers.grid_design_server.gd_auth.asyncpg.connect",
            new_callable=AsyncMock,
            return_value=conn,
        ),
    ):
        await gd_auth.assert_grid_access("ExampleGrid", CUSTOMER_ORG_ID)

    conn.fetchrow.assert_called_once()
    query, *params = conn.fetchrow.call_args.args
    assert "lower(name) = lower($1)" in query
    assert "organization_id = $2" in query
    assert "deleted_at IS NULL" in query
    assert params == ["ExampleGrid", CUSTOMER_ORG_ID]
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_staff_org_with_no_matching_grid_raises_without_leaking_details():
    conn = _mock_conn(fetchrow_return=None)
    with (
        patch.dict(os.environ, _ENV, clear=False),
        patch(
            "mcp_servers.servers.grid_design_server.gd_auth.asyncpg.connect",
            new_callable=AsyncMock,
            return_value=conn,
        ),
    ):
        with pytest.raises(gd_auth.GridAccessDenied) as excinfo:
            await gd_auth.assert_grid_access("OtherOrgsGrid", CUSTOMER_ORG_ID)

    message = str(excinfo.value)
    assert "OtherOrgsGrid" in message
    assert "organization_id" not in message.lower()
    assert str(CUSTOMER_ORG_ID) not in message
    assert "select" not in message.lower()
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_none_organization_id_denied_without_connecting():
    with (
        patch.dict(os.environ, _ENV, clear=False),
        patch(
            "mcp_servers.servers.grid_design_server.gd_auth.asyncpg.connect",
            new_callable=AsyncMock,
        ) as mock_connect,
    ):
        with pytest.raises(gd_auth.GridAccessDenied):
            await gd_auth.assert_grid_access("SomeGrid", None)

    mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_grid_access_denied_is_a_permission_error():
    conn = _mock_conn(fetchrow_return=None)
    with (
        patch.dict(os.environ, _ENV, clear=False),
        patch(
            "mcp_servers.servers.grid_design_server.gd_auth.asyncpg.connect",
            new_callable=AsyncMock,
            return_value=conn,
        ),
    ):
        with pytest.raises(PermissionError):
            await gd_auth.assert_grid_access("OtherOrgsGrid", CUSTOMER_ORG_ID)


# ── resolve_grid_name_for_design ─────────────────────────────────────────────


def _patch_repository(designs_row, grids_row):
    repos = {
        "designs": MagicMock(),
        "grids": MagicMock(),
    }
    repos["designs"].get.return_value = designs_row
    repos["grids"].get.return_value = grids_row
    return patch.object(gd_auth, "Repository", side_effect=lambda t: repos[t]), repos


def test_resolve_grid_name_for_design_found():
    patcher, repos = _patch_repository(
        designs_row={"id": "d1", "grid": "g1"}, grids_row={"id": "g1", "name": "ExampleGrid"}
    )
    with patcher:
        name = gd_auth.resolve_grid_name_for_design("d1")
    assert name == "ExampleGrid"
    repos["designs"].get.assert_called_once_with("d1")
    repos["grids"].get.assert_called_once_with("g1")


def test_resolve_grid_name_for_design_missing_design_returns_none():
    patcher, repos = _patch_repository(designs_row=None, grids_row=None)
    with patcher:
        name = gd_auth.resolve_grid_name_for_design("missing-design")
    assert name is None
    repos["grids"].get.assert_not_called()


def test_resolve_grid_name_for_design_missing_grid_returns_none():
    patcher, repos = _patch_repository(designs_row={"id": "d1", "grid": "g1"}, grids_row=None)
    with patcher:
        name = gd_auth.resolve_grid_name_for_design("d1")
    assert name is None
