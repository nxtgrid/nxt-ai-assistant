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

    def is_(self, field: str, value: Any) -> "_Table":
        self.predicates.append(("is", field, value))
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
            elif operation == "is":
                matches = [
                    row
                    for row in matches
                    if (row.get(field) is None if value == "null" else row.get(field) == value)
                ]
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


@pytest.mark.asyncio
async def test_recent_om_messages_is_scoped_to_one_active_topic_and_excludes_alerts() -> None:
    repo, client = _repo()
    client.tables["chat_messages"].extend(
        [
            {
                "group_id": "-1001",
                "telegram_topic_id": "42",
                "created_at": "2026-08-21T10:00:00+00:00",
                "role": "user",
                "content": "Please check inverter 3",
                "sender_telegram_id": "100",
                "from_chat_id": "-1001",
                "metadata": {},
                "archived_at": None,
            },
            {
                "group_id": "-1001",
                "telegram_topic_id": "42",
                "created_at": "2026-08-21T10:01:00+00:00",
                "role": "assistant",
                "content": "notify copy",
                "metadata": {"channel": "notify_endpoint"},
                "archived_at": None,
            },
            {
                "group_id": "-1001",
                "telegram_topic_id": "43",
                "created_at": "2026-08-21T10:02:00+00:00",
                "role": "user",
                "content": "sibling topic",
                "metadata": {},
                "archived_at": None,
            },
            {
                "group_id": "-1002",
                "telegram_topic_id": "42",
                "created_at": "2026-08-21T10:03:00+00:00",
                "role": "user",
                "content": "other chat",
                "metadata": {},
                "archived_at": None,
            },
            {
                "group_id": "-1001",
                "telegram_topic_id": "42",
                "created_at": "2026-08-21T10:04:00+00:00",
                "role": "user",
                "content": "archived",
                "metadata": {},
                "archived_at": "2026-08-21T10:05:00+00:00",
            },
        ]
    )

    rows = await repo.recent_om_messages(
        chat_id="-1001", topic_id="42", since="2026-08-21T00:00:00+00:00"
    )

    assert [row.content for row in rows] == ["Please check inverter 3"]
    assert rows[0].created_at == "2026-08-21T10:00:00+00:00"


# --------------------------------------------------------------------------- #
# Downtime clock -- the "at most one downtime alert per day" ledger
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_record_success_marks_a_downtime_delivery() -> None:
    repo, client = _repo()

    await repo.record_success(
        grid_name="Acme Grid",
        external_chat_id="-1001",
        external_topic_id="42",
        external_message_id=9001,
        source="n8n",
        dedup_key=None,
        ticket_id=None,
        ticket_ref=None,
        rendered_text="Grid down",
        alert={"subject": "Grid down"},
        downtime=True,
    )

    assert client.tables["notify_alert_deliveries"][0]["downtime"] is True


@pytest.mark.asyncio
async def test_record_success_defaults_to_not_a_downtime_delivery() -> None:
    repo, client = _repo()

    await repo.record_success(
        grid_name="Acme Grid",
        external_chat_id="-1001",
        external_topic_id=None,
        external_message_id=9002,
        source="n8n",
        dedup_key=None,
        ticket_id=None,
        ticket_ref=None,
        rendered_text="MPPT underperforming",
        alert={"subject": "MPPT underperforming"},
    )

    assert client.tables["notify_alert_deliveries"][0]["downtime"] is False


@pytest.mark.asyncio
async def test_latest_downtime_sent_at_ignores_other_grids_and_non_downtime_rows() -> None:
    repo, client = _repo()
    client.tables["notify_alert_deliveries"] = [
        {"grid_name": "Acme Grid", "sent_at": "2026-08-26T06:00:00+00:00", "downtime": True},
        {"grid_name": "Acme Grid", "sent_at": "2026-08-28T09:00:00+00:00", "downtime": False},
        {"grid_name": "Other Grid", "sent_at": "2026-08-28T11:00:00+00:00", "downtime": True},
        {"grid_name": "Acme Grid", "sent_at": "2026-08-27T18:00:00+00:00", "downtime": True},
    ]

    assert await repo.latest_downtime_sent_at("Acme Grid") == "2026-08-27T18:00:00+00:00"


@pytest.mark.asyncio
async def test_latest_downtime_sent_at_is_none_when_the_grid_has_never_been_reported_down() -> None:
    repo, client = _repo()
    client.tables["notify_alert_deliveries"] = [
        {"grid_name": "Acme Grid", "sent_at": "2026-08-28T09:00:00+00:00", "downtime": False},
    ]

    assert await repo.latest_downtime_sent_at("Acme Grid") is None


@pytest.mark.asyncio
async def test_latest_downtime_sent_at_fails_open_on_a_ledger_outage() -> None:
    """No clock means no proof an alert already went out today -- the caller
    must treat that as "send", never as "already told them"."""
    repo, client = _repo()
    client.fail_tables["notify_alert_deliveries"] = RuntimeError("ledger down")

    assert await repo.latest_downtime_sent_at("Acme Grid") is None
    assert delivery_history_failures_last_hour() > 0
