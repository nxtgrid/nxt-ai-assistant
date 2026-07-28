# Anansi Ticket Schema Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every Anansi-created or Anansi-adopted ticket one durable local record, list it consistently whether Jira is available or not, distinguish customer escalations from operational notifications, and link only to external messages with recorded delivery receipts.

**Architecture:** Introduce `tickets` as the canonical ticket identity and current-state projection, keep escalation, correlation, comments, chat, and outbound delivery as explicit related domains, and make one repository the sole owner of each table. Roll out with one idempotent expand/backfill SQL, a compatibility application release, and one idempotent validate/contract SQL. The final checked-in `db/schema/chat_db.sql` must be regenerated from and structurally compared with the final live `public` schema.

**Tech Stack:** PostgreSQL/Supabase, Python 3.11, supabase-py, FastAPI, NiceGUI, Pydantic, pytest/pytest-asyncio, Ruff, psycopg/psql/pg_dump.

## Global Constraints

- Work only in the isolated worktree:
  `/Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/investigate-ticket-view`.
- Include only Anansi-related tickets: tickets Anansi creates, or existing Jira
  issues Anansi adopts before commenting on, updating, correlating, or otherwise
  managing them. Do not crawl or import the Jira project.
- Never infer a backend from a ticket reference prefix or from the absence of an
  internal-ticket row. Resolve it from `tickets.backend`.
- `TicketRepository` alone writes `tickets`, `ticket_comments`, and
  `chat_messages.ticket_id`; `EscalationRepository` alone writes `escalations`;
  `DeliveryRepository` alone writes `message_deliveries`; `CorrelationStore`
  alone writes correlation tables.
- Preserve exact event-time evidence in correlation events. Do not move all
  histories into a generic JSON event table.
- Preserve ticket visibility through Jira outages. A failed Jira read may leave
  the local projection stale, but must never hide or close the ticket.
- Produce exactly two production DDL artifacts:
  `0005a_ticket_schema_expand_and_backfill.sql` and
  `0005b_ticket_schema_validate_and_contract.sql`.
- Both SQL files must be transactional, idempotent, and usable as clean
  copy-paste scripts in the Supabase SQL editor.
- Use a direct PostgreSQL connection only when a PostgreSQL connection URI or
  Supabase management credential with DDL capability is available. A
  `CHAT_DB_SERVICE_KEY` alone is a PostgREST service role and is not sufficient
  to execute arbitrary DDL.
- Never print database credentials, connection URIs, or service keys in logs,
  tests, terminal output, commits, or documentation.
- Do not run the contract migration until comparison metrics show no recent
  legacy-only writes and all migration invariants pass.
- Use database-side filtering, ordering, counting, and pagination for the
  Tickets list. Do not restore the current per-source fetch cap and Python merge.
- Render Telegram links only from `message_deliveries`; never synthesize a
  message link from a chat session or a ticket reference.
- Keep commits task-scoped. Run each task's focused tests before its commit.

## Target File Structure

New files:

```text
db/migrations/
├── 0005a_ticket_schema_expand_and_backfill.sql
└── 0005b_ticket_schema_validate_and_contract.sql

chat_orchestrator/orchestrator/services/
├── escalation_repository.py
└── ticketing/
    ├── delivery_repository.py
    └── repository.py

chat_orchestrator/tests/
├── test_ticket_schema_expand_migration.py
├── test_ticket_schema_contract_migration.py
├── test_chat_db_schema_snapshot.py
└── services/
    ├── test_escalation_repository.py
    ├── test_jira_webhooks.py
    └── ticketing/
        ├── test_delivery_repository.py
        ├── test_repository.py
        └── test_reconciliation.py
```

Modified files:

```text
db/schema/chat_db.sql
chat_orchestrator/orchestrator/api/app.py
chat_orchestrator/orchestrator/services/callback_handlers.py
chat_orchestrator/orchestrator/services/escalation_service.py
chat_orchestrator/orchestrator/services/jira_webhooks.py
chat_orchestrator/orchestrator/services/supabase_client.py
chat_orchestrator/orchestrator/services/ticketing/backend.py
chat_orchestrator/orchestrator/services/ticketing/correlation_render.py
chat_orchestrator/orchestrator/services/ticketing/correlation_store.py
chat_orchestrator/orchestrator/services/ticketing/correlator.py
chat_orchestrator/orchestrator/services/ticketing/internal_backend.py
chat_orchestrator/orchestrator/services/ticketing/jira_backend.py
chat_orchestrator/orchestrator/services/ticketing/service.py
chat_orchestrator/tests/api/test_notify_ticketing.py
chat_orchestrator/tests/services/test_callback_handlers_ticketing.py
chat_orchestrator/tests/services/test_escalation_service_ticketing.py
chat_orchestrator/tests/services/ticketing/test_correlation_render.py
chat_orchestrator/tests/services/ticketing/test_correlation_store.py
chat_orchestrator/tests/services/ticketing/test_correlator.py
chat_orchestrator/tests/services/ticketing/test_internal_backend.py
chat_orchestrator/tests/services/ticketing/test_jira_backend.py
chat_orchestrator/tests/services/ticketing/test_service.py
anansi_app/services/supabase_reader.py
anansi_app/nicegui_app/pages/tickets.py
anansi_app/tests/test_supabase_reader_tickets.py
anansi_app/tests/test_tickets_page.py
```

---

### Task 1: Add the expand-migration test harness and canonical schema

**Files:**

- Create: `chat_orchestrator/tests/test_ticket_schema_expand_migration.py`
- Create: `db/migrations/0005a_ticket_schema_expand_and_backfill.sql`
- Reference: `db/schema/chat_db.sql`
- Reference: `db/migrations/0001_jira_optional_ticket_backend.sql`
- Reference: `db/migrations/0002_internal_ticket_ref_allocation.sql`

**Interfaces and invariants:**

- Final new relations created by SQL 1: `tickets`, `escalations`,
  `ticket_comments`, `message_deliveries`, and `ticket_list_view`.
- Existing `chat_messages`, `ticket_correlations`, and
  `ticket_correlation_events` receive nullable `ticket_id` columns during
  expansion.
- `tickets.ticket_ref` is uniquely indexed when non-null.
- An `active` ticket requires non-null `ticket_ref`, `backend`, and
  `activated_at`.
- A delivery requires at least one of `ticket_id` or `escalation_id`.
- `(channel, external_chat_id, external_message_id)` is unique.
- SQL 1 does not drop a legacy table or legacy column.

- [ ] **Step 1: Write static migration-contract tests**

  Add tests that read SQL 1 as text and assert:

  ```python
  REQUIRED_RELATIONS = (
      "tickets",
      "escalations",
      "ticket_comments",
      "message_deliveries",
  )

  def test_expand_migration_is_transactional_and_non_destructive():
      sql = MIGRATION.read_text()
      assert sql.lstrip().startswith("BEGIN;")
      assert sql.rstrip().endswith("COMMIT;")
      assert "DROP TABLE internal_tickets" not in sql
      assert "DROP TABLE escalation_mappings" not in sql
      assert "DROP COLUMN ticket_ref" not in sql
  ```

  Also assert that all four relations, `ticket_list_view`, the partial unique
  ticket-ref index, all check constraints, and all required indexes occur in
  the script.

