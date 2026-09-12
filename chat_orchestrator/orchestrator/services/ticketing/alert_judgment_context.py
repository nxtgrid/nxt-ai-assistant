"""Bounded, best-effort evidence collection for LLM alert judgment."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import Enum
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shared.grid_status import GridStatus, SiteStatus

from .alert_judgment import DeterministicFinding
from .notify_alert_delivery_repository import (
    OMChatMessage,
    PriorAlertMessage,
    delivery_history_failures_last_hour,
)

_T = TypeVar("_T")
_DESCRIPTION_LIMIT = 2_000
_MESSAGE_LIMIT = 500
_TICKET_LIMIT = 15
_PRIOR_ALERT_LIMIT = 20
_OM_MESSAGE_LIMIT = 50


class ContextStatus(str, Enum):
    AVAILABLE = "available"
    EMPTY = "empty"
    UNMANAGED = "unmanaged"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class _ContextModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ContextSourceResult(_ContextModel):
    status: ContextStatus
    item_count: int = Field(default=0, ge=0)
    detail: str = ""


class OpenTicketContext(_ContextModel):
    ref: str
    ticket_id: str | None = None
    backend: str = ""
    summary: str = ""
    description: str = ""
    age_hours: float | None = None
    root_cause_kind: str | None = None
    affected_keys: list[dict[str, Any]] = Field(default_factory=list)
    occurrence_count: int = 1
    status: str = ""
    signatures: list[str] = Field(default_factory=list)
    severity: str = ""


class AlertTelemetry(_ContextModel):
    # Why site_status is UNKNOWN, when it is. "" means the reading was usable.
    # "unmanaged" is a fact about the site; "stale" means the plant's gateway
    # has stopped reporting (it is dark, and device-level alerts derived from
    # the same feed are suspect); the rest are gaps in our own knowledge.
    # site_status collapses all of these to UNKNOWN, so anything that needs to
    # tell them apart -- the delivery policy, and the judgment LLM, which is
    # handed this whole model -- has to read this field.
    unavailable_reason: str = ""
    generation_management: Literal["managed", "unmanaged", "unknown"] = "unknown"
    grid_status: GridStatus = GridStatus.UNKNOWN
    site_status: SiteStatus = SiteStatus.UNKNOWN
    output_kw: float | None = None
    battery_voltage_v: float | None = None
    l1_voltage_v: float | None = None
    l2_voltage_v: float | None = None
    l3_voltage_v: float | None = None
    observed_at: str | None = None
    fresh: bool = False
    # Pre-fault evidence from get_recent_production_errors_and_power (VRM's
    # real alarm-log/diagnostics endpoints), named to match what
    # ticketing.correlation.prompt already instructs the model to look for --
    # see _alias_raw_alarm_fields for why these names differ from what the
    # raw telemetry provider returns them as.
    active_ve_bus_errors: list[dict[str, Any]] = Field(default_factory=list)
    recent_ve_bus_errors_30min: list[dict[str, Any]] = Field(default_factory=list)
    power_trend_past_30min: list[dict[str, Any]] = Field(default_factory=list)


class AlertJudgmentContext(_ContextModel):
    deterministic_findings: list[DeterministicFinding] = Field(default_factory=list)
    open_tickets: list[OpenTicketContext] = Field(default_factory=list)
    telemetry: AlertTelemetry = Field(default_factory=AlertTelemetry)
    prior_alerts: list[PriorAlertMessage] = Field(default_factory=list)
    om_messages: list[OMChatMessage] = Field(default_factory=list)
    # is_hps_on / is_hps_on_updated_at / DCU status roll-up
    # (auth_service.get_grid_operational_facts, via
    # correlation_rules.get_grid_operational_context) -- what the prompt's
    # "Root Cause Rules" section reads to decide whether a new alert is a
    # child of an existing grid-off/isolated state. Left as a raw dict
    # (matching how the legacy prompt path already treats it) rather than a
    # narrow sub-model: a stricter schema is exactly what silently dropped
    # the alarm fields above once already.
    grid_operational_facts: dict[str, Any] = Field(default_factory=dict)
    availability: dict[str, ContextSourceResult] = Field(default_factory=dict)

    def has_degradation(self) -> bool:
        return any(
            result.status in {ContextStatus.FAILED, ContextStatus.TIMED_OUT}
            for result in self.availability.values()
        )

    def availability_payload(self) -> dict[str, dict[str, Any]]:
        return {name: result.model_dump(mode="json") for name, result in self.availability.items()}


Provider = Callable[[], Awaitable[Any]]


def _count(value: Any) -> int:
    if isinstance(value, Mapping):
        return 1
    if isinstance(value, Sequence) and not isinstance(value, str):
        return len(value)
    return int(value is not None)


def _alias_raw_alarm_fields(value: Any) -> Any:
    """``get_live_telemetry`` (mcp_servers/customer_server/client_grid_status.py)
    returns pre-fault alarm/power-trend evidence under its own raw key names
    -- ``active_alarms``, ``recent_alarms_30min``, ``power_history_30min``.
    ``AlertTelemetry`` (and the judgment prompt's own instructions, and
    ``UrgentAlertContext.llm_facts()``, the sibling view of this same
    telemetry used for ticket-description context) use
    ``active_ve_bus_errors``/``recent_ve_bus_errors_30min``/
    ``power_trend_past_30min`` instead. Rename here rather than requiring the
    raw provider to change shape or the prompt to change vocabulary -- this
    stays the one place that has to know both names, so a mismatch is loud
    (a missing key) rather than a silent drop.

    A value that isn't a mapping, or one that already carries the aliased
    names (e.g. a test fixture), passes through unchanged."""
    if not isinstance(value, Mapping):
        return value
    aliased = dict(value)
    for raw_key, aliased_key in (
        ("active_alarms", "active_ve_bus_errors"),
        ("recent_alarms_30min", "recent_ve_bus_errors_30min"),
        ("power_history_30min", "power_trend_past_30min"),
    ):
        if aliased_key not in aliased and raw_key in aliased:
            aliased[aliased_key] = aliased[raw_key]
    return aliased


async def _capture(
    name: str, awaitable: Awaitable[_T], timeout_seconds: float
) -> tuple[_T | None, ContextSourceResult]:
    try:
        value = await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return None, ContextSourceResult(
            status=ContextStatus.TIMED_OUT, detail=f"{name} timed out"
        )
    except Exception as exc:
        return None, ContextSourceResult(status=ContextStatus.FAILED, detail=type(exc).__name__)
    return value, ContextSourceResult(
        status=ContextStatus.AVAILABLE if value else ContextStatus.EMPTY,
        item_count=_count(value),
    )


class AlertJudgmentContextAssembler:
    """Collect isolated context sources concurrently without losing alerts on failure."""

    def __init__(
        self,
        *,
        deterministic_findings_provider: Provider,
        open_tickets_provider: Provider,
        telemetry_provider: Provider,
        prior_alerts_provider: Provider,
        om_messages_provider: Provider,
        grid_operational_facts_provider: Provider,
        delivery_failures_provider: Callable[[], int] = delivery_history_failures_last_hour,
        timeout_seconds: float = 3.0,
    ) -> None:
        self._providers = {
            "deterministic_findings": deterministic_findings_provider,
            "open_tickets": open_tickets_provider,
            "telemetry": telemetry_provider,
            "prior_alerts": prior_alerts_provider,
            "om_messages": om_messages_provider,
            "grid_operational_facts": grid_operational_facts_provider,
        }
        self._delivery_failures_provider = delivery_failures_provider
        self._timeout_seconds = timeout_seconds

    async def assemble(
        self, *, grid_name: str, chat_id: str, topic_id: str | None, alert: Any
    ) -> AlertJudgmentContext:
        """Collect all sources concurrently; call-site values scope provider closures."""
        del grid_name, chat_id, topic_id, alert
        names = tuple(self._providers)
        tasks = [
            asyncio.create_task(_capture(name, self._providers[name](), self._timeout_seconds))
            for name in names
        ]
        captured = dict(zip(names, await asyncio.gather(*tasks)))
        values = {name: result[0] for name, result in captured.items()}
        availability = {name: result[1] for name, result in captured.items()}

        findings = self._convert_findings(values["deterministic_findings"], availability)
        tickets = self._convert_tickets(values["open_tickets"], availability)
        telemetry = self._convert_telemetry(values["telemetry"], availability)
        prior_alerts = self._convert_messages(
            values["prior_alerts"], PriorAlertMessage, _PRIOR_ALERT_LIMIT, availability, "prior_alerts"
        )
        om_messages = self._convert_messages(
            values["om_messages"], OMChatMessage, _OM_MESSAGE_LIMIT, availability, "om_messages"
        )
        grid_operational_facts = self._convert_grid_operational_facts(
            values["grid_operational_facts"], availability
        )
        if self._delivery_failures_provider() > 0:
            availability["prior_alerts"] = ContextSourceResult(
                status=ContextStatus.FAILED,
                item_count=len(prior_alerts),
                detail="delivery_history_write_failed",
            )
        return AlertJudgmentContext(
            deterministic_findings=findings,
            open_tickets=tickets,
            telemetry=telemetry,
            prior_alerts=prior_alerts,
            om_messages=om_messages,
            grid_operational_facts=grid_operational_facts,
            availability=availability,
        )

    @staticmethod
    def _mark_invalid(
        availability: dict[str, ContextSourceResult], name: str, error: Exception
    ) -> None:
        availability[name] = ContextSourceResult(status=ContextStatus.FAILED, detail=type(error).__name__)

    def _convert_findings(
        self, value: Any, availability: dict[str, ContextSourceResult]
    ) -> list[DeterministicFinding]:
        if availability["deterministic_findings"].status is not ContextStatus.AVAILABLE:
            return []
        try:
            return [DeterministicFinding.model_validate(item) for item in value]
        except (TypeError, ValidationError) as exc:
            self._mark_invalid(availability, "deterministic_findings", exc)
            return []

    def _convert_tickets(
        self, value: Any, availability: dict[str, ContextSourceResult]
    ) -> list[OpenTicketContext]:
        if availability["open_tickets"].status is not ContextStatus.AVAILABLE:
            return []
        try:
            tickets = [OpenTicketContext.model_validate(item) for item in value]
            return [ticket.model_copy(update={"description": ticket.description[:_DESCRIPTION_LIMIT]}) for ticket in tickets][:_TICKET_LIMIT]
        except (TypeError, ValidationError) as exc:
            self._mark_invalid(availability, "open_tickets", exc)
            return []

    def _convert_telemetry(
        self, value: Any, availability: dict[str, ContextSourceResult]
    ) -> AlertTelemetry:
        if availability["telemetry"].status is not ContextStatus.AVAILABLE:
            return AlertTelemetry()
        try:
            telemetry = AlertTelemetry.model_validate(_alias_raw_alarm_fields(value))
        except ValidationError as exc:
            self._mark_invalid(availability, "telemetry", exc)
            return AlertTelemetry()
        if telemetry.generation_management == "unmanaged":
            availability["telemetry"] = ContextSourceResult(
                status=ContextStatus.UNMANAGED, item_count=1
            )
        elif telemetry.generation_management != "managed":
            availability["telemetry"] = ContextSourceResult(
                status=ContextStatus.FAILED, item_count=1, detail="generation_management_unknown"
            )
        return telemetry

    def _convert_messages(
        self,
        value: Any,
        model: type[PriorAlertMessage] | type[OMChatMessage],
        limit: int,
        availability: dict[str, ContextSourceResult],
        name: str,
    ) -> list[PriorAlertMessage] | list[OMChatMessage]:
        if availability[name].status is not ContextStatus.AVAILABLE:
            return []
        try:
            messages = [model.model_validate(item) for item in value]
            return [message.model_copy(update={"content": message.content[:_MESSAGE_LIMIT]}) for message in messages][:limit]
        except (TypeError, ValidationError) as exc:
            self._mark_invalid(availability, name, exc)
            return []

    def _convert_grid_operational_facts(
        self, value: Any, availability: dict[str, ContextSourceResult]
    ) -> dict[str, Any]:
        """``get_grid_operational_facts`` returns ``{}`` by design (grid not
        found, or any lookup failure -- see its own docstring), which
        ``_capture`` already reports as EMPTY, not FAILED -- an intentional
        "no extra context", not degradation. Kept as a raw dict rather than a
        typed sub-model for the same reason as the alarm fields above."""
        if availability["grid_operational_facts"].status not in (
            ContextStatus.AVAILABLE,
            ContextStatus.EMPTY,
        ):
            return {}
        if isinstance(value, Mapping):
            return dict(value)
        if value is not None:
            self._mark_invalid(
                availability, "grid_operational_facts", TypeError(f"expected a mapping, got {type(value).__name__}")
            )
        return {}
