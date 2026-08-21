"""Tests for one-request live telemetry used by urgent /chat/notify alerts."""

from __future__ import annotations

import asyncio
import builtins
import sys
from pathlib import Path

import pytest

from orchestrator.services.urgent_alert_context import build_urgent_alert_context


def test_live_context_defers_the_default_mcp_import_until_lookup(monkeypatch):
    """A non-urgent alert must not even import the telemetry client."""
    original_import = builtins.__import__

    def fail_mcp_import(name, *args, **kwargs):
        if name.startswith("mcp_servers"):
            raise AssertionError("telemetry client was imported eagerly")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_mcp_import)

    context = build_urgent_alert_context(
        subject="! Warning: Component changed",
        grid_name="Acme Grid",
    )

    assert context.is_incoming_urgent() is False


def test_subject_urgency_wins_over_a_lower_structured_severity():
    async def read_telemetry(_grid_name: str) -> dict:
        return {"output_kw": 1.0, "battery_voltage_v": None}

    context = build_urgent_alert_context(
        subject="! Urgent: Grid down",
        incoming_severity="warning",
        grid_name="Acme Grid",
        read_telemetry=read_telemetry,
    )

    assert context.is_incoming_urgent() is True


@pytest.mark.asyncio
async def test_live_context_preserves_zero_kw_and_reuses_the_single_lookup():
    calls = 0

    async def read_telemetry(grid_name: str) -> dict:
        nonlocal calls
        calls += 1
        assert grid_name == "Acme Grid"
        return {"output_kw": 0.0, "battery_voltage_v": None}

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter off",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_telemetry=read_telemetry,
    )

    output, line, facts = await asyncio.gather(
        context.output_kw(), context.telegram_output_line(), context.llm_facts()
    )

    assert output == 0.0
    assert line == "⚡ Live output: 0.0 kW"
    assert facts == {"live_inverter_output_kw": 0.0}
    assert calls == 1


@pytest.mark.asyncio
async def test_live_context_exposes_cached_normalized_site_status():
    calls = 0

    async def read_telemetry(_grid_name: str) -> dict:
        nonlocal calls
        calls += 1
        return {
            "generation_management": "managed",
            "grid_status": "likely_isolated",
            "site_status": "isolated",
            "output_kw": 0.8,
            "battery_voltage_v": 51.8,
            "l1_voltage_v": 230.0,
            "l2_voltage_v": 230.0,
            "l3_voltage_v": 230.0,
            "observed_at": "2026-08-21T10:00:00+00:00",
            "fresh": True,
        }

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter output low",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_telemetry=read_telemetry,
    )

    telemetry = await context.telemetry()

    assert telemetry["site_status"] == "isolated"
    assert telemetry["grid_status"] == "likely_isolated"
    assert telemetry["l1_voltage_v"] == 230.0
    assert calls == 1


@pytest.mark.asyncio
async def test_live_context_reports_unavailable_when_lookup_raises():
    async def read_telemetry(_grid_name: str) -> dict:
        raise RuntimeError("VRM unavailable")

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter off",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_telemetry=read_telemetry,
    )

    assert await context.output_kw() is None
    assert await context.battery_voltage_v() is None
    assert await context.telegram_output_line() == "⚡ Live output: unavailable"
    assert await context.llm_facts() == {"live_inverter_output": "unavailable"}


@pytest.mark.asyncio
async def test_live_context_reports_unavailable_when_lookup_times_out():
    async def read_telemetry(_grid_name: str) -> dict:
        await asyncio.sleep(1)
        return {"output_kw": 2.4, "battery_voltage_v": 51.8}

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter off",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_telemetry=read_telemetry,
        timeout_seconds=0.001,
    )

    assert await context.telegram_output_line() == "⚡ Live output: unavailable"


