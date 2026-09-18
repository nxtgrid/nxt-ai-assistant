"""Notify-pipeline watchdog: catches the /notify webhook (n8n) going silent
fleet-wide, the way nothing did for 5 days across every grid in the incident
this module exists because of.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

import pytest

from orchestrator.services.ticketing.notify_watchdog import (
    NOTIFY_WATCHDOG_LABEL,
    _parse_sent_at,
    run_notify_watchdog,
    silence_detected,
    watchdog_enabled,
    watchdog_threshold_hours,
)

NOW = datetime(2026, 9, 18, 8, 0, 0, tzinfo=timezone.utc)


class TestSilenceDetected:
    def test_none_is_never_silence(self):
        """An empty-forever table (fresh deploy, or /notify never wired up)
        must not be mistaken for a pipeline that went quiet."""
        assert silence_detected(None, NOW, 24) is False

    def test_recent_delivery_is_not_silence(self):
        assert silence_detected(NOW - timedelta(hours=1), NOW, 24) is False

    def test_boundary_just_under_threshold_is_not_silence(self):
        assert silence_detected(NOW - timedelta(hours=23, minutes=59), NOW, 24) is False

    def test_past_threshold_is_silence(self):
        assert silence_detected(NOW - timedelta(hours=25), NOW, 24) is True

    def test_naive_timestamp_is_treated_as_utc(self):
        naive = (NOW - timedelta(hours=25)).replace(tzinfo=None)
        assert silence_detected(naive, NOW, 24) is True


class TestParseSentAt:
    def test_none_returns_none(self):
        assert _parse_sent_at(None) is None

    def test_invalid_string_returns_none(self):
        assert _parse_sent_at("not-a-timestamp") is None

    def test_z_suffix_is_handled(self):
        parsed = _parse_sent_at("2026-09-13T06:48:57Z")
        assert parsed == datetime(2026, 9, 13, 6, 48, 57, tzinfo=timezone.utc)

    def test_naive_iso_string_defaults_to_utc(self):
        parsed = _parse_sent_at("2026-09-13T06:48:57")
        assert parsed == datetime(2026, 9, 13, 6, 48, 57, tzinfo=timezone.utc)


class TestWatchdogEnabled:
    def test_default_is_on(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
        assert watchdog_enabled() is True

    def test_explicit_false_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_WATCHDOG_ENABLED", "false")
        assert watchdog_enabled() is False

    def test_default_threshold_is_24_hours(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_WATCHDOG_HOURS", raising=False)
        assert watchdog_threshold_hours() == 24.0

    def test_threshold_is_configurable(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_WATCHDOG_HOURS", "12")
        assert watchdog_threshold_hours() == 12.0


class _FakeDeliveryRepo:
    def __init__(self, most_recent: Optional[str], raises: bool = False):
        self._most_recent = most_recent
        self._raises = raises

    async def most_recent_delivery_at(self) -> Optional[str]:
        if self._raises:
            raise RuntimeError("ledger down")
        return self._most_recent


class _FakeJiraBackend:
    def __init__(self, open_ticket: Optional[str] = None, raises: bool = False):
        self.open_ticket = open_ticket
        self.raises = raises
        self.searched_labels: List[str] = []

    async def find_open_ticket_by_label(self, label: str) -> Optional[str]:
        if self.raises:
            raise RuntimeError("jira search down")
        self.searched_labels.append(label)
        return self.open_ticket


class _FakeTicketResult:
    def __init__(self, ref: str):
        self.ref = ref


class _FakeOutcome:
    def __init__(self, ref: Optional[str], error: Optional[str] = None):
        self.result = _FakeTicketResult(ref) if ref else None
        self.error = error


class _FakeTicketService:
    def __init__(
        self,
        backend: Optional[_FakeJiraBackend],
        create_ref: Optional[str] = "OPS-9001",
        create_raises: bool = False,
    ):
        self._backend = backend
        self._create_ref = create_ref
        self._create_raises = create_raises
        self.created_requests: List[Any] = []

    async def resolve_backend(self) -> Any:
        return self._backend

    async def create_ticket_with_internal_fallback(self, req: Any) -> _FakeOutcome:
        self.created_requests.append(req)
        if self._create_raises:
            raise RuntimeError("jira create down")
        return _FakeOutcome(self._create_ref)


class _FakeTelegram:
    def __init__(self, raises: bool = False):
        self.raises = raises
        self.sent: List[Tuple[str, str]] = []

    async def __call__(self, chat_id: str, text: str) -> None:
        if self.raises:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_disabled_short_circuits_before_touching_anything(monkeypatch):
    monkeypatch.setenv("NOTIFY_WATCHDOG_ENABLED", "false")
    repo = _FakeDeliveryRepo(most_recent="2026-08-01T00:00:00+00:00")
    tickets = _FakeTicketService(backend=None)
    telegram = _FakeTelegram()

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="-100999",
        now=NOW,
    )

    assert result == {"enabled": False}
    assert tickets.created_requests == []
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_recent_delivery_files_nothing(monkeypatch):
    monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
    repo = _FakeDeliveryRepo(most_recent=(NOW - timedelta(hours=2)).isoformat())
    backend = _FakeJiraBackend()
    tickets = _FakeTicketService(backend=backend)
    telegram = _FakeTelegram()

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="-100999",
        now=NOW,
        threshold_hours=24,
    )

    assert result["silent"] is False
    assert tickets.created_requests == []
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_never_had_a_delivery_files_nothing(monkeypatch):
    """A deployment that has never wired up /notify at all must not fire,
    even with the watchdog left on (the documented off switch is
    NOTIFY_WATCHDOG_ENABLED=false, but this is a second line of defense)."""
    monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
    repo = _FakeDeliveryRepo(most_recent=None)
    tickets = _FakeTicketService(backend=_FakeJiraBackend())
    telegram = _FakeTelegram()

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="-100999",
        now=NOW,
    )

    assert result["silent"] is False
    assert tickets.created_requests == []
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_silence_files_a_ticket_and_sends_telegram(monkeypatch):
    monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
    last = NOW - timedelta(hours=120)  # the real incident's shape: days, not hours
    repo = _FakeDeliveryRepo(most_recent=last.isoformat())
    backend = _FakeJiraBackend(open_ticket=None)
    tickets = _FakeTicketService(backend=backend, create_ref="OPS-9001")
    telegram = _FakeTelegram()

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="-100999",
        now=NOW,
        threshold_hours=24,
    )

    assert result["silent"] is True
    assert result["filed_ticket"] == "OPS-9001"
    assert backend.searched_labels == [NOTIFY_WATCHDOG_LABEL]
    assert len(tickets.created_requests) == 1
    req = tickets.created_requests[0]
    assert req.labels == [NOTIFY_WATCHDOG_LABEL]
    assert req.grid_name is None
    assert len(telegram.sent) == 1
    chat_id, text = telegram.sent[0]
    assert chat_id == "-100999"
    assert "OPS-9001" in text
    assert "24h" in text


@pytest.mark.asyncio
async def test_already_tracked_open_ticket_files_nothing_new(monkeypatch):
    """No daily re-spam while an already-filed ticket is still open -- the
    same 'a re-fire is not news' philosophy the correlation prompt uses."""
    monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
    last = NOW - timedelta(hours=48)
    repo = _FakeDeliveryRepo(most_recent=last.isoformat())
    backend = _FakeJiraBackend(open_ticket="OPS-8800")
    tickets = _FakeTicketService(backend=backend)
    telegram = _FakeTelegram()

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="-100999",
        now=NOW,
        threshold_hours=24,
    )

    assert result["silent"] is True
    assert result["already_tracked"] == "OPS-8800"
    assert tickets.created_requests == []
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_dedup_check_failure_still_files_rather_than_stay_silent(monkeypatch):
    monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
    last = NOW - timedelta(hours=48)
    repo = _FakeDeliveryRepo(most_recent=last.isoformat())
    backend = _FakeJiraBackend(raises=True)
    tickets = _FakeTicketService(backend=backend, create_ref="OPS-9002")
    telegram = _FakeTelegram()

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="-100999",
        now=NOW,
        threshold_hours=24,
    )

    assert result["filed_ticket"] == "OPS-9002"
    assert len(tickets.created_requests) == 1


@pytest.mark.asyncio
async def test_ticket_creation_failure_still_sends_telegram(monkeypatch):
    """A ticketing outage must not swallow the alert entirely -- matches
    _create_notify_ticket's own 'a ticketing outage is not allowed to lose
    an alert' rule for ordinary grid alerts."""
    monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
    last = NOW - timedelta(hours=48)
    repo = _FakeDeliveryRepo(most_recent=last.isoformat())
    backend = _FakeJiraBackend(open_ticket=None)
    tickets = _FakeTicketService(backend=backend, create_raises=True)
    telegram = _FakeTelegram()

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="-100999",
        now=NOW,
        threshold_hours=24,
    )

    assert result["filed_ticket"] is None
    assert len(telegram.sent) == 1
    assert "Ticket:" not in telegram.sent[0][1]


