-- Chat Database (Supabase / PostgreSQL)
-- Schema generated from the live production database.
-- Run this in your Supabase SQL editor to create all tables required by Anansi.
--
-- Prerequisites:
--   pgvector extension (enabled by default on Supabase)
--   uuid-ossp extension (or use gen_random_uuid() — both work on Supabase)

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Custom Types ──────────────────────────────────────────────────────────────

CREATE TYPE artifact_type AS ENUM (
    'system_instruction', 'qa_pair', 'response_template',
    'decision_rule', 'entity_training', 'dspy_example', 'dspy_metric'
);

CREATE TYPE bot_mode AS ENUM ('customer_support', 'staff', 'shared');

CREATE TYPE sync_source AS ENUM ('manual', 'google_sheets', 'dspy_optimizer', 'api');

CREATE TYPE sync_status AS ENUM ('pending', 'in_progress', 'success', 'failed', 'partial');

-- ── Sessions & Messages ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chat_sessions (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              text UNIQUE NOT NULL,
    user_id                 text,
    title                   text,
    metadata                jsonb DEFAULT '{}',
    created_at              timestamptz DEFAULT now(),
    updated_at              timestamptz DEFAULT now(),
    ended_at                timestamptz,
    organization_id         integer,
    telegram_chat_id        text,
    telegram_topic_id       text,
    is_escalated            boolean DEFAULT false,
    escalated_at            timestamptz,
    escalation_message_id   bigint
);

CREATE INDEX IF NOT EXISTS chat_sessions_session_id_idx ON chat_sessions (session_id);
CREATE INDEX IF NOT EXISTS chat_sessions_telegram_chat_id_idx ON chat_sessions (telegram_chat_id);
CREATE INDEX IF NOT EXISTS chat_sessions_org_id_idx ON chat_sessions (organization_id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id                      uuid REFERENCES chat_sessions (id) ON DELETE CASCADE,
    role                            text NOT NULL,           -- user | model | tool | system
    content                         text,
    function_call                   jsonb,
    tool_result                     jsonb,
    metadata                        jsonb DEFAULT '{}',
    created_at                      timestamptz DEFAULT now(),
    message_index                   integer NOT NULL,
    from_chat_id                    text,
    group_id                        text,
    telegram_message_id             bigint,
    reply_to_telegram_message_id    bigint,
    sender_telegram_id              text,
    thread_id                       text,
    archived_at                     timestamptz,
    telegram_topic_id               text  -- added by 0016_chat_messages_topic.sql
);

CREATE INDEX IF NOT EXISTS chat_messages_session_id_idx ON chat_messages (session_id);
CREATE INDEX IF NOT EXISTS chat_messages_session_index_idx ON chat_messages (session_id, message_index);
-- Telegram edit handling looks messages up by telegram_message_id
CREATE INDEX IF NOT EXISTS chat_messages_telegram_msg_idx ON chat_messages (session_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS chat_messages_created_at_idx ON chat_messages (created_at);
-- Skill builder's Rewind button sets archived_at; every history read filters
-- on it (see 0012_message_archive.sql).
CREATE INDEX IF NOT EXISTS chat_messages_archived_idx ON chat_messages (session_id)
    WHERE archived_at IS NULL;
-- ChatWatermarkRepository filters by topic within a group (0016_chat_messages_topic.sql).
CREATE INDEX IF NOT EXISTS chat_messages_group_topic_msg_idx
    ON chat_messages (group_id, telegram_topic_id, telegram_message_id DESC);

-- ── Conversation Summaries ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          uuid REFERENCES chat_sessions (id) ON DELETE CASCADE,
    summary_text        text NOT NULL,
    message_range_start integer NOT NULL,
    message_range_end   integer NOT NULL,
    topic_entities      jsonb,
    token_count         integer,
    created_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversation_summaries_session_id_idx ON conversation_summaries (session_id);

-- ── Escalations ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS escalation_mappings (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              text NOT NULL,
    escalation_message_id   bigint NOT NULL,
    customer_chat_id        text NOT NULL,
    customer_topic_id       text,
    customer_username       text,
    customer_email          text,
    org_hashtag             text,
    reason                  text,
    action_type             text,
    jira_ticket_key         text,
    ticket_ref              text,                    -- backend-agnostic ref: Jira key or internal ref (e.g. 'TKT-000123')
    ticket_backend          text CHECK (ticket_backend IN ('jira', 'internal')), -- 'jira' | 'internal'
    organization_id         integer,
    escalation_topic_id     integer,
    is_active               boolean DEFAULT true,
    created_at              timestamptz DEFAULT now(),
    resolved_at             timestamptz,
    question_text           text,
    thread_id               text
);

-- Backfill-safe additions for pre-existing installations (Jira-optional ticket backend).
-- No-ops when escalation_mappings is created fresh above, since the columns already exist.
ALTER TABLE escalation_mappings ADD COLUMN IF NOT EXISTS ticket_ref text;
ALTER TABLE escalation_mappings ADD COLUMN IF NOT EXISTS ticket_backend text CHECK (ticket_backend IN ('jira', 'internal'));

CREATE INDEX IF NOT EXISTS escalation_mappings_session_id_idx ON escalation_mappings (session_id);
CREATE INDEX IF NOT EXISTS escalation_mappings_customer_chat_id_idx ON escalation_mappings (customer_chat_id);
CREATE INDEX IF NOT EXISTS escalation_mappings_thread_id_idx ON escalation_mappings (thread_id);
CREATE INDEX IF NOT EXISTS escalation_mappings_ticket_ref_idx ON escalation_mappings (ticket_ref);

-- Defensive: installs that already ran the ADD COLUMN IF NOT EXISTS above
-- from before this CHECK constraint was added will have ticket_backend
-- without it. ADD COLUMN IF NOT EXISTS skips the whole clause (including
-- the inline CHECK) when the column already exists, so it won't retrofit
-- the constraint on its own -- add it explicitly if missing.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'escalation_mappings_ticket_backend_check'
    ) THEN
        ALTER TABLE escalation_mappings
            ADD CONSTRAINT escalation_mappings_ticket_backend_check
            CHECK (ticket_backend IN ('jira', 'internal'));
    END IF;
END $$;

-- Backfill: for escalations already resolved via Jira, ticket_ref/ticket_backend
-- mirror jira_ticket_key so callers can query either column going forward.
-- Invariant: ticket_backend = 'jira'  => ticket_ref = jira_ticket_key (both populated).
--            ticket_backend = 'internal' => jira_ticket_key stays NULL, ticket_ref is set.
UPDATE escalation_mappings
    SET ticket_ref = jira_ticket_key, ticket_backend = 'jira'
    WHERE jira_ticket_key IS NOT NULL AND ticket_ref IS NULL;

