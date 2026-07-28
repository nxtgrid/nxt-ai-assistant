# Anansi Ticket Schema Consolidation Design

## Goal

Give every ticket created, adopted, or managed by Anansi one durable local
record, regardless of whether Jira is configured or available. The admin
Tickets page must list those records consistently, distinguish customer
escalations from operational notifications, and link only to Telegram
messages whose destination and message ID were actually recorded.

The work must also leave the Chat DB with a coherent public schema snapshot:
`db/schema/chat_db.sql` will describe the complete final `public` schema after
the migration, rather than only the subset remembered by recent feature work.

## Scope

- Consolidate local ticket identity and lifecycle state for Jira and internal
  tickets.
- Normalize the relationships between tickets, escalations, chat messages,
  formal ticket comments, alert correlation, and outbound Telegram delivery
  receipts.
- Migrate all recoverable Anansi ticket history.
- Replace ad hoc table access with repositories that own one domain boundary.
- Update the read-only Tickets list and detail page to use the consolidated
  model with database-backed filtering and pagination.
- Refresh the checked-in Chat DB schema from the final live `public` schema.
- Deliver at most two ordered, idempotent production migration SQL files.

## Non-goals

- Listing every issue in the configured Jira project. A Jira issue enters the
  local model only after Anansi creates it or adopts it for an Anansi action.
- Replacing Jira workflows, assignment, priority, or project configuration.
- Combining all domain history into a generic JSON event table.
- Redesigning general chat retention, customer identity, or unrelated Chat DB
  tables.
- Making the Tickets page a write-capable ticket-management interface.

## Existing Problems

### Ticket identity has no canonical owner

Internal tickets live in `internal_tickets`. Jira tickets are inferred from
`escalation_mappings` or `ticket_correlations`. `TicketService` determines the
backend by looking for a ref in `internal_tickets` and otherwise assuming Jira.
That makes absence of data carry business meaning and causes Jira-backed
notification tickets to disappear from the Tickets view.

`escalation_mappings` also carries three overlapping identities:
`jira_ticket_key`, `ticket_ref`, and `ticket_backend`. A failed best-effort
stamp can leave a real ticket without the local fields used by readers.

### Lifecycle and presentation state have competing owners

Ticket status is represented by some combination of:

- `internal_tickets.status`;
- Jira's remote workflow status;
- `escalation_mappings.is_active` and `resolved_at`; and
- `ticket_correlations.status`.

Ticket summaries and Telegram coordinates are similarly repeated across
internal tickets, escalation question text, correlation render state,
conversation messages, session escalation fields, and mapping rows. The
Tickets page therefore assembles an approximate projection in Python and caps
each source before filtering.

### Message relationships are annotations rather than relationships

`chat_messages.metadata.ticket_ref` is the current ticket association for
forwarded messages. It is updated through a read/merge/write JSON operation,
has no foreign key, and is queried through a JSON expression. Notification
delivery can happen even when no chat session exists, so `chat_messages` alone
cannot be the durable receipt for every outbound message.

### Escalation state is ambiguous

`escalation_mappings.is_active` means waiting for staff in one path, atomically
claimed in another, temporarily disabled during ticket creation, and resolved
in another. `chat_sessions.is_escalated`, `escalated_at`, and
`escalation_message_id` repeat part of the same lifecycle.

## Design Principles

1. One table owns each durable fact.
2. Backend selection is stored, never inferred from reference shape or missing
   rows.
3. Jira unavailability may make a projection stale, but it must not make a
   ticket disappear.
4. Domain tables remain explicit. Fewer tables are not a goal when combining
   unrelated lifecycles would weaken constraints.
5. Cross-system writes use durable local intent and reconciliation rather than
   relying on a best-effort post-create stamp.
6. Readers paginate and filter in the database before enriching the selected
   page.

## Final Physical Schema

### `tickets`

`tickets` is the canonical local identity and current projection for every
Anansi-related ticket.

