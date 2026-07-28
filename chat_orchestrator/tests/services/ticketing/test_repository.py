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
        return _Response(self.client.select_rows)


class _Client:
    def __init__(self):
        self.calls = []
        self.rows = []
        self.select_rows = []

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