-- ── Internal Tickets (Jira-optional ticket backend) ──────────────────────────
-- Lets Anansi track escalation/notify tickets without a Jira project. Jira
-- remains supported via escalation_mappings.jira_ticket_key; internal tickets
-- are the alternate backend selected via escalation_mappings.ticket_backend.

CREATE SEQUENCE IF NOT EXISTS internal_ticket_seq;

CREATE TABLE IF NOT EXISTS internal_tickets (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref              text UNIQUE NOT NULL,              -- e.g. 'TKT-000123'
    escalation_mapping_id   uuid,                              -- nullable (notify tickets have none)
    session_id              text,
    organization_id         integer,
    grid_name               text,
    summary                 text NOT NULL,
    description             text,
    ticket_type             text,
    status                  text NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open','in_progress','done')),
    assignee_email          text,
    labels                  jsonb DEFAULT '[]',
    source                  text NOT NULL DEFAULT 'escalation' -- 'escalation' | 'notify'
                            CHECK (source IN ('escalation','notify')),
    created_at              timestamptz DEFAULT now(),
    updated_at              timestamptz DEFAULT now(),
    resolved_at             timestamptz
);
CREATE INDEX IF NOT EXISTS internal_tickets_mapping_idx ON internal_tickets (escalation_mapping_id);
CREATE INDEX IF NOT EXISTS internal_tickets_status_idx ON internal_tickets (status);
CREATE INDEX IF NOT EXISTS internal_tickets_org_idx ON internal_tickets (organization_id);