- [ ] **Step 2: Run the focused test and confirm it fails**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/test_ticket_schema_expand_migration.py -q
  ```

  Expected: failure because SQL 1 does not exist.

- [ ] **Step 3: Add a scratch-Postgres runner to the test**

  Reuse the `initdb`/`pg_ctl`/`createdb`/`psql` pattern from
  `test_internal_ticket_ref_allocation_migration.py`. Seed the minimum legacy
  schema needed by SQL 1, apply SQL 1 twice, and query `pg_catalog` for:

  - relation and view existence;
  - column names and nullability;
  - foreign keys and check constraints;
  - indexes;
  - `updated_at` trigger; and
  - retained legacy tables and columns.

  Mark only the live-Postgres test with `skipif` when the four PostgreSQL
  binaries are unavailable. Static tests must always run.

- [ ] **Step 4: Implement the structural half of SQL 1**

  Start the file with `BEGIN;`, use `CREATE TABLE IF NOT EXISTS`, and use
  guarded `DO $$ ... $$` blocks for constraints that PostgreSQL cannot create
  with `IF NOT EXISTS`.

  Define `tickets` exactly as approved:

  ```sql
  CREATE TABLE IF NOT EXISTS public.tickets (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      ticket_ref text,
      backend text,
      created_via text NOT NULL,
      provisioning_state text NOT NULL DEFAULT 'pending',
      status text NOT NULL DEFAULT 'open',
      backend_status text,
      summary text NOT NULL,
      description text,
      ticket_type text,
      organization_id integer,
      grid_name text,
      assignee_email text,
      labels jsonb NOT NULL DEFAULT '[]'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      activated_at timestamptz,
      updated_at timestamptz NOT NULL DEFAULT now(),
      resolved_at timestamptz,
      backend_synced_at timestamptz
  );
  ```

  Add the exact approved enumerated checks, active-ticket check, partial unique
  index, query indexes, and shared `updated_at` trigger. Create the remaining
  new tables with the exact columns and constraints from the design spec.

  Use final table names in SQL 1. Do not create generic `*_v2` tables.

- [ ] **Step 5: Add temporary legacy-to-canonical capture triggers**

  To close the gap between running SQL 1 and deploying the new application,
  add one-way, idempotent triggers that mirror subsequent legacy writes:

  - `internal_tickets` → `tickets`;
  - `internal_ticket_comments` → `ticket_comments`; and
  - `escalation_mappings` → `escalations`.

  The trigger functions must use the same deterministic ticket lookup and
  provenance rules as the bulk backfill in Task 2. They are rollout
  compatibility infrastructure, not permanent domain logic, and SQL 2 will
  remove them.

- [ ] **Step 6: Create the initial `ticket_list_view`**

  Make the view return one row per ticket with:

  ```text
  id, ticket_ref, backend, created_via, provisioning_state, status,
  backend_status, summary, ticket_type, organization_id, grid_name,
  assignee_email, created_at, updated_at, resolved_at, backend_synced_at,
  escalation_count, has_escalation, activity_count, affected_count,
  occurrence_count, latest_activity_at
  ```

  Use pre-aggregated subqueries or lateral aggregates so comments, messages,
  deliveries, and escalations cannot multiply one another's counts.

- [ ] **Step 7: Run focused tests**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/test_ticket_schema_expand_migration.py -q
  ```

  Expected: static tests pass; live tests pass or skip only when PostgreSQL
  binaries are unavailable.

- [ ] **Step 8: Commit**

  ```bash
  git add db/migrations/0005a_ticket_schema_expand_and_backfill.sql \
    chat_orchestrator/tests/test_ticket_schema_expand_migration.py
  git commit -m "feat(db): add canonical ticket schema expansion"
  ```

---

### Task 2: Backfill all recoverable Anansi ticket history

**Files:**

- Modify: `db/migrations/0005a_ticket_schema_expand_and_backfill.sql`
- Modify: `chat_orchestrator/tests/test_ticket_schema_expand_migration.py`

**Backfill precedence:**

1. `internal_tickets` proves an internal ticket and its source.
2. An `escalation_mappings` relationship proves `created_via='escalation'`
   unless the internal source is more specific.
3. A correlation decision that selected an already-existing Jira candidate
   proves `created_via='adopted'`.
4. Explicit notify creation evidence proves `created_via='notification'`.
5. Correlation-only Jira history without sufficient evidence is `legacy`.

- [ ] **Step 1: Add representative legacy fixtures**

  Seed the scratch database with:

  - one escalation-created internal ticket;
  - one notification-created internal ticket;
  - one Jira escalation present in `escalation_mappings`;
  - one Jira notification present only in `ticket_correlations`;
  - one adopted Jira candidate proved by a correlation event;
  - one ambiguous correlation-only Jira ticket;
  - duplicate references across mapping/correlation sources;
  - internal and Jira comments;
  - tagged `chat_messages.metadata.ticket_ref` rows;
  - escalation and notification Telegram coordinates with provable chat/message
    IDs; and
  - one incomplete coordinate set that must not become a delivery.

- [ ] **Step 2: Write backfill assertions**

  Assert:

  ```python
  assert scalar("select count(*) from tickets") == "6"
  assert scalar("""
      select count(*) from tickets
      where provisioning_state = 'active'
        and (ticket_ref is null or backend is null or activated_at is null)
  """) == "0"
  assert scalar("""
      select count(*) from (
        select ticket_ref from tickets
        where ticket_ref is not null
        group by ticket_ref having count(*) > 1
      ) duplicates
  """) == "0"
  ```

  Add exact per-ref assertions for `backend`, `created_via`, normalized
  `status`, relationship backfills, comments, chat-message FKs, correlations,
  correlation events, and delivery receipt inclusion/exclusion.

- [ ] **Step 3: Run the test and confirm the new assertions fail**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/test_ticket_schema_expand_migration.py -q
  ```

  Expected: schema applies, but backfill counts and relationships fail.

- [ ] **Step 4: Implement deterministic ticket backfill**

  Build a CTE that unions legacy candidates, ranks evidence per `ticket_ref`,
  and inserts one canonical row. Use `INSERT ... ON CONFLICT (ticket_ref)
  WHERE ticket_ref IS NOT NULL DO UPDATE` only to fill or improve canonical
  fields; do not overwrite a known origin with `legacy`.

  Map statuses to `open`, `in_progress`, or `done`. For legacy Jira rows without
  a trustworthy remote status, use `open` unless a resolved timestamp proves
  `done`, and leave `backend_synced_at` null.

- [ ] **Step 5: Implement relationship and receipt backfills**

  In dependency order:

  1. upsert `escalations` and resolve `ticket_id`;
  2. insert `ticket_comments`;
  3. set `chat_messages.ticket_id` from metadata refs;
  4. set correlation `ticket_id` values;
  5. set correlation-event `ticket_id` values where a ref resolves; and
  6. insert deliveries only when the full external destination and message ID
     are known.

  Use `ON CONFLICT` or `NOT EXISTS` guards so a second SQL 1 run creates no
  duplicates.

- [ ] **Step 6: Add in-transaction invariant failures**

  End SQL 1 with a `DO $$` validation block that raises exceptions for:

  - duplicate non-null ticket refs;
  - active tickets missing ref/backend/activation;
  - legacy internal tickets without canonical rows;
  - known legacy Jira relationships without canonical rows;
  - escalation rows whose known ticket ref failed to resolve;
  - resolvable chat metadata without `ticket_id`;
  - resolvable correlations without `ticket_id`; and
  - duplicate delivery receipt identities.

  Do not fail for legitimately ambiguous event rows with `ticket_id IS NULL`.

- [ ] **Step 7: Verify idempotency**

  Apply SQL 1 twice in the scratch test and assert the full row-count tuple is
  unchanged:

  ```text
  tickets, escalations, ticket_comments, message_deliveries,
  linked chat_messages, linked correlations, linked correlation events
  ```

- [ ] **Step 8: Run focused tests**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/test_ticket_schema_expand_migration.py -q
  ```

  Expected: all pass or the live test skips for missing PostgreSQL binaries.

