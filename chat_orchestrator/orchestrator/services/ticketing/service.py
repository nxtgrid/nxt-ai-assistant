"""TicketService -- resolves and delegates to the active ticket backend.

This is the seam future callers (``EscalationService``, and later the
``/notify`` endpoint) are meant to depend on instead of talking to
``JiraTicketBackend``/``InternalTicketBackend`` directly. Wiring
``EscalationService`` to actually call through here is a later task --
this module only builds the standalone service so that task can wire it
in without also having to design its public surface.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from shared.config import flag_registry as fr
from shared.utils.logging import get_logger

from .backend import (
    TicketBackend,
    TicketBackendError,
    TicketCreateOutcome,
    TicketCreateRequest,
    TicketResult,
    TicketStatus,
    TicketSummary,
)
from .internal_backend import InternalTicketBackend
from .jira_backend import JiraTicketBackend
from .repository import TicketRepository

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
        ticket_repository: Optional[TicketRepository] = None,
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
        self._tickets = ticket_repository or TicketRepository(get_client=self._raw_client)
        self._internal: TicketBackend = internal_backend or InternalTicketBackend(
            get_client=self._raw_client, ticket_repository=self._tickets
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

    async def resolve_backend(self, override: Optional[str] = None) -> TicketBackend:
        """Pick a backend per an override value (``auto``|``jira``|``internal``).

        - ``internal``: always internal.
        - ``jira``: Jira if creds are present, else internal (never hard-fails).
        - ``auto`` (default, and any unrecognized value): Jira if
          ``JiraTicketBackend.is_available()`` (creds + healthy cached probe),
          else internal.

        Args:
            override: Explicit override value, takes precedence over the
                ``TICKET_BACKEND_OVERRIDE`` flag when given. Lets callers with
                their own backend-selection policy (e.g. the ``/notify``
                endpoint's ``NOTIFY_TICKETS_BACKEND``) reuse this resolution
                logic without being tied to the customer-escalation flag.
                Existing callers that omit this keep reading
                ``TICKET_BACKEND_OVERRIDE`` exactly as before.
        """
        override = (override or fr.get("TICKET_BACKEND_OVERRIDE") or "auto").strip().lower()

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

        The canonical ``tickets`` record is the only authority.  In
        particular, a ticket reference is not a backend discriminator: Jira
        project keys are deployment-specific and internal prefixes are
        configurable.
        """
        ticket = await self._tickets.get_by_ref(ref)
        if ticket is None or ticket.backend is None:
            raise TicketBackendError(f"no canonical ticket backend recorded for ref {ref}")
        if ticket.backend == "internal":
            return self._internal
        if ticket.backend == "jira":
            return self._jira
        raise TicketBackendError(f"unsupported canonical ticket backend for ref {ref}")

    # ------------------------------------------------------------------
    # Escalation-mapping stamping
    # ------------------------------------------------------------------

    async def _stamp_escalation_mapping(self, mapping_id: str, ref: str, backend: str) -> None:
        """Best-effort -- a failure here is safe to swallow, not just convenient to.

        If this UPDATE fails after the ticket was already created, the mapping
        row's ticket_ref/ticket_backend stay NULL despite a real ticket
        existing. That's recoverable without a retry loop here: both backends'
        find_by_escalation() locate the ticket independently of this stamp
        (Jira via the caller-supplied escalation label, internal via the
        escalation_mapping_id column on the ticket row itself) -- so the next
        dedup check still finds it rather than filing a duplicate.
        """
        raw = self._raw_client()
        if raw is None:
            LOGGER.warning(
                "ticket service: no Supabase client -- cannot stamp ticket_ref for mapping {}",
                mapping_id,
            )
            return
        try:
            raw.table("escalation_mappings").update(
                {"ticket_ref": ref, "ticket_backend": backend}
            ).eq("id", mapping_id).execute()
        except Exception:
            LOGGER.warning(
                "ticket service: failed to stamp ticket_ref for mapping {}", mapping_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # TicketBackend-shaped public API
    # ------------------------------------------------------------------

    async def create_ticket(
        self, req: TicketCreateRequest, backend_override: Optional[str] = None
    ) -> TicketResult:
        """Create a ticket. ``backend_override`` is forwarded to ``resolve_backend``
        (see its docstring) -- omit to use ``TICKET_BACKEND_OVERRIDE`` as usual."""
        created_via = "notification" if req.source == "notify" else "escalation"
        intent = await self._tickets.create_intent(req, created_via=created_via)
        backend = await self.resolve_backend(override=backend_override)
        await self._tickets.set_pending_backend(intent.id, backend.name)
        result = await backend.create_ticket(req)
        await self._tickets.activate(intent.id, result)
        if req.escalation_mapping_id:
            await self._stamp_escalation_mapping(
                req.escalation_mapping_id, result.ref, result.backend
            )
        return result.model_copy(update={"ticket_id": intent.id})

    async def create_ticket_with_internal_fallback(
        self, req: TicketCreateRequest, backend_override: Optional[str] = None
    ) -> TicketCreateOutcome:
        """Create a notify ticket, retrying internal only after Jira fails.

        This deliberately resolves the primary backend once. A successful
        internal primary stays a normal internal creation; it is not retried.
        """
        created_via = "notification" if req.source == "notify" else "escalation"
        intent = await self._tickets.create_intent(req, created_via=created_via)
        primary = await self.resolve_backend(override=backend_override)
        await self._tickets.set_pending_backend(intent.id, primary.name)
        try:
            result = await primary.create_ticket(req)
        except TicketBackendError as primary_error:
            if primary.name != "jira":
                return TicketCreateOutcome(result=None, error=str(primary_error))
            try:
                await self._tickets.set_pending_backend(intent.id, "internal")
                result = await self._internal.create_ticket(req)
            except TicketBackendError as internal_error:
                return TicketCreateOutcome(
                    result=None,
                    error=f"Jira: {primary_error}; internal: {internal_error}",
                    fallback_used=True,
                )
            await self._tickets.activate(intent.id, result)
            canonical_result = result.model_copy(update={"ticket_id": intent.id})
            if req.escalation_mapping_id:
                await self._stamp_escalation_mapping(
                    req.escalation_mapping_id, result.ref, result.backend
                )
            return TicketCreateOutcome(
                result=canonical_result,
                error=str(primary_error),
                fallback_used=True,
            )

        await self._tickets.activate(intent.id, result)
        canonical_result = result.model_copy(update={"ticket_id": intent.id})
        if req.escalation_mapping_id:
            await self._stamp_escalation_mapping(req.escalation_mapping_id, result.ref, result.backend)
        return TicketCreateOutcome(result=canonical_result)

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        backend = await self._backend_for_ref(ref)
        return await backend.add_comment(ref, body, public)

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        backend = await self._backend_for_ref(ref)
        return await backend.get_status(ref)

    async def get_backend_name(self, ref: str) -> str:
        """Return the persisted backend that owns ``ref``.

        Callers that need to render a ticket must use this instead of inferring
        the backend from a reference prefix: Jira project keys differ by
        deployment and internal prefixes are configurable.
        """
        return (await self._backend_for_ref(ref)).name

    async def transition_to_done(self, ref: str) -> None:
        backend = await self._backend_for_ref(ref)
        await backend.transition_to_done(ref)
        if backend is self._jira:
            # Unlike the internal backend (which persists via the repository it
            # shares with this service), jira_backend.transition_to_done() only
            # calls the Jira transitions API -- it has no repository reference,
            # so the canonical row would otherwise stay "open" forever.
            try:
                await self._tickets.transition_to_done_by_ref(ref)
            except Exception:
                LOGGER.warning(
                    "transition_to_done: failed to persist canonical status for jira ticket {}",
                    ref,
                    exc_info=True,
                )

    async def update_ticket(
        self,
        ref: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        priority_id: Optional[str] = None,
    ) -> bool:
        """Update an existing ticket's summary/description/priority.

        Routes by the ref's *persisted* backend (like ``add_comment``/
        ``get_status``), used by alert correlation to re-render a ticket
        after an amend (see ``correlation_render.py``).
        """
        backend = await self._backend_for_ref(ref)
        return await backend.update_ticket(
            ref, summary=summary, description=description, priority_id=priority_id
        )

    async def sync_jira_ticket_statuses(self, limit: int = 200) -> Dict[str, int]:
        """Pull live Jira status for open Jira-backed canonical tickets and close done ones.

        Complements the near-instant Jira webhook and the escalation sweep's
        own reconciliation loop (which only reconciles tickets tied to an
        escalation mapping): this walks every open Jira ticket in the
        canonical ``tickets`` table, so it also catches tickets filed via
        ``/notify`` with no linked escalation, or a closure the webhook
        missed. Meant to be called from the same daily sweep job.
        """
        refs = await self._tickets.list_open_by_backend("jira", limit=limit)
        checked = 0
        closed = 0
        for ref in refs:
            checked += 1
            try:
                status = await self._jira.get_status(ref)
            except Exception:
                LOGGER.warning("ticket status sync: get_status failed for {}", ref, exc_info=True)
                continue
            if status is None or not status.is_done:
                continue
            try:
                await self._tickets.transition_to_done_by_ref(ref)
                closed += 1
            except Exception:
                LOGGER.warning("ticket status sync: failed to close {}", ref, exc_info=True)
        return {"checked": checked, "closed": closed}

    async def find_open_by_grid(
        self, grid_name: str, limit: int = 20, backend_override: Optional[str] = None
    ) -> List[TicketSummary]:
        """Find open tickets for a grid on the currently-resolved backend.

        Uses ``resolve_backend`` (same override semantics as ``create_ticket``)
        rather than querying both backends -- correlation's own
        ``ticket_correlations`` index already covers historical tickets
        across a backend switch; this call exists to also catch tickets
        filed by humans directly in Jira (or by n8n before a cutover).
        """
        backend = await self.resolve_backend(override=backend_override)
        return await backend.find_open_by_grid(grid_name, limit=limit)

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        """Resolve escalation deduplication through canonical ticket ownership.

        A completed create attaches the ticket to ``escalations.ticket_id``;
        that relation is the only durable dedup authority.  Backend-specific
        searches can find an untracked external ticket, but cannot safely
        establish its Anansi ownership or backend without an explicit adopt
        flow, so they are intentionally not used here.
        """
        return await self._tickets.find_ref_for_escalation(mapping_id)
