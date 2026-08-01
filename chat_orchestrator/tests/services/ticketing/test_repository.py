"""Contract tests for the canonical ticket repository."""

from __future__ import annotations

import pytest

from orchestrator.services.ticketing.backend import BackendTicketResult, TicketCreateRequest
from orchestrator.services.ticketing.repository import TicketRepository


class _Response:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, client, table):
        self.client = client
        self.table_name = table
        self.payload = None
        self.filters = []
        self.mode = "select"

    def insert(self, payload):
        self.mode = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.mode = "update"
        self.payload = payload
        return self

    def select(self, *_args):
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def neq(self, column, value):
        self.filters.append((column, f"neq:{value}"))
        return self

    def order(self, _column, **_kwargs):
        return self

    def limit(self, _limit):
        return self

    def execute(self):
        self.client.calls.append((self.table_name, self.mode, self.payload, self.filters))
        if self.mode == "insert":
            row = {"id": "ticket-1", **self.payload}
            self.client.rows.append(row)
            return _Response([row])
        if self.mode == "update":
            row = {
                "id": "ticket-1",
                "summary": "Grid down",
                "created_via": "notification",
                **self.payload,
            }
            return _Response([row])
        return _Response(self.client.select_rows_by_table.get(self.table_name, self.client.select_rows))


class _Client:
    def __init__(self):
        self.calls = []
        self.rows = []
        self.select_rows = []
        self.select_rows_by_table = {}

    def table(self, name):
        return _Query(self, name)


@pytest.mark.asyncio
async def test_create_intent_persists_a_pending_backend_neutral_ticket():
    client = _Client()
    repository = TicketRepository(client=client)

    result = await repository.create_intent(
        TicketCreateRequest(
            summary="Grid down",
            description="details",
            organization_id=7,
            grid_name="Grid A",
            labels=["alert"],
        ),
        created_via="notification",
    )

    assert result.id == "ticket-1"
    assert result.provisioning_state == "pending"
    assert client.calls == [
        (
            "tickets",
            "insert",
            {
                "summary": "Grid down",
                "description": "details",
                "organization_id": 7,
                "grid_name": "Grid A",
                "assignee_email": None,
                "ticket_type": None,
                "labels": ["alert"],
                "created_via": "notification",
                "provisioning_state": "pending",
                "status": "open",
            },
            [],
        )
    ]


@pytest.mark.asyncio
async def test_activate_updates_the_existing_ticket_intent_with_backend_identity():
    client = _Client()
    repository = TicketRepository(client=client)

    result = await repository.activate(
        "ticket-1",
        BackendTicketResult(ref="OPS-123", backend="jira", ticket_type="Incident"),
    )

    assert result.id == "ticket-1"
    assert result.provisioning_state == "active"
    table, mode, payload, filters = client.calls[0]
    assert (table, mode, filters) == ("tickets", "update", [("id", "ticket-1")])
    assert payload["ticket_ref"] == "OPS-123"
    assert payload["backend"] == "jira"
    assert payload["ticket_type"] == "Incident"
    assert payload["provisioning_state"] == "active"
    assert payload["activated_at"]
    assert payload["backend_synced_at"]


@pytest.mark.asyncio
async def test_get_by_ref_returns_the_persisted_backend_instead_of_inferring_it():
    client = _Client()
    client.select_rows = [{
        "id": "ticket-1", "ticket_ref": "OPS-123", "backend": "jira",
        "summary": "Grid down", "created_via": "notification", "provisioning_state": "active",
    }]

    record = await TicketRepository(client=client).get_by_ref("OPS-123")

    assert record is not None
    assert record.backend == "jira"


@pytest.mark.asyncio
async def test_get_by_id_returns_the_ticket_for_its_own_uuid():
    client = _Client()
    client.select_rows = [{
        "id": "ticket-1", "ticket_ref": "OPS-123", "backend": "jira",
        "summary": "Grid down", "created_via": "notification", "provisioning_state": "active",
    }]

    record = await TicketRepository(client=client).get_by_id("ticket-1")

    assert record is not None
    assert record.ticket_ref == "OPS-123"
    table, mode, _payload, filters = client.calls[0]
    assert (table, mode, filters) == ("tickets", "select", [("id", "ticket-1")])


@pytest.mark.asyncio
async def test_get_by_id_returns_none_when_not_found():
    client = _Client()
    client.select_rows = []

    assert await TicketRepository(client=client).get_by_id("missing") is None


@pytest.mark.asyncio
async def test_get_status_by_ref_reads_the_canonical_ticket_projection():
    client = _Client()
    client.select_rows = [{
        "id": "ticket-1", "ticket_ref": "TKT-1", "backend": "internal",
        "summary": "Grid down", "ticket_type": "Task", "status": "done",
        "created_via": "notification", "provisioning_state": "active",
    }]

    status = await TicketRepository(client=client).get_status_by_ref("TKT-1")

    assert status is not None
    assert status.summary == "Grid down"
    assert status.is_done is True
    assert status.ticket_type == "Task"