| Column | Type and constraint | Meaning |
| --- | --- | --- |
| `id` | `uuid primary key default gen_random_uuid()` | Stable Anansi identity |
| `ticket_ref` | `text`, nullable only while provisioning | Jira key or allocated internal ref |
| `backend` | `text check (backend in ('jira','internal'))`, nullable only while provisioning | Owning backend |
| `created_via` | `text not null check (created_via in ('escalation','notification','adopted','legacy'))` | How Anansi first became responsible |
| `provisioning_state` | `text not null check (provisioning_state in ('pending','active','failed'))` | Cross-system creation lifecycle |
| `status` | `text not null check (status in ('open','in_progress','done'))` | Normalized current status |
| `backend_status` | `text` | Raw Jira/internal workflow value |
| `summary` | `text not null` | Current summary rendered by Anansi |
| `description` | `text` | Current description rendered by Anansi |
| `ticket_type` | `text` | Backend-neutral classification/type label |
| `organization_id` | `integer` | Organization context |
| `grid_name` | `text` | Grid context |
| `assignee_email` | `text` | Common assignment projection |
| `labels` | `jsonb not null default '[]'` | Current normalized labels |
| `created_at` | `timestamptz not null default now()` | Local intent/adoption time |
| `activated_at` | `timestamptz` | Backend ticket became usable |
| `updated_at` | `timestamptz not null default now()` | Local projection update |
| `resolved_at` | `timestamptz` | Normalized completion time |
| `backend_synced_at` | `timestamptz` | Last successful backend refresh |

The table has:

- a partial unique index on `ticket_ref` where it is not null;
- indexes on `(status, created_at desc)`, `(backend, status, created_at desc)`,
  `(created_via, status, created_at desc)`, `organization_id`, and
  `(grid_name, status)`;
- a check requiring `ticket_ref`, `backend`, and `activated_at` when
  `provisioning_state = 'active'`; and
- an `updated_at` trigger.

For internal tickets, this row is authoritative. For Jira tickets, it is the
durable Anansi projection; Jira remains authoritative for Jira-only workflow
semantics. Successful Jira reads, writes, webhooks, and sweeps refresh the
projection and `backend_synced_at`.

The existing `internal_ticket_seq` and `next_internal_ticket_ref` function
remain because only the internal backend allocates `TKT-*` references.

### `escalations`

`escalation_mappings` is renamed to `escalations`. It owns the customer-to-staff
escalation workflow, not ticket identity.

| Column | Type and constraint | Meaning |
| --- | --- | --- |
| `id` | `uuid primary key` | Existing mapping identity |
| `chat_session_id` | `uuid not null references chat_sessions(id)` | Source conversation |
| `thread_id` | `text references chat_threads(thread_id)` | Optional issue thread |
| `ticket_id` | `uuid references tickets(id)` | Optional resulting/adopted ticket |
| `state` | `text not null check (state in ('open','processing','tracked','resolved'))` | Explicit escalation lifecycle |
| `customer_username` | `text` | Historical customer snapshot |
| `customer_email` | `text` | Historical customer snapshot |
| `org_hashtag` | `text` | Historical routing snapshot |
| `reason` | `text` | Escalation reason |
| `action_type` | `text` | Requested staff action |
| `question_text` | `text` | Original customer question/context |
| `created_at` | `timestamptz not null default now()` | Escalation time |
| `resolved_at` | `timestamptz` | Escalation workflow completion |

Customer chat/topic and organization IDs are obtained through
`chat_sessions`. Username, email, and organization hashtag remain explicit
snapshots because they are presentation/audit values that may change after the
escalation.

One ticket may have many escalations. This models follow-ups without producing
duplicate ticket rows.

The final table does not contain `jira_ticket_key`, `ticket_ref`,
`ticket_backend`, `is_active`, `escalation_message_id`, or
`escalation_topic_id`.

### `ticket_comments`

`internal_ticket_comments` is renamed to `ticket_comments` and generalized to
both backends.

| Column | Type and constraint |
| --- | --- |
| `id` | `uuid primary key default gen_random_uuid()` |
| `ticket_id` | `uuid not null references tickets(id) on delete cascade` |
| `backend_comment_id` | `text` |
| `author` | `text` |
| `body` | `text not null` |
| `is_public` | `boolean not null default false` |
| `source` | `text not null check (source in ('customer','staff','notify','jira','system'))` |
| `created_at` | `timestamptz not null default now()` |

Anansi-originated formal comments are written here after the backend action
succeeds. Jira webhook comments may also be mirrored here. Conversation
messages remain in `chat_messages`; the two sources are not double-written as
independent timeline entries.

### `message_deliveries`

`message_deliveries` is the durable receipt for an outbound external message.
It exists separately from `chat_messages` because notifications may be sent to
a target that has no chat session.

| Column | Type and constraint |
| --- | --- |
| `id` | `uuid primary key default gen_random_uuid()` |
| `ticket_id` | `uuid references tickets(id) on delete cascade` |
| `escalation_id` | `uuid references escalations(id) on delete cascade` |
| `chat_message_id` | `uuid references chat_messages(id) on delete set null` |
| `purpose` | `text not null check (purpose in ('escalation','notification','update'))` |
| `channel` | `text not null check (channel in ('telegram'))` |
| `external_chat_id` | `text not null` |
| `external_topic_id` | `text` |
| `external_message_id` | `bigint not null` |
| `sent_at` | `timestamptz not null default now()` |

