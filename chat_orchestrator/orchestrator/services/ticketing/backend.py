"""Shared ticket-backend abstraction.

Defines the ``TicketBackend`` Protocol that both ``JiraTicketBackend`` and
``InternalTicketBackend`` implement, plus the Pydantic data-carrier types
used to create/query tickets across either backend. ``TicketService``
(``service.py``) resolves which concrete backend to use per-call and is the
only thing ``EscalationService`` is meant to depend on going forward (that
rewiring is a later task -- see ``service.py`` module docstring).
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

TicketSourceLiteral = Literal["escalation", "notify"]
TicketBackendName = Literal["jira", "internal"]


class TicketCreateRequest(BaseModel):
    """Everything either ticket backend needs to file a new ticket.

    A single shape covers both backends so callers (``EscalationService``,
    and later the ``/notify`` endpoint) don't need backend-specific branches.
    """

    summary: str
    description: str = ""
    grid_name: Optional[str] = None
    assignee_email: Optional[str] = None
    organization_short_name: Optional[str] = None
    organization_id: Optional[int] = None
    labels: List[str] = Field(default_factory=list)
    """Caller-supplied, not auto-generated. For escalation tickets, pass
    ``[f"escalation-{escalation_mapping_id[:8]}"]`` -- ``JiraTicketBackend``'s
    dedup guard (``find_by_escalation``) searches Jira by this exact label
    format (``_search_jira_for_escalation``); omitting it means a retry can
    never find a prior Jira ticket and may file a duplicate."""
    escalation_mapping_id: Optional[str] = None
    session_id: Optional[str] = None
    customer_chat_id: Optional[str] = None
    customer_topic_id: Optional[str] = None
    # A normalized type name for internal tickets, or a Jira issue type id
    # selected from the configured project's create metadata.
    ticket_type: Optional[str] = None
    # Extra context for LLM-only ticket decisions (for example Jira issue-type
    # selection). It is never persisted in Jira or internal ticket fields.
    llm_context: Dict[str, Any] = Field(default_factory=dict)
    source: TicketSourceLiteral = "escalation"


class TicketResult(BaseModel):
    """Result of a successful ``create_ticket`` call."""

    ref: str
    backend: TicketBackendName
    url: Optional[str] = None
    ticket_type: Optional[str] = None


class TicketStatus(BaseModel):
    """Current status of an existing ticket, as read back from a backend."""

    summary: str
    is_done: bool
    raw_status: str = ""
    ticket_type: Optional[str] = None


class TicketSummary(BaseModel):
    """A candidate ticket returned by ``find_open_by_grid`` for correlation.

    Deliberately carries enough for the alert correlator to reason about the
    candidate (age, current affected-component list via ``labels``/metadata
    the caller has already merged in) without a second round-trip per
    candidate.
    """

    ref: str
    backend: TicketBackendName
    summary: str
    description: str = ""
    status: str = ""
    is_done: bool = False
    created_at: Optional[str] = None
    labels: List[str] = Field(default_factory=list)


class TicketBackendError(RuntimeError):
    """Raised by a backend's ``create_ticket`` when ticket creation fails.

    Backends never return a partial/failed ``TicketResult`` -- a failure to
    create a ticket is exceptional, not a normal return value, so callers
    (``TicketService``) can rely on ``create_ticket`` always returning a
    fully-populated result or raising.
    """


@runtime_checkable
class TicketBackend(Protocol):
    """Interface implemented by ``JiraTicketBackend`` and ``InternalTicketBackend``.

    ``TicketService.resolve_backend()`` picks a concrete implementation of
    this Protocol per-call; every method here mirrors what the design calls
    for so Task 4 (wiring ``EscalationService`` to ``TicketService``) has a
    single, stable surface to depend on.
    """

    name: str

    async def is_available(self) -> bool:
        """Whether this backend can currently accept new tickets."""
        ...

    async def create_ticket(self, req: TicketCreateRequest) -> TicketResult:
        """Create a new ticket. Raises ``TicketBackendError`` on failure."""
        ...

    async def add_comment(self, ref: str, body: str, public: bool = False) -> bool:
        """Post a comment to an existing ticket. Returns True on success."""
        ...

    async def get_status(self, ref: str) -> Optional[TicketStatus]:
        """Fetch the current status of a ticket, or None if not found."""
        ...

    async def transition_to_done(self, ref: str) -> None:
        """Mark a ticket as done/resolved. Non-blocking -- failures are logged, not raised."""
        ...

    async def find_by_escalation(self, mapping_id: str) -> Optional[str]:
        """Find a ticket ref already filed for this escalation mapping (dedup guard)."""
        ...

    async def update_ticket(
        self,
        ref: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        priority_id: Optional[str] = None,
    ) -> bool:
        """Update summary/description (and, for Jira, priority) of an existing ticket.

        Used by alert correlation to re-render a ticket after an amend (see
        ``correlation_render.py``). Never raises -- returns ``True``/``False``.
        A backend that doesn't support a given field (e.g. ``priority_id`` on
        the internal backend) silently ignores it rather than failing.
        """
        ...

    async def find_open_by_grid(self, grid_name: str, limit: int = 20) -> List["TicketSummary"]:
        """Find open tickets for a grid, most-recent-first (correlation candidates).

        Never raises -- returns ``[]`` on any failure so a backend outage
        degrades correlation to "no candidates found" (i.e. file a new
        ticket) rather than a hard error.
        """
        ...