- [ ] **Step 9: Commit**

  ```bash
  git add db/migrations/0005a_ticket_schema_expand_and_backfill.sql \
    chat_orchestrator/tests/test_ticket_schema_expand_migration.py
  git commit -m "feat(db): backfill canonical Anansi tickets"
  ```

---

### Task 3: Introduce typed ticket and delivery repositories

**Files:**

- Create: `chat_orchestrator/orchestrator/services/ticketing/repository.py`
- Create: `chat_orchestrator/orchestrator/services/ticketing/delivery_repository.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/backend.py`
- Create: `chat_orchestrator/tests/services/ticketing/test_repository.py`
- Create: `chat_orchestrator/tests/services/ticketing/test_delivery_repository.py`

**Public interfaces:**

```python
class BackendTicketResult(BaseModel):
    ref: str
    backend: TicketBackendName
    url: str | None = None
    ticket_type: str | None = None

class TicketResult(BackendTicketResult):
    ticket_id: str

class TicketRecord(BaseModel):
    id: str
    ticket_ref: str | None
    backend: TicketBackendName | None
    created_via: Literal["escalation", "notification", "adopted", "legacy"]
    provisioning_state: Literal["pending", "active", "failed"]
    status: Literal["open", "in_progress", "done"]
    backend_status: str | None = None
    summary: str
    description: str | None = None
    ticket_type: str | None = None
    organization_id: int | None = None
    grid_name: str | None = None
    assignee_email: str | None = None
    labels: list[str] = Field(default_factory=list)

class TicketRepository:
    async def create_intent(
        self, req: TicketCreateRequest, *, created_via: str
    ) -> TicketRecord: ...
    async def activate(
        self, ticket_id: str, result: BackendTicketResult, *,
        backend_status: str = "open"
    ) -> TicketRecord: ...
    async def adopt_jira(
        self, *, ref: str, summary: str, description: str = "",
        ticket_type: str | None = None, organization_id: int | None = None,
        grid_name: str | None = None, labels: list[str] | None = None
    ) -> TicketRecord: ...
    async def get_by_id(self, ticket_id: str) -> TicketRecord | None: ...
    async def get_by_ref(self, ref: str) -> TicketRecord | None: ...
    async def set_pending_backend(
        self, ticket_id: str, backend: TicketBackendName
    ) -> None: ...
    async def mark_failed(self, ticket_id: str, reason: str) -> None: ...
    async def update_projection(
        self, ticket_id: str, *, status: str, backend_status: str,
        summary: str | None = None, description: str | None = None
    ) -> None: ...
    async def list_pending_jira(self, limit: int = 100) -> list[TicketRecord]: ...
    async def add_comment(
        self, ticket_id: str, *, body: str, author: str | None,
        is_public: bool, source: str, backend_comment_id: str | None = None
    ) -> None: ...
    async def attach_chat_message(self, message_id: str, ticket_id: str) -> None: ...

class DeliveryRepository:
    async def record(
        self, *, ticket_id: str | None, escalation_id: str | None,
        purpose: Literal["escalation", "notification", "update"],
        external_chat_id: str, external_topic_id: str | None,
        external_message_id: int, chat_message_id: str | None = None
    ) -> dict[str, Any]: ...
    async def attach_ticket(self, delivery_id: str, ticket_id: str) -> None: ...
    async def attach_ticket_for_escalation(
        self, escalation_id: str, ticket_id: str
    ) -> None: ...
    async def first_notification(self, ticket_id: str) -> dict[str, Any] | None: ...
```

- [ ] **Step 1: Write repository contract tests**

  Use the project's fluent Supabase fake pattern. Test exact table names and
  payloads for:

  - pending intent creation;
  - activation of the same UUID;
  - adoption idempotency on `ticket_ref`;
  - lookup by ID and ref;
  - persisting the intended backend while a row is pending;
  - normalized status projection and `backend_synced_at`;
  - failed provisioning without inventing a ref;
  - formal comment insertion;
  - chat-message FK attachment;
  - idempotent delivery upsert; and
  - first notification ordered by `sent_at`.

- [ ] **Step 2: Run tests and confirm import failures**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/ticketing/test_repository.py \
    tests/services/ticketing/test_delivery_repository.py -q
  ```

  Expected: collection fails because repository modules do not exist.

- [ ] **Step 3: Split backend and service result types**

  Change `TicketBackend.create_ticket` to return `BackendTicketResult`.
  Preserve `TicketResult` as the application-facing result and add the
  canonical `ticket_id`.

  Update backend test fixtures and mocks to return `BackendTicketResult`.
  Do not change `TicketCreateOutcome.result`; it remains
  `TicketResult | None`.

- [ ] **Step 4: Implement `TicketRepository`**

  Accept the same lazy raw-client getter shape used by current ticket backends.
  Centralize Supabase response parsing and translate client/database failures
  to `TicketRepositoryError`.

  `create_intent` must derive `created_via` from the caller, not from backend
  availability. `activate` must update the existing row, never insert a second
  row. `adopt_jira` must use an upsert on `ticket_ref` and return the existing
  canonical ID on retries.

- [ ] **Step 5: Implement `DeliveryRepository`**

  Use the database uniqueness constraint for idempotency. Reject calls with
  both `ticket_id` and `escalation_id` absent before touching Supabase.
  Treat external IDs as strings/integers exactly as declared; do not parse a
  Telegram URL in the repository.

- [ ] **Step 6: Run focused tests and lint**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/ticketing/test_repository.py \
    tests/services/ticketing/test_delivery_repository.py -q
  ruff check orchestrator/services/ticketing/backend.py \
    orchestrator/services/ticketing/repository.py \
    orchestrator/services/ticketing/delivery_repository.py
  ```

  Expected: all pass.

- [ ] **Step 7: Commit**

  ```bash
  git add chat_orchestrator/orchestrator/services/ticketing/backend.py \
    chat_orchestrator/orchestrator/services/ticketing/repository.py \
    chat_orchestrator/orchestrator/services/ticketing/delivery_repository.py \
    chat_orchestrator/tests/services/ticketing/test_repository.py \
    chat_orchestrator/tests/services/ticketing/test_delivery_repository.py
  git commit -m "feat(ticketing): add canonical ticket repositories"
  ```

---

### Task 4: Make ticket creation durable across Jira availability and fallback

**Files:**

- Modify: `chat_orchestrator/orchestrator/services/ticketing/service.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/internal_backend.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/jira_backend.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_service.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_internal_backend.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_jira_backend.py`
- Create: `chat_orchestrator/tests/services/ticketing/test_reconciliation.py`

**Creation contract:**

- `TicketService` creates the pending local intent before any backend call.
- The stable Jira label is exactly `anansi-ticket-<ticket UUID>`.
- Jira-to-internal fallback activates the same pending ticket row.
- All operations on an existing ref resolve `tickets.backend`.
- A remote Jira success followed by local activation failure leaves a pending
  record recoverable by the stable label.

