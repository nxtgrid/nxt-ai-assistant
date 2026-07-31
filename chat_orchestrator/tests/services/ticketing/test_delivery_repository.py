"""Tests for durable outbound-message delivery receipts."""

from __future__ import annotations

import pytest

from orchestrator.services.ticketing.delivery_repository import DeliveryRepository


class _Response:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, client):
        self.client = client
        self.payload = None
        self._mode = None
        self._filters: list[tuple] = []

    def upsert(self, payload, **_kwargs):
        self.payload = payload
        self._mode = "upsert"
        return self

    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._mode == "upsert":
            self.client.payloads.append(self.payload)
            return _Response([{"id": "delivery-1", **self.payload}])
        matches = [
            row
            for row in self.client.rows
            if all(row.get(col) == val for col, val in self._filters)
        ]
        return _Response(matches)


class _Client:
    def __init__(self, rows=None):
        self.payloads = []
        self.rows = rows or []

    def table(self, name):
        assert name == "message_deliveries"
        return _Query(self)


@pytest.mark.asyncio
async def test_record_upserts_a_notification_receipt_by_external_identity():
    client = _Client()
    repository = DeliveryRepository(client=client)

    receipt = await repository.record(
        ticket_id="ticket-1",
        escalation_id=None,
        purpose="notification",
        external_chat_id="-100123",
        external_topic_id="7",
        external_message_id=42,
    )

    assert receipt["id"] == "delivery-1"
    assert client.payloads == [
        {
            "ticket_id": "ticket-1",
            "escalation_id": None,
            "purpose": "notification",
            "channel": "telegram",
            "external_chat_id": "-100123",
            "external_topic_id": "7",
            "external_message_id": 42,
            "chat_message_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_find_escalation_delivery_returns_matching_row():
    client = _Client(
        rows=[
            {
                "escalation_id": "esc-1",
                "external_message_id": 555,
                "purpose": "escalation",
                "external_topic_id": "9",
            },
            # A "notification" delivery for the same message_id in a
            # different chat must never match -- purpose disambiguates.
            {
                "escalation_id": "esc-2",
                "external_message_id": 555,
                "purpose": "notification",
                "external_topic_id": None,
            },
        ]
    )
    repository = DeliveryRepository(client=client)

    row = await repository.find_escalation_delivery(external_message_id=555)

    assert row == {
        "escalation_id": "esc-1",
        "external_message_id": 555,
        "purpose": "escalation",
        "external_topic_id": "9",
    }


@pytest.mark.asyncio
async def test_find_escalation_delivery_returns_none_when_no_match():
    client = _Client(rows=[])
    repository = DeliveryRepository(client=client)

    assert await repository.find_escalation_delivery(external_message_id=999) is None
