# Internal Ticket Canonical Cutover Design

## Goal

Make `tickets` and `ticket_comments` the sole production persistence model for
internal Anansi tickets, preventing the compatibility trigger from creating a
second canonical ticket while retaining legacy tables only until SQL 2 removes
them.

## Design

`InternalTicketBackend.create_ticket` becomes a backend identity allocator: it
uses `next_internal_ticket_ref` and returns an internal `BackendTicketResult`,
but does not insert any row. `TicketService.create_ticket` already owns the
canonical intent and activates it with that returned reference, making it the
only creator of the `tickets` row.

`TicketRepository` gains the canonical operations needed by the internal
backend: get status, set status, update summary/description, append a comment,
find the ticket for an escalation, and list open internal tickets for a grid.
The backend delegates those operations to the repository; it does not query
`internal_tickets` or `internal_ticket_comments` directly.

The escalation and Supabase compatibility callers are rewired to canonical
repository/service APIs. The Anansi page already uses the canonical list and
detail readers; its remaining legacy reader helpers are removed once no page
or production caller refers to them.

## Compatibility and safety

- The reference-allocation RPC and `internal_ticket_seq` remain; only the
  legacy table insert is removed.
- Existing internal tickets stay available through SQL 1 backfill and the
  canonical records it created.
- A failed canonical activation is surfaced as ticket creation failure; there
  is never a legacy-only fallback write.
- `internal_tickets` and `internal_ticket_comments` are not dropped here.
  SQL 2 remains the only cleanup migration, after the access search and
  migration invariants prove the cutover complete.

## Verification

Tests cover no legacy table write during internal creation, canonical status,
comments, updates, escalation deduplication, and grid search. A production
access search must return no `internal_tickets` or `internal_ticket_comments`
access outside SQL migrations and explicitly retained test fixtures.
