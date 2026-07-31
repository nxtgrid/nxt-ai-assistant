"""Persistence boundary for escalation-triggering media attachments.

Keyed by escalation_id, not ticket_id, because media is captured at
escalation time (see EscalationService.escalate_to_support) which always
happens before any ticket exists -- an escalation may sit unfiled for a
while, or never get filed at all. ticket_id is stamped on later, once
TicketService.create_ticket() actually creates a ticket for the escalation.
"""

from __future__ import annotations

from typing import Any, Callable, List, Literal, Optional

from pydantic import BaseModel

BUCKET_NAME = "escalation-media"


class EscalationAttachment(BaseModel):
    id: str
    escalation_id: str
    ticket_id: Optional[str] = None
    storage_path: str
    media_type: Literal["image", "video", "audio", "document"]
    mime_type: str
    size_bytes: int
    jira_attachment_id: Optional[str] = None


class AttachmentRepositoryError(RuntimeError):
    """Raised when an escalation_attachments operation cannot be completed."""


class AttachmentRepository:
    """The sole writer/reader for ``escalation_attachments``."""

    def __init__(
        self,
        client: Optional[Any] = None,
        get_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        if client is None and get_client is None:
            raise ValueError("AttachmentRepository requires either `client` or `get_client`")
        self._client_instance = client
        self._get_client = get_client

    def _raw_client(self) -> Any:
        client = self._client_instance
        if client is None and self._get_client is not None:
            client = self._get_client()
        if client is None:
            raise AttachmentRepositoryError("attachment repository has no database client")
        return client

    async def insert(
        self,
        *,
        escalation_id: str,
        storage_path: str,
        media_type: Literal["image", "video", "audio", "document"],
        mime_type: str,
        size_bytes: int,
    ) -> EscalationAttachment:
        payload = {
            "escalation_id": escalation_id,
            "storage_path": storage_path,
            "media_type": media_type,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }
        try:
            response = self._raw_client().table("escalation_attachments").insert(payload).execute()
        except AttachmentRepositoryError:
            raise
        except Exception as exc:
            raise AttachmentRepositoryError(f"failed to insert escalation attachment: {exc}") from exc
        rows = getattr(response, "data", None) or []
        if not rows:
            raise AttachmentRepositoryError("escalation attachment insert returned no row")
        return EscalationAttachment.model_validate(rows[0])

    async def list_by_escalation(self, escalation_id: str) -> List[EscalationAttachment]:
        try:
            response = (
                self._raw_client()
                .table("escalation_attachments")
                .select("*")
                .eq("escalation_id", escalation_id)
                .execute()
            )
        except AttachmentRepositoryError:
            raise
        except Exception as exc:
            raise AttachmentRepositoryError(f"failed to list escalation attachments: {exc}") from exc
        rows = getattr(response, "data", None) or []
        return [EscalationAttachment.model_validate(row) for row in rows]

    async def link_ticket(self, escalation_id: str, ticket_id: str) -> None:
        try:
            self._raw_client().table("escalation_attachments").update(
                {"ticket_id": ticket_id}
            ).eq("escalation_id", escalation_id).execute()
        except Exception as exc:
            raise AttachmentRepositoryError(
                f"failed to link attachments for escalation {escalation_id} to ticket {ticket_id}: {exc}"
            ) from exc

    async def mark_synced(self, attachment_id: str, jira_attachment_id: str) -> None:
        try:
            self._raw_client().table("escalation_attachments").update(
                {"jira_attachment_id": jira_attachment_id}
            ).eq("id", attachment_id).execute()
        except Exception as exc:
            raise AttachmentRepositoryError(
                f"failed to mark attachment {attachment_id} synced: {exc}"
            ) from exc