- [ ] **Step 1: Write service tests for intent-first creation**

  Add tests for call order and identity:

  ```python
  assert calls == [
      ("repo.create_intent", "notification"),
      ("jira.create_ticket", f"anansi-ticket-{ticket_id}"),
      ("repo.activate", ticket_id, "OPS-123"),
  ]
  assert result.ticket_id == ticket_id
  ```

  Cover escalation origin, notification origin, configured internal backend,
  Jira failure with internal fallback, both backends failing, activation
  failure after Jira success, and existing-ref routing.

- [ ] **Step 2: Write backend tests for the new division of responsibility**

  Assert `InternalTicketBackend.create_ticket` calls only
  `next_internal_ticket_ref` and returns `BackendTicketResult`; it must not
  insert into `internal_tickets` or `tickets`.

  Assert `JiraTicketBackend` preserves caller labels and returns
  `BackendTicketResult`. Add a search helper test for the exact stable UUID
  label.

- [ ] **Step 3: Run the tests and confirm failures**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/ticketing/test_service.py \
    tests/services/ticketing/test_internal_backend.py \
    tests/services/ticketing/test_jira_backend.py -q
  ```

  Expected: failures because creation currently delegates before recording an
  intent and the internal backend inserts its own row.

- [ ] **Step 4: Refactor creation orchestration**

  Inject `TicketRepository` into `TicketService`. Map
  `TicketCreateRequest.source` as:

  ```python
  created_via = {
      "escalation": "escalation",
      "notify": "notification",
  }[req.source]
  ```

  After creating the intent, copy the request with:

  ```python
  stable_label = f"anansi-ticket-{intent.id}"
  backend_request = req.model_copy(
      update={"labels": [*req.labels, stable_label]}
  )
  ```

  Deduplicate the label before the copy. Immediately after backend resolution
  and before the backend create call, persist the selected backend with
  `TicketRepository.set_pending_backend`. If Jira fails and internal fallback
  is allowed, set that same pending row to `backend='internal'` before calling
  the internal backend. Activate using the backend result and return a
  `TicketResult(ticket_id=intent.id, ...)`.

- [ ] **Step 5: Remove missing-row backend inference**

  Replace `_backend_for_ref`'s `internal_tickets` probe with
  `TicketRepository.get_by_ref`. Raise a typed, logged not-found error when an
  unmanaged ref reaches an existing-ticket mutation. Adoption is explicit and
  occurs before that call.

  Successful `get_status`, `add_comment`, `update_ticket`, and
  `transition_to_done` calls update the canonical projection or comment record
  through `TicketRepository`.

- [ ] **Step 6: Add bounded reconciliation**

  Add:

  ```python
  async def reconcile_pending_jira(self, limit: int = 100) -> dict[str, int]:
      ...
  ```

  For each pending intent whose attempted backend was Jira, search Jira by
  `anansi-ticket-<uuid>`. Activate one exact match. Leave a recent no-match
  pending for retry; mark an expired no-match or an ambiguous multi-match
  failed and log structured counts. Never create a second Jira issue during
  reconciliation.

  The pending row needs its intended backend available to this query. Store
  `backend='jira'` while pending; the active-ticket check, not backend
  nullability, governs provisioning validity.

- [ ] **Step 7: Run focused tests and lint**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/ticketing/test_service.py \
    tests/services/ticketing/test_internal_backend.py \
    tests/services/ticketing/test_jira_backend.py \
    tests/services/ticketing/test_reconciliation.py -q
  ruff check orchestrator/services/ticketing tests/services/ticketing
  ```

  Expected: all pass.

- [ ] **Step 8: Commit**

  ```bash
  git add chat_orchestrator/orchestrator/services/ticketing \
    chat_orchestrator/tests/services/ticketing
  git commit -m "refactor(ticketing): persist backend-neutral ticket intent"
  ```

---

### Task 5: Give escalation lifecycle one repository and explicit states

**Files:**

- Create: `chat_orchestrator/orchestrator/services/escalation_repository.py`
- Modify: `chat_orchestrator/orchestrator/services/escalation_service.py`
- Modify: `chat_orchestrator/orchestrator/services/callback_handlers.py`
- Modify: `chat_orchestrator/orchestrator/services/supabase_client.py`
- Create: `chat_orchestrator/tests/services/test_escalation_repository.py`
- Modify: `chat_orchestrator/tests/services/test_escalation_service_ticketing.py`
- Modify: `chat_orchestrator/tests/services/test_callback_handlers_ticketing.py`

**Public interface:**

```python
class EscalationRepository:
    async def create(self, ...) -> dict[str, Any]: ...
    async def get(self, escalation_id: str) -> dict[str, Any] | None: ...
    async def claim(self, escalation_id: str) -> dict[str, Any] | None: ...
    async def attach_ticket(
        self, escalation_id: str, ticket_id: str
    ) -> None: ...
    async def release(self, escalation_id: str) -> None: ...
    async def resolve(self, escalation_id: str) -> None: ...
    async def has_blocking_escalation(self, chat_session_id: str) -> bool: ...
```

State transitions:

```text
create -> open
open --claim--> processing
processing --attach_ticket--> tracked
processing --release on failure--> open
open|processing|tracked --resolve--> resolved
```

- [ ] **Step 1: Write atomic state-transition tests**

  Assert `claim` uses one conditional update equivalent to:

  ```sql
  UPDATE escalations
  SET state = 'processing'
  WHERE id = :id AND state = 'open'
  RETURNING *;
  ```

  Cover unsuccessful double claim, attach, release, resolve, and session-level
  blocking-state derivation.

- [ ] **Step 2: Run tests and confirm import failure**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/test_escalation_repository.py -q
  ```

  Expected: module import fails.

- [ ] **Step 3: Implement `EscalationRepository`**

  Keep every direct `escalations` table access in this module. Convert database
  failures to `EscalationRepositoryError`; return `None` from `claim` only when
  the row was absent or no longer open.

- [ ] **Step 4: Rewire escalation creation and tracking**

  Replace direct `escalation_mappings` writes in `EscalationService`,
  `callback_handlers`, and ticket-stamping code with repository calls.

  Ticket creation/adoption must return `ticket_id`; then:

  1. attach the ticket to the claimed escalation;
  2. move it to `tracked`; and
  3. ask `DeliveryRepository` to attach the same ticket to the escalation's
     receipt.

  If ticketing fails, call `release`, returning the escalation to `open`.

- [ ] **Step 5: Remove ticket/escalation writes from the general client**

  Delete or reduce the ticket and escalation mutation helpers in
  `EnhancedSupabaseClient`. Chat/session methods may remain. Search for and
  replace every production call before deleting a helper.

  Do not remove legacy database relations yet; that occurs in SQL 2.

- [ ] **Step 6: Replace session escalation flags**

  Any behavior currently reading `chat_sessions.is_escalated` must call
  `has_blocking_escalation`. Preserve the current non-blocking-reason behavior
  in service logic, but derive durable blocking state only from
  `escalations.state IN ('open', 'processing')`.

- [ ] **Step 7: Run focused tests and legacy-access search**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/test_escalation_repository.py \
    tests/services/test_escalation_service_ticketing.py \
    tests/services/test_callback_handlers_ticketing.py -q
  rg -n 'table\("escalation_mappings"\)|table\('\''escalation_mappings'\''\)' \
    orchestrator
  ```

  Expected: tests pass; production search has no direct access outside an
  explicitly documented rollout compatibility check.

