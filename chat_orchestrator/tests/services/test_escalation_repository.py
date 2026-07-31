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

    def is_(self, column: str, value: Any) -> "_Query":
        self.filters.append(("is", column, value))
        return self

    def gt(self, column: str, value: Any) -> "_Query":
        self.filters.append(("gt", column, value))
        return self

    def lt(self, column: str, value: Any) -> "_Query":
        self.filters.append(("lt", column, value))
        return self

    def gte(self, column: str, value: Any) -> "_Query":
        self.filters.append(("gte", column, value))
        return self

    def order(self, column: str) -> "_Query":
        self.filters.append(("order", column, None))
        return self

    @property
    def not_(self) -> "_Not":
        return _Not(self)

    def limit(self, _limit: int) -> "_Query":
        return self

    def execute(self) -> _Response:
        self.client.calls.append((self.table_name, self.mode, self.payload, self.filters))
        return _Response(self.client.responses.pop(0))


class _Not:
    """Mirrors the supabase-py `query.not_.is_(...)` chaining shim."""

    def __init__(self, query: _Query) -> None:
        self._query = query

    def is_(self, column: str, value: Any) -> _Query:
        self._query.filters.append(("not_is", column, value))
        return self._query


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
async def test_list_unfiled_builds_state_ticket_and_age_bound_filters():
    client = _Client([[{"id": "esc-1", "created_at": "2026-01-01T00:00:00Z"}]])

    rows = await EscalationRepository(client=client).list_unfiled(
        state="open",
        created_after="2025-12-31T00:00:00Z",
        created_before="2026-01-02T00:00:00Z",
        exclude_reasons=("safety_escalation",),
        limit=20,
    )

    assert rows == [{"id": "esc-1", "created_at": "2026-01-01T00:00:00Z"}]
    assert client.calls == [
        (
            "escalations",
            "select",
            None,
            [
                ("eq", "state", "open"),
                ("is", "ticket_id", "null"),
                ("neq", "reason", "safety_escalation"),
                ("gt", "created_at", "2025-12-31T00:00:00Z"),
                ("lt", "created_at", "2026-01-02T00:00:00Z"),
                ("order", "created_at", None),
            ],
        )
    ]


@pytest.mark.asyncio
async def test_list_unfiled_omits_age_bounds_when_not_given():
    client = _Client([[]])

    await EscalationRepository(client=client).list_unfiled(state="open")

    assert client.calls == [
        (
            "escalations",
            "select",
            None,
            [("eq", "state", "open"), ("is", "ticket_id", "null"), ("order", "created_at", None)],
        )
    ]


@pytest.mark.asyncio
async def test_list_claimed_orphans_filters_processing_untracked_unresolved():
    client = _Client([[{"id": "esc-1", "created_at": "t"}]])

    rows = await EscalationRepository(client=client).list_claimed_orphans(limit=50)

    assert rows == [{"id": "esc-1", "created_at": "t"}]
    assert client.calls == [
        (
            "escalations",
            "select",
            None,
            [
                ("eq", "state", "processing"),
                ("is", "ticket_id", "null"),
                ("is", "resolved_at", "null"),
            ],
        )
    ]


@pytest.mark.asyncio
async def test_list_claimed_orphans_applies_created_after_when_given():
    client = _Client([[]])

    await EscalationRepository(client=client).list_claimed_orphans(created_after="2026-01-01T00:00:00Z")

    assert client.calls == [
        (
            "escalations",
            "select",
            None,
            [
                ("eq", "state", "processing"),
                ("is", "ticket_id", "null"),
                ("is", "resolved_at", "null"),
                ("gte", "created_at", "2026-01-01T00:00:00Z"),
            ],
        )
    ]


@pytest.mark.asyncio
async def test_list_active_tracked_filters_open_with_ticket_attached():
    client = _Client([[{"id": "esc-1", "created_at": "t"}]])

    rows = await EscalationRepository(client=client).list_active_tracked(limit=100)

    assert rows == [{"id": "esc-1", "created_at": "t"}]
    assert client.calls == [
        (
            "escalations",
            "select",
            None,
            [
                ("eq", "state", "open"),
                ("not_is", "ticket_id", "null"),
                ("order", "created_at", None),
            ],
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
