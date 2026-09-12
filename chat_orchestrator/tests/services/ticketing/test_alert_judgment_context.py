from __future__ import annotations

import asyncio

import pytest

from orchestrator.services.ticketing.alert_judgment import DeterministicFinding
from orchestrator.services.ticketing.alert_judgment_context import (
    AlertJudgmentContextAssembler,
    ContextStatus,
)
from orchestrator.services.ticketing.notify_alert_delivery_repository import (
    OMChatMessage,
    PriorAlertMessage,
)


def _finding() -> DeterministicFinding:
    return DeterministicFinding(kind="signature_match", explanation="Same signature")


def _ticket(index: int) -> dict[str, object]:
    return {
        "ref": f"OPS-{index}",
        "ticket_id": f"ticket-{index}",
        "summary": "Open issue",
        "description": "d" * 2_100,
    }


def _prior(index: int) -> PriorAlertMessage:
    return PriorAlertMessage(
        external_chat_id="-1001",
        external_message_id=index,
        sent_at="2026-08-21T10:00:00+00:00",
        content="prior",
    )


def _om(index: int) -> OMChatMessage:
    return OMChatMessage(
        created_at="2026-08-21T10:00:00+00:00",
        content="m" * 600,
        role="user",
    )


def _telemetry(*, management: str = "managed") -> dict[str, object]:
    return {
        "generation_management": management,
        "grid_status": "hps_on" if management == "managed" else "unknown",
        "site_status": "on" if management == "managed" else "unknown",
        "output_kw": 12.5,
        "battery_voltage_v": 51.2,
        "l1_voltage_v": 230.0,
        "l2_voltage_v": 230.0,
        "l3_voltage_v": 230.0,
        "observed_at": "2026-08-21T10:00:00+00:00",
        "fresh": True,
    }


def _assembler(**overrides: object) -> AlertJudgmentContextAssembler:
    providers: dict[str, object] = {
        "deterministic_findings_provider": lambda: _resolved([_finding()]),
        "open_tickets_provider": lambda: _resolved([_ticket(index) for index in range(16)]),
        "telemetry_provider": lambda: _resolved(_telemetry()),
        "prior_alerts_provider": lambda: _resolved([_prior(index) for index in range(21)]),
        "om_messages_provider": lambda: _resolved([_om(index) for index in range(51)]),
        "grid_operational_facts_provider": lambda: _resolved({}),
        "delivery_failures_provider": lambda: 0,
    }
    providers.update(overrides)
    return AlertJudgmentContextAssembler(**providers)  # type: ignore[arg-type]


async def _resolved(value: object) -> object:
    return value


@pytest.mark.asyncio
async def test_assembler_bounds_and_labels_every_source() -> None:
    context = await _assembler().assemble(
        grid_name="Acme Grid", chat_id="-1001", topic_id="42", alert={"subject": "Failure"}
    )

    assert len(context.open_tickets) == 15
    assert len(context.prior_alerts) == 20
    assert len(context.om_messages) == 50
    assert len(context.open_tickets[0].description) == 2_000
    assert len(context.om_messages[0].content) == 500
    assert set(context.availability) == {
        "deterministic_findings",
        "open_tickets",
        "telemetry",
        "prior_alerts",
        "om_messages",
        "grid_operational_facts",
    }
    assert all(
        source.status in (ContextStatus.AVAILABLE, ContextStatus.EMPTY)
        for source in context.availability.values()
    )


@pytest.mark.asyncio
async def test_telemetry_carries_alarm_and_power_trend_data_through() -> None:
    """``get_live_telemetry`` (mcp_servers/customer_server/client_grid_status.py)
    returns alarm/power-trend data under its own raw key names
    (``active_alarms``, ``recent_alarms_30min``, ``power_history_30min``).
    The judgment prompt (ticketing.correlation.prompt) instructs the model to
    read this evidence under different names -- ``active_ve_bus_errors``,
    ``recent_ve_bus_errors_30min``, ``power_trend_past_30min`` -- so it must
    survive the raw-to-typed conversion under those names, not be dropped by
    ``AlertTelemetry``'s ``extra="ignore"`` config.
    """
    active_alarms = [{"code": "Alarm", "description": "Alarm: Overload", "severity": "alarm"}]
    recent_alarms = [
        {
            "description": "Alarm: Overload",
            "timestamp": "2026-01-01T10:35:00+00:00",
            "severity": "alarm",
        }
    ]
    power_history = [{"timestamp": "2026-01-01T10:20:00+00:00", "output_kw": 8.5}]

    context = await _assembler(
        telemetry_provider=lambda: _resolved(
            {
                **_telemetry(),
                "active_alarms": active_alarms,
                "recent_alarms_30min": recent_alarms,
                "power_history_30min": power_history,
            }
        )
    ).assemble(grid_name="Test Grid", chat_id="-1001", topic_id="42", alert={})

    assert context.telemetry.active_ve_bus_errors == active_alarms
    assert context.telemetry.recent_ve_bus_errors_30min == recent_alarms
    assert context.telemetry.power_trend_past_30min == power_history