- [ ] **Step 8: Commit**

  ```bash
  git add chat_orchestrator/orchestrator/services/escalation_repository.py \
    chat_orchestrator/orchestrator/services/escalation_service.py \
    chat_orchestrator/orchestrator/services/callback_handlers.py \
    chat_orchestrator/orchestrator/services/supabase_client.py \
    chat_orchestrator/tests/services/test_escalation_repository.py \
    chat_orchestrator/tests/services/test_escalation_service_ticketing.py \
    chat_orchestrator/tests/services/test_callback_handlers_ticketing.py
  git commit -m "refactor(escalations): centralize explicit lifecycle state"
  ```

---

### Task 6: Record real Telegram delivery receipts and ticket-message relationships

**Files:**

- Modify: `chat_orchestrator/orchestrator/api/app.py`
- Modify: `chat_orchestrator/orchestrator/services/escalation_service.py`
- Modify: `chat_orchestrator/orchestrator/services/callback_handlers.py`
- Modify: `chat_orchestrator/orchestrator/services/supabase_client.py`
- Modify: `chat_orchestrator/tests/api/test_notify_ticketing.py`
- Modify: `chat_orchestrator/tests/services/test_escalation_service_ticketing.py`
- Modify: `chat_orchestrator/tests/services/test_callback_handlers_ticketing.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_delivery_repository.py`

- [ ] **Step 1: Add notification delivery tests**

  Cover:

  - a successful `/notify` send records one `purpose='notification'` receipt;
  - retrying the same external message ID is idempotent;
  - a notify target without a chat session still records a receipt;
  - a persisted chat message gets both `chat_messages.ticket_id` and
    `message_deliveries.chat_message_id`;
  - a failed Telegram send records no receipt; and
  - an update/amendment uses `purpose='update'`.

- [ ] **Step 2: Add escalation delivery tests**

  Assert the initial staff escalation message records a receipt with
  `escalation_id` and no ticket ID. Tracking the escalation attaches the
  canonical ticket ID to that same receipt.

- [ ] **Step 3: Run focused tests and confirm failures**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/api/test_notify_ticketing.py \
    tests/services/test_escalation_service_ticketing.py \
    tests/services/test_callback_handlers_ticketing.py \
    tests/services/ticketing/test_delivery_repository.py -q
  ```

  Expected: new receipt assertions fail.

- [ ] **Step 4: Record receipts at the successful send boundary**

  Call `DeliveryRepository.record` only after Telegram returns the external
  message ID. Pass the exact destination chat ID and topic ID used for the
  send. If local receipt recording fails after a successful send, log the
  external identity at error level for reconciliation without retrying the
  external send.

- [ ] **Step 5: Replace JSON ticket tags with the FK**

  Replace `tag_message_as_ticket_comment(message_id, ticket_ref)` calls with
  `TicketRepository.attach_chat_message(message_id, ticket_id)`.

  Formal backend comments go to `ticket_comments`. Conversation messages
  remain only in `chat_messages`; do not duplicate their content into
  `ticket_comments`.

- [ ] **Step 6: Make reply threading read delivery receipts**

  Replace cached correlation Telegram coordinates with
  `DeliveryRepository.first_notification(ticket_id)`. If no receipt exists,
  send a fresh message without a reply target and record its new receipt.

- [ ] **Step 7: Run focused tests and search for JSON relationship writes**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/api/test_notify_ticketing.py \
    tests/services/test_escalation_service_ticketing.py \
    tests/services/test_callback_handlers_ticketing.py \
    tests/services/ticketing/test_delivery_repository.py -q
  rg -n 'ticket_ref|ticket_role' orchestrator/services/supabase_client.py \
    orchestrator/api/app.py orchestrator/services
  ```

  Expected: tests pass; remaining `ticket_ref` occurrences are ticket API
  values, not `chat_messages.metadata` relationship writes.

- [ ] **Step 8: Commit**

  ```bash
  git add chat_orchestrator/orchestrator/api/app.py \
    chat_orchestrator/orchestrator/services/escalation_service.py \
    chat_orchestrator/orchestrator/services/callback_handlers.py \
    chat_orchestrator/orchestrator/services/supabase_client.py \
    chat_orchestrator/tests/api/test_notify_ticketing.py \
    chat_orchestrator/tests/services/test_escalation_service_ticketing.py \
    chat_orchestrator/tests/services/test_callback_handlers_ticketing.py \
    chat_orchestrator/tests/services/ticketing/test_delivery_repository.py
  git commit -m "feat(ticketing): persist external message deliveries"
  ```

---

### Task 7: Key alert correlation by canonical ticket ID

**Files:**

- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_store.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlator.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_render.py`
- Modify: `chat_orchestrator/orchestrator/api/app.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlation_store.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlator.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlation_render.py`
- Modify: `chat_orchestrator/tests/api/test_notify_ticketing.py`

**Final correlation ownership:**

- Mutable correlation state is keyed by `ticket_id`.
- Current ticket backend, ref, summary, status, organization, and grid come from
  `TicketRepository`.
- Reply destinations come from `DeliveryRepository`.
- Correlation events retain event-time evidence and nullable `ticket_id`.

- [ ] **Step 1: Rewrite store contract tests around `ticket_id`**

  Replace ref-key assertions with UUID-key assertions. Explicitly assert that
  correlation write payloads do not contain:

  ```text
  ticket_ref, ticket_backend, grid_name, organization_id, summary_current,
  status, telegram_chat_id, telegram_topic_id, telegram_message_id
  ```

- [ ] **Step 2: Add adoption tests**

  When candidate discovery returns a Jira ref missing from `tickets`, assert:

  1. `TicketRepository.adopt_jira` runs first;
  2. subsequent comment/update receives the canonical ticket;
  3. correlation state is written with its `ticket_id`; and
  4. a second run reuses the same canonical row.

- [ ] **Step 3: Run focused tests and confirm failures**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/ticketing/test_correlation_store.py \
    tests/services/ticketing/test_correlator.py \
    tests/services/ticketing/test_correlation_render.py -q
  ```

  Expected: failures because store and render paths still depend on refs and
  cached Telegram coordinates.

- [ ] **Step 4: Refactor `CorrelationStore`**

  Make all mutable-state methods accept `ticket_id`. Make event methods accept
  `ticket_id: str | None`. Remove methods that update status, summary, backend,
  grid, organization, or Telegram coordinates in the correlation row.

- [ ] **Step 5: Refactor correlator and renderer**

  Resolve current ticket fields through `TicketRepository`. Adopt an unmanaged
  Jira candidate before the first Anansi mutation. Obtain reply targets through
  `DeliveryRepository`.

  Keep candidate refs and model evidence in the event record because those are
  facts about the decision at that time.