@pytest.mark.asyncio
async def test_add_comment_by_ref_uses_the_canonical_ticket_id():
    client = _Client()
    client.select_rows = [{
        "id": "ticket-1", "ticket_ref": "TKT-1", "backend": "internal",
        "summary": "Grid down", "created_via": "notification", "provisioning_state": "active",
    }]

    await TicketRepository(client=client).add_comment_by_ref("TKT-1", "Investigating")

    assert client.calls[-1] == (
        "ticket_comments", "insert",
        {"ticket_id": "ticket-1", "body": "Investigating", "author": None,
         "is_public": False, "source": "staff"}, [],
    )


@pytest.mark.asyncio
async def test_transition_to_done_by_ref_updates_canonical_ticket_state():
    client = _Client()
    client.select_rows = [{
        "id": "ticket-1", "ticket_ref": "TKT-1", "backend": "internal",
        "summary": "Grid down", "created_via": "notification", "provisioning_state": "active",
    }]

    await TicketRepository(client=client).transition_to_done_by_ref("TKT-1")

    table, mode, payload, filters = client.calls[-1]
    assert (table, mode, filters) == ("tickets", "update", [("id", "ticket-1")])
    assert payload["status"] == "done"
    assert payload["resolved_at"]


@pytest.mark.asyncio
async def test_update_by_ref_updates_canonical_summary_and_description():
    client = _Client()
    client.select_rows = [{
        "id": "ticket-1", "ticket_ref": "TKT-1", "backend": "internal",
        "summary": "Grid down", "created_via": "notification", "provisioning_state": "active",
    }]

    await TicketRepository(client=client).update_by_ref("TKT-1", summary="Grid restored")

    assert client.calls[-1] == (
        "tickets", "update", {"summary": "Grid restored"}, [("id", "ticket-1")]
    )


@pytest.mark.asyncio
async def test_find_ref_for_escalation_follows_the_canonical_ticket_relation():
    client = _Client()
    client.select_rows_by_table = {
        "escalations": [{"ticket_id": "ticket-1"}],
        "tickets": [{
            "id": "ticket-1", "ticket_ref": "TKT-1", "backend": "internal",
            "summary": "Grid down", "created_via": "escalation", "provisioning_state": "active",
        }],
    }

    ref = await TicketRepository(client=client).find_ref_for_escalation("escalation-1")

    assert ref == "TKT-1"
    assert client.calls[-2] == ("escalations", "select", None, [("id", "escalation-1")])
    assert client.calls[-1] == ("tickets", "select", None, [("id", "ticket-1")])


@pytest.mark.asyncio
async def test_list_open_by_backend_reads_active_non_done_tickets_for_that_backend():
    client = _Client()
    client.select_rows_by_table = {
        "tickets": [
            {
                "id": "ticket-1", "ticket_ref": "OPS-1", "backend": "jira",
                "status": "open", "summary": "Grid down",
                "created_via": "notification", "provisioning_state": "active",
            },
            {
                "id": "ticket-2", "ticket_ref": "OPS-2", "backend": "jira",
                "status": "in_progress", "summary": "Meter fault",
                "created_via": "notification", "provisioning_state": "active",
            },
        ],
    }

    refs = await TicketRepository(client=client).list_open_by_backend("jira", limit=50)

    assert refs == ["OPS-1", "OPS-2"]
    assert client.calls[-1] == (
        "tickets", "select", None,
        [
            ("backend", "jira"),
            ("provisioning_state", "active"),
            ("status", "neq:done"),
        ],
    )


@pytest.mark.asyncio
async def test_find_open_internal_by_grid_reads_active_canonical_tickets():
    client = _Client()
    client.select_rows_by_table = {
        "tickets": [{
            "id": "ticket-1", "ticket_ref": "TKT-1", "backend": "internal",
            "summary": "Grid down", "description": "details", "ticket_type": "Task",
            "status": "open", "grid_name": "Kudi", "created_at": "2026-01-01T00:00:00Z",
            "labels": ["alert"], "created_via": "notification", "provisioning_state": "active",
        }],
    }

    tickets = await TicketRepository(client=client).find_open_internal_by_grid("Kudi", limit=5)

    assert [ticket.ref for ticket in tickets] == ["TKT-1"]
    assert tickets[0].backend == "internal"
    assert tickets[0].is_done is False
    assert client.calls[-1] == (
        "tickets", "select", None,
        [
            ("backend", "internal"),
            ("provisioning_state", "active"),
            ("grid_name", "Kudi"),
            ("status", "neq:done"),
        ],
    )
