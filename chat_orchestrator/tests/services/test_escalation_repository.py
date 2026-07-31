"""Contract tests for the canonical escalation lifecycle repository."""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.services.escalation_repository import EscalationRepository


class _Response:
    def __init__(self, data: list[dict[str, Any]]):
        self.data = data


class _Query:
    def __init__(self, client: "_Client", table_name: str):
        self.client = client
        self.table_name = table_name
        self.mode = "select"
        self.payload: dict[str, Any] | None = None
        self.filters: list[tuple[str, str, Any]] = []

    def insert(self, payload: dict[str, Any]) -> "_Query":
        self.mode = "insert"
        self.payload = payload
        return self

    def select(self, *_args: Any) -> "_Query":
        return self

    def update(self, payload: dict[str, Any]) -> "_Query":
        self.mode = "update"
        self.payload = payload
        return self

    def eq(self, column: str, value: Any) -> "_Query":
        self.filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values: list[Any]) -> "_Query":
        self.filters.append(("in", column, values))
        return self

    def neq(self, column: str, value: Any) -> "_Query":
        self.filters.append(("neq", column, value))
        return self

    def limit(self, _limit: int) -> "_Query":
        return self

    def execute(self) -> _Response:
        self.client.calls.append((self.table_name, self.mode, self.payload, self.filters))
        return _Response(self.client.responses.pop(0))


class _Client:
    def __init__(self, responses: list[list[dict[str, Any]]]):
        self.calls: list[tuple[str, str, dict[str, Any] | None, list[tuple[str, str, Any]]]] = []
        self.responses = responses

    def table(self, table_name: str) -> _Query:
        return _Query(self, table_name)


@pytest.mark.asyncio
async def test_claim_conditionally_transitions_only_open_escalations_to_processing():
    client = _Client([[{"id": "esc-1", "state": "processing"}]])

    result = await EscalationRepository(client=client).claim("esc-1")

    assert result == {"id": "esc-1", "state": "processing"}
    assert client.calls == [
        (
            "escalations",
            "update",
            {"state": "processing"},
            [("eq", "id", "esc-1"), ("eq", "state", "open")],
        )
    ]


@pytest.mark.asyncio
async def test_claim_returns_none_when_another_worker_already_claimed_it():
    client = _Client([[]])

    assert await EscalationRepository(client=client).claim("esc-1") is None


@pytest.mark.asyncio
async def test_lifecycle_transitions_remain_explicit_and_session_scoped():
    client = _Client([[{"id": "esc-1", "state": "tracked"}], [], [], [{"id": "esc-1"}]])
    repository = EscalationRepository(client=client)

    await repository.attach_ticket("esc-1", "ticket-1")
    await repository.release("esc-1")
    await repository.resolve("esc-1")
    assert await repository.has_blocking_escalation("session-1") is True

    assert client.calls[0] == (
        "escalations", "update", {"ticket_id": "ticket-1", "state": "tracked"},
        [("eq", "id", "esc-1"), ("eq", "state", "processing")],
    )
    assert client.calls[1] == (
        "escalations", "update", {"state": "open"},
        [("eq", "id", "esc-1"), ("eq", "state", "processing")],
    )
    assert client.calls[2][0:2] == ("escalations", "update")
    assert client.calls[2][2]["state"] == "resolved"
    assert client.calls[2][2]["resolved_at"]
    assert client.calls[3] == (
        "escalations", "select", None,
        [("eq", "chat_session_id", "session-1"), ("in", "state", ["open", "processing"])],
    )


@pytest.mark.asyncio
async def test_reopen_sets_state_open_and_clears_resolved_at():
    client = _Client([[{"id": "esc-1", "state": "open", "resolved_at": None}]])

    await EscalationRepository(client=client).reopen("esc-1")

    assert client.calls == [
        (
            "escalations",
            "update",
            {"state": "open", "resolved_at": None},
            [("eq", "id", "esc-1")],
        )
    ]


@pytest.mark.asyncio
async def test_has_blocking_escalation_excludes_given_reasons():
    """Regression: a canonical-reads consumer must be able to reproduce the
    legacy "non-blocking reasons don't hold the session" distinction (see
    supabase_client.save_escalation_mapping's NON_BLOCKING_REASONS)."""
    client = _Client([[{"id": "esc-1"}]])

    result = await EscalationRepository(client=client).has_blocking_escalation(
        "session-1", exclude_reasons=("safety_escalation", "system_error")
    )

    assert result is True
    assert client.calls == [
        (
            "escalations", "select", None,
            [
                ("eq", "chat_session_id", "session-1"),
                ("in", "state", ["open", "processing"]),
                ("neq", "reason", "safety_escalation"),
                ("neq", "reason", "system_error"),
            ],
        )
    ]