@pytest.mark.asyncio
async def test_telegram_line_appends_battery_voltage_when_available():
    async def read_telemetry(_grid_name: str) -> dict:
        return {"output_kw": 2.4, "battery_voltage_v": 51.8}

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter off",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_telemetry=read_telemetry,
    )

    assert await context.telegram_output_line() == "⚡ Live output: 2.4 kW · 🔋 Battery: 51.8 V"
    assert await context.llm_facts() == {"live_inverter_output_kw": 2.4, "battery_voltage_v": 51.8}


@pytest.mark.asyncio
async def test_telegram_line_omits_battery_clause_when_only_output_is_known():
    """Each half degrades independently -- a known output with no battery
    reading must not print a redundant 'unavailable' battery clause."""

    async def read_telemetry(_grid_name: str) -> dict:
        return {"output_kw": 2.4, "battery_voltage_v": None}

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter off",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_telemetry=read_telemetry,
    )

    assert await context.telegram_output_line() == "⚡ Live output: 2.4 kW"
    assert await context.llm_facts() == {"live_inverter_output_kw": 2.4}


@pytest.mark.asyncio
async def test_telegram_line_reports_battery_with_unavailable_output():
    """The converse: output unknown but battery known -- output keeps its
    own 'unavailable' wording, battery still appends."""

    async def read_telemetry(_grid_name: str) -> dict:
        return {"output_kw": None, "battery_voltage_v": 51.8}

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter off",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_telemetry=read_telemetry,
    )

    assert (
        await context.telegram_output_line() == "⚡ Live output: unavailable · 🔋 Battery: 51.8 V"
    )
    assert await context.llm_facts() == {
        "live_inverter_output": "unavailable",
        "battery_voltage_v": 51.8,
    }


