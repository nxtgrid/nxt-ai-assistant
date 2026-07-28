# Internal Ticket Canonical Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove all production reliance on `internal_tickets` and
`internal_ticket_comments` before the contract migration.

**Architecture:** `TicketService` creates and activates canonical tickets;
`InternalTicketBackend` allocates references and delegates canonical operations
to `TicketRepository`. Legacy relations remain migration-only compatibility
objects until SQL 2.

## Constraints

- Work in `.worktrees/investigate-ticket-view` only.
- Do not change SQL 1 or drop any legacy relation in this workstream.
- Do not infer a backend from reference syntax or legacy-table absence.
- `TicketRepository` is the only writer of `tickets` and `ticket_comments`.
- Use TDD and commit each completed slice.

### Task 1: Add canonical internal-ticket repository operations

**Files:** `ticketing/repository.py`, `tests/services/ticketing/test_repository.py`

1. Write failing tests for `get_status_by_ref`, `set_status_by_ref`,
   `update_by_ref`, `add_comment_by_ref`, `find_ref_for_escalation`, and
   `find_open_internal_by_grid` against `tickets`, `ticket_comments`, and
   `escalations`.
2. Verify the tests fail because the APIs do not exist.
3. Implement each operation using database-side filters and normalized
   `TicketStatus`/`TicketSummary` results. `set_status_by_ref('done')` sets
   `resolved_at`; comments use source `staff` by default.
4. Run repository tests and Ruff.
5. Commit `feat(ticketing): add canonical internal ticket operations`.

### Task 2: Make internal creation reference-only

**Files:** `ticketing/internal_backend.py`, `tests/services/ticketing/test_internal_backend.py`

1. Change the creation tests to assert that only `next_internal_ticket_ref` is
   invoked and no `internal_tickets` insert occurs.
2. Verify the tests fail under the current two-round-trip creator.
3. Remove the insert and return `BackendTicketResult` after successful ref
   allocation. Inject `TicketRepository` into the backend and delegate
   comment/status/update/search/dedup methods to its canonical APIs.
4. Run internal-backend, repository, and TicketService tests; verify no
   production access to either legacy internal relation remains in this module.
5. Commit `refactor(ticketing): make internal backend canonical`.

### Task 3: Rewire escalation and compatibility callers

**Files:** `escalation_service.py`, `supabase_client.py`,
`tests/services/test_escalation_service_ticketing.py`,
`tests/services/test_supabase_client_ticketing.py`

1. Add failing regressions proving escalation deduplication resolves through
   `escalations.ticket_id` and canonical `tickets.backend`, and completion
   changes canonical ticket status.
2. Replace `get_internal_ticket` backend detection and internal-table helper
   mutations with `TicketService`/`TicketRepository` calls. Retire general
   client internal-ticket mutation helpers after callers move.
3. Run focused escalation and Supabase-client tests plus Ruff.
4. Commit `refactor(escalations): remove legacy internal ticket access`.

### Task 4: Remove stale reader implementation and prove the cutoff

**Files:** `anansi_app/services/supabase_reader.py`, relevant Anansi tests

1. Write a static/access test proving the Tickets page and reader use only
   canonical ticket methods for list/detail paths.
2. Delete the unused legacy source-merge and internal-comment helpers, keeping
   only any clearly named migration-test compatibility helpers if required.
3. Run Anansi reader/page tests and Ruff.
4. Run:
   ```bash
   rg -n 'internal_tickets|internal_ticket_comments' chat_orchestrator/orchestrator anansi_app --glob '*.py'
   ```
   Expected: no production access; remaining hits are comments explaining the
   migration or test-only fixtures.
5. Commit `refactor(anansi): remove legacy internal ticket reads`.

### Task 5: Full verification and SQL 2 readiness checkpoint

1. Run ticketing, escalation, callback, notify, and Anansi reader/page suites.
2. Run Ruff over changed orchestrator and Anansi paths.
3. Record the clean legacy-access search and migration invariants as the gate
   for SQL 2. Do not run SQL 2 or refresh the schema snapshot until the live
   schema export is available.
4. Commit only verification/documentation changes if any are needed.