CREATE TABLE IF NOT EXISTS internal_ticket_comments (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref    text NOT NULL REFERENCES internal_tickets(ticket_ref) ON DELETE CASCADE,
    author        text,               -- staff name / source system
    body          text NOT NULL,
    is_public     boolean DEFAULT false,   -- mirrors Jira jsdPublic (public = forward to customer)
    source        text DEFAULT 'staff',    -- 'staff' | 'customer' | 'notify' | 'system'
    created_at    timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS internal_ticket_comments_ref_idx ON internal_ticket_comments (ticket_ref, created_at);

-- RPC: allocate the next internal_ticket_seq ref, formatted with its
-- prefix. PostgREST only exposes functions created explicitly for RPC use,
-- so this thin wrapper is what makes nextval('internal_ticket_seq') reachable
-- through the Supabase client -- it does not touch internal_tickets itself.
-- See db/migrations/0002_internal_ticket_ref_allocation.sql.
CREATE OR REPLACE FUNCTION next_internal_ticket_ref(p_prefix text DEFAULT 'TKT')
RETURNS text LANGUAGE sql AS $$
    SELECT p_prefix || '-' || lpad(nextval('internal_ticket_seq')::text, 6, '0');
$$;

-- ── Alert Correlation (smart /notify ticketing) ──────────────────────────────
-- Mutable *state* for grouping incoming alerts (n8n/VRM/Grafana via
-- /chat/notify) against a grid's already-open tickets, on either backend.
-- Keyed by ticket_id (db/migrations/0005b) -- current ticket ref, backend,
-- summary, status, organization, and grid are read by joining `tickets`;
-- reply/delivery coordinates are read from `message_deliveries`. One ticket
-- has at most one correlation row (ticket_id is the primary key).
-- See db/migrations/0003_alert_correlation.sql,
-- db/migrations/0005b_ticket_schema_validate_and_contract.sql, and
-- docs/superpowers/plans/2026-07-27-smart-alert-correlation-notify.md.
--
-- NOTE: this file is not yet a full regeneration of the live post-0005b
-- schema (that is a separate, still-outstanding task) -- only these two
-- correlation tables have been brought current here. In particular, this
-- file predates the 0005a/0005b consolidation and does not define `tickets`
-- (or `escalations`/`ticket_comments`/`message_deliveries`), so ticket_id
-- below is left as a bare uuid rather than a dangling REFERENCES tickets(id)
-- -- that FK does exist in production (added by 0005a).

CREATE TABLE IF NOT EXISTS ticket_correlations (
    ticket_id            uuid PRIMARY KEY,
    root_cause_kind      text,
    primary_signature    text,
    signatures           jsonb NOT NULL DEFAULT '[]',
    affected_keys        jsonb NOT NULL DEFAULT '[]',
    summary_base         text,
    description_base     text,
    severity             text,
    occurrence_count     integer NOT NULL DEFAULT 1,
    escalated_at         timestamptz,
    last_alert_at        timestamptz DEFAULT now(),
    created_at           timestamptz DEFAULT now(),
    updated_at           timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ticket_correlations_last_alert_idx
    ON ticket_correlations (last_alert_at DESC);
CREATE INDEX IF NOT EXISTS ticket_correlations_sig_idx
    ON ticket_correlations USING gin (signatures jsonb_path_ops);

-- Full audit trail of every correlation decision -- event-time evidence, so
-- grid_name and the candidate/alert snapshot are preserved here even though
-- they are no longer cached on ticket_correlations. ticket_id is nullable:
-- an event that never resolved to a ticket (a hard failure) still needs to
-- exist as a record of the decision.
CREATE TABLE IF NOT EXISTS ticket_correlation_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       uuid,
    grid_name       text NOT NULL,
    source          text,
    signature       text,
    dedup_key       text,
    decision        text NOT NULL,
    decided_by      text NOT NULL,
    confidence      real,
    reason          text,
    candidate_refs  jsonb NOT NULL DEFAULT '[]',
    alert           jsonb,
    llm_raw         text,
    judgment        jsonb,
    context_availability  jsonb,
    send_decision         boolean,
    send_forced_by        jsonb NOT NULL DEFAULT '[]',
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ticket_correlation_events_grid_idx
    ON ticket_correlation_events (grid_name, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ticket_correlation_events_dedup_idx
    ON ticket_correlation_events (dedup_key) WHERE dedup_key IS NOT NULL;

-- Successful `/chat/notify` Telegram deliveries. This has a separate ledger
-- because `message_deliveries` is ticket/escalation-owned and cannot provide
-- the reliable per-grid alert-reminder clock used by correlation suppression.
CREATE TABLE IF NOT EXISTS notify_alert_deliveries (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    grid_name               text NOT NULL,
    external_chat_id        text NOT NULL,
    external_topic_id       text,
    external_message_id     bigint NOT NULL,
    sent_at                 timestamptz NOT NULL DEFAULT now(),
    source                  text,
    dedup_key               text,
    ticket_id       uuid,
    ticket_ref              text,
    rendered_text           text NOT NULL,
    alert                   jsonb NOT NULL DEFAULT '{}',
    downtime                boolean NOT NULL DEFAULT false,
    CONSTRAINT notify_alert_deliveries_chat_message_uniq
        UNIQUE (external_chat_id, external_message_id)
);
CREATE INDEX IF NOT EXISTS notify_alert_deliveries_grid_sent_idx
    ON notify_alert_deliveries (grid_name, sent_at DESC);
CREATE INDEX IF NOT EXISTS notify_alert_deliveries_grid_downtime_sent_idx
    ON notify_alert_deliveries (grid_name, sent_at DESC)
    WHERE downtime;

ALTER TABLE ticket_correlation_events ADD COLUMN IF NOT EXISTS judgment jsonb;
ALTER TABLE ticket_correlation_events ADD COLUMN IF NOT EXISTS context_availability jsonb;
ALTER TABLE ticket_correlation_events ADD COLUMN IF NOT EXISTS send_decision boolean;
ALTER TABLE ticket_correlation_events
    ADD COLUMN IF NOT EXISTS send_forced_by jsonb NOT NULL DEFAULT '[]';

-- ── Conversation Threads ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id               text PRIMARY KEY,
    session_id              text NOT NULL,
    organization_id         integer,
    issue_type              text CHECK (issue_type IN ('token', 'hps', 'meter', 'transaction', 'commissioning', 'other')),
    status                  text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chat_threads_session_id_idx ON chat_threads (session_id);
CREATE INDEX IF NOT EXISTS chat_threads_organization_id_idx ON chat_threads (organization_id);
CREATE INDEX IF NOT EXISTS chat_threads_issue_type_idx ON chat_threads (issue_type);

-- FK from escalation_mappings to chat_threads (defined after both tables exist)
ALTER TABLE escalation_mappings
    ADD CONSTRAINT IF NOT EXISTS escalation_mappings_thread_id_fkey
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id);

-- ── Per-org metadata ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS org_metadata (
    organization_id     integer PRIMARY KEY,
    telegram_config     jsonb DEFAULT '{}',
    created_at          timestamptz DEFAULT now()
);

-- ── Bot Artifacts (system instructions from DB) ───────────────────────────────
-- Optional: stores versioned system instructions and Q&A pairs.
-- If you use Google Docs for instructions, this table can be empty.

CREATE TABLE IF NOT EXISTS bot_artifacts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_type       artifact_type NOT NULL,
    bot_mode            bot_mode NOT NULL,
    name                text NOT NULL,
    category            text,
    tags                text[],
    content             jsonb NOT NULL,
    version             integer NOT NULL DEFAULT 1,
    is_active           boolean NOT NULL DEFAULT true,
    priority            integer DEFAULT 0,
    metadata            jsonb,
    source              sync_source NOT NULL DEFAULT 'manual',
    google_sheets_id    text,
    google_sheets_name  text,
    google_sheets_row   integer,
    last_synced_at      timestamptz,
    deleted_at          timestamptz,
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now(),
    created_by          text,
    updated_by          text
);

CREATE INDEX IF NOT EXISTS bot_artifacts_mode_type_idx ON bot_artifacts (bot_mode, artifact_type);
CREATE INDEX IF NOT EXISTS bot_artifacts_active_idx ON bot_artifacts (is_active) WHERE is_active = true;

-- ── RAG Documents ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS documents (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id                   text NOT NULL,
    source_type                 text NOT NULL,
    title                       text,
    raw_content                 text NOT NULL,
    content                     text,
    content_hash                text,
    content_type                text NOT NULL,
    metadata                    jsonb DEFAULT '{}',
    allowed_organization_ids    uuid[],       -- NOTE: cast integer org IDs to uuid or adjust type
    allowed_role_ids            text[],
    allowed_user_ids            uuid[],
    ingested_at                 timestamptz DEFAULT now(),
    updated_at                  timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_content_hash_idx ON documents (content_hash);
CREATE INDEX IF NOT EXISTS documents_source_id_idx ON documents (source_id);

CREATE TABLE IF NOT EXISTS chunks (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id         uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    chunk_index         integer NOT NULL,
    content             text NOT NULL,
    embedding           vector(768),
    embedding_model     text,
    embedding_task_type text,
    chunk_metadata      jsonb DEFAULT '{}',
    created_at          timestamptz DEFAULT now(),
    -- Dense vectors are poor at exact token match (part numbers, error codes
    -- like E-402). Generated + STORED so it backfills on write with no
    -- ingestion change. See 0021_chunks_fulltext.sql.
    content_tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED
);

CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks (document_id);

-- Vector similarity index (required for RAG search performance at scale)
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx ON chunks USING gin (content_tsv);

-- GraphRAG entities
CREATE TABLE IF NOT EXISTS entities (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                text NOT NULL,
    type                text NOT NULL,
    description         text,
    embedding           vector(768),
    embedding_model     text,
    embedding_task_type text,
    metadata            jsonb DEFAULT '{}',
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now(),
    UNIQUE (name, type)
);

-- NOTE: the ivfflat index on entities.embedding was removed on 2026-07-11 —
-- its only consumer (the search_entities / get_entity_graph RPCs) was never
-- wired up and has been dropped (see
-- anansi_app/db/migrations/2026-07-11-chat-db-cleanup.sql). The embedding
-- column is retained; re-add the index if entity vector search is implemented.

CREATE TABLE IF NOT EXISTS entity_mentions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       uuid NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
    chunk_id        uuid NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    mention_text    text,
    context         text,
    confidence      float NOT NULL DEFAULT 1.0,
    created_at      timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS relationships (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_entity_id    uuid NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
    target_entity_id    uuid NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
    relationship_type   text NOT NULL,
    description         text,
    strength            float,
    metadata            jsonb DEFAULT '{}',
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS relationship_evidence (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    relationship_id     uuid NOT NULL REFERENCES relationships (id) ON DELETE CASCADE,
    chunk_id            uuid NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
    document_id         uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    evidence_text       text,
    confidence          float NOT NULL DEFAULT 1.0,
    created_at          timestamptz DEFAULT now()
);

-- NOTE: the GraphRAG "communities"/"community_members" tables were removed on
-- 2026-07-11 — never populated or queried by any code path
-- (see anansi_app/db/migrations/2026-07-11-chat-db-cleanup.sql).

-- ── Expert / Workflow Packets ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_work_packets (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    packet_id               text UNIQUE NOT NULL,
    packet_type             text NOT NULL,
    packet_title            text,
    packet_goal             text,
    assigned_expert         text,
    packet_status           text NOT NULL DEFAULT 'pending',
    packet_inputs           jsonb DEFAULT '{}',
    packet_state            jsonb DEFAULT '{}',
    packet_outputs          jsonb DEFAULT '{}',
    organization_id         integer,
    requested_by_email      text,
    requested_in_session    text,
    sessions_involved       text[],
    current_step            text,
    steps_completed         jsonb DEFAULT '[]',
    external_system         text,
    external_id             text,
    external_url            text,
    external_version        text,
    started_at              timestamptz,
    completed_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    state_version           integer NOT NULL DEFAULT 0,
    token_usage             jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS agent_work_packets_status_org_idx ON agent_work_packets (packet_status, organization_id);
CREATE INDEX IF NOT EXISTS agent_work_packets_session_idx ON agent_work_packets (requested_in_session);

CREATE TABLE IF NOT EXISTS agent_work_packet_logs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    packet_id   uuid NOT NULL REFERENCES agent_work_packets (id) ON DELETE CASCADE,
    log_type    text NOT NULL,
    step_name   text,
    message     text NOT NULL,
    input_data  jsonb,
    output_data jsonb,
    error_data  jsonb,
    session_id  text,
    triggered_by text,
    duration_ms  integer,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ── Multi-turn Decisions ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pending_decisions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      text NOT NULL,
    decision_type   text NOT NULL,
    context         jsonb DEFAULT '{}',
    prompt          text NOT NULL,
    created_at      timestamptz DEFAULT now(),
    expires_at      timestamptz DEFAULT now() + INTERVAL '24 hours',
    resolved_at     timestamptz,
    resolution      text
);

CREATE INDEX IF NOT EXISTS pending_decisions_session_id_idx ON pending_decisions (session_id);

-- ── Scheduled Messages ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_type    text NOT NULL,
    payload         jsonb DEFAULT '{}',
    scheduled_for   timestamptz NOT NULL,
    status          text NOT NULL DEFAULT 'pending',
    processed_by    text,
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz,
    result          jsonb,
    retry_count     integer NOT NULL DEFAULT 0
);

-- Partial indexes covering both branches of the claim_scheduled_messages poll
CREATE INDEX IF NOT EXISTS scheduled_messages_pending_idx
    ON scheduled_messages (scheduled_for) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_scheduled_messages_processing
    ON scheduled_messages (processed_at) WHERE status = 'processing';

-- RPC: atomically claim pending scheduled messages (prevents duplicate processing)
CREATE OR REPLACE FUNCTION claim_scheduled_messages(batch_size INT, processor_id TEXT)
RETURNS SETOF scheduled_messages LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    UPDATE scheduled_messages
    SET status = 'processing', processed_by = processor_id, processed_at = now()
    WHERE id IN (
        SELECT id FROM scheduled_messages
        WHERE (status = 'pending' AND scheduled_for <= now())
           OR (status = 'processing' AND processed_at < now() - INTERVAL '5 minutes')
        LIMIT batch_size
        FOR UPDATE SKIP LOCKED
    )
    RETURNING *;
END;
$$;

-- ── User Schedules ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_schedules (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id                 text NOT NULL,
    topic_id                text,
    created_by_user_id      text NOT NULL,
    created_by_email        text,
    organization_id         integer,
    -- Exactly one of command / skill_id is set per row (see the
    -- user_schedules_command_xor_skill_chk / _skill_requires_anchor_chk
    -- constraints below): the pre-existing single-chat command mechanism,
    -- or a skill (Phase 5 of docs/superpowers/plans/2026-08-06-user-designed-skills.md)
    -- fanned out across every eligible anchor_entity_type entity (see
    -- orchestrator/experts/entity_fanout.py). skill_inputs is scoped to
    -- this one schedule, separate from skills.inputs (what a skill accepts
    -- at all).
    command                 text,
    schedule_type           text NOT NULL DEFAULT 'once',
    cron_expression         text,
    timezone                text DEFAULT 'UTC',
    next_run_at             timestamptz,
    is_active               boolean DEFAULT true,
    status                  text DEFAULT 'active',
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    last_run_at             timestamptz,
    run_count               integer DEFAULT 0,
    friendly_name           text,
    user_context            jsonb DEFAULT '{}',
    skill_id                uuid REFERENCES skills (id),
    anchor_entity_type      text,
    skill_inputs            jsonb NOT NULL DEFAULT '{}',
    CONSTRAINT user_schedules_anchor_entity_type_chk
        CHECK (anchor_entity_type IS NULL OR anchor_entity_type IN ('grid', 'organization')),
    CONSTRAINT user_schedules_command_xor_skill_chk
        CHECK ((command IS NOT NULL) <> (skill_id IS NOT NULL)),
    CONSTRAINT user_schedules_skill_requires_anchor_chk
        CHECK ((skill_id IS NULL) = (anchor_entity_type IS NULL))
);

-- At most one schedule per skill (0026); unlimited command-type rows, since
-- Postgres does not treat repeated NULLs as conflicting in a unique index.
CREATE UNIQUE INDEX IF NOT EXISTS user_schedules_skill_id_unique
    ON user_schedules (skill_id)
    WHERE skill_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS user_schedule_logs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id             uuid NOT NULL REFERENCES user_schedules (id) ON DELETE CASCADE,
    executed_at             timestamptz NOT NULL DEFAULT now(),
    -- 'success' | 'failed' | 'skipped' (application-enforced, not a DB
    -- constraint -- this column predates Phase 5 and was never one).
    -- 'skipped' plus error_message-as-reason is how a fanned-out skill run
    -- records "this chat was silently skipped" per the plan's Phase 5,
    -- item 3 -- see run_skill_dispatch.py.
    status                  text NOT NULL,
    result_message          text,
    error_message           text,
    telegram_message_id     text,
    verification_passed     boolean,
    verification_feedback   text,
    execution_time_ms       integer,
    -- Which fan-out target this row is for (a skill run produces one row
    -- per eligible entity per tick, not one row per tick). NULL for the
    -- pre-existing single-chat command path, which only ever has one target.
    anchor_entity_id        text,
    anchor_entity_name      text
);

-- ── User Preferences ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_preferences (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_user_id   text NOT NULL,
    preference_key      text NOT NULL,
    preference_value    text NOT NULL,
    raw_expression      text,
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now(),
    UNIQUE (canonical_user_id, preference_key)
);

-- Distilled historic understanding per grid / per organization, generated by a
-- nightly batch (anansi_app/scripts/episodic_scheduler.py, over
-- shared/episodic_memory.py) and read at render time by EpisodicProvider.
-- NOTE: from 0019 until 2026-08-25 nothing scheduled that batch and repo-root
-- scripts/ was in no deployed image, so this table stayed empty and the
-- episodic context module contributed nothing. Deliberately NOT conversation_summaries, which is per-session
-- and wired to progressive within-session summarization -- a different lifecycle.
--
-- edited_by set = an operator corrected this row by hand; the nightly job leaves
-- it alone thereafter. Uses generated_at rather than updated_at -- do NOT add
-- this table to the update_updated_at trigger loop below.
CREATE TABLE IF NOT EXISTS episodic_distillations (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    anchor_type    text NOT NULL,
    anchor_id      text NOT NULL,
    anchor_name    text,
    summary        text NOT NULL,
    message_count  integer NOT NULL DEFAULT 0,
    covers_from    timestamptz,
    covers_to      timestamptz,
    generated_at   timestamptz NOT NULL DEFAULT now(),
    edited_by      text,
    CONSTRAINT episodic_anchor_type_chk CHECK (anchor_type IN ('grid', 'organization')),
    CONSTRAINT episodic_anchor_unique UNIQUE (anchor_type, anchor_id)
);

CREATE INDEX IF NOT EXISTS episodic_distillations_anchor_idx
    ON episodic_distillations (anchor_type, anchor_id);

-- ── Equipment Actions ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS equipment_actions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_name         text NOT NULL,
    grid_name           text NOT NULL,
    site_id             text,
    requester_email     text NOT NULL,
    requester_user_id   integer,
    chat_id             text,
    session_id          text,
    success             boolean NOT NULL,
    error_message       text,
    api_response        jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- ── Broadcast Messaging ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS broadcasts (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message                 text NOT NULL,
    created_by              text NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    scheduled_for           timestamptz,
    status                  text NOT NULL DEFAULT 'pending',
    target_group_ids        text[] NOT NULL DEFAULT '{}',
    total_recipients        integer DEFAULT 0,
    successful_sends        integer DEFAULT 0,
    failed_sends            integer DEFAULT 0,
    verification_passed     boolean,
    verification_feedback   text,
    metadata                jsonb DEFAULT '{}',
    -- Recurrence (optional). When schedule_type IS NULL/'once' the broadcast is a
    -- one-shot (legacy behaviour). A recurring "template" row carries the cron;
    -- each fire spawns a child "occurrence" row (recurrence_parent_id set) which
    -- holds no image blobs of its own and reads images from its parent template.
    schedule_type           text,         -- NULL/'once' | 'recurring' | 'biweekly'
    cron_expression         text,         -- UTC cron for the recurring template
    timezone                text,         -- timezone the schedule was authored in
    next_run_at             timestamptz,  -- next fire for an active recurring template
    recurrence_parent_id    uuid REFERENCES broadcasts (id) ON DELETE SET NULL
);

-- Look up occurrences by their parent template
CREATE INDEX IF NOT EXISTS idx_broadcasts_recurrence_parent
    ON broadcasts (recurrence_parent_id)
    WHERE recurrence_parent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS broadcast_logs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    broadcast_id            uuid NOT NULL REFERENCES broadcasts (id) ON DELETE CASCADE,
    chat_id                 text NOT NULL,
    chat_name               text,
    enriched_message        text,
    sent_at                 timestamptz,
    success                 boolean NOT NULL,
    telegram_message_id     integer,
    error_message           text
);

