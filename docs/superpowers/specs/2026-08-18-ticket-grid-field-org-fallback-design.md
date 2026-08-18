# Ticket Grid Field Org Fallback Design

## Goal

Populate the Jira/internal ticket "Grid" field for escalations where the
customer's chat has no registered `(chat_id, topic_id)` match, by falling
back to the customer's organization and, when needed, a mention of the grid
name in their own chat history — without adding latency or a new
conversational round-trip to ticket filing.

## Evidence and root cause

Ticket OPS-3429 shipped with Grid unset. `escalation_service.py`'s grid
resolution has exactly one strategy: an exact match of the customer's own
`(customer_chat_id, customer_topic_id)` against
`grids.internal_telegram_group_chat_id` /
`internal_telegram_group_thread_id`. When `customer_topic_id` is falsy (a
plain DM, or a group not using Telegram's forum/topics feature) the lookup
is skipped entirely — no log line, no fallback. Two production-proven
primitives that could resolve this already exist but aren't used here:
`chat_sessions.organization_id` (persisted on every message by
`resolve_auth.py`, including via its DM/personal-chat fallback path) and
`auth_service.get_grid_names_for_organization(organization_id)`. Neither is
consulted before the ticket is created with a blank Grid.

## Design

### Resolution order

Grid resolution becomes four tiers, evaluated in order, each degrading
silently to the next on failure:

1. **Exact chat/topic match (existing, unchanged).**
2. **Single-grid org.** Read `organization_id` off the chat session already
   fetched for the ticket description, call
   `get_grid_names_for_organization(organization_id)`; if it returns exactly
   one grid, use it.
3. **Text mention.** If the org has 2+ grids, search the chat history
   already fetched for the ticket description — `role == "user"` messages
   only — for a mention of one of the org's grid names. A confident single
   match wins.
4. **Flag for staff.** If still unresolved, create the ticket with Grid
   blank (today's behavior) and attach an internal (non-public) comment
   listing the org's candidate grids, plus a WARNING log line — replacing
   today's silent skip.

### New module: `ticketing/grid_resolution.py`

A small, independently-testable function separate from
`escalation_service.py` (already 3500+ lines, and previously the origin of
a similar extraction into `ticketing/jira_backend.py`):

```python
async def resolve_grid_name(
    *,
    matched_grid_name: str | None,   # Tier-1 result, passed through unchanged
    organization_id: int | None,     # session.organization_id
    messages: list[dict],            # already-fetched chat history
) -> GridResolution:                 # grid_name: str | None, candidates: list[str]
```

It owns tiers 2 and 3 only; tier 1's existing SQL lookup and tier 4's
comment-posting stay in `escalation_service.py`, which already has the
ticket ref and comment-posting call available at the right point.

### Text-matching helper

`shared/utils/grid_matcher.py` gains a second function alongside the
existing `find_best_grid_match`. That function uses `token_sort_ratio`,
suited to comparing two short, comparable-length strings (correcting a
provided grid name against Jira's option list) — not to finding a name
inside a much longer transcript. The new helper uses
`rapidfuzz.fuzz.partial_ratio` instead, scored per candidate grid name
against the joined message text, with:

- threshold 90 (higher than the existing 80, since free text carries more
  incidental partial-match risk than a structured input field), and
- the same ambiguity guard as `find_best_grid_match` — reject if the top two
  candidates score within 10 points of each other.

### Flag-for-staff mechanism

Reuses `TicketService.add_comment(ref, body, public=False)`, which already
resolves to whichever backend (Jira or internal) owns the ref, so no new
per-backend code is needed. Comment body names the organization and lists
its candidate grid names.

## Scope and boundaries

This only changes ticket creation going forward; it does not backfill
existing blank-Grid tickets such as OPS-3429. It does not add any
conversational round-trip — no tier here holds up ticket creation waiting on
a reply, from either the customer or staff. It does not change tier 1's
existing exact-match logic. Grid resolution failures at any tier degrade to
"skip this tier," never to a failed or delayed ticket creation.

## Verification

Unit tests will cover:

1. `grid_matcher`'s new helper: exact mention, fuzzy mention, no mention, an
   ambiguous mention (two grids score within 10 points), and a mention that
   appears only in a `role != "user"` message (must not match).
2. `resolve_grid_name`'s tiers: single-grid org (tier 2 wins), multi-grid
   org with a text match (tier 3 wins), multi-grid org with no match
   (unresolved, candidates returned for flagging), zero-grid org, and a
   missing/unresolvable `organization_id` (degrades to unresolved, no
   crash).
3. `escalation_service.py`'s integration point: tier-1 match still takes
   precedence when present; a flagged (unresolved) ticket gets the internal
   comment and WARNING log; ticket creation still succeeds when every new
   tier raises.
