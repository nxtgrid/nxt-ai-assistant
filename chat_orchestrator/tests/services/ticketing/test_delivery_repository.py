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

    def upsert(self, payload, **_kwargs):
        self.payload = payload
        return self

    def execute(self):
        self.client.payloads.append(self.payload)
        return _Response([{"id": "delivery-1", **self.payload}])


class _Client:
    def __init__(self):
        self.payloads = []

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