@pytest.mark.asyncio
async def test_telemetry_alarm_fields_default_empty_when_absent() -> None:
    context = await _assembler().assemble(
        grid_name="Test Grid", chat_id="-1001", topic_id="42", alert={}
    )

    assert context.telemetry.active_ve_bus_errors == []
    assert context.telemetry.recent_ve_bus_errors_30min == []
    assert context.telemetry.power_trend_past_30min == []


@pytest.mark.asyncio
async def test_grid_operational_facts_are_assembled_as_their_own_source() -> None:
    """``is_hps_on``/``is_hps_on_updated_at`` (auth_service.get_grid_operational_facts,
    wired via correlation_rules.get_grid_operational_context) are what the
    prompt's Root Cause Rules section reads to decide whether a new alert is a
    child of an existing grid-off/isolated state -- a source the assembler
    never gathered at all before this."""
    facts = {
        "grid_name": "Test Grid",
        "organization_id": 2,
        "is_hps_on": False,
        "is_hps_on_updated_at": "2026-01-01T05:00:00+00:00",
        "dcu_total_count": 3,
        "dcu_offline_count": 1,
    }

    context = await _assembler(
        grid_operational_facts_provider=lambda: _resolved(facts)
    ).assemble(grid_name="Test Grid", chat_id="-1001", topic_id="42", alert={})

    assert context.grid_operational_facts == facts
    assert context.availability["grid_operational_facts"].status is ContextStatus.AVAILABLE


@pytest.mark.asyncio
async def test_grid_operational_facts_empty_dict_is_a_successful_empty_state() -> None:
    """``get_grid_operational_facts`` returns ``{}`` -- deliberately, per its own
    docstring -- when the grid isn't found or the lookup fails; that is
    'no extra context', not a failure, and must not count as degradation."""
    context = await _assembler().assemble(
        grid_name="Test Grid", chat_id="-1001", topic_id="42", alert={}
    )

    assert context.grid_operational_facts == {}
    assert context.availability["grid_operational_facts"].status is ContextStatus.EMPTY
    assert context.has_degradation() is False


@pytest.mark.asyncio
async def test_one_provider_failure_does_not_cancel_the_others() -> None:
    async def fail() -> object:
        raise RuntimeError("database unavailable")

    context = await _assembler(om_messages_provider=fail).assemble(
        grid_name="Acme Grid", chat_id="-1001", topic_id="42", alert={}
    )

    assert context.availability["om_messages"].status is ContextStatus.FAILED
    assert context.telemetry.generation_management == "managed"
    assert context.telemetry.grid_status == "hps_on"
    assert context.telemetry.site_status == "on"
    assert context.open_tickets


@pytest.mark.asyncio
async def test_unmanaged_and_empty_are_successful_states() -> None:
    context = await _assembler(
        telemetry_provider=lambda: _resolved(_telemetry(management="unmanaged")),
        prior_alerts_provider=lambda: _resolved([]),
    ).assemble(grid_name="Acme Grid", chat_id="-1001", topic_id="42", alert={})

    assert context.availability["telemetry"].status is ContextStatus.UNMANAGED
    assert context.availability["prior_alerts"].status is ContextStatus.EMPTY
    assert context.has_degradation() is False


@pytest.mark.asyncio
async def test_provider_timeout_is_marked_without_cancelling_other_sources() -> None:
    async def slow() -> object:
        await asyncio.sleep(1)
        return []

    context = await _assembler(om_messages_provider=slow, timeout_seconds=0.001).assemble(
        grid_name="Acme Grid", chat_id="-1001", topic_id="42", alert={}
    )

    assert context.availability["om_messages"].status is ContextStatus.TIMED_OUT
    assert context.telemetry.site_status == "on"
