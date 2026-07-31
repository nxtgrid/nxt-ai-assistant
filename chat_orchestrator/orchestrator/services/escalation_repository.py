"""Canonical persistence boundary for the Anansi escalation lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional


class EscalationRepositoryError(RuntimeError):
    """Raised when a canonical escalation operation cannot be completed."""


class EscalationRepository:
    """The sole writer for the canonical ``escalations`` relation."""

    def __init__(
        self,
        client: Optional[Any] = None,
        get_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        if client is None and get_client is None:
            raise ValueError("EscalationRepository requires either `client` or `get_client`")
        self._client_instance = client
        self._get_client = get_client

    def _raw_client(self) -> Any:
        client = self._client_instance
        if client is None and self._get_client is not None:
            client = self._get_client()
        if client is None:
            raise EscalationRepositoryError("escalation repository has no database client")
        return client

    @staticmethod
    def _rows(response: Any) -> list[dict[str, Any]]:
        return list(getattr(response, "data", None) or [])

    async def create(
        self,
        escalation_id: str,
        chat_session_id: str,
        **details: Any,
    ) -> dict[str, Any]:
        payload = {"id": escalation_id, "chat_session_id": chat_session_id, "state": "open", **details}
        try:
            rows = self._rows(self._raw_client().table("escalations").insert(payload).execute())
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(f"failed to create escalation: {exc}") from exc
        if not rows:
            raise EscalationRepositoryError("canonical escalation create returned no row")
        return rows[0]

    async def get(self, escalation_id: str) -> dict[str, Any] | None:
        try:
            rows = self._rows(
                self._raw_client().table("escalations").select("*").eq("id", escalation_id).limit(1).execute()
            )
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(f"failed to read escalation: {exc}") from exc
        return rows[0] if rows else None

    async def claim(self, escalation_id: str) -> dict[str, Any] | None:
        """Atomically claim an open escalation; only one tracker can succeed."""
        try:
            rows = self._rows(
                self._raw_client()
                .table("escalations")
                .update({"state": "processing"})
                .eq("id", escalation_id)
                .eq("state", "open")
                .execute()
            )
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(f"failed to claim escalation: {exc}") from exc
        return rows[0] if rows else None

    async def attach_ticket(self, escalation_id: str, ticket_id: str) -> None:
        await self._transition(
            escalation_id,
            {"ticket_id": ticket_id, "state": "tracked"},
            from_state="processing",
        )

    async def release(self, escalation_id: str) -> None:
        await self._transition(escalation_id, {"state": "open"}, from_state="processing")

    async def resolve(self, escalation_id: str) -> None:
        payload = {"state": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat()}
        try:
            self._raw_client().table("escalations").update(payload).eq("id", escalation_id).execute()
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(f"failed to resolve escalation: {exc}") from exc

    async def resolve_if_active(self, escalation_id: str) -> bool:
        """Atomically resolve, unless a concurrent caller already did --
        returns False when the row was already "resolved", mirroring the
        legacy is_active=True UPDATE guard (prevents a retried Jira webhook
        from sending a duplicate customer notification)."""
        payload = {"state": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat()}
        try:
            rows = self._rows(
                self._raw_client()
                .table("escalations")
                .update(payload)
                .eq("id", escalation_id)
                .neq("state", "resolved")
                .execute()
            )
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(f"failed to resolve escalation: {exc}") from exc
        return bool(rows)

    async def resolve_all_for_session(self, chat_session_id: str) -> int:
        """Resolve every non-resolved escalation for a session -- canonical
        equivalent of the legacy close_escalation's "deactivate every mapping"
        semantics (support only needs to reply "Closed" once to clear
        everything). Returns the number of escalations resolved."""
        payload = {"state": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat()}
        try:
            rows = self._rows(
                self._raw_client()
                .table("escalations")
                .update(payload)
                .eq("chat_session_id", chat_session_id)
                .neq("state", "resolved")
                .execute()
            )
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(
                f"failed to resolve escalations for session: {exc}"
            ) from exc
        return len(rows)

    async def reopen(self, escalation_id: str) -> None:
        """Unconditionally move a resolved escalation back to "open" --
        the counterpart to resolve(), for the "reply Reopen" flow."""
        payload = {"state": "open", "resolved_at": None}
        try:
            self._raw_client().table("escalations").update(payload).eq("id", escalation_id).execute()
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(f"failed to reopen escalation: {exc}") from exc

    async def has_blocking_escalation(
        self, chat_session_id: str, *, exclude_reasons: Optional[tuple[str, ...]] = None
    ) -> bool:
        try:
            query = (
                self._raw_client()
                .table("escalations")
                .select("id")
                .eq("chat_session_id", chat_session_id)
                .in_("state", ["open", "processing"])
            )
            for reason in exclude_reasons or ():
                query = query.neq("reason", reason)
            rows = self._rows(query.limit(1).execute())
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(f"failed to check blocking escalations: {exc}") from exc
        return bool(rows)

    async def get_by_ticket_id(self, ticket_id: str) -> dict[str, Any] | None:
        """Non-resolved escalation attached to a ticket -- canonical
        equivalent of get_escalation_mapping_by_jira_key /
        get_escalation_mapping_by_ticket_ref. attach_ticket() only ever links
        one escalation to a given ticket_id, so there's no "most recent"
        ambiguity to order by, unlike the legacy lookups.
        """
        try:
            rows = self._rows(
                self._raw_client()
                .table("escalations")
                .select("*")
                .eq("ticket_id", ticket_id)
                .neq("state", "resolved")
                .limit(1)
                .execute()
            )
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(
                f"failed to look up escalation by ticket id: {exc}"
            ) from exc
        return rows[0] if rows else None

    async def _transition(
        self,
        escalation_id: str,
        payload: dict[str, Any],
        *,
        from_state: str,
    ) -> None:
        try:
            self._raw_client().table("escalations").update(payload).eq("id", escalation_id).eq(
                "state", from_state
            ).execute()
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(f"failed to transition escalation: {exc}") from exc

    # ------------------------------------------------------------------
    # Sweep queries (canonical equivalents of the legacy
    # get_stale_unfiled_escalations / get_orphaned_claimed_escalations /
    # get_old_unfiled_escalations / get_active_tracked_escalations)
    # ------------------------------------------------------------------

    async def list_unfiled(
        self,
        *,
        state: str = "open",
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        exclude_reasons: Optional[tuple[str, ...]] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Escalations in ``state`` with no ticket attached (``ticket_id IS
        NULL``), oldest first. Canonical equivalent of both
        get_stale_unfiled_escalations (both a lower and upper age bound) and
        get_old_unfiled_escalations (upper bound only) -- the two legacy
        methods differ only in which bounds are set.
        """
        try:
            query = (
                self._raw_client()
                .table("escalations")
                .select("id, created_at")
                .eq("state", state)
                .is_("ticket_id", "null")
            )
            for reason in exclude_reasons or ():
                query = query.neq("reason", reason)
            if created_after is not None:
                query = query.gt("created_at", created_after)
            if created_before is not None:
                query = query.lt("created_at", created_before)
            rows = self._rows(query.order("created_at").limit(limit).execute())
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(f"failed to list unfiled escalations: {exc}") from exc
        return rows

    async def list_claimed_orphans(
        self, *, created_after: Optional[str] = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Escalations claimed (state="processing") but never completed --
        no ticket attached and never resolved. Canonical equivalent of
        get_orphaned_claimed_escalations, used by startup recovery to
        release claims orphaned by a process kill between claim() and
        track_as_ticket() completing.
        """
        try:
            query = (
                self._raw_client()
                .table("escalations")
                .select("id, created_at")
                .eq("state", "processing")
                .is_("ticket_id", "null")
                .is_("resolved_at", "null")
            )
            if created_after is not None:
                query = query.gte("created_at", created_after)
            rows = self._rows(query.limit(limit).execute())
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(
                f"failed to list claimed-orphan escalations: {exc}"
            ) from exc
        return rows

    async def list_active_tracked(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Open escalations with a ticket already attached -- e.g. a
        follow-up escalation pre-linked to an existing ticket at creation
        time, never explicitly claimed/resolved by staff. Canonical
        equivalent of get_active_tracked_escalations, used by the sweep to
        reconcile tickets closed outside the webhook path and notify
        customers of ones still open.
        """
        try:
            rows = self._rows(
                self._raw_client()
                .table("escalations")
                .select("id, created_at")
                .eq("state", "open")
                .not_.is_("ticket_id", "null")
                .order("created_at")
                .limit(limit)
                .execute()
            )
        except EscalationRepositoryError:
            raise
        except Exception as exc:
            raise EscalationRepositoryError(
                f"failed to list actively-tracked escalations: {exc}"
            ) from exc
        return rows