- [ ] **Step 6: Run focused tests and lint**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/ticketing/test_correlation_store.py \
    tests/services/ticketing/test_correlator.py \
    tests/services/ticketing/test_correlation_render.py \
    tests/api/test_notify_ticketing.py -q
  ruff check orchestrator/services/ticketing orchestrator/api/app.py
  ```

  Expected: all pass.

- [ ] **Step 7: Commit**

  ```bash
  git add chat_orchestrator/orchestrator/services/ticketing \
    chat_orchestrator/orchestrator/api/app.py \
    chat_orchestrator/tests/services/ticketing \
    chat_orchestrator/tests/api/test_notify_ticketing.py
  git commit -m "refactor(ticketing): key alert correlation by ticket id"
  ```

---

### Task 8: Synchronize Jira projections without losing outage visibility

**Files:**

- Modify: `chat_orchestrator/orchestrator/services/ticketing/service.py`
- Modify: `chat_orchestrator/orchestrator/services/jira_webhooks.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_service.py`
- Create: `chat_orchestrator/tests/services/test_jira_webhooks.py`

- [ ] **Step 1: Add projection tests**

  Cover:

  - successful Jira status read updates normalized status, raw status, and
    `backend_synced_at`;
  - successful comment/update/transition refreshes the projection;
  - Jira webhook status changes resolve the canonical ticket by ref;
  - unknown Jira webhook issues are ignored unless the webhook represents an
    action already initiated by Anansi;
  - Jira timeout leaves the prior local projection unchanged; and
  - Jira done status sets `resolved_at`, while reopening clears it.

- [ ] **Step 2: Run focused tests and confirm failures**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/ticketing/test_service.py \
    tests/services/test_jira_webhooks.py -q
  ```

  Expected: projection assertions fail.

- [ ] **Step 3: Centralize Jira status normalization**

  Add one pure mapping function used by reads and webhooks:

  ```python
  def normalize_ticket_status(raw_status: str, is_done: bool) -> str:
      if is_done:
          return "done"
      if raw_status.strip().lower() in {"in progress", "in_progress"}:
          return "in_progress"
      return "open"
  ```

  Extend only when existing Jira configuration proves additional in-progress
  names. Do not treat an unknown status as done.

- [ ] **Step 4: Update projections after successful backend operations**

  Call `TicketRepository.update_projection` after a trustworthy remote result.
  On exceptions, return/log the backend failure and preserve the last local
  state. Webhooks resolve only already-managed refs.

- [ ] **Step 5: Run focused tests and lint**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/services/ticketing/test_service.py \
    tests/services/test_jira_webhooks.py -q
  ruff check orchestrator/services/ticketing/service.py \
    orchestrator/services/jira_webhooks.py
  ```

  Expected: all pass.

- [ ] **Step 6: Commit**

  ```bash
  git add chat_orchestrator/orchestrator/services/ticketing/service.py \
    chat_orchestrator/orchestrator/services/jira_webhooks.py \
    chat_orchestrator/tests/services/ticketing/test_service.py \
    chat_orchestrator/tests/services/test_jira_webhooks.py
  git commit -m "feat(ticketing): synchronize durable Jira projections"
  ```

---

### Task 9: Replace the Tickets page merge with the canonical read model

**Files:**

- Modify: `anansi_app/services/supabase_reader.py`
- Modify: `anansi_app/nicegui_app/pages/tickets.py`
- Modify: `anansi_app/tests/test_supabase_reader_tickets.py`
- Modify: `anansi_app/tests/test_tickets_page.py`

**Reader interface:**

```python
@dataclass(frozen=True)
class TicketPage:
    items: list[dict[str, Any]]
    total: int

def list_tickets(
    self, *,
    page: int = 1,
    page_size: int = 50,
    status: str | None = None,
    backend: str | None = None,
    created_via: str | None = None,
    has_escalation: bool | None = None,
    search: str | None = None,
) -> TicketPage: ...

def get_ticket_detail(self, ticket_id: str) -> dict[str, Any] | None: ...
```

- [ ] **Step 1: Replace reader fixtures with canonical rows**

  Seed `ticket_list_view`, `tickets`, `escalations`, `ticket_comments`,
  `chat_messages`, and `message_deliveries`. Extend the fluent fake to support
  `select(..., count="exact")`, `ilike`, `or_`, and range-after-filter
  semantics matching supabase-py.

- [ ] **Step 2: Write pagination and filtering tests**

  Assert:

  - only `ticket_list_view` is queried for the list;
  - range is `(page - 1) * page_size` through `start + page_size - 1`;
  - total comes from the database count;
  - status/backend/origin/escalation filters are sent to the query;
  - search matches ref or summary;
  - ordering is `latest_activity_at DESC`;
  - Jira notification and adopted tickets appear without an escalation; and
  - no arbitrary 500-row source cap exists.

- [ ] **Step 3: Write detail timeline tests**

  Assert the detail query merges bounded rows from comments, linked chat
  messages, and deliveries, ordered by timestamp. Delivery items include a
  purpose-specific label and a link only when the Telegram destination is a
  valid supergroup ID.

  Test link formatting separately:

  ```python
  assert telegram_message_url("-1001234567890", 42) == \
      "https://t.me/c/1234567890/42"
  assert telegram_message_url("123456789", 42) is None
  assert telegram_message_url("-1001234567890", None) is None
  ```

- [ ] **Step 4: Run reader/page tests and confirm failures**

  Run:

  ```bash
  PYTHONPATH="$PWD:$PWD/anansi_app" \
    pytest anansi_app/tests/test_supabase_reader_tickets.py \
      anansi_app/tests/test_tickets_page.py -q
  ```

  Expected: failures because the reader still merges capped legacy sources.

- [ ] **Step 5: Implement canonical list and detail reads**

  Query `ticket_list_view` with filters before `.range`. Query detail by
  canonical UUID and fetch a bounded timeline for that one ticket.

  Keep `SupabaseReader` read-only. Do not add Jira calls or database writes.

- [ ] **Step 6: Update the NiceGUI page**

  Add distinct chips/filters for:

  - backend: Jira/Internal;
  - origin: Customer escalation/Operational notification/Adopted/Legacy;
  - normalized status; and
  - `has_escalation`.

  Label delivery links as “Escalation message”, “Notification message”, or
  “Update message”. For private chats or incomplete receipts, show delivery
  text without a hyperlink.

- [ ] **Step 7: Run focused tests and lint**

  Run:

  ```bash
  PYTHONPATH="$PWD:$PWD/anansi_app" \
    pytest anansi_app/tests/test_supabase_reader_tickets.py \
      anansi_app/tests/test_tickets_page.py -q
  ruff check anansi_app/services/supabase_reader.py \
    anansi_app/nicegui_app/pages/tickets.py \
    anansi_app/tests/test_supabase_reader_tickets.py \
    anansi_app/tests/test_tickets_page.py
  ```

  Expected: all pass.

- [ ] **Step 8: Commit**

  ```bash
  git add anansi_app/services/supabase_reader.py \
    anansi_app/nicegui_app/pages/tickets.py \
    anansi_app/tests/test_supabase_reader_tickets.py \
    anansi_app/tests/test_tickets_page.py
  git commit -m "feat(anansi): list canonical tickets with escalation context"
  ```

---

### Task 10: Add the validate-and-contract migration

**Files:**

- Create: `db/migrations/0005b_ticket_schema_validate_and_contract.sql`
- Create: `chat_orchestrator/tests/test_ticket_schema_contract_migration.py`
- Modify: `chat_orchestrator/tests/test_ticket_schema_expand_migration.py`

- [ ] **Step 1: Write static contract tests**

  Assert SQL 2 is transactional and contains guarded cleanup for:

  - expansion capture triggers/functions;
  - `internal_tickets` and `internal_ticket_comments`;
  - `escalation_mappings`;
  - legacy correlation identity/projection/delivery columns;
  - `chat_messages.metadata` ticket keys;
  - `chat_sessions.is_escalated`, `escalated_at`, and
    `escalation_message_id`; and
  - final constraints and `ticket_list_view`.

  Assert it does not drop `internal_ticket_seq` or
  `next_internal_ticket_ref`.

- [ ] **Step 2: Write a full expand→contract live test**

  In scratch PostgreSQL:

  1. seed representative legacy data;
  2. run SQL 1 twice;
  3. simulate canonical-only writes from the new app;
  4. satisfy the legacy-write cutoff assertion;
  5. run SQL 2 twice; and
  6. compare all canonical rows and relationships before/after contract.

  Query `information_schema`/`pg_catalog` to prove legacy objects are gone and
  final foreign keys, checks, indexes, trigger, function, and view remain.

- [ ] **Step 3: Run tests and confirm SQL 2 is missing**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/test_ticket_schema_contract_migration.py -q
  ```

  Expected: failure because SQL 2 does not exist.

