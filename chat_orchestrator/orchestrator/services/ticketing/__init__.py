"""Ticket-backend abstraction (Jira-optional ticket backend).

Exposes ``TicketService`` -- the seam callers should depend on -- plus the
``TicketBackend`` Protocol and its data-carrier types, and the two concrete
backend implementations for direct use in tests/DI.
"""

from .backend import (
    TicketBackend,
    TicketBackendError,
    TicketCreateRequest,
    TicketResult,
    TicketStatus,
)
from .internal_backend import InternalTicketBackend
from .jira_backend import JiraTicketBackend
from .service import TicketService

__all__ = [
    "TicketBackend",
    "TicketBackendError",
    "TicketCreateRequest",
    "TicketResult",
    "TicketStatus",
    "InternalTicketBackend",
    "JiraTicketBackend",
    "TicketService",
]
