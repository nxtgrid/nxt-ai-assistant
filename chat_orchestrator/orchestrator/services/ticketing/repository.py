"""Canonical persistence boundary for Anansi-related tickets."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from .backend import BackendTicketResult, TicketCreateRequest


class TicketRepositoryError(RuntimeError):
    """Raised when the canonical ticket store cannot complete an operation."""


class TicketRecord(BaseModel):
    id: str
    ticket_ref: Optional[str] = None
    backend: Optional[Literal["jira", "internal"]] = None
    created_via: Literal["escalation", "notification", "adopted", "legacy"]
    provisioning_state: Literal["pending", "active", "failed"]
    status: Literal["open", "in_progress", "done"] = "open"
    backend_status: Optional[str] = None
    summary: str
    description: Optional[str] = None
    ticket_type: Optional[str] = None
    organization_id: Optional[int] = None
    grid_name: Optional[str] = None
    assignee_email: Optional[str] = None
    labels: list[str] = Field(default_factory=list)


class TicketRepository:
    """The sole writer for tickets, formal comments, and chat ticket links."""

    def __init__(
        self,
        client: Optional[Any] = None,
        get_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        if client is None and get_client is None:
            raise ValueError("TicketRepository requires either `client` or `get_client`")
        self._client_instance = client
        self._get_client = get_client

    def _raw_client(self) -> Any:
        client = self._client_instance
        if client is None and self._get_client is not None:
            client = self._get_client()
        if client is None:
            raise TicketRepositoryError("canonical ticket repository has no database client")
        return client

    @staticmethod
    def _record(response: Any) -> TicketRecord:
        rows = getattr(response, "data", None) or []
        if not rows:
            raise TicketRepositoryError("canonical ticket write returned no row")
        return TicketRecord.model_validate(rows[0])

    async def create_intent(
        self,
        req: TicketCreateRequest,
        *,
        created_via: Literal["escalation", "notification", "adopted", "legacy"],
    ) -> TicketRecord:
        payload = {
            "summary": req.summary,
            "description": req.description or None,
            "organization_id": req.organization_id,
            "grid_name": req.grid_name,
            "assignee_email": req.assignee_email,
            "ticket_type": req.ticket_type,
            "labels": req.labels,
            "created_via": created_via,
            "provisioning_state": "pending",
            "status": "open",
        }
        try:
            response = self._raw_client().table("tickets").insert(payload).execute()
        except TicketRepositoryError:
            raise
        except Exception as exc:
            raise TicketRepositoryError(f"failed to create canonical ticket intent: {exc}") from exc
        return self._record(response)

    async def activate(
        self,
        ticket_id: str,
        result: BackendTicketResult,
        *,
        backend_status: str = "open",
    ) -> TicketRecord:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "ticket_ref": result.ref,
            "backend": result.backend,
            "ticket_type": result.ticket_type,
            "provisioning_state": "active",
            "status": "open",
            "backend_status": backend_status,
            "activated_at": now,
            "backend_synced_at": now,
        }
        try:
            response = self._raw_client().table("tickets").update(payload).eq("id", ticket_id).execute()
        except TicketRepositoryError:
            raise
        except Exception as exc:
            raise TicketRepositoryError(f"failed to activate canonical ticket: {exc}") from exc
        return self._record(response)

    async def set_pending_backend(self, ticket_id: str, backend: str) -> None:
        self._raw_client().table("tickets").update({"backend": backend}).eq("id", ticket_id).execute()

    async def get_by_ref(self, ref: str) -> TicketRecord | None:
        """Return canonical ticket identity for a backend reference, if known."""
        try:
            response = (
                self._raw_client()
                .table("tickets")
                .select("*")
                .eq("ticket_ref", ref)
                .limit(1)
                .execute()
            )
        except TicketRepositoryError:
            raise
        except Exception as exc:
            raise TicketRepositoryError(f"failed to read canonical ticket: {exc}") from exc

        rows = getattr(response, "data", None) or []
        return TicketRecord.model_validate(rows[0]) if rows else None
