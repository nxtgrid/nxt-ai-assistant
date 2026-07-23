"""TicketService -- resolves and delegates to the active ticket backend.

This is the seam future callers (``EscalationService``, and later the
``/notify`` endpoint) are meant to depend on instead of talking to
``JiraTicketBackend``/``InternalTicketBackend`` directly. Wiring
``EscalationService`` to actually call through here is a later task --
this module only builds the standalone service so that task can wire it
in without also having to design its public surface.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from shared.config import flag_registry as fr
from shared.utils.logging import get_logger

from .backend import TicketBackend, TicketCreateRequest, TicketResult, TicketStatus
from .internal_backend import InternalTicketBackend
from .jira_backend import JiraTicketBackend

LOGGER = get_logger(__name__)


class TicketService:
    """Resolves which ticket backend to use and delegates every call to it.

    Every method stamps ``ticket_ref``/``ticket_backend`` on the
    corresponding ``escalation_mappings`` row (when ``escalation_mapping_id``
    is set) so the record is uniform and recoverable regardless of which
    backend actually filed the ticket.
    """

    def __init__(
        self,
        supabase_client: Optional[Any] = None,
        get_supabase_client: Optional[Callable[[], Optional[Any]]] = None,
        jira_backend: Optional[TicketBackend] = None,
        internal_backend: Optional[TicketBackend] = None,
    ) -> None:
        """
        Args:
            supabase_client: An ``EnhancedSupabaseClient``-like wrapper (has
                ``_get_client()``), used to stamp ``escalation_mappings`` and,
                by default, to back ``InternalTicketBackend``.
            get_supabase_client: Getter callable that lazily produces the
                wrapper above -- mirrors
                ``EscalationService._get_supabase_client()``. Used when
                ``supabase_client`` isn't available yet at construction time.
            jira_backend: Pre-built backend, for dependency injection in tests.
            internal_backend: Pre-built backend, for dependency injection in tests.
        """
        self._supabase_client_instance = supabase_client
        self._get_supabase_client_fn = get_supabase_client
        self._jira: TicketBackend = jira_backend or JiraTicketBackend()
        self._internal: TicketBackend = internal_backend or InternalTicketBackend(
            get_client=self._raw_client
        )

    # ------------------------------------------------------------------
    # Supabase access (wrapper -> raw client, matching EscalationService's
    # own lazy-singleton pattern for _get_supabase_client()).
    # ------------------------------------------------------------------

    def _wrapper(self) -> Optional[Any]:
        if self._supabase_client_instance is not None:
            return self._supabase_client_instance
        if self._get_supabase_client_fn is not None:
            return self._get_supabase_client_fn()
        return None

    def _raw_client(self) -> Optional[Any]:
        wrapper = self._wrapper()
        if wrapper is None:
            return None
        return wrapper._get_client()

    # ------------------------------------------------------------------
    # Backend resolution
    # ------------------------------------------------------------------

    async def resolve_backend(self) -> TicketBackend:
        """Pick a backend per ``TICKET_BACKEND_OVERRIDE`` (``auto``|``jira``|``internal``).

        - ``internal``: always internal.
        - ``jira``: Jira if creds are present, else internal (never hard-fails).
        - ``auto`` (default, and any unrecognized value): Jira if
          ``JiraTicketBackend.is_available()`` (creds + healthy cached probe),
          else internal.
        """
        override = (fr.get("TICKET_BACKEND_OVERRIDE") or "auto").strip().lower()

        if override == "internal":
            return self._internal

        if override == "jira":
            has_creds = getattr(self._jira, "has_credentials", None)
            if callable(has_creds) and has_creds():
                return self._jira
            return self._internal

        # "auto" (default) and any unrecognized override value.
        if await self._jira.is_available():
            return self._jira
        return self._internal

    async def _backend_for_ref(self, ref: str) -> TicketBackend:
        """Route by the ref's *persisted* backend, not current availability.

        A Jira ticket filed before an outage must still be read as Jira when
        Jira comes back, and an internal ticket must stay internal -- so this
        checks whether ``ref`` exists in ``internal_tickets`` rather than
        re-running ``resolve_backend()``.
        """
        raw = self._raw_client()
        if raw is not None:
            try:
                response = (
                    raw.table("internal_tickets")
                    .select("ticket_ref")
                    .eq("ticket_ref", ref)
                    .limit(1)
                    .execute()
                )
                rows = getattr(response, "data", None) or []
                if rows:
                    return self._internal
            except Exception:
                LOGGER.warning(
                    "ticket service: internal_tickets lookup failed for ref %s", ref, exc_info=True
                )
        return self._jira

    # ------------------------------------------------------------------
    # Escalation-mapping stamping
    # ------------------------------------------------------------------

    async def _stamp_escalation_mapping(self, mapping_id: str, ref: str, backend: str) -> None:
        raw = self._raw_client()
        if raw is None:
            LOGGER.warning(
                "ticket service: no Supabase client -- cannot stamp ticket_ref for mapping %s",
                mapping_id,
            )
            return
        try:
            raw.table("escalation_mappings").update(
                {"ticket_ref": ref, "ticket_backend": backend}
            ).eq("id", mapping_id).execute()
        except Exception:
            LOGGER.warning(
                "ticket service: failed to stamp ticket_ref for mapping %s", mapping_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # TicketBackend-shaped public API
    # ------------------------------------------------------------------

    async def create_ticket(self, req: TicketCreateRequest) -> TicketResult:
        backend = await self.resolve_backend()
        result = await backend.create_ticket(req)
        if req.escalation_mapping_id:
            await self._stamp_escalation_mapping(
                req.escalation_mapping_id, result.ref, result.backend
            )
        return result

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        backend = await self._backend_for_ref(ref)
        return await backend.add_comment(ref, body, public)

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        backend = await self._backend_for_ref(ref)
        return await backend.get_status(ref)

    async def transition_to_done(self, ref: str) -> None:
        backend = await self._backend_for_ref(ref)
        await backend.transition_to_done(ref)

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        """Dedup guard used before filing a new ticket for an escalation.

        Checks both backends -- at ticket-creation time we don't yet know
        which backend a prior (possibly failed-to-persist) attempt used.
        """
        jira_ref = await self._jira.find_by_escalation(mapping_id)
        if jira_ref:
            return jira_ref
        return await self._internal.find_by_escalation(mapping_id)