A check requires at least one of `ticket_id` or `escalation_id`. A unique index
on `(channel, external_chat_id, external_message_id)` makes delivery recording
idempotent.

An escalation delivery may initially have only `escalation_id`; tracking that
escalation attaches `ticket_id` to the same receipt. Notification deliveries
start with `ticket_id`. Alert reply threading reads the first relevant delivery
instead of caching Telegram coordinates inside correlation state.

The UI produces `t.me/c` links only for Telegram supergroup IDs that can be
converted safely. Private-chat deliveries remain visible in the timeline but
do not receive a fabricated direct link.

### `chat_messages`

Add nullable `ticket_id uuid references tickets(id) on delete set null` and an
index on `(ticket_id, created_at)`.

`ticket_id` replaces `metadata.ticket_ref` and `metadata.ticket_role` as the
relationship source of truth. Existing metadata keys may remain during
compatibility rollout but are no longer read after cutover.

### `ticket_correlations`

`ticket_correlations` remains a separate mutable state table because it owns the
alert aggregation algorithm, not the ticket lifecycle.

Its final columns are:

- `ticket_id uuid primary key references tickets(id) on delete cascade`;
- `root_cause_kind`;
- `primary_signature`;
- `signatures jsonb`;
- `affected_keys jsonb`;
- `summary_base`;
- `description_base`;
- `severity`;
- `occurrence_count`;
- `escalated_at`;
- `last_alert_at`;
- `created_at`; and
- `updated_at`.

Remove `ticket_ref`, `ticket_backend`, `grid_name`, `organization_id`,
`summary_current`, `status`, `telegram_chat_id`, `telegram_topic_id`, and
`telegram_message_id`. Current ticket summary, status, backend, organization,
and grid are joined from `tickets`; delivery coordinates are read from
`message_deliveries`.

### `ticket_correlation_events`

The existing append-only audit table remains. Replace `ticket_ref` with nullable
`ticket_id uuid references tickets(id) on delete set null`. Events that fail
before a ticket is selected remain valid with `ticket_id = null`.

Grid name, source, signature, dedup key, decision evidence, candidate refs,
normalized alert facts, and raw LLM output remain event-time evidence rather
than current ticket state. The unique partial index on `dedup_key` remains.

### Chat session cleanup

After escalation reads use `escalations.state`, remove
`chat_sessions.is_escalated`, `escalated_at`, and `escalation_message_id`.
Session escalation state is derived from whether an `open` or `processing`
blocking escalation exists for that session. Non-blocking escalation reasons
remain explicit behavior in the escalation service.

## Read Model

The Tickets page reads a SQL view named `ticket_list_view`, backed by `tickets`
and aggregate joins:

- one row per ticket;
- normalized backend, origin, status, summary, organization and grid;
- escalation count and `has_escalation`;
- comment/chat activity count;
- affected-component and occurrence counts;
- latest recorded activity timestamp; and
- Jira staleness from `backend_synced_at`.

Filtering, sorting, counting, and pagination happen against the view in
PostgreSQL. The current per-source 500-row fetch cap and Python merge are
removed.

Ticket detail is loaded by `tickets.id` or `ticket_ref`. Its timeline is an
ordered merge of `ticket_comments`, linked `chat_messages`, and
`message_deliveries`. Repository code performs the bounded detail queries;
there is no generic JSON event table.

The UI shows separate chips for:

- backend: Jira or Internal;
- creation origin: Customer escalation, Operational notification, Adopted, or
  Legacy/unknown;
- current status; and
- an escalation marker when `has_escalation` is true.

Links are purpose-specific: “Escalation message”, “Notification message”, and
“Update message”. A link is rendered only from a valid delivery receipt.

## Service Boundaries

### `TicketRepository`

The sole owner of direct reads and writes to:

- `tickets`;
- `ticket_comments`;
- ticket relationships on `chat_messages`.

It exposes typed create-intent, activation, adoption, status projection,
comment, list, detail, and reconciliation methods.

### `TicketService`

Orchestrates backend selection and backend operations. It never infers a
backend from a reference prefix or missing row. Existing-ref operations resolve
the persisted `tickets.backend`.

### `EscalationRepository`