CREATE TABLE IF NOT EXISTS broadcast_templates (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text UNIQUE NOT NULL,
    content         text NOT NULL,
    image_attachments jsonb DEFAULT '[]',
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ── RAG Vector Search RPCs ────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION search_chunks(
    query_embedding vector(768),
    match_count     int DEFAULT 10,
    similarity_threshold float DEFAULT 0.5
)
RETURNS TABLE (
    id          uuid,
    document_id uuid,
    content     text,
    similarity  float,
    metadata    jsonb
) LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT c.id, c.document_id, c.content,
           1 - (c.embedding <=> query_embedding) AS similarity,
           c.chunk_metadata AS metadata
    FROM chunks c
    WHERE 1 - (c.embedding <=> query_embedding) > similarity_threshold
    ORDER BY c.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;

-- CONFIRMED 2026-08-19 via SELECT pg_get_functiondef(
-- 'search_chunks_with_permissions'::regproc) against live production --
-- this is the real function, word for word, not a reconstruction. It does
-- NOT use documents.allowed_organization_ids (that column is dead: every
-- row is '{}', its own default). The real filter is per-chunk, on
-- chunk_metadata's allowed_role_ids/allowed_org_ids JSONB arrays: absent or
-- both empty means public; a present non-empty array means the caller's
-- user_role_ids/user_org_ids must overlap it. See the
-- real-permission-model-is-chunk-metadata-not-documents-column memory.
CREATE OR REPLACE FUNCTION search_chunks_with_permissions(
    query_embedding vector,
    match_threshold double precision DEFAULT 0.7,
    match_count integer DEFAULT 10,
    user_role_ids integer[] DEFAULT '{}'::integer[],
    user_org_ids integer[] DEFAULT '{}'::integer[]
)
RETURNS TABLE(chunk_id uuid, document_id uuid, content text, chunk_metadata jsonb, similarity double precision)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT
        c.id as chunk_id,
        c.document_id,
        c.content,
        c.chunk_metadata,
        1 - (c.embedding <=> query_embedding) as similarity
    FROM chunks c
    WHERE 1 - (c.embedding <=> query_embedding) > match_threshold
      AND (
          -- Check allowed_role_ids in chunk_metadata (if user has any matching role)
          (
              c.chunk_metadata ? 'allowed_role_ids'
              AND c.chunk_metadata->'allowed_role_ids' IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM unnest(user_role_ids) AS ur(rid)
                  WHERE c.chunk_metadata->'allowed_role_ids' @> to_jsonb(ur.rid)
              )
          )
          OR
          -- Check allowed_org_ids in chunk_metadata (if user belongs to any matching org)
          (
              c.chunk_metadata ? 'allowed_org_ids'
              AND c.chunk_metadata->'allowed_org_ids' IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM unnest(user_org_ids) AS uo(oid)
                  WHERE c.chunk_metadata->'allowed_org_ids' @> to_jsonb(uo.oid)
              )
          )
          OR
          -- No restrictions = public document (accessible to all)
          (
              NOT (c.chunk_metadata ? 'allowed_role_ids')
              AND NOT (c.chunk_metadata ? 'allowed_org_ids')
          )
          OR
          -- Empty arrays = public document
          (
              c.chunk_metadata->'allowed_role_ids' = '[]'::jsonb
              AND c.chunk_metadata->'allowed_org_ids' = '[]'::jsonb
          )
      )
    ORDER BY c.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;

