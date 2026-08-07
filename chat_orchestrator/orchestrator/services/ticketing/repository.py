"""Canonical persistence boundary for Anansi-related tickets."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from .backend import BackendTicketResult, TicketCreateRequest, TicketStatus, TicketSummary


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

    async def get_by_id(self, ticket_id: str) -> TicketRecord | None:
        """Return canonical ticket identity by its own uuid, if known."""
        try:
            response = (
                self._raw_client()
                .table("tickets")
                .select("*")
                .eq("id", ticket_id)
                .limit(1)
                .execute()
            )
        except TicketRepositoryError:
            raise
        except Exception as exc:
            raise TicketRepositoryError(f"failed to read canonical ticket: {exc}") from exc

        rows = getattr(response, "data", None) or []
        return TicketRecord.model_validate(rows[0]) if rows else None

    async def get_status_by_ref(self, ref: str) -> TicketStatus | None:
        ticket = await self.get_by_ref(ref)
        if ticket is None:
            return None
        return TicketStatus(
            summary=ticket.summary,
            is_done=ticket.status == "done",
            raw_status=ticket.status,
            ticket_type=ticket.ticket_type,
        )

    async def add_comment_by_ref(
        self,
        ref: str,
        body: str,
        *,
        author: str | None = None,
        is_public: bool = False,
        source: Literal["customer", "staff", "notify", "jira", "system"] = "staff",
    ) -> None:
        ticket = await self.get_by_ref(ref)
        if ticket is None:
            raise TicketRepositoryError(f"cannot add comment: unknown ticket ref {ref}")
        payload = {
            "ticket_id": ticket.id,
            "body": body,
            "author": author,
            "is_public": is_public,
            "source": source,
        }
        try:
            response = self._raw_client().table("ticket_comments").insert(payload).execute()
        except Exception as exc:
            raise TicketRepositoryError(f"failed to add canonical ticket comment: {exc}") from exc
        if not getattr(response, "data", None):
            raise TicketRepositoryError("canonical ticket comment write returned no row")

    async def list_comments_by_ref(self, ref: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Return the most recent comments for a ticket, oldest-first.

        Ordered newest-first in the query so ``limit`` keeps the *latest*
        comments, then reversed so the summariser reads them chronologically.
        Returns [] for an unknown ref rather than raising -- this feeds a
        best-effort notification, not a correctness-critical path.
        """
        ticket = await self.get_by_ref(ref)
        if ticket is None:
            return []
        try:
            response = (
                self._raw_client()
                .table("ticket_comments")
                .select("author,body,is_public,source,created_at")
                .eq("ticket_id", ticket.id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
        except Exception as exc:
            raise TicketRepositoryError(f"failed to read canonical ticket comments: {exc}") from exc
        rows = list(getattr(response, "data", None) or [])
        return list(reversed(rows))

    async def transition_to_done_by_ref(self, ref: str) -> bool:
        """Close a ticket. Returns True only if this call is what closed it.

        The ``status != 'done'`` guard makes this idempotent: Jira retries
        webhook deliveries, and several close paths can race for the same
        ticket. Callers use the return value to decide whether to announce
        the closure, so a redundant close must report False rather than
        raising (every caller already treats failures as non-fatal).
        """
        ticket = await self.get_by_ref(ref)
        if ticket is None:
            raise TicketRepositoryError(f"cannot close: unknown ticket ref {ref}")
        payload = {"status": "done", "resolved_at": datetime.now(timezone.utc).isoformat()}
        try:
            response = (
                self._raw_client()
                .table("tickets")
                .update(payload)
                .eq("id", ticket.id)
                .neq("status", "done")
                .execute()
            )
        except Exception as exc:
            raise TicketRepositoryError(f"failed to close canonical ticket: {exc}") from exc
        return bool(getattr(response, "data", None))

    async def set_in_progress_by_ref(self, ref: str) -> bool:
        """Mark a ticket in progress. Returns True only if this call is what
        flipped it.

        Guards against two cases: a redundant flip when it's already
        "in_progress" (Jira retries webhook deliveries same as any other
        event), and -- unlike ``transition_to_done_by_ref``, which has only
        one guard because "done" has nowhere further to go -- against
        regressing an already-"done" ticket, in case a reordered or
        out-of-order webhook delivery reports "in progress" after a closure
        this record already knows about.
        """
        ticket = await self.get_by_ref(ref)
        if ticket is None:
            raise TicketRepositoryError(f"cannot update: unknown ticket ref {ref}")
        try:
            response = (
                self._raw_client()
                .table("tickets")
                .update({"status": "in_progress"})
                .eq("id", ticket.id)
                .neq("status", "in_progress")
                .neq("status", "done")
                .execute()
            )
        except Exception as exc:
            raise TicketRepositoryError(f"failed to update canonical ticket: {exc}") from exc
        return bool(getattr(response, "data", None))

    async def update_by_ref(
        self, ref: str, *, summary: str | None = None, description: str | None = None
    ) -> None:
        payload = {key: value for key, value in {"summary": summary, "description": description}.items() if value is not None}
        if not payload:
            return
        ticket = await self.get_by_ref(ref)
        if ticket is None:
            raise TicketRepositoryError(f"cannot update: unknown ticket ref {ref}")
        try:
            response = self._raw_client().table("tickets").update(payload).eq("id", ticket.id).execute()
        except Exception as exc:
            raise TicketRepositoryError(f"failed to update canonical ticket: {exc}") from exc
        if not getattr(response, "data", None):
            raise TicketRepositoryError("canonical ticket update returned no row")

    async def find_ref_for_escalation(self, escalation_id: str) -> str | None:
        """Return an active ticket reference linked from a canonical escalation."""
        try:
            escalation_response = (
                self._raw_client()
                .table("escalations")
                .select("ticket_id")
                .eq("id", escalation_id)
                .limit(1)
                .execute()
            )
            escalation_rows = getattr(escalation_response, "data", None) or []
            if not escalation_rows or not escalation_rows[0].get("ticket_id"):
                return None
            ticket_response = (
                self._raw_client()
                .table("tickets")
                .select("ticket_ref")
                .eq("id", escalation_rows[0]["ticket_id"])
                .limit(1)
                .execute()
            )
        except TicketRepositoryError:
            raise
        except Exception as exc:
            raise TicketRepositoryError(
                f"failed to resolve canonical escalation ticket: {exc}"
            ) from exc
        ticket_rows = getattr(ticket_response, "data", None) or []
        return ticket_rows[0].get("ticket_ref") if ticket_rows else None

    async def list_open_by_backend(self, backend: str, *, limit: int = 200) -> list[str]:
        """Return ticket_refs for active, non-done tickets on the given backend.

        Used by the ticket-status sync sweep to find Jira-backed tickets whose
        canonical status may be stale -- unlike ``find_open_internal_by_grid``,
        this isn't scoped to a grid since the sweep walks every open ticket.
        """
        try:
            response = (
                self._raw_client()
                .table("tickets")
                .select("ticket_ref")
                .eq("backend", backend)
                .eq("provisioning_state", "active")
                .neq("status", "done")
                .limit(limit)
                .execute()
            )
        except TicketRepositoryError:
            raise
        except Exception as exc:
            raise TicketRepositoryError(f"failed to list open {backend} tickets: {exc}") from exc

        rows = getattr(response, "data", None) or []
        return [row["ticket_ref"] for row in rows if row.get("ticket_ref")]

    async def find_open_internal_by_grid(
        self, grid_name: str, *, limit: int = 20
    ) -> list[TicketSummary]:
        """Return active, non-done internal tickets for correlation candidates."""
        try:
            response = (
                self._raw_client()
                .table("tickets")
                .select("*")
                .eq("backend", "internal")
                .eq("provisioning_state", "active")
                .eq("grid_name", grid_name)
                .neq("status", "done")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
        except TicketRepositoryError:
            raise
        except Exception as exc:
            raise TicketRepositoryError(f"failed to find canonical internal tickets: {exc}") from exc

        rows = getattr(response, "data", None) or []
        return [
            TicketSummary(
                ref=row["ticket_ref"],
                backend="internal",
                summary=row.get("summary") or "",
                description=row.get("description") or "",
                status=row.get("status") or "",
                is_done=row.get("status") == "done",
                created_at=row.get("created_at"),
                labels=row.get("labels") or [],
            )
            for row in rows
            if row.get("ticket_ref")
        ]
