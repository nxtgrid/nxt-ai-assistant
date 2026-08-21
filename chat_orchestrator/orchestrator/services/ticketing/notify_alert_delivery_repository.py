"""Durable history for successfully delivered `/chat/notify` alerts."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)
_FAILURE_WINDOW_SECONDS = 3600.0
_failure_counts: dict[str, int] = defaultdict(int)
_failure_window_started_at = time.monotonic()


class _ContextRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PriorAlertMessage(_ContextRecord):
    external_chat_id: str
    external_topic_id: str | None = None
    external_message_id: int
    sent_at: str
    content: str = Field(default="", max_length=500)
    ticket_ref: str | None = None
    source: str | None = None


class OMChatMessage(_ContextRecord):
    created_at: str
    role: str = ""
    content: str = Field(default="", max_length=500)
    sender_telegram_id: str | None = None
    from_chat_id: str | None = None


def _record_failure(operation: str, error: Exception) -> None:
    global _failure_window_started_at
    now = time.monotonic()
    if now - _failure_window_started_at > _FAILURE_WINDOW_SECONDS:
        _failure_counts.clear()
        _failure_window_started_at = now
    _failure_counts[operation] += 1
    LOGGER.warning("notify alert delivery history: {} failed: {}", operation, error)


def delivery_history_failures_last_hour() -> int:
    if time.monotonic() - _failure_window_started_at > _FAILURE_WINDOW_SECONDS:
        return 0
    return sum(_failure_counts.values())


class NotifyAlertDeliveryRepository:
    """Best-effort reader/writer for successful grid-alert Telegram posts."""

    def __init__(
        self,
        client: Any | None = None,
        get_client: Callable[[], Any | None] | None = None,
    ) -> None:
        self._client_instance = client
        self._get_client = get_client

    def _raw_client(self) -> Any | None:
        if self._client_instance is not None:
            return self._client_instance
        if self._get_client is None:
            return None
        try:
            return self._get_client()
        except Exception as exc:
            _record_failure("get_client", exc)
            return None

    async def record_success(
        self,
        *,
        grid_name: str,
        external_chat_id: str,
        external_topic_id: str | None,
        external_message_id: int,
        source: str | None,
        dedup_key: str | None,
        ticket_id: str | None,
        ticket_ref: str | None,
        rendered_text: str,
        alert: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Record a post only after Telegram supplied a real message identifier."""
        client = self._raw_client()
        if client is None:
            _record_failure("record_success", RuntimeError("database client unavailable"))
            return None
        try:
            response = (
                client.table("notify_alert_deliveries")
                .upsert(
                    {
                        "grid_name": grid_name,
                        "external_chat_id": str(external_chat_id),
                        "external_topic_id": str(external_topic_id)
                        if external_topic_id is not None
                        else None,
                        "external_message_id": int(external_message_id),
                        "source": source,
                        "dedup_key": dedup_key,
                        "ticket_id": ticket_id,
                        "ticket_ref": ticket_ref,
                        "rendered_text": rendered_text,
                        "alert": alert,
                    },
                    on_conflict="external_chat_id,external_message_id",
                )
                .execute()
            )
            rows = getattr(response, "data", None) or []
            if not rows:
                raise RuntimeError("delivery ledger write returned no row")
            return rows[0]
        except Exception as exc:
            _record_failure("record_success", exc)
            return None

    async def recent_for_grid(
        self, grid_name: str, since: str, limit: int = 20
    ) -> list[PriorAlertMessage]:
        client = self._raw_client()
        if client is None:
            _record_failure("recent_for_grid", RuntimeError("database client unavailable"))
            return []

        records: list[PriorAlertMessage] = []
        try:
            response = (
                client.table("notify_alert_deliveries")
                .select(
                    "external_chat_id,external_topic_id,external_message_id,sent_at,"
                    "rendered_text,ticket_ref,source"
                )
                .eq("grid_name", grid_name)
                .gte("sent_at", since)
                .order("sent_at", desc=True)
                .limit(limit)
                .execute()
            )
            records.extend(
                PriorAlertMessage(
                    external_chat_id=str(row["external_chat_id"]),
                    external_topic_id=row.get("external_topic_id"),
                    external_message_id=int(row["external_message_id"]),
                    sent_at=str(row["sent_at"]),
                    content=str(row.get("rendered_text") or "")[:500],
                    ticket_ref=row.get("ticket_ref"),
                    source=row.get("source"),
                )
                for row in (getattr(response, "data", None) or [])
            )
        except Exception as exc:
            _record_failure("recent_for_grid.ledger", exc)

        try:
            response = (
                client.table("chat_messages")
                .select(
                    "group_id,telegram_topic_id,telegram_message_id,created_at,content,metadata"
                )
                .contains("metadata", {"channel": "notify_endpoint", "grid_name": grid_name})
                .gte("created_at", since)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            for row in getattr(response, "data", None) or []:
                message_id = row.get("telegram_message_id")
                chat_id = row.get("group_id")
                if message_id is None or not chat_id:
                    continue
                records.append(
                    PriorAlertMessage(
                        external_chat_id=str(chat_id),
                        external_topic_id=row.get("telegram_topic_id"),
                        external_message_id=int(message_id),
                        sent_at=str(row.get("created_at") or ""),
                        content=str(row.get("content") or "")[:500],
                    )
                )
        except Exception as exc:
            _record_failure("recent_for_grid.legacy", exc)

        deduplicated = {
            (record.external_chat_id, record.external_message_id): record for record in reversed(records)
        }
        return sorted(deduplicated.values(), key=lambda record: record.sent_at, reverse=True)[:limit]

    async def latest_for_grid(self, grid_name: str) -> PriorAlertMessage | None:
        records = await self.recent_for_grid(grid_name, "1970-01-01T00:00:00+00:00", limit=1)
        return records[0] if records else None
