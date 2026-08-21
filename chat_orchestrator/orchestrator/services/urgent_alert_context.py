"""Request-local live telemetry for resilient urgent alert delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from orchestrator.services.ticketing.alert_facts import derive_severity
from shared.config import flag_registry as fr
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

LiveTelemetry = Dict[str, Any]
LiveTelemetryReader = Callable[[str], Awaitable[LiveTelemetry]]
_UNAVAILABLE: LiveTelemetry = {
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


class LiveTelemetryLookup:
    """Lazily resolve and cache one grid's current live telemetry (inverter
    output + battery voltage) per request."""

    def __init__(
        self,
        grid_name: str,
        read_telemetry: LiveTelemetryReader,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self._grid_name = grid_name
        self._read_telemetry = read_telemetry
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(fr.get("URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS"))
            if "URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS" in fr.FLAGS
            else 3.0
        )
        self._task: Optional[asyncio.Task[LiveTelemetry]] = None

    async def get(self) -> LiveTelemetry:
        if self._task is None:
            self._task = asyncio.create_task(self._read_once())
        return await self._task

    async def _read_once(self) -> LiveTelemetry:
        try:
            telemetry = await asyncio.wait_for(
                self._read_telemetry(self._grid_name), timeout=self._timeout_seconds
            )
            return {**_UNAVAILABLE, **telemetry}
        except asyncio.TimeoutError:
            LOGGER.warning("Urgent alert live telemetry timed out for grid {!r}", self._grid_name)
        except Exception:
            LOGGER.warning(
                "Urgent alert live telemetry failed for grid {!r}", self._grid_name, exc_info=True
            )
        return dict(_UNAVAILABLE)


@dataclass(frozen=True)
class UrgentAlertContext:
    """Canonical alert facts plus a shared lazy live-telemetry lookup."""

    subject: str
    incoming_severity: str
    _telemetry_lookup: LiveTelemetryLookup = field(repr=False)

    def is_incoming_urgent(self) -> bool:
        # Structured severity is preferred, but a stale/mislabelled incoming
        # value must not downgrade an explicitly urgent subject line.
        return self.incoming_severity == "urgent" or derive_severity(self.subject) == "urgent"

    async def output_kw(self) -> Optional[float]:
        telemetry = await self._telemetry_lookup.get()
        return telemetry.get("output_kw")

    async def battery_voltage_v(self) -> Optional[float]:
        telemetry = await self._telemetry_lookup.get()
        return telemetry.get("battery_voltage_v")

    async def telemetry(self) -> LiveTelemetry:
        """Return the cached complete observation for judgment context assembly."""
        return dict(await self._telemetry_lookup.get())

    async def llm_facts(self) -> dict[str, object]:
        telemetry = await self._telemetry_lookup.get()
        output_kw = telemetry.get("output_kw")
        facts: dict[str, object] = (
            {"live_inverter_output": "unavailable"}
            if output_kw is None
            else {"live_inverter_output_kw": output_kw}
        )
        battery_voltage_v = telemetry.get("battery_voltage_v")
        if battery_voltage_v is not None:
            facts["battery_voltage_v"] = battery_voltage_v
        return facts

    async def telegram_output_line(self) -> str:
        telemetry = await self._telemetry_lookup.get()
        output_kw = telemetry.get("output_kw")
        line = "⚡ Live output: unavailable" if output_kw is None else f"⚡ Live output: {output_kw:.1f} kW"
        battery_voltage_v = telemetry.get("battery_voltage_v")
        if battery_voltage_v is None:
            return line
        return f"{line} · 🔋 Battery: {battery_voltage_v:.1f} V"


def build_urgent_alert_context(
    *,
    subject: str,
    incoming_severity: str = "",
    grid_name: str,
    read_telemetry: Optional[LiveTelemetryReader] = None,
    timeout_seconds: Optional[float] = None,
) -> UrgentAlertContext:
    """Build alert context without making telemetry I/O until it is needed."""
    if read_telemetry is None:
        async def read_telemetry(grid_name: str) -> LiveTelemetry:
            # The MCP package has a separate import root in production.  Keep
            # that import inside the lazy reader so non-urgent alerts and
            # silent duplicates neither depend on nor initialize it.
            from mcp_servers.servers.customer_server.client import customer_client

            return await customer_client.get_live_telemetry(grid_name)

    normalized_subject = subject.strip() or "Notification"
    normalized_severity = (incoming_severity or derive_severity(normalized_subject)).strip().lower()
    return UrgentAlertContext(
        subject=normalized_subject,
        incoming_severity=normalized_severity,
        _telemetry_lookup=LiveTelemetryLookup(grid_name, read_telemetry, timeout_seconds),
    )
