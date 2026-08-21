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
    }
    assert all(source.status is ContextStatus.AVAILABLE for source in context.availability.values())


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
