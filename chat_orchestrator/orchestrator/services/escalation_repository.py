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