-- Hybrid dense+sparse search fused with Reciprocal Rank Fusion. Dense vectors
-- are poor at exact token match (part numbers, error codes like E-402); the
-- sparse ranker (content_tsv, see the chunks table above) catches those. RRF
-- rather than a weighted score blend: cosine similarity and ts_rank are on
-- incomparable scales, so any weighting would need retuning per corpus; RRF
-- uses only rank position. Permission filtering matches
-- search_chunks_with_permissions exactly (chunk_metadata-based, not
-- documents.allowed_organization_ids -- see that function's comment above).
CREATE OR REPLACE FUNCTION search_chunks_hybrid(
    query_embedding vector(768),
    query_text      text,
    p_org_ids       integer[] DEFAULT NULL,
    match_count     int       DEFAULT 10,
    rrf_k           int       DEFAULT 60
)
RETURNS TABLE (
    id          uuid,
    document_id uuid,
    content     text,
    score       float,
    metadata    jsonb
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    WITH permitted AS (
        SELECT c.id, c.document_id, c.content, c.chunk_metadata, c.embedding, c.content_tsv
        FROM chunks c
        WHERE p_org_ids IS NULL
           OR (
               (
                   c.chunk_metadata ? 'allowed_role_ids'
                   AND c.chunk_metadata->'allowed_role_ids' IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM unnest('{}'::integer[]) AS ur(rid)
                       WHERE c.chunk_metadata->'allowed_role_ids' @> to_jsonb(ur.rid)
                   )
               )
               OR (
                   c.chunk_metadata ? 'allowed_org_ids'
                   AND c.chunk_metadata->'allowed_org_ids' IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM unnest(p_org_ids) AS uo(oid)
                       WHERE c.chunk_metadata->'allowed_org_ids' @> to_jsonb(uo.oid)
                   )
               )
               OR (
                   NOT (c.chunk_metadata ? 'allowed_role_ids')
                   AND NOT (c.chunk_metadata ? 'allowed_org_ids')
               )
               OR (
                   c.chunk_metadata->'allowed_role_ids' = '[]'::jsonb
                   AND c.chunk_metadata->'allowed_org_ids' = '[]'::jsonb
               )
           )
    ),
    dense AS (
        SELECT p.id,
               row_number() OVER (ORDER BY p.embedding <=> query_embedding) AS rank
        FROM permitted p
        WHERE p.embedding IS NOT NULL
        ORDER BY p.embedding <=> query_embedding
        LIMIT match_count * 4
    ),
    sparse AS (
        SELECT p.id,
               row_number() OVER (
                   ORDER BY ts_rank(p.content_tsv,
                                    websearch_to_tsquery('english', query_text)) DESC
               ) AS rank
        FROM permitted p
        WHERE p.content_tsv @@ websearch_to_tsquery('english', query_text)
        ORDER BY ts_rank(p.content_tsv,
                         websearch_to_tsquery('english', query_text)) DESC
        LIMIT match_count * 4
    ),
    fused AS (
        SELECT COALESCE(d.id, s.id) AS id,
               (
                   COALESCE(1.0 / (rrf_k + d.rank), 0.0)
                 + COALESCE(1.0 / (rrf_k + s.rank), 0.0)
               )::float AS score
        FROM dense d
        FULL OUTER JOIN sparse s ON s.id = d.id
    )
    SELECT p.id, p.document_id, p.content, f.score, p.chunk_metadata
    FROM fused f
    JOIN permitted p ON p.id = f.id
    ORDER BY f.score DESC
    LIMIT match_count;