- [ ] **Step 4: Implement pre-contract assertions**

  Before any drop, abort if:

  - canonical counts are below the legacy recoverable counts;
  - a known legacy relationship lacks a canonical FK;
  - a ticket ref is duplicated;
  - an active ticket is structurally invalid;
  - a delivery identity is duplicated;
  - a recent legacy row lacks a matching canonical row; or
  - a ticket metadata annotation resolves to a managed ref but lacks
    `chat_messages.ticket_id`.

  Put all validation and cleanup inside one transaction so any failure rolls
  back the whole contract.

- [ ] **Step 5: Implement final cleanup**

  Drop compatibility triggers and legacy relations/columns only after the
  assertions. Replace the legacy correlation primary key deterministically:
  drop the primary-key constraint on `id`, require `ticket_id NOT NULL`, add
  the primary key on `ticket_id`, then drop `id` and the redundant columns.
  Recreate the signature and last-alert indexes against the final columns.

  Remove ticket relationship keys from `chat_messages.metadata` with a guarded
  update before declaring the FK authoritative.

  Recreate `ticket_list_view` against final names after all drops.

- [ ] **Step 6: Run migration tests**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/test_ticket_schema_expand_migration.py \
    tests/test_ticket_schema_contract_migration.py -q
  ```

  Expected: all pass or live tests skip only for missing PostgreSQL binaries.

- [ ] **Step 7: Commit**

  ```bash
  git add db/migrations/0005b_ticket_schema_validate_and_contract.sql \
    chat_orchestrator/tests/test_ticket_schema_expand_migration.py \
    chat_orchestrator/tests/test_ticket_schema_contract_migration.py
  git commit -m "feat(db): validate and contract legacy ticket schema"
  ```

---

### Task 11: Refresh and verify the complete final Chat DB public schema

**Files:**

- Modify: `db/schema/chat_db.sql`
- Create: `chat_orchestrator/tests/test_chat_db_schema_snapshot.py`
- Reference: all files under `db/migrations/`
- Reference: `anansi_app/db/migrations/2026-07-11-chat-db-cleanup.sql`

**Snapshot rule:** `db/schema/chat_db.sql` is the complete final `public`
schema, not a hand-maintained subset and not a migration replay script. Its
generation source is a schema-only dump of the actual current Chat DB restored
into scratch PostgreSQL and migrated through SQL 1 and SQL 2.

- [ ] **Step 1: Add schema-snapshot structural tests**

  Create local stand-in roles for every application-role grant present in the
  dump, then load `db/schema/chat_db.sql` into a fresh scratch database. Assert
  it is self-contained and that its resulting catalog contains every canonical
  ticket object, all unrelated public objects named in the checked-in snapshot,
  no legacy ticket relations/columns, and no unresolved dependency.

  Add a reusable normalized catalog-manifest extractor containing:

  ```text
  schemas, extensions used by public objects, tables, columns and defaults,
  primary/foreign/unique/check constraints, indexes, sequences, functions,
  triggers, views, RLS enablement, policies, and grants to application roles
  ```

  Ignore owners, comments, object OIDs, generated dump timestamps, and grants
  to environment-specific owner roles. Preserve and compare grants to the
  Supabase application roles used by this repository.

- [ ] **Step 2: Run the test and confirm the stale snapshot fails**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/test_chat_db_schema_snapshot.py -q
  ```

  Expected: failure listing missing or stale public objects in the current
  snapshot.

- [ ] **Step 3: Clone the real public schema and migrate the clone**

  Obtain a schema-only dump of the actual current Chat DB. If a PostgreSQL
  connection URI is available, run:

  ```bash
  pg_dump --schema=public --schema-only --no-owner \
    --file /tmp/anansi-chat-db-current.sql "$CHAT_DB_DATABASE_URL"
  ```

  If direct PostgreSQL access is unavailable, pause this task until the user
  exports the current `public` schema from Supabase. Do not substitute the
  known-outdated checked-in snapshot.

  Restore that dump to a scratch database, apply SQL 1 and SQL 2 with
  `ON_ERROR_STOP=1`, then generate the repository snapshot:

  ```bash
  pg_dump --schema=public --schema-only --no-owner \
    --file db/schema/chat_db.sql "$ANANSI_SCRATCH_DATABASE_URL"
  ```

  Use task-specific environment variables and never use or overwrite `HOME`.
  Record the reproducible restore/migrate/dump commands at the top of the
  snapshot as SQL comments, without connection values.

- [ ] **Step 4: Inspect the generated snapshot**

  Confirm it contains all unrelated existing `public` objects as well as the
  ticket changes, including RLS policies and required grants. Confirm it
  contains no Supabase platform schemas, table data, owners, service keys,
  URLs, or passwords.

- [ ] **Step 5: Compare the migrated clone with the snapshot**

  Restore `db/schema/chat_db.sql` into a second scratch database. Use the
  manifest extractor to compare it with the migrated live-schema clone from
  Step 3. They must be identical modulo the documented owner-role exclusions.
  Then run:

  ```bash
  cd chat_orchestrator
  pytest tests/test_chat_db_schema_snapshot.py \
    tests/test_ticket_schema_expand_migration.py \
    tests/test_ticket_schema_contract_migration.py -q
  ```

  Expected: identical manifests; all migration tests pass or live tests skip
  only when PostgreSQL binaries are missing.

- [ ] **Step 6: Commit**

  ```bash
  git add db/schema/chat_db.sql \
    chat_orchestrator/tests/test_chat_db_schema_snapshot.py
  git commit -m "chore(db): refresh complete Chat DB public schema"
  ```

---

### Task 12: Remove obsolete accesses and run the complete verification suite

**Files:**

- Modify: `chat_orchestrator/orchestrator/services/supabase_client.py`
- Modify: `chat_orchestrator/orchestrator/services/escalation_service.py`
- Modify: `chat_orchestrator/orchestrator/api/app.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_store.py`
- Modify: `anansi_app/services/supabase_reader.py`
- Modify: `chat_orchestrator/tests/services/test_escalation_service_ticketing.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlation_store.py`
- Modify: `chat_orchestrator/tests/api/test_notify_ticketing.py`
- Modify: `anansi_app/tests/test_supabase_reader_tickets.py`

