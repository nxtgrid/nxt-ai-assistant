from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

from orchestrator.services.ticketing import notify_alert_delivery_repository as delivery_module
from orchestrator.services.ticketing.notify_alert_delivery_repository import (
    NotifyAlertDeliveryRepository,
    delivery_history_failures_last_hour,
)


class _Result:
    def __init__(self, data: list[dict[str, Any]]):
        self.data = data


class _Table:
    def __init__(self, client: "_Client", name: str):
        self.client = client
        self.name = name
        self.mode = ""
        self.payload: dict[str, Any] | None = None
        self.predicates: list[tuple[str, str, Any]] = []
        self.order_field: str | None = None
        self.desc = False
        self.max_rows: int | None = None

    def select(self, *_args: Any) -> "_Table":
        self.mode = "select"
        return self

    def upsert(self, payload: dict[str, Any], **_kwargs: Any) -> "_Table":
        self.mode = "upsert"
        self.payload = payload
        return self

    def eq(self, field: str, value: Any) -> "_Table":
        self.predicates.append(("eq", field, value))
        return self

    def gte(self, field: str, value: Any) -> "_Table":
        self.predicates.append(("gte", field, value))
        return self

    def contains(self, field: str, value: Any) -> "_Table":
        self.predicates.append(("contains", field, value))
        return self

    def order(self, field: str, desc: bool = False) -> "_Table":
        self.order_field = field
        self.desc = desc
        return self

    def limit(self, value: int) -> "_Table":
        self.max_rows = value
        return self

    def execute(self) -> _Result:
        if self.name in self.client.fail_tables:
            raise self.client.fail_tables[self.name]
        rows = self.client.tables[self.name]
        if self.mode == "upsert":
            assert self.payload is not None
            for row in rows:
                if (
                    row.get("external_chat_id") == self.payload["external_chat_id"]
                    and row.get("external_message_id") == self.payload["external_message_id"]
                ):
                    row.update(self.payload)
                    return _Result([row])
            row = dict(self.payload)
            rows.append(row)
            return _Result([row])

        matches = list(rows)
        for operation, field, value in self.predicates:
            if operation == "eq":
                matches = [row for row in matches if row.get(field) == value]
            elif operation == "gte":
                matches = [row for row in matches if (row.get(field) or "") >= value]
            else:
                matches = [
                    row
                    for row in matches
                    if all((row.get(field) or {}).get(key) == item for key, item in value.items())
                ]
        if self.order_field:
            matches.sort(key=lambda row: row.get(self.order_field) or "", reverse=self.desc)
        if self.max_rows is not None:
            matches = matches[: self.max_rows]
        return _Result(matches)


class _Client:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "notify_alert_deliveries": [],
            "chat_messages": [],
        }
        self.fail_tables: dict[str, Exception] = {}

    def table(self, name: str) -> _Table:
        return _Table(self, name)


def _repo() -> tuple[NotifyAlertDeliveryRepository, _Client]:
    client = _Client()
    return NotifyAlertDeliveryRepository(get_client=lambda: client), client


@pytest.mark.asyncio
async def test_record_success_writes_one_grid_delivery_receipt() -> None:
    repo, client = _repo()

    row = await repo.record_success(
        grid_name="Acme Grid",
        external_chat_id="-1001",
        external_topic_id="42",
        external_message_id=9001,
        source="grafana",
        dedup_key="alert-1",
        ticket_id="00000000-0000-0000-0000-000000000001",
        ticket_ref="OPS-1234",
        rendered_text="Grid outage",
        alert={"subject": "Grid outage"},
    )

    assert row is not None
    assert row["external_message_id"] == 9001
    assert client.tables["notify_alert_deliveries"] == [row]


@pytest.mark.asyncio
async def test_recent_for_grid_merges_and_deduplicates_legacy_alerts() -> None:
    repo, client = _repo()
    client.tables["notify_alert_deliveries"].append(
        {
            "grid_name": "Acme Grid",
            "external_chat_id": "-1001",
            "external_topic_id": "42",
            "external_message_id": 100,
            "sent_at": "2026-08-21T10:00:00+00:00",
            "rendered_text": "ledger alert",
            "ticket_ref": "OPS-1",
        }
    )
    client.tables["chat_messages"].extend(
        [
            {
                "group_id": "-1001",
                "telegram_topic_id": "42",
                "telegram_message_id": 100,
                "created_at": "2026-08-21T10:00:00+00:00",
                "content": "duplicate legacy alert",
                "metadata": {"channel": "notify_endpoint", "grid_name": "Acme Grid"},
            },
            {
                "group_id": "-1001",
                "telegram_topic_id": "42",
                "telegram_message_id": 101,
                "created_at": "2026-08-21T11:00:00+00:00",
                "content": "legacy alert",
                "metadata": {"channel": "notify_endpoint", "grid_name": "Acme Grid"},
            },
        ]
    )

    rows = await repo.recent_for_grid("Acme Grid", "2026-08-21T00:00:00+00:00", limit=20)

    assert [row.external_message_id for row in rows] == [101, 100]
    assert rows[1].content == "ledger alert"


@pytest.mark.asyncio
async def test_write_failure_marks_history_degraded(monkeypatch) -> None:
    repo, client = _repo()
    client.fail_tables["notify_alert_deliveries"] = RuntimeError("database unavailable")
    monkeypatch.setattr(delivery_module, "_failure_counts", defaultdict(int))

    result = await repo.record_success(
        grid_name="Acme Grid",
        external_chat_id="-1001",
        external_topic_id=None,
        external_message_id=9001,
        source=None,
        dedup_key=None,
        ticket_id=None,
        ticket_ref=None,
        rendered_text="Grid outage",
        alert={"subject": "Grid outage"},
    )

    assert result is None
    assert delivery_history_failures_last_hour() == 1
