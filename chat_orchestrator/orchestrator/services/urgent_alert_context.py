"""Request-local live telemetry for resilient urgent alert delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from orchestrator.services.ticketing.alert_facts import derive_severity
from shared.config import flag_registry as fr
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

LiveOutputReader = Callable[[str], Awaitable[Optional[float]]]


class LiveOutputLookup:
    """Lazily resolve and cache one grid's current inverter output per request."""

    def __init__(
        self,
        grid_name: str,
        read_output: LiveOutputReader,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self._grid_name = grid_name
        self._read_output = read_output
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(fr.get("URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS"))
            if "URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS" in fr.FLAGS
            else 3.0
        )
        self._task: Optional[asyncio.Task[Optional[float]]] = None

    async def get(self) -> Optional[float]:
        if self._task is None:
            self._task = asyncio.create_task(self._read_once())
        return await self._task

    async def _read_once(self) -> Optional[float]:
        try:
            return await asyncio.wait_for(
                self._read_output(self._grid_name), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            LOGGER.warning("Urgent alert live output timed out for grid %r", self._grid_name)
        except Exception:
            LOGGER.warning("Urgent alert live output failed for grid %r", self._grid_name, exc_info=True)
        return None


@dataclass(frozen=True)
class UrgentAlertContext:
    """Canonical alert facts plus a shared lazy live-output lookup."""

    subject: str
    incoming_severity: str
    _output_lookup: LiveOutputLookup = field(repr=False)

    def is_incoming_urgent(self) -> bool:
        return self.incoming_severity == "urgent"

    async def output_kw(self) -> Optional[float]:
        return await self._output_lookup.get()

    async def llm_facts(self) -> dict[str, object]:
        output_kw = await self.output_kw()
        if output_kw is None:
            return {"live_inverter_output": "unavailable"}
        return {"live_inverter_output_kw": output_kw}

    async def telegram_output_line(self) -> str:
        output_kw = await self.output_kw()
        if output_kw is None:
            return "⚡ Live output: unavailable"
        return f"⚡ Live output: {output_kw:.1f} kW"


def build_urgent_alert_context(
    *,
    subject: str,
    incoming_severity: str = "",
    grid_name: str,
    read_output: Optional[LiveOutputReader] = None,
    timeout_seconds: Optional[float] = None,
) -> UrgentAlertContext:
    """Build alert context without making telemetry I/O until it is needed."""
    if read_output is None:
        from mcp_servers.servers.customer_server.client import customer_client

        read_output = customer_client.get_live_inverter_output

    normalized_subject = subject.strip() or "Notification"
    normalized_severity = (incoming_severity or derive_severity(normalized_subject)).strip().lower()
    return UrgentAlertContext(
        subject=normalized_subject,
        incoming_severity=normalized_severity,
        _output_lookup=LiveOutputLookup(grid_name, read_output, timeout_seconds),
    )
