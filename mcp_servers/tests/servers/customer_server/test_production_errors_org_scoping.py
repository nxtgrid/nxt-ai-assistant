"""``get_recent_production_errors_and_power`` had no organization scoping of
its own -- unlike ``get_grid_status``/``get_all_grids_status``, its grid
lookup took no ``organization_id`` at all. That was safe only because the
tool was staff-only (``visible_to_customer: False``). Making it
customer-visible (so a customer conversation can check for an active alarm
behind a stale/Unknown grid status -- see ``live_metrics_view`` and
``customer.system.prompt``'s "Stale or Unknown Live Data") means it now needs
the same organization scoping every other customer-facing grid lookup in this
file already has, or any customer could read another organization's grid
alarm/power history just by knowing (or guessing) its name.

``STAFF_ORG_ID`` and the internal alert-judgment caller (``get_live_telemetry``,
which calls this with no ``organization_id`` at all) both stay unscoped --
matching how every other org-scoped query in this file already treats
``STAFF_ORG_ID``/``None`` as "see everything".
"""

import os
import sys
from typing import Any, List

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.customer_server import client_grid_status as mod  # noqa: E402
from servers.customer_server.client_base import STAFF_ORG_ID  # noqa: E402


class _FakeConnection:
    """Records the SQL and args each query ran with. The grid lookup always
    misses on ``generation_external_site_id`` so the method returns right
    after the query under test, never reaching a real VRM call."""

    def __init__(self):
        self.calls: List[tuple] = []

    async def fetchrow(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        return {"id": 1, "generation_external_site_id": None}


class _FakePool:
    def __init__(self, connection: _FakeConnection):
        self._connection = connection

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, *exc_info):
        return False


class _FakeAuthService:
    def __init__(self, connection: _FakeConnection):
        self._pool = _FakePool(connection)

    async def _get_db_pool(self):
        return self._pool


def _client(monkeypatch, conn: _FakeConnection) -> "mod.ClientGridStatusMixin":
    monkeypatch.setattr(mod, "get_auth_service", lambda: _FakeAuthService(conn))
    return mod.ClientGridStatusMixin()


@pytest.mark.asyncio
async def test_customer_lookup_is_scoped_to_their_organization(monkeypatch):
    conn = _FakeConnection()
    client = _client(monkeypatch, conn)

    await client.get_recent_production_errors_and_power("Example Grid", organization_id=7)

    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "organization_id = $2" in sql
    assert args == ("Example Grid", 7)


@pytest.mark.asyncio
async def test_staff_lookup_is_unscoped(monkeypatch):
    conn = _FakeConnection()
    client = _client(monkeypatch, conn)

    await client.get_recent_production_errors_and_power(
        "Example Grid", organization_id=STAFF_ORG_ID
    )

    sql, args = conn.calls[0]
    assert "organization_id" not in sql
    assert args == ("Example Grid",)


@pytest.mark.asyncio
async def test_internal_alert_caller_with_no_organization_id_is_unscoped(monkeypatch):
    """``get_live_telemetry`` (the automated alert-judgment path) calls this
    with no ``organization_id`` at all -- must keep working exactly as before."""
    conn = _FakeConnection()
    client = _client(monkeypatch, conn)

    await client.get_recent_production_errors_and_power("Example Grid")

    sql, args = conn.calls[0]
    assert "organization_id" not in sql
    assert args == ("Example Grid",)


@pytest.mark.asyncio
async def test_tool_dispatcher_requires_organization_id():
    """The MCP tool wrapper must fail closed, not fall through to the client
    method's own ``organization_id=None`` (unscoped) default -- that default
    exists for the internal alert-judgment caller, not a customer session."""
    import servers.customer_server.customer_mcp_server as server_mod

    result = await server_mod._tool_get_recent_production_errors_and_power(
        {"grid_name": "Example Grid"}
    )

    assert "organization_id is required" in result[0].text