END;
$$;

-- entities/relationships/entity_mentions carry NO permission columns. The
-- real permission mechanism -- confirmed via pg_get_functiondef against the
-- live search_chunks_with_permissions, see the real-permission-model-is-
-- chunk-metadata-not-documents-column memory -- is chunks.chunk_metadata's
-- allowed_org_ids/allowed_role_ids JSONB arrays (integer, absent-or-both-
-- empty = public), never documents.allowed_organization_ids, which is dead
-- schema (every row is '{}', its own column default). 0020 corrected this
-- function from the documents-column version 0018 originally shipped with,
-- which crashed outright on a real (integer) org id. p_org_ids IS NULL means
-- unrestricted (staff). The role-based branch is structural parity with
-- search_chunks_with_permissions; no caller can drive it yet (no client-side
-- role-name -> numeric-id mapping exists), same honest limitation
-- rag_provider.build_search_arguments documents for user_role_ids. Note both
-- endpoints of a relationship must be visible -- a relationship whose far
-- end is only mentioned in a chunk this caller can't see is not surfaced.
CREATE OR REPLACE FUNCTION summarize_entity_graph(
    p_org_ids    integer[] DEFAULT NULL,
    p_max_types  int       DEFAULT 20,
    p_examples   int       DEFAULT 3
)
RETURNS TABLE (
    kind          text,     -- 'entity' | 'relationship'
    type_name     text,
    item_count    bigint,
    examples      text[]
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    WITH visible_entities AS (
        SELECT DISTINCT e.id, e.name, e.type
        FROM entities e
        WHERE p_org_ids IS NULL
           OR EXISTS (
               SELECT 1
               FROM entity_mentions em
               JOIN chunks c ON c.id = em.chunk_id
               WHERE em.entity_id = e.id
                 AND (
                     (
                         c.chunk_metadata ? 'allowed_role_ids'
                         AND c.chunk_metadata->'allowed_role_ids' IS NOT NULL
                         AND EXISTS (
                             SELECT 1 FROM unnest('{}'::integer[]) AS ur(rid)
                             WHERE c.chunk_metadata->'allowed_role_ids' @> to_jsonb(ur.rid)
                         )
                     )
                     OR (
                         c.chunk_metadata ? 'allowed_org_ids'
                         AND c.chunk_metadata->'allowed_org_ids' IS NOT NULL
                         AND EXISTS (
                             SELECT 1 FROM unnest(p_org_ids) AS uo(oid)
                             WHERE c.chunk_metadata->'allowed_org_ids' @> to_jsonb(uo.oid)
                         )
                     )
                     OR (
                         NOT (c.chunk_metadata ? 'allowed_role_ids')
                         AND NOT (c.chunk_metadata ? 'allowed_org_ids')
                     )
                     OR (
                         c.chunk_metadata->'allowed_role_ids' = '[]'::jsonb
                         AND c.chunk_metadata->'allowed_org_ids' = '[]'::jsonb
                     )
                 )
           )
    ),
    entity_types AS (
        SELECT 'entity'::text AS kind,
               ve.type        AS type_name,
               count(*)       AS item_count,
               (array_agg(ve.name ORDER BY ve.name))[1:p_examples] AS examples
        FROM visible_entities ve
        GROUP BY ve.type
        ORDER BY count(*) DESC
        LIMIT p_max_types
    ),
    rel_types AS (
        SELECT 'relationship'::text  AS kind,
               r.relationship_type   AS type_name,
               count(*)              AS item_count,
               ARRAY[]::text[]       AS examples
        FROM relationships r
        JOIN visible_entities s ON s.id = r.source_entity_id
        JOIN visible_entities t ON t.id = r.target_entity_id
        GROUP BY r.relationship_type
        ORDER BY count(*) DESC
        LIMIT p_max_types
    )
    SELECT * FROM entity_types
    UNION ALL
    SELECT * FROM rel_types;
END;
$$;

-- Same real permission model as search_chunks_with_permissions/
-- search_chunks_hybrid/summarize_entity_graph above: chunks.chunk_metadata,
-- never documents.allowed_organization_ids. Factored into
-- chunk_permission_visible() since three functions below need it
-- identically -- search_chunks_with_permissions/search_chunks_hybrid/
-- summarize_entity_graph predate this helper and still inline their own
-- copy (a worthwhile follow-up, not done retroactively here). A
-- relationship is visible only when BOTH endpoints are.
CREATE OR REPLACE FUNCTION chunk_permission_visible(
    p_chunk_metadata jsonb,
    p_org_ids        integer[]
) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT
        (
            p_chunk_metadata ? 'allowed_role_ids'
            AND p_chunk_metadata->'allowed_role_ids' IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM unnest('{}'::integer[]) AS ur(rid)
                WHERE p_chunk_metadata->'allowed_role_ids' @> to_jsonb(ur.rid)
            )
        )
        OR (
            p_chunk_metadata ? 'allowed_org_ids'
            AND p_chunk_metadata->'allowed_org_ids' IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM unnest(coalesce(p_org_ids, '{}'::integer[])) AS uo(oid)
                WHERE p_chunk_metadata->'allowed_org_ids' @> to_jsonb(uo.oid)
            )
        )
        OR (
            NOT (p_chunk_metadata ? 'allowed_role_ids')
            AND NOT (p_chunk_metadata ? 'allowed_org_ids')
        )
        OR (
            p_chunk_metadata->'allowed_role_ids' = '[]'::jsonb
            AND p_chunk_metadata->'allowed_org_ids' = '[]'::jsonb
        );
