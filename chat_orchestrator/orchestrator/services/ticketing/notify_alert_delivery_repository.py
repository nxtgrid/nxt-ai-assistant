"""Durable history for successfully delivered `/chat/notify` alerts."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

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
    content: str = ""
    ticket_ref: str | None = None
    source: str | None = None


class OMChatMessage(_ContextRecord):
    created_at: str
    role: str = ""
    content: str = ""
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
        downtime: bool = False,
    ) -> dict[str, Any] | None:
        """Record a post only after Telegram supplied a real message identifier.

        ``downtime`` marks a delivery that told the topic the grid itself is
        down (inverter in fault and/or phases at 0 V). It is the clock
        ``downtime_alert_policy`` reads to hold downtime alerts to one a day
        without ever letting an unrelated equipment alert reset that clock.
        """
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
                        "downtime": bool(downtime),
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

    async def latest_downtime_sent_at(self, grid_name: str) -> str | None:
        """``sent_at`` of the newest delivery that reported this grid down.

        Returns ``None`` both when the grid has never been reported down and
        when the ledger read fails. That collapse is deliberate and fail-open:
        an unreadable clock is not evidence the topic was already told today,
        and ``decide_downtime_override`` treats ``None`` as "send".
        """
        client = self._raw_client()
        if client is None:
            _record_failure(
                "latest_downtime_sent_at", RuntimeError("database client unavailable")
            )
            return None
        try:
            response = (
                client.table("notify_alert_deliveries")
                .select("sent_at")
                .eq("grid_name", grid_name)
                .eq("downtime", True)
                .order("sent_at", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            _record_failure("latest_downtime_sent_at", exc)
            return None
        rows = getattr(response, "data", None) or []
        if not rows:
            return None
        sent_at = rows[0].get("sent_at")
        return str(sent_at) if sent_at else None

    async def latest_for_grid(self, grid_name: str) -> PriorAlertMessage | None:
        records = await self.recent_for_grid(grid_name, "1970-01-01T00:00:00+00:00", limit=1)
        return records[0] if records else None

    async def recent_om_messages(
        self,
        *,
        chat_id: str,
        topic_id: str | None,
        since: str,
        limit: int = 50,
    ) -> list[OMChatMessage]:
        """Return bounded human/O&M evidence from exactly one active Telegram topic."""
        client = self._raw_client()
        if client is None:
            _record_failure("recent_om_messages", RuntimeError("database client unavailable"))
            return []
        try:
            query = (
                client.table("chat_messages")
                .select(
                    "created_at,role,content,sender_telegram_id,from_chat_id,metadata"
                )
                .eq("group_id", str(chat_id))
                .is_("archived_at", "null")
            )
            if topic_id is not None:
                query = query.eq("telegram_topic_id", str(topic_id))
            response = (
                query.gte("created_at", since)
                .order("created_at", desc=False)
                .limit(max(limit * 2, limit))
                .execute()
            )
            messages: list[OMChatMessage] = []
            for row in getattr(response, "data", None) or []:
                metadata = row.get("metadata") or {}
                content = str(row.get("content") or "").strip()
                if not content or metadata.get("channel") == "notify_endpoint":
                    continue
                messages.append(
                    OMChatMessage(
                        created_at=str(row.get("created_at") or ""),
                        role=str(row.get("role") or ""),
                        content=content[:500],
                        sender_telegram_id=(
                            str(row["sender_telegram_id"])
                            if row.get("sender_telegram_id") is not None
                            else None
                        ),
                        from_chat_id=(
                            str(row["from_chat_id"])
                            if row.get("from_chat_id") is not None
                            else None
                        ),
                    )
                )
                if len(messages) == limit:
                    break
            return messages
        except Exception as exc:
            _record_failure("recent_om_messages", exc)
            return []
