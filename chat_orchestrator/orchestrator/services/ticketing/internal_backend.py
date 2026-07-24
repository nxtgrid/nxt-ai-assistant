"""Internal (Jira-optional) ticket backend, backed by chat_db via Supabase.

Lets Anansi track escalation tickets without a Jira project configured.
Tables (created by Task 1's migration -- see db/schema/chat_db.sql and
db/migrations/0001_jira_optional_ticket_backend.sql): ``internal_tickets``
and ``internal_ticket_comments``. Refs are allocated from the
``internal_ticket_seq`` sequence via the ``next_internal_ticket_ref`` RPC
function (db/migrations/0002_internal_ticket_ref_allocation.sql) -- a thin
wrapper that exists only because PostgREST doesn't expose the built-in
``nextval()`` directly. ``create_ticket`` calls that RPC to get a ref, then
does a normal ``.table("internal_tickets").insert(...)`` as a second,
ordinary round-trip. Postgres sequences are race-free under concurrency on
their own, so this two-round-trip shape never risks a duplicate ref -- at
worst a failed insert leaves an unused (harmless) sequence gap.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from shared.config import flag_registry as fr
from shared.utils.logging import get_logger

from .backend import TicketBackendError, TicketCreateRequest, TicketResult, TicketStatus

LOGGER = get_logger(__name__)


class InternalTicketBackend:
    """Ticket backend backed by chat_db's ``internal_tickets`` table.

    Accepts either a ready-made Supabase (postgrest) client or a getter
    callable that lazily produces one -- mirrors
    ``EscalationService._get_supabase_client()``'s lazy-singleton pattern.
    The client passed in here is the *raw* client returned by
    ``EnhancedSupabaseClient._get_client()`` (i.e. something with
    ``.table(...)``/``.rpc(...)``), not the ``EnhancedSupabaseClient``
    wrapper itself -- callers pass ``get_client=lambda: wrapper._get_client()``.
    """

    name = "internal"

    def __init__(
        self,
        client: Optional[Any] = None,
        get_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        if client is None and get_client is None:
            raise ValueError("InternalTicketBackend requires either `client` or `get_client`")
        self._client_instance = client
        self._get_client_fn = get_client

    def _client(self) -> Optional[Any]:
        if self._client_instance is not None:
            return self._client_instance
        if self._get_client_fn is not None:
            try:
                return self._get_client_fn()
            except Exception:
                LOGGER.warning("internal ticket backend: get_client() raised", exc_info=True)
                return None
        return None

    # ------------------------------------------------------------------
    # TicketBackend Protocol
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """True whenever a Supabase client is configured (true whenever the bot runs)."""
        return self._client() is not None

    async def create_ticket(self, req: TicketCreateRequest) -> TicketResult:
        client = self._client()
        if client is None:
            raise TicketBackendError("internal ticket backend: no Supabase client configured")

        prefix = fr.get("INTERNAL_TICKET_PREFIX")

        # Round-trip 1: allocate a uniquely-formatted ref via the
        # next_internal_ticket_ref RPC (a thin wrapper around nextval(),
        # needed only because PostgREST doesn't expose nextval() directly).
        try:
            ref_response = client.rpc(
                "next_internal_ticket_ref",
                {"p_prefix": prefix},
            ).execute()
        except Exception as e:
            raise TicketBackendError(f"internal ticket ref allocation failed: {e}") from e

        ticket_ref = getattr(ref_response, "data", None)
        if not ticket_ref:
            raise TicketBackendError(
                "internal ticket creation failed: next_internal_ticket_ref RPC returned no ref"
            )

        # Round-trip 2: ordinary insert -- no stored procedure duplicating
        # internal_tickets' column list required.
        try:
            insert_response = (
                client.table("internal_tickets")
                .insert(
                    {
                        "ticket_ref": ticket_ref,
                        "summary": req.summary,
                        "description": req.description or None,
                        "escalation_mapping_id": req.escalation_mapping_id,
                        "session_id": req.session_id,
                        "organization_id": req.organization_id,
                        "grid_name": req.grid_name,
                        "assignee_email": req.assignee_email,
                        "labels": req.labels or [],
                        "source": req.source,
                    }
                )
                .execute()
            )
        except Exception as e:
            raise TicketBackendError(f"internal ticket creation failed: {e}") from e

        rows = getattr(insert_response, "data", None) or []
        if not rows:
            raise TicketBackendError(
                "internal ticket creation failed: insert into internal_tickets returned no row"
            )
        return TicketResult(ref=ticket_ref, backend="internal", url=None)

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        client = self._client()
        if client is None:
            return False
        try:
            client.table("internal_ticket_comments").insert(
                {
                    "ticket_ref": ref,
                    "body": body,
                    "is_public": public,
                    "source": "staff",
                }
            ).execute()
            return True
        except Exception as e:
            LOGGER.warning("Failed to add internal comment to %s: %s", ref, e)
            return False

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        client = self._client()
        if client is None:
            return None
        try:
            response = (
                client.table("internal_tickets")
                .select("summary,status")
                .eq("ticket_ref", ref)
                .limit(1)
                .execute()
            )
        except Exception as e:
            LOGGER.warning("Failed to fetch internal ticket status for %s: %s", ref, e)
            return None

        rows = getattr(response, "data", None) or []
        if not rows:
            return None
        row = rows[0]
        status = row.get("status", "")
        return TicketStatus(
            summary=row.get("summary", ""),
            is_done=(status == "done"),
            raw_status=status,
        )

    async def transition_to_done(self, ref: str) -> None:
        """Mark a ticket as done.

        Note: SupabaseClient.update_internal_ticket_status() (in
        supabase_client.py) writes the same two fields independently for
        EscalationService-side callers going through that wrapper instead of
        this Protocol path. The "done" transition's semantics are duplicated
        across both, not shared -- update both if the closure logic changes.
        """
        client = self._client()
        if client is None:
            return
        try:
            client.table("internal_tickets").update(
                {
                    "status": "done",
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("ticket_ref", ref).execute()
        except Exception as e:
            LOGGER.warning("Failed to transition internal ticket %s to done: %s", ref, e)

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        client = self._client()
        if client is None:
            return None
        try:
            response = (
                client.table("internal_tickets")
                .select("ticket_ref")
                .eq("escalation_mapping_id", mapping_id)
                .limit(1)
                .execute()
            )
        except Exception as e:
            LOGGER.debug("Error looking up internal ticket for escalation %s: %s", mapping_id, e)
            return None

        rows = getattr(response, "data", None) or []
        return rows[0]["ticket_ref"] if rows else None