$$;

CREATE OR REPLACE FUNCTION search_entities_permitted(
    p_query    text,
    p_org_ids  integer[] DEFAULT NULL,
    p_type     text      DEFAULT NULL,
    p_limit    int       DEFAULT 10
)
RETURNS TABLE (id uuid, name text, type text, description text)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT e.id, e.name, e.type, e.description
    FROM entities e
    WHERE (p_type IS NULL OR e.type = p_type)
      AND e.name ILIKE '%' || p_query || '%'
      AND (
          p_org_ids IS NULL
          OR EXISTS (
              SELECT 1 FROM entity_mentions em
              JOIN chunks c ON c.id = em.chunk_id
              WHERE em.entity_id = e.id
                AND chunk_permission_visible(c.chunk_metadata, p_org_ids)
          )
      )
    ORDER BY length(e.name), e.name
    LIMIT p_limit;
END;
$$;

CREATE OR REPLACE FUNCTION get_entity_neighbors_permitted(
    p_entity_id  uuid,
    p_org_ids    integer[] DEFAULT NULL,
    p_rel_type   text      DEFAULT NULL,
    p_limit      int       DEFAULT 25
)
RETURNS TABLE (
    neighbor_id       uuid,
    neighbor_name     text,
    neighbor_type     text,
    relationship_type text,
    description       text,
    direction         text
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    WITH visible AS (
        SELECT e.id
        FROM entities e
        WHERE p_org_ids IS NULL
           OR EXISTS (
               SELECT 1 FROM entity_mentions em
               JOIN chunks c ON c.id = em.chunk_id
               WHERE em.entity_id = e.id
                 AND chunk_permission_visible(c.chunk_metadata, p_org_ids)
           )
    )
    SELECT t.id, t.name, t.type, r.relationship_type, r.description, 'outgoing'::text
    FROM relationships r
    JOIN entities t ON t.id = r.target_entity_id
    WHERE r.source_entity_id = p_entity_id
      AND (p_rel_type IS NULL OR r.relationship_type = p_rel_type)
      AND r.source_entity_id IN (SELECT id FROM visible)
      AND r.target_entity_id IN (SELECT id FROM visible)
    UNION ALL
    SELECT s.id, s.name, s.type, r.relationship_type, r.description, 'incoming'::text
    FROM relationships r
    JOIN entities s ON s.id = r.source_entity_id
    WHERE r.target_entity_id = p_entity_id
      AND (p_rel_type IS NULL OR r.relationship_type = p_rel_type)
      AND r.source_entity_id IN (SELECT id FROM visible)
      AND r.target_entity_id IN (SELECT id FROM visible)
    LIMIT p_limit;
END;
$$;

CREATE OR REPLACE FUNCTION get_entity_evidence_permitted(
    p_entity_id uuid,
    p_org_ids   integer[] DEFAULT NULL,
    p_limit     int       DEFAULT 5
)
RETURNS TABLE (chunk_id uuid, document_id uuid, document_title text, excerpt text)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT em.chunk_id, em.document_id, d.title, coalesce(em.context, c.content)
    FROM entity_mentions em
    JOIN documents d ON d.id = em.document_id
    JOIN chunks c ON c.id = em.chunk_id
    WHERE em.entity_id = p_entity_id
      AND (p_org_ids IS NULL OR chunk_permission_visible(c.chunk_metadata, p_org_ids))
    ORDER BY em.confidence DESC
    LIMIT p_limit;
END;
$$;

-- ── Bot Artifact RPCs ─────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION get_customer_support_artifacts(p_org_id integer DEFAULT NULL)
RETURNS SETOF bot_artifacts LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT * FROM bot_artifacts
    WHERE bot_mode IN ('customer_support', 'shared')
      AND is_active = true
      AND deleted_at IS NULL
    ORDER BY priority DESC, updated_at DESC;
END;
$$;

CREATE OR REPLACE FUNCTION get_staff_instructions(p_org_id integer DEFAULT NULL)
RETURNS SETOF bot_artifacts LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT * FROM bot_artifacts
    WHERE bot_mode IN ('staff', 'shared')
      AND artifact_type = 'system_instruction'
      AND is_active = true
      AND deleted_at IS NULL
    ORDER BY priority DESC, updated_at DESC;
END;
$$;

CREATE OR REPLACE FUNCTION get_bot_artifacts(
    p_mode      bot_mode DEFAULT NULL,
    p_type      artifact_type DEFAULT NULL,
    p_org_id    integer DEFAULT NULL
)
RETURNS SETOF bot_artifacts LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT * FROM bot_artifacts
    WHERE (p_mode IS NULL OR bot_mode = p_mode OR bot_mode = 'shared')
      AND (p_type IS NULL OR artifact_type = p_type)
      AND is_active = true
      AND deleted_at IS NULL
    ORDER BY priority DESC, updated_at DESC;
END;
$$;

-- ── Org Telegram Topic RPCs ───────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION set_org_telegram_topic(
    p_organization_id   integer,
    p_topic_key         text,
    p_topic_id          text
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO org_metadata (organization_id, telegram_config)
    VALUES (p_organization_id, jsonb_build_object(p_topic_key, p_topic_id))
    ON CONFLICT (organization_id) DO UPDATE
    SET telegram_config = org_metadata.telegram_config || jsonb_build_object(p_topic_key, p_topic_id);
END;
$$;

CREATE OR REPLACE FUNCTION clear_org_telegram_topic(
    p_organization_id   integer,
    p_topic_key         text
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE org_metadata
    SET telegram_config = telegram_config - p_topic_key
    WHERE organization_id = p_organization_id;
END;
$$;

-- ── Prompt Library ────────────────────────────────────────────────────────────
-- Versioned prompt overrides, labels, Google Doc bindings, and knowledge
-- modules. See db/migrations/0006_prompt_library.sql — this block mirrors it
-- for reference; that migration file is the one applied by hand.

CREATE TABLE IF NOT EXISTS prompt_versions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_id    text NOT NULL,
    version      integer NOT NULL,
    body         text NOT NULL,
    checksum     text NOT NULL,
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    created_via  text NOT NULL DEFAULT 'ui',
    CONSTRAINT prompt_versions_via_chk CHECK (created_via IN ('ui', 'api', 'import')),
    CONSTRAINT prompt_versions_unique UNIQUE (prompt_id, version)
);

CREATE INDEX IF NOT EXISTS prompt_versions_prompt_idx
    ON prompt_versions (prompt_id, version DESC);

CREATE TABLE IF NOT EXISTS prompt_labels (
    prompt_id    text NOT NULL,
    label        text NOT NULL,
    version      integer NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    PRIMARY KEY (prompt_id, label)
);

CREATE TABLE IF NOT EXISTS prompt_doc_bindings (
    prompt_id      text PRIMARY KEY,
    doc_id         text NOT NULL,
    last_synced_at timestamptz,
    -- When true, this doc outranks a live DB version instead of losing to
    -- it (see 0009_prompt_doc_override.sql). Default false preserves the
    -- pre-existing DB > doc > bundled order for every binding.
    is_override    boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS knowledge_modules (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                text NOT NULL UNIQUE,
    title               text NOT NULL,
    summary             text NOT NULL,
    body                text,
    tags                text[] NOT NULL DEFAULT '{}',
    scope               text NOT NULL DEFAULT 'global',
    mode                text NOT NULL DEFAULT 'pinned',
    source              text NOT NULL DEFAULT 'manual',
    source_ref          text,
    -- Sheet tab name. NULL means the first tab (or a Doc, which has none).
    source_tab          text,
    -- gdoc only. 'acl_mirror' = resolve only for a caller who can read the
    -- file in Drive. 'published' = resolve for everyone the prompt serves.
    doc_audience        text,
    -- Who chose 'published'. Separate from updated_by, which any later title
    -- edit clobbers.
    doc_audience_set_by text,
    edit_groups         text[] NOT NULL DEFAULT '{}',
    version             integer NOT NULL DEFAULT 1,
    is_active           boolean NOT NULL DEFAULT true,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text,
    CONSTRAINT knowledge_modules_mode_chk CHECK (mode IN ('pinned', 'on_demand')),
    CONSTRAINT knowledge_modules_source_chk
        CHECK (source IN ('manual', 'gdoc', 'ingested', 'graph', 'directory', 'episodic')),
    -- A gdoc or provider-backed module stores no body -- it resolves at
    -- request time. See 0027_doc_backed_modules.sql.
    CONSTRAINT knowledge_modules_body_required_chk
        CHECK (source IN ('gdoc', 'graph', 'directory', 'episodic') OR body IS NOT NULL),
    CONSTRAINT knowledge_modules_doc_audience_chk
        CHECK ((source = 'gdoc' AND doc_audience IN ('acl_mirror', 'published'))
               OR (source <> 'gdoc' AND doc_audience IS NULL)),
    CONSTRAINT knowledge_modules_gdoc_ref_chk
        CHECK (source <> 'gdoc' OR source_ref IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS knowledge_modules_tags_idx
    ON knowledge_modules USING gin (tags);
CREATE INDEX IF NOT EXISTS knowledge_modules_active_idx
    ON knowledge_modules (is_active) WHERE is_active = true;

CREATE TABLE IF NOT EXISTS prompt_knowledge_overrides (
    prompt_id    text NOT NULL,
    module_id    uuid NOT NULL REFERENCES knowledge_modules (id) ON DELETE CASCADE,
    pinned       boolean NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text,
    PRIMARY KEY (prompt_id, module_id)
);

-- User-designed skills (docs/superpowers/plans/2026-08-06-user-designed-skills.md,
-- Phase 3). See db/migrations/0011_skills.sql for the steps element shape.
CREATE TABLE IF NOT EXISTS skills (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug           text NOT NULL UNIQUE,
    title          text NOT NULL,
    summary        text NOT NULL,
    steps          jsonb NOT NULL DEFAULT '[]',
    inputs         jsonb NOT NULL DEFAULT '[]',
    staff_only     boolean NOT NULL DEFAULT true,
    status         text NOT NULL DEFAULT 'active',
    created_by     text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT skills_status_chk CHECK (status IN ('draft', 'active', 'disabled', 'unusable'))
);

-- ── Auto-update triggers ──────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

-- Also expose as update_updated_at_column (used by some handlers)
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DO $$
DECLARE t text;
BEGIN
    FOR t IN VALUES
        ('chat_sessions'), ('agent_work_packets'), ('user_schedules'),
        ('user_preferences'), ('broadcast_templates'), ('documents'),
        ('entities'), ('relationships'),
        ('bot_artifacts'), ('internal_tickets'), ('ticket_correlations'),
        ('prompt_labels'), ('knowledge_modules'), ('prompt_knowledge_overrides'),
        ('skills')
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS %I ON %I; CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION update_updated_at()',
            'trg_' || t || '_updated_at', t, 'trg_' || t || '_updated_at', t
        );
    END LOOP;
END $$;
