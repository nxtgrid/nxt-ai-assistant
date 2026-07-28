"""Internal (Jira-optional) ticket backend, backed by chat_db via Supabase.

Lets Anansi track escalation tickets without a Jira project configured.
Refs are allocated from the ``internal_ticket_seq`` sequence via the
``next_internal_ticket_ref`` RPC function.  This backend only allocates that
identity: ``TicketService`` is responsible for creating and activating the
canonical ``tickets`` row around the backend call.  In particular, creation
must not also write the retired ``internal_tickets`` relation, since that
would produce a second ticket identity for one request.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from shared.config import flag_registry as fr
from shared.utils.logging import get_logger

from .backend import (
    TicketBackendError,
    TicketCreateRequest,
    TicketResult,
    TicketStatus,
    TicketSummary,
)
from .repository import TicketRepository

LOGGER = get_logger(__name__)


class InternalTicketBackend:
    """Reference allocator and operation adapter for internal tickets.

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
        ticket_repository: Optional[TicketRepository] = None,
    ) -> None:
        if client is None and get_client is None:
            raise ValueError("InternalTicketBackend requires either `client` or `get_client`")
        self._client_instance = client
        self._get_client_fn = get_client
        self._tickets = ticket_repository or TicketRepository(get_client=self._client)

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

        return TicketResult(
            ref=ticket_ref,
            backend="internal",
            url=None,
            ticket_type=req.ticket_type or "Task",
        )

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        try:
            await self._tickets.add_comment_by_ref(ref, body, is_public=public)
            return True
        except Exception as e:
            LOGGER.warning("Failed to add internal comment to %s: %s", ref, e)
            return False

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        try:
            return await self._tickets.get_status_by_ref(ref)
        except Exception as e:
            LOGGER.warning("Failed to fetch internal ticket status for %s: %s", ref, e)
            return None

    async def transition_to_done(self, ref: str) -> None:
        """Mark a ticket as done.

        The canonical repository owns the state transition and resolved time.
        """
        try:
            await self._tickets.transition_to_done_by_ref(ref)
        except Exception as e:
            LOGGER.warning("Failed to transition internal ticket %s to done: %s", ref, e)

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        try:
            return await self._tickets.find_ref_for_escalation(mapping_id)
        except Exception as e:
            LOGGER.debug("Error looking up internal ticket for escalation %s: %s", mapping_id, e)
            return None

    async def update_ticket(
        self,
        ref: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        priority_id: Optional[str] = None,
    ) -> bool:
        """Update summary/description of an internal ticket.

        ``priority_id`` has no equivalent on the internal backend (severity is
        carried as a label instead, set at creation) -- silently ignored so
        callers that pass it uniformly across both backends don't need a
        backend-specific branch.
        """
        payload: dict[str, Any] = {}
        if summary is not None:
            payload["summary"] = summary
        if description is not None:
            payload["description"] = description
        if not payload:
            return True
        try:
            await self._tickets.update_by_ref(ref, **payload)
            return True
        except Exception as e:
            LOGGER.warning("Failed to update internal ticket %s: %s", ref, e)
            return False

    async def find_open_by_grid(self, grid_name: str, limit: int = 20) -> List[TicketSummary]:
        try:
            return await self._tickets.find_open_internal_by_grid(grid_name, limit=limit)
        except Exception as e:
            LOGGER.warning("Failed to find open internal tickets for grid %s: %s", grid_name, e)
            return []
