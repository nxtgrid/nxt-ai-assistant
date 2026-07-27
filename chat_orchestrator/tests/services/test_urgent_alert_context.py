"""Tests for one-request live telemetry used by urgent /chat/notify alerts."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from orchestrator.services.urgent_alert_context import build_urgent_alert_context


@pytest.mark.asyncio
async def test_live_context_preserves_zero_kw_and_reuses_the_single_lookup():
    calls = 0

    async def read_output(grid_name: str) -> float | None:
        nonlocal calls
        calls += 1
        assert grid_name == "Acme Grid"
        return 0.0

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter off",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_output=read_output,
    )

    output, line, facts = await asyncio.gather(
        context.output_kw(), context.telegram_output_line(), context.llm_facts()
    )

    assert output == 0.0
    assert line == "⚡ Live output: 0.0 kW"
    assert facts == {"live_inverter_output_kw": 0.0}
    assert calls == 1


@pytest.mark.asyncio
async def test_live_context_reports_unavailable_when_lookup_raises():
    async def read_output(_grid_name: str) -> float | None:
        raise RuntimeError("VRM unavailable")

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter off",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_output=read_output,
    )

    assert await context.output_kw() is None
    assert await context.telegram_output_line() == "⚡ Live output: unavailable"
    assert await context.llm_facts() == {"live_inverter_output": "unavailable"}


@pytest.mark.asyncio
async def test_live_context_reports_unavailable_when_lookup_times_out():
    async def read_output(_grid_name: str) -> float | None:
        await asyncio.sleep(1)
        return 2.4

    context = build_urgent_alert_context(
        subject="! Urgent: Inverter off",
        incoming_severity="urgent",
        grid_name="Acme Grid",
        read_output=read_output,
        timeout_seconds=0.001,
    )

    assert await context.telegram_output_line() == "⚡ Live output: unavailable"


@pytest.mark.asyncio
async def test_customer_live_output_rejects_stale_vrm_data(monkeypatch):
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mcp_servers"))
    from mcp_servers.servers.customer_server import client_grid_status
    from mcp_servers.servers.customer_server.client import CustomerServiceClient

    class FakeConnection:
        async def fetchrow(self, _query, _grid_name):
            return {"generation_external_site_id": "123"}

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

    class FakeVoltage:
        error = None
        total_power_kw = 2.4
        data_timestamp = datetime.now(timezone.utc) - timedelta(minutes=31)

    class FakeVRMPlatform:
        async def initialize(self):
            return None

        async def get_current_inverter_voltage(self, site_id):
            assert site_id == "123"
            return FakeVoltage()

    monkeypatch.setattr(client_grid_status, "get_auth_service", lambda: FakeAuthService())
    monkeypatch.setattr(client_grid_status, "VRMPlatform", FakeVRMPlatform)

    assert await CustomerServiceClient().get_live_inverter_output("Acme Grid") is None