def _fake_grid_lookup_env(monkeypatch, *, site_id: str = "123", managed: bool = True):
    """Shared plumbing for the get_live_telemetry tests below: a fake auth
    pool that resolves one grid to one VRM site id. Returns the
    (client_grid_status module, CustomerServiceClient class) pair so each
    test only has to patch VRMPlatform.

    Imported as ``servers.customer_server.*`` (relying on the ``sys.path``
    insert below), matching exactly how ``client.py`` imports
    ``ClientGridStatusMixin`` internally -- importing instead as
    ``mcp_servers.servers.customer_server.*`` resolves to a second, distinct
    module object under a different fully-qualified name, so patching *that*
    copy's ``get_auth_service``/``VRMPlatform`` silently misses the mixin
    ``CustomerServiceClient`` actually calls through, and every assertion
    here would pass by accident (any unpatched failure degrades to the same
    ``None``/empty-dict result a correctly-mocked stale/absent reading
    would).
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mcp_servers"))
    from servers.customer_server import client_grid_status
    from servers.customer_server.client import CustomerServiceClient

    class FakeConnection:
        async def fetchrow(self, _query, _grid_name):
            return {
                "generation_external_site_id": site_id,
                client_grid_status.MANAGED_GENERATION_COLUMN: managed,
                "is_hps_on": True,
                "is_fs_on": False,
                "is_hps_on_threshold_kw": 2.0,
            }

    class Acquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return False

    class FakePool:
        def acquire(self):
            return Acquire()

    class FakeAuthService:
        async def _get_db_pool(self):
            return FakePool()

    monkeypatch.setattr(client_grid_status, "get_auth_service", lambda: FakeAuthService())
    return client_grid_status, CustomerServiceClient


@pytest.mark.asyncio
async def test_customer_live_output_rejects_stale_vrm_data(monkeypatch):
    from datetime import datetime, timedelta, timezone

    client_grid_status, CustomerServiceClient = _fake_grid_lookup_env(monkeypatch)

    class FakeVoltage:
        error = None
        total_power_kw = 2.4
        data_timestamp = datetime.now(timezone.utc) - timedelta(minutes=31)
        is_producing = True
        l1_voltage_v = 230.0
        l2_voltage_v = 230.0
        l3_voltage_v = 230.0

    class FakeBattery:
        voltage_v = 51.2

    class FakeVRMPlatform:
        async def initialize(self):
            return None

        async def get_current_inverter_voltage(self, site_id):
            assert site_id == "123"
            return FakeVoltage()

        async def get_current_battery_status(self, site_id):
            assert site_id == "123"
            return FakeBattery()

    monkeypatch.setattr(client_grid_status, "VRMPlatform", FakeVRMPlatform)

    assert await CustomerServiceClient().get_live_inverter_output("Acme Grid") is None


@pytest.mark.asyncio
async def test_live_telemetry_returns_output_and_battery_voltage_together(monkeypatch):
    from datetime import datetime, timezone

    client_grid_status, CustomerServiceClient = _fake_grid_lookup_env(monkeypatch)

    class FakeVoltage:
        error = None
        total_power_kw = 2.4
        data_timestamp = datetime.now(timezone.utc)
        is_producing = True
        l1_voltage_v = 230.0
        l2_voltage_v = 230.0
        l3_voltage_v = 230.0

    class FakeBattery:
        voltage_v = 51.8

    class FakeVRMPlatform:
        async def initialize(self):
            return None

        async def get_current_inverter_voltage(self, site_id):
            return FakeVoltage()

        async def get_current_battery_status(self, site_id):
            return FakeBattery()

    monkeypatch.setattr(client_grid_status, "VRMPlatform", FakeVRMPlatform)

    telemetry = await CustomerServiceClient().get_live_telemetry("Acme Grid")
    assert telemetry == {
        "generation_management": "managed",
        "grid_status": "hps_on",
        "site_status": "on",
        "output_kw": 2.4,
        "battery_voltage_v": 51.8,
        "l1_voltage_v": 230.0,
        "l2_voltage_v": 230.0,
        "l3_voltage_v": 230.0,
        "observed_at": telemetry["observed_at"],
        "fresh": True,
    }


@pytest.mark.asyncio
async def test_live_telemetry_battery_voltage_survives_a_stale_inverter_reading(monkeypatch):
    """C1: each field is independently None -- a stale/errored inverter
    reading must not blank out an otherwise-good battery reading."""
    from datetime import datetime, timedelta, timezone

    client_grid_status, CustomerServiceClient = _fake_grid_lookup_env(monkeypatch)

    class FakeVoltage:
        error = None
        total_power_kw = 2.4
        data_timestamp = datetime.now(timezone.utc) - timedelta(minutes=45)
        is_producing = True
        l1_voltage_v = 230.0
        l2_voltage_v = 230.0
        l3_voltage_v = 230.0

    class FakeBattery:
        voltage_v = 51.8

    class FakeVRMPlatform:
        async def initialize(self):
            return None

        async def get_current_inverter_voltage(self, site_id):
            return FakeVoltage()

        async def get_current_battery_status(self, site_id):
            return FakeBattery()

    monkeypatch.setattr(client_grid_status, "VRMPlatform", FakeVRMPlatform)

    telemetry = await CustomerServiceClient().get_live_telemetry("Acme Grid")
    assert telemetry == {
        "generation_management": "managed",
        "grid_status": "unknown",
        "site_status": "unknown",
        "output_kw": None,
        "battery_voltage_v": 51.8,
        "l1_voltage_v": None,
        "l2_voltage_v": None,
        "l3_voltage_v": None,
        "observed_at": telemetry["observed_at"],
        "fresh": False,
    }


@pytest.mark.asyncio
async def test_live_telemetry_output_survives_a_failed_battery_fetch(monkeypatch):
    from datetime import datetime, timezone

    client_grid_status, CustomerServiceClient = _fake_grid_lookup_env(monkeypatch)

    class FakeVoltage:
        error = None
        total_power_kw = 2.4
        data_timestamp = datetime.now(timezone.utc)
        is_producing = True
        l1_voltage_v = 230.0
        l2_voltage_v = 230.0
        l3_voltage_v = 230.0

    class FakeVRMPlatform:
        async def initialize(self):
            return None

        async def get_current_inverter_voltage(self, site_id):
            return FakeVoltage()

        async def get_current_battery_status(self, site_id):
            raise RuntimeError("BatterySummary widget unavailable")

    monkeypatch.setattr(client_grid_status, "VRMPlatform", FakeVRMPlatform)

    telemetry = await CustomerServiceClient().get_live_telemetry("Acme Grid")
    assert telemetry == {
        "generation_management": "managed",
        "grid_status": "hps_on",
        "site_status": "on",
        "output_kw": 2.4,
        "battery_voltage_v": None,
        "l1_voltage_v": 230.0,
        "l2_voltage_v": 230.0,
        "l3_voltage_v": 230.0,
        "observed_at": telemetry["observed_at"],
        "fresh": True,
    }


@pytest.mark.asyncio
async def test_live_telemetry_returns_both_none_when_grid_has_no_vrm_site(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mcp_servers"))
    from servers.customer_server import client_grid_status
    from servers.customer_server.client import CustomerServiceClient

    class FakeConnection:
        async def fetchrow(self, _query, _grid_name):
            return {"generation_external_site_id": None}

    class Acquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return False

    class FakePool:
        def acquire(self):
            return Acquire()

    class FakeAuthService:
        async def _get_db_pool(self):
            return FakePool()

    monkeypatch.setattr(client_grid_status, "get_auth_service", lambda: FakeAuthService())

    telemetry = await CustomerServiceClient().get_live_telemetry("Off-grid Site")
    assert telemetry == {
        "generation_management": "unknown",
        "grid_status": "unknown",
        "site_status": "unknown",
        "output_kw": None,
        "battery_voltage_v": None,
        "l1_voltage_v": None,
        "l2_voltage_v": None,
        "l3_voltage_v": None,
        "observed_at": None,
        "fresh": False,
    }


@pytest.mark.asyncio
async def test_unmanaged_generation_skips_vrm_and_is_not_an_error(monkeypatch):
    client_grid_status, CustomerServiceClient = _fake_grid_lookup_env(monkeypatch, managed=False)

    class FakeVRMPlatform:
        def __init__(self):
            raise AssertionError("unmanaged grids must not initialize VRM")

    monkeypatch.setattr(client_grid_status, "VRMPlatform", FakeVRMPlatform)

    telemetry = await CustomerServiceClient().get_live_telemetry("Acme Grid")

    assert telemetry == {
        "generation_management": "unmanaged",
        "grid_status": "unknown",
        "site_status": "unknown",
        "output_kw": None,
        "battery_voltage_v": None,
        "l1_voltage_v": None,
        "l2_voltage_v": None,
        "l3_voltage_v": None,
        "observed_at": None,
        "fresh": False,
    }


@pytest.mark.asyncio
async def test_all_zero_fresh_phases_are_off(monkeypatch):
    from datetime import datetime, timezone

    client_grid_status, CustomerServiceClient = _fake_grid_lookup_env(monkeypatch)

    class FakeVoltage:
        error = None
        total_power_kw = 0.0
        data_timestamp = datetime.now(timezone.utc)
        is_producing = False
        l1_voltage_v = 0.0
        l2_voltage_v = 0.0
        l3_voltage_v = 0.0

    class FakeVRMPlatform:
        async def initialize(self):
            return None

        async def get_current_inverter_voltage(self, site_id):
            return FakeVoltage()

        async def get_current_battery_status(self, site_id):
            return None

    monkeypatch.setattr(client_grid_status, "VRMPlatform", FakeVRMPlatform)

    telemetry = await CustomerServiceClient().get_live_telemetry("Acme Grid")

    assert telemetry["grid_status"] == "off"
    assert telemetry["site_status"] == "off"
    assert telemetry["l1_voltage_v"] == telemetry["l2_voltage_v"] == telemetry["l3_voltage_v"] == 0.0
    assert telemetry["fresh"] is True
