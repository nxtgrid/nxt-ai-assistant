"""Persistence boundary for outbound Telegram delivery receipts."""

from __future__ import annotations

from typing import Any, Callable, Dict, Literal, Optional


class DeliveryRepository:
    """The only writer for ``message_deliveries``."""

    def __init__(
        self,
        client: Optional[Any] = None,
        get_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        if client is None and get_client is None:
            raise ValueError("DeliveryRepository requires either `client` or `get_client`")
        self._client_instance = client
        self._get_client = get_client

    def _raw_client(self) -> Any:
        client = self._client_instance or (self._get_client() if self._get_client else None)
        if client is None:
            raise RuntimeError("delivery repository has no database client")
        return client

    async def record(
        self,
        *,
        ticket_id: Optional[str],
        escalation_id: Optional[str],
        purpose: Literal["escalation", "notification", "update"],
        external_chat_id: str,
        external_topic_id: Optional[str],
        external_message_id: int,
        chat_message_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if ticket_id is None and escalation_id is None:
            raise ValueError("a delivery receipt requires a ticket or escalation owner")
        payload = {
            "ticket_id": ticket_id,
            "escalation_id": escalation_id,
            "purpose": purpose,
            "channel": "telegram",
            "external_chat_id": external_chat_id,
            "external_topic_id": external_topic_id,
            "external_message_id": external_message_id,
            "chat_message_id": chat_message_id,
        }
        response = (
            self._raw_client()
            .table("message_deliveries")
            .upsert(payload, on_conflict="channel,external_chat_id,external_message_id")
            .execute()
        )
        rows = getattr(response, "data", None) or []
        if not rows:
            raise RuntimeError("delivery receipt write returned no row")
        return rows[0]

    async def find_escalation_delivery(
        self,
        *,
        external_message_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Return the 'escalation' delivery receipt for a Telegram message id.

        Used to resolve a staff reply (which arrives keyed by the message it
        replied to) back to the ``escalation_id`` it belongs to, without
        going through the legacy ``escalation_mappings`` table.
        """
        response = (
            self._raw_client()
            .table("message_deliveries")
            .select("escalation_id,external_topic_id")
            .eq("external_message_id", external_message_id)
            .eq("purpose", "escalation")
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    async def find_notification(
        self,
        *,
        escalation_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent 'notification' delivery for *escalation_id*, or None.

        Used by ``_notify_customer`` to decide whether to edit an existing message
        (sweep promoting TKT → OPS) rather than sending a duplicate new one.
        """
        response = (
            self._raw_client()
            .table("message_deliveries")
            .select("external_chat_id,external_topic_id,external_message_id")
            .eq("escalation_id", escalation_id)
            .eq("purpose", "notification")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None