- [ ] **Step 1: Prove production code no longer reads legacy ticket storage**

  Run:

  ```bash
  rg -n \
    'internal_tickets|internal_ticket_comments|escalation_mappings|metadata->>ticket_ref|ticket_role|chat_sessions.*is_escalated' \
    chat_orchestrator/orchestrator anansi_app \
    -g '*.py'
  ```

  Classify every match. Remove all runtime table/column access. Historical
  migration names in comments or explicit migration validation code may
  remain.

- [ ] **Step 2: Prove repository ownership boundaries**

  Run:

  ```bash
  rg -n 'table\("tickets"\)|table\('\''tickets'\''\)' \
    chat_orchestrator/orchestrator -g '*.py'
  rg -n 'table\("escalations"\)|table\('\''escalations'\''\)' \
    chat_orchestrator/orchestrator -g '*.py'
  rg -n 'table\("message_deliveries"\)|table\('\''message_deliveries'\''\)' \
    chat_orchestrator/orchestrator -g '*.py'
  ```

  Expected:

  - ticket writes occur only in `ticketing/repository.py`;
  - escalation writes occur only in `escalation_repository.py`; and
  - delivery writes occur only in `ticketing/delivery_repository.py`.

  Read-only Anansi queries may access the view and detail tables.

- [ ] **Step 3: Run the Chat Orchestrator suite**

  Run:

  ```bash
  cd chat_orchestrator
  pytest tests/ -x -q
  ruff check orchestrator tests
  ```

  Expected: all tests pass; PostgreSQL-dependent tests may skip only when the
  required local binaries are absent.

- [ ] **Step 4: Run the Anansi suite**

  From the repository root:

  ```bash
  PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q
  ruff check anansi_app
  ```

  Expected: all tests and lint pass.

- [ ] **Step 5: Check SQL and repository diffs**

  Run:

  ```bash
  git diff --check
  git status --short
  ```

  Review both SQL files in full. Confirm SQL 1 contains no destructive
  contraction and SQL 2 begins with all required invariants.

- [ ] **Step 6: Commit any final cleanup**

  If the searches required code changes:

  ```bash
  git add chat_orchestrator anansi_app
  git commit -m "refactor(ticketing): remove legacy ticket storage access"
  ```

  If no changes were required, do not create an empty commit.

---

### Task 13: Execute the production rollout or hand off exactly two SQL scripts

**Files:**

- Use: `db/migrations/0005a_ticket_schema_expand_and_backfill.sql`
- Use: `db/migrations/0005b_ticket_schema_validate_and_contract.sql`
- Verify: `db/schema/chat_db.sql`

**Release order:**

```text
preflight dump
  -> SQL 1 expand/backfill
  -> deploy canonical application
  -> observe and compare
  -> SQL 2 validate/contract
  -> final dump
  -> regenerate/compare chat_db.sql
```

- [ ] **Step 1: Detect DDL-capable credentials without exposing them**

  Check only for variable presence and connection capability. Prefer a
  PostgreSQL URI such as `CHAT_DB_DATABASE_URL` or an explicitly supplied
  Supabase management connection. Do not echo its value.

  If only `CHAT_DB_URL` plus `CHAT_DB_SERVICE_KEY` exists, select the
  copy-paste path: those credentials support PostgREST data access, not
  arbitrary public-schema DDL.

- [ ] **Step 2: Capture preflight evidence**

  With a DDL-capable URI, run:

  ```bash
  pg_dump --schema=public --schema-only --no-owner \
    --file /tmp/anansi-chat-db-before.sql "$CHAT_DB_DATABASE_URL"
  psql "$CHAT_DB_DATABASE_URL" \
    -v ON_ERROR_STOP=1 \
    -c "select current_database(), current_user;"
  ```

  Store dumps only under a task-specific `/tmp` path. Do not commit them.

- [ ] **Step 3: Apply SQL 1**

  Run:

  ```bash
  psql "$CHAT_DB_DATABASE_URL" -v ON_ERROR_STOP=1 \
    -f db/migrations/0005a_ticket_schema_expand_and_backfill.sql
  ```

  Immediately query canonical counts, origin/backend breakdown, unresolved
  relationships, duplicate refs, and invalid active tickets. Stop rollout if
  any invariant differs from the scratch migration tests.

- [ ] **Step 4: Deploy and observe the canonical application**

  Deploy the application version containing Tasks 3–9. Observe:

  - ticket creation success/failure by backend;
  - pending provisioning count and reconciliation;
  - legacy-versus-canonical recoverable ticket counts;
  - legacy-only writes since deployment;
  - escalation claim/release/track transitions;
  - delivery-receipt write failures; and
  - Tickets page totals and filters.

  Do not proceed while any recent legacy-only write or unresolved known
  relationship remains.

- [ ] **Step 5: Apply SQL 2 only after the gate passes**

  Run:

  ```bash
  psql "$CHAT_DB_DATABASE_URL" -v ON_ERROR_STOP=1 \
    -f db/migrations/0005b_ticket_schema_validate_and_contract.sql
  ```

  SQL 2 must enforce the same gate itself and roll back on failure.

- [ ] **Step 6: Capture and compare the final public schema**

  Run:

  ```bash
  pg_dump --schema=public --schema-only --no-owner \
    --file /tmp/anansi-chat-db-after.sql "$CHAT_DB_DATABASE_URL"
  ```

  Run the catalog-manifest comparison from Task 11 against the live final
  schema and checked-in `db/schema/chat_db.sql`. Resolve any difference by
  regenerating the checked-in snapshot from the live final public schema and
  rerunning tests; do not hand-edit around an unexplained difference.

- [ ] **Step 7: Use the two-script handoff when direct DDL is unavailable**

  Give the user, in order, only:

  1. `db/migrations/0005a_ticket_schema_expand_and_backfill.sql`; and
  2. `db/migrations/0005b_ticket_schema_validate_and_contract.sql`.

  State that SQL 1 is run before the application cutover and SQL 2 only after
  the observation gate. Do not generate a third cleanup, validation, or helper
  SQL file; all mandatory validation must already be inside the two scripts.

---

## Final Acceptance Checklist

- [ ] Every recoverable Anansi-created internal or Jira ticket has exactly one
  canonical `tickets` row.
- [ ] Existing Jira issues appear only after an Anansi adoption action; the
  implementation does not import unrelated Jira project issues.
- [ ] Jira notification tickets appear without requiring an escalation row.
- [ ] Customer escalations and operational notifications are visually and
  queryably distinct.
- [ ] Existing-ref operations route through persisted `tickets.backend`.
- [ ] Pending Jira intents reconcile by `anansi-ticket-<uuid>` without creating
  duplicates.
- [ ] Jira outages preserve the last local projection and ticket visibility.
- [ ] Telegram links are generated only from valid delivery receipts.
- [ ] Private-chat deliveries never receive fabricated `t.me/c` links.
- [ ] Correlation state is keyed by `ticket_id` and contains no duplicated
  ticket projection or delivery coordinates.
- [ ] Runtime code has one repository owner per domain table.
- [ ] SQL 1 is non-destructive, transactional, and idempotent.
- [ ] SQL 2 validates before contraction, is transactional, and is idempotent.
- [ ] `db/schema/chat_db.sql` matches the complete final live `public` schema by
  normalized catalog manifest.
- [ ] Full Chat Orchestrator and Anansi tests pass.
- [ ] Production was changed directly with DDL-capable credentials, or the user
  received exactly the two ordered copy-paste SQL files.