The sole owner of `escalations`. Atomic claims become a state transition from
`open` to `processing`. Success moves to `tracked`; failure returns to `open`;
intentional closure moves to `resolved`.

### `DeliveryRepository`

The sole owner of `message_deliveries`. Ticketing, escalation, and notification
services call its typed idempotent `record`, `attach_ticket`, and reply-target
queries rather than writing delivery rows themselves.

### `CorrelationStore`

Owns only `ticket_correlations` and `ticket_correlation_events`. It receives
`ticket_id`, never backend routing data. It uses ticket repository queries for
current candidate status and presentation.

General `EnhancedSupabaseClient` chat methods stop performing ticket and
escalation table writes after cutover.

## Data Flows

### New ticket

1. `TicketService` creates a `pending` local ticket intent with a stable UUID.
2. It resolves the backend.
3. Internal creation allocates a `TKT-*` ref and activates the same row.
4. Jira creation includes an `anansi-ticket-<uuid>` label, creates the remote
   issue, then activates the same row with its Jira key.
5. If Jira creation fails and internal fallback is allowed, the same pending
   row is activated as internal.
6. If the remote Jira issue is created but local activation fails, the pending
   row remains recoverable by the stable Jira label.

A bounded reconciliation job searches only pending Jira intents and either
activates the matching issue or marks the intent failed with structured logs.

### Escalation tracking

1. An escalation is atomically moved from `open` to `processing`.
2. Existing ticket dedup returns a canonical `ticket_id`, adopting a Jira issue
   first when necessary.
3. New ticket creation returns a canonical `ticket_id`.
4. The escalation receives `ticket_id` and moves to `tracked`.
5. Its existing escalation delivery receipt receives the same `ticket_id`.

### Notification ticketing

1. `/notify` creates or resolves a canonical ticket before delivery.
2. Correlation is keyed by `ticket_id`.
3. A successful Telegram send writes one idempotent `message_deliveries` row
   even when no chat session exists.
4. If a chat message is also persisted, its `ticket_id` and the delivery's
   `chat_message_id` are set.

### Existing Jira adoption

When correlation discovers a Jira issue that Anansi has not recorded, Anansi
creates an active `tickets` row with `created_via = 'adopted'` before adding a
comment or updating it. That issue then appears in the Tickets view because it
has become Anansi-related.

### Status synchronization and Jira outages

Every successful status read or mutation updates `tickets.status`,
`backend_status`, and `backend_synced_at`. Jira webhooks update the same row.
During an outage the local row remains visible with its last-known status and a
staleness indication. An unavailable read never marks the ticket done.

## Migration and Production Rollout

The rollout uses two SQL artifacts so no application deployment depends on an
unsafe all-at-once destructive migration.

### SQL 1: expand and backfill

`db/migrations/0005a_ticket_schema_expand_and_backfill.sql` will:

1. create the new canonical tables, columns, indexes, checks, triggers, and
   `ticket_list_view`;
2. backfill `tickets` from `internal_tickets`;
3. backfill Jira tickets from the union of `escalation_mappings` and
   `ticket_correlations`, deduplicated by ticket ref;
4. assign `created_via` only from evidence: the existing internal ticket
   `source` is authoritative; an escalation mapping proves `escalation`; a
   correlation audit event that targeted an already-existing candidate proves
   `adopted`; and notification creation evidence proves `notification`.
   Ambiguous correlation-only history is marked `legacy` rather than assigned
   invented provenance;
5. backfill escalation-to-ticket relationships;
6. backfill escalation and notification delivery receipts where the exact chat
   and message IDs can be proved;
7. backfill `chat_messages.ticket_id` and generalized comments from existing
   ticket refs;
8. backfill correlation foreign keys; and
9. add validation queries that abort on duplicate refs, active tickets without
   a backend/ref, unresolved known relationships, or delivery uniqueness
   violations.

The SQL is transactional and idempotent. It does not drop legacy columns or
tables, so the old application can continue running.

The application release then dual-writes compatibility fields where necessary,
switches reads to the canonical model, and emits comparison metrics for legacy
versus canonical ticket counts.

### SQL 2: validate and contract

`db/migrations/0005b_ticket_schema_validate_and_contract.sql` will:

1. rerun count and referential-integrity assertions;
2. require zero recent legacy-only writes;
3. rename final tables where expansion used temporary names;
4. remove legacy ticket identity, status, and message-coordinate columns;
5. remove JSON ticket annotations after their FK backfill is verified;
6. remove redundant session escalation columns;
7. drop or archive replaced tables; and
8. recreate the final views and constraints against final names.