@pytest.mark.asyncio
async def test_telegram_failure_does_not_raise(monkeypatch):
    monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
    last = NOW - timedelta(hours=48)
    repo = _FakeDeliveryRepo(most_recent=last.isoformat())
    tickets = _FakeTicketService(backend=_FakeJiraBackend(open_ticket=None))
    telegram = _FakeTelegram(raises=True)

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="-100999",
        now=NOW,
        threshold_hours=24,
    )

    assert result["silent"] is True


@pytest.mark.asyncio
async def test_empty_escalation_chat_id_skips_telegram_without_erroring(monkeypatch):
    monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
    last = NOW - timedelta(hours=48)
    repo = _FakeDeliveryRepo(most_recent=last.isoformat())
    tickets = _FakeTicketService(backend=_FakeJiraBackend(open_ticket=None))
    telegram = _FakeTelegram()

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="",
        now=NOW,
        threshold_hours=24,
    )

    assert result["filed_ticket"] is not None
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_delivery_lookup_failure_returns_an_error_result_without_raising(monkeypatch):
    monkeypatch.delenv("NOTIFY_WATCHDOG_ENABLED", raising=False)
    repo = _FakeDeliveryRepo(most_recent=None, raises=True)
    tickets = _FakeTicketService(backend=_FakeJiraBackend())
    telegram = _FakeTelegram()

    result = await run_notify_watchdog(
        delivery_repo=repo,
        ticket_service=tickets,
        send_telegram=telegram,
        escalation_chat_id="-100999",
        now=NOW,
    )

    assert result == {"enabled": True, "error": "delivery_lookup_failed"}
    assert tickets.created_requests == []
    assert telegram.sent == []
