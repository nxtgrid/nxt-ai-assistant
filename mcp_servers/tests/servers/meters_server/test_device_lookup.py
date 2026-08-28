"""find_meters_by_device_id resolves a device against the auth DB.

The lookup used to GET ``{chat_db}/rest/v1/meters`` with a hand-minted
Supabase JWT. The chat database has no ``meters`` table -- meters live in the
auth DB -- so that request could never succeed. It failed, was swallowed into
a warning, and returned ``[]``. Every caller then behaved as though the device
served no meters, which is precisely what ``get_dcu_status_by_id``'s org check
reads as "not yours", making the tool unusable for every non-staff user.

The auth DB models a LoRaWAN base station as a ``dcus`` row whose
``communication_protocol`` is ``CALIN_LORAWAN`` -- there is no separate
gateway entity and no ``meters.gateway_id``, so device kind comes from the
protocol rather than from guessing at the shape of the id.
"""

import os
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
_MCP_ROOT = os.path.join(_REPO_ROOT, "mcp_servers")
for _p in (_MCP_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_BASE_ENV = {
    "AUTH_DB_HOST": "db.example.invalid",
    "AUTH_DB_USER": "reader",
    "AUTH_DB_PASSWORD": "unused",  # pragma: allowlist secret
    "AUTH_DB_NAME": "postgres",
    "STAFF_ORG_ID": "2",
}


def _load():
    with patch.dict(os.environ, _BASE_ENV, clear=False):
        import importlib as _il

        import servers.meters_server.meters_mcp_server as m

        _il.reload(m)
        return m


@pytest.fixture()
def mod():
    return _load()


class _FakeConnection:
    """Records the SQL and arguments each query was issued with."""

    def __init__(self, dcu_row: Optional[Dict[str, Any]], meter_rows: List[Dict[str, Any]]):
        self.dcu_row = dcu_row
        self.meter_rows = meter_rows
        self.calls: List[tuple] = []
        self.closed = False

    async def fetchrow(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        return self.dcu_row

    async def fetch(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        return list(self.meter_rows)

    async def close(self):
        self.closed = True


def _patch_connection(mod, monkeypatch, connection):
    async def _connect():
        if isinstance(connection, Exception):
            raise connection
        return connection

    monkeypatch.setattr(mod, "_auth_db_connect", _connect)


@pytest.mark.asyncio
async def test_dcu_device_returns_the_meters_it_serves(mod, monkeypatch):
    conn = _FakeConnection(
        {"id": 41, "communication_protocol": "CALIN_GPRS"},
        [{"external_reference": "M-1"}, {"external_reference": "M-2"}],
    )
    _patch_connection(mod, monkeypatch, conn)

    meters = await mod.client.find_meters_by_device_id("999000111", organization_id=7)

    assert [m["meter_no"] for m in meters] == ["M-1", "M-2"]
    assert all(m["dcu_id"] == "999000111" for m in meters)
    assert all(m["gateway_id"] is None for m in meters)
    assert conn.closed is True


@pytest.mark.asyncio
async def test_lorawan_device_is_reported_as_a_base_station(mod, monkeypatch):
    conn = _FakeConnection(
        {"id": 9, "communication_protocol": "CALIN_LORAWAN"},
        [{"external_reference": "M-9"}],
    )
    _patch_connection(mod, monkeypatch, conn)

    meters = await mod.client.find_meters_by_device_id("ffff0000ffff0001", organization_id=7)

    assert meters[0]["gateway_id"] == "ffff0000ffff0001"
    assert meters[0]["dcu_id"] is None
    assert meters[0]["meter_type"] == "lorawan"


@pytest.mark.asyncio
async def test_lookup_is_scoped_to_the_callers_organization(mod, monkeypatch):
    """Explicit org scoping replaces the RLS the minted JWT used to carry."""
    conn = _FakeConnection(
        {"id": 41, "communication_protocol": "CALIN_GPRS"}, [{"external_reference": "M-1"}]
    )
    _patch_connection(mod, monkeypatch, conn)

    await mod.client.find_meters_by_device_id("999000111", organization_id=7)

    assert all(7 in args for _sql, args in conn.calls)
    assert any("rls_organization_id" in sql for sql, _args in conn.calls)


@pytest.mark.asyncio
async def test_staff_lookup_is_unscoped(mod, monkeypatch):
    conn = _FakeConnection(
        {"id": 41, "communication_protocol": "CALIN_GPRS"}, [{"external_reference": "M-1"}]
    )
    _patch_connection(mod, monkeypatch, conn)

    meters = await mod.client.find_meters_by_device_id("999000111", organization_id=None)

    assert [m["meter_no"] for m in meters] == ["M-1"]
    assert all(None in args for _sql, args in conn.calls)


@pytest.mark.asyncio
async def test_unknown_device_returns_no_meters(mod, monkeypatch):
    conn = _FakeConnection(None, [])
    _patch_connection(mod, monkeypatch, conn)

    assert await mod.client.find_meters_by_device_id("nope", organization_id=7) == []
    assert conn.closed is True


@pytest.mark.asyncio
async def test_database_failure_returns_no_meters(mod, monkeypatch):
    """Unchanged fail-safe: a lookup outage must not raise into the tool."""
    _patch_connection(mod, monkeypatch, RuntimeError("auth db unreachable"))

    assert await mod.client.find_meters_by_device_id("999000111", organization_id=7) == []


@pytest.mark.asyncio
async def test_meter_type_never_carries_the_auth_db_commercial_tier(mod, monkeypatch):
    """`meters.meter_type` in the auth DB is ('HPS','FS') -- a commercial tier,
    not the vendor protocol the unified layer branches on. Leaking it here
    would make `matched_types == {"calin_v1"}` and its siblings compare
    against the wrong vocabulary, silently routing to the wrong vendor API."""
    conn = _FakeConnection(
        {"id": 41, "communication_protocol": "CALIN_GPRS"}, [{"external_reference": "M-1"}]
    )
    _patch_connection(mod, monkeypatch, conn)

    meters = await mod.client.find_meters_by_device_id("999000111", organization_id=7)

    assert meters[0]["meter_type"] not in {"HPS", "FS", "hps", "fs"}
    assert all("meter_type" not in sql for sql, _args in conn.calls)


# ---------------------------------------------------------------------------
# get_dcu_status_by_id -- org scoping
# ---------------------------------------------------------------------------


class _FakePermissions:
    def __init__(self, org_id: int):
        self.organization_ids = [org_id]


def _stub_auth(mod, monkeypatch, org_id: int):
    class _Auth:
        async def get_user_permissions(self, email: str):
            return _FakePermissions(org_id)

    monkeypatch.setattr(mod, "get_auth_service", lambda: _Auth())


def _stub_lookup(mod, monkeypatch, meters: List[Dict[str, Any]], seen: Dict[str, Any]):
    async def _find(device_id, organization_id=None):
        seen["device_id"] = device_id
        seen["organization_id"] = organization_id
        return meters

    async def _status(**kwargs):
        seen["status_kwargs"] = kwargs
        return {"device_id": kwargs.get("device_id"), "status": "online"}

    monkeypatch.setattr(mod.client, "find_meters_by_device_id", _find)
    monkeypatch.setattr(mod.client, "unified_get_device_status_by_id", _status)


@pytest.mark.asyncio
async def test_customer_device_lookup_is_scoped_to_their_org(mod, monkeypatch):
    """The scoped query *is* the permission check: it can only return meters
    the caller's organization owns."""
    seen: Dict[str, Any] = {}
    _stub_auth(mod, monkeypatch, org_id=7)
    _stub_lookup(mod, monkeypatch, [{"meter_no": "M-1"}], seen)

    await mod._tool_get_dcu_status_by_id(
        {"device_id": "999000111", "user_email": "customer@example.com"}
    )

    assert seen["organization_id"] == 7


@pytest.mark.asyncio
async def test_staff_device_lookup_is_unscoped(mod, monkeypatch):
    seen: Dict[str, Any] = {}
    _stub_auth(mod, monkeypatch, org_id=mod.STAFF_ORG_ID)
    _stub_lookup(mod, monkeypatch, [{"meter_no": "M-1"}], seen)

    await mod._tool_get_dcu_status_by_id(
        {"device_id": "999000111", "user_email": "staff@example.com"}
    )

    assert seen["organization_id"] is None


@pytest.mark.asyncio
async def test_customer_is_refused_a_device_serving_none_of_their_meters(mod, monkeypatch):
    import json

    seen: Dict[str, Any] = {}
    _stub_auth(mod, monkeypatch, org_id=7)
    _stub_lookup(mod, monkeypatch, [], seen)

    result = await mod._tool_get_dcu_status_by_id(
        {"device_id": "999000111", "user_email": "customer@example.com"}
    )

    assert "not accessible" in json.loads(result[0].text)["error"]


@pytest.mark.asyncio
async def test_customer_with_a_matching_meter_gets_the_status(mod, monkeypatch):
    """Regression: the lookup used to run unscoped and always return [], so a
    customer was refused every device even when the device served their own
    meters."""
    import json

    seen: Dict[str, Any] = {}
    _stub_auth(mod, monkeypatch, org_id=7)
    _stub_lookup(mod, monkeypatch, [{"meter_no": "M-1"}], seen)

    result = await mod._tool_get_dcu_status_by_id(
        {"device_id": "999000111", "user_email": "customer@example.com"}
    )

    assert json.loads(result[0].text)["status"] == "online"
