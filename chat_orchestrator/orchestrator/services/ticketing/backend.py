"""Shared ticket-backend abstraction.

Defines the ``TicketBackend`` Protocol that both ``JiraTicketBackend`` and
``InternalTicketBackend`` implement, plus the Pydantic data-carrier types
used to create/query tickets across either backend. ``TicketService``
(``service.py``) resolves which concrete backend to use per-call and is the
only thing ``EscalationService`` is meant to depend on going forward (that
rewiring is a later task -- see ``service.py`` module docstring).
"""

from __future__ import annotations

from typing import List, Literal, Optional, Protocol, runtime_checkable

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
    escalation_mapping_id: Optional[str] = None
    session_id: Optional[str] = None
    customer_chat_id: Optional[str] = None
    customer_topic_id: Optional[str] = None
    source: TicketSourceLiteral = "escalation"


class TicketResult(BaseModel):
    """Result of a successful ``create_ticket`` call."""

    ref: str
    backend: TicketBackendName
    url: Optional[str] = None


class TicketStatus(BaseModel):
    """Current status of an existing ticket, as read back from a backend."""

    summary: str
    is_done: bool
    raw_status: str = ""


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