It is also transactional and idempotent. It is run only after the new
application version has been observed successfully.

### Direct execution versus copy-paste

`CHAT_DB_SERVICE_KEY` is a Supabase service-role/PostgREST credential and is
not assumed to permit arbitrary DDL. The implementation may execute the SQL
directly only when the environment supplies a PostgreSQL connection URI or an
equivalent Supabase management credential with explicit schema-migration
authority.

If such authority is available, the flow is:

1. save a schema-only pre-migration dump;
2. execute SQL 1 with `ON_ERROR_STOP=1`;
3. run application and data verification;
4. execute SQL 2 with `ON_ERROR_STOP=1`; and
5. save and diff a schema-only post-migration dump.

If only the current service-role key is available, the user receives the same
two self-contained SQL files for ordered copy-paste into the Supabase SQL
editor. No additional snippets or manual data edits are required.

## Complete `db/schema/chat_db.sql` Refresh

The checked-in schema file is a clean-install snapshot, not migration history.
After SQL 2, it must be rebuilt from the actual final production `public`
schema rather than patched only around the ticket tables.

The refresh process inventories and reconciles:

- extensions referenced by public objects;
- public enum and composite types;
- sequences;
- tables and generated/default expressions;
- primary, unique, check, and foreign-key constraints;
- indexes;
- functions and RPCs;
- triggers;
- views;
- row-level-security enablement and policies; and
- grants required by the application.

With PostgreSQL credentials, a schema-only `pg_dump` of `public` is the
comparison baseline. Repository-specific explanatory comments and safe
`IF NOT EXISTS` clean-install conventions are retained only where they do not
change the dumped semantics. The final file must create a fresh local database
whose catalog matches the post-migration public schema, modulo owners,
environment-specific grants, and Supabase-managed extension internals.

Tests compare an object manifest extracted from `db/schema/chat_db.sql` with a
post-migration catalog manifest. This prevents later feature work from silently
leaving the snapshot stale again.

## Error Handling and Recovery

- A failed Jira create leaves a recoverable pending intent; it does not create
  an untracked active ticket.
- A Jira outage preserves last-known status and visibility.
- A failed delivery does not create a `message_deliveries` row.
- A sent delivery whose receipt write fails is logged with all idempotency
  coordinates and retried without resending the Telegram message.
- A failed escalation ticket create returns `processing` to `open` unless the
  stable Jira label proves that a remote ticket was created.
- Backfill rows with insufficient evidence remain in legacy tables through the
  validation period and make SQL 2 fail rather than being silently discarded.
- Contract migration assertions fail the transaction before destructive
  statements when invariants are not met.

## Verification

### Schema and migration

- Apply SQL 1 twice to a production-shaped test database.
- Verify internal, Jira escalation, Jira notification, correlated, adopted, and
  duplicate-follow-up fixtures each produce one canonical ticket.
- Verify all recoverable comments, chat links, escalation links, and delivery
  receipts.
- Run the old reader during the expand phase and the new reader after cutover.
- Apply SQL 2 twice after validation.
- Create a fresh database from `db/schema/chat_db.sql` and compare its public
  catalog manifest with the migrated database.

### Services

- Test backend resolution from `tickets.backend` across configuration changes.
- Test pending Jira recovery by stable Anansi label.
- Test Jira failure followed by internal fallback on the same local intent.
- Test all escalation state transitions and concurrent claims.
- Test Jira status failure preserving the last-known local status.
- Test formal comments and chat messages without duplicate timeline entries.
- Test idempotent delivery recording and reply-target selection.

### Tickets page

- Test database-side pagination beyond 500 records.
- Test backend, creation-origin, status, organization, and search filters.
- Test escalation markers for zero, one, and multiple linked escalations.
- Test escalation, notification, and update links from valid delivery receipts.
- Test that private chats and incomplete receipts do not render invalid links.
- Test Jira outage and stale-status presentation.

## Success Criteria

- Every active Anansi-related ticket has exactly one `tickets` row.
- Every existing-ref operation routes through persisted `tickets.backend`.
- The Tickets list has no per-source over-fetch cap and includes Jira-backed
  notification tickets.
- Escalations and notifications are visibly distinct without being separate
  ticket systems.
- Telegram links come only from durable delivery receipts.
- Correlation state contains no duplicate ticket lifecycle or delivery fields.
- No feature code relies on JSON metadata for ticket identity.
- The final Chat DB public schema can be recreated from
  `db/schema/chat_db.sql`.
- Production deployment requires no more than two ordered SQL executions.
