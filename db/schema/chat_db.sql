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
    thread_id                       text
);

CREATE INDEX IF NOT EXISTS chat_messages_session_id_idx ON chat_messages (session_id);
CREATE INDEX IF NOT EXISTS chat_messages_session_index_idx ON chat_messages (session_id, message_index);

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
    organization_id         integer,
    escalation_topic_id     integer,
    is_active               boolean DEFAULT true,
    created_at              timestamptz DEFAULT now(),
    resolved_at             timestamptz,
    question_text           text,
    thread_id               text
);

CREATE INDEX IF NOT EXISTS escalation_mappings_session_id_idx ON escalation_mappings (session_id);
CREATE INDEX IF NOT EXISTS escalation_mappings_customer_chat_id_idx ON escalation_mappings (customer_chat_id);
CREATE INDEX IF NOT EXISTS escalation_mappings_thread_id_idx ON escalation_mappings (thread_id);

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
    created_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks (document_id);

-- Vector similarity index (required for RAG search performance at scale)
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

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

CREATE INDEX IF NOT EXISTS entities_embedding_idx ON entities
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

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

CREATE TABLE IF NOT EXISTS communities (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title               text,
    summary             text,
    level               integer NOT NULL DEFAULT 0,
    parent_community_id uuid REFERENCES communities (id) ON DELETE CASCADE,
    embedding           vector(768),
    embedding_model     text,
    metadata            jsonb DEFAULT '{}',
    created_at          timestamptz DEFAULT now(),
    updated_at          timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS community_members (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    community_id    uuid NOT NULL REFERENCES communities (id) ON DELETE CASCADE,
    entity_id       uuid NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
    rank            float NOT NULL DEFAULT 1.0,
    created_at      timestamptz DEFAULT now(),
    UNIQUE (community_id, entity_id)
);

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
    updated_at              timestamptz NOT NULL DEFAULT now()
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

-- ── Persistent Agents ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS persistent_agent_instances (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    expert_id           text NOT NULL,
    instance_name       text NOT NULL,
    anchor_entity_type  text NOT NULL,
    anchor_entity_id    text NOT NULL,
    anchor_metadata     jsonb DEFAULT '{}',
    thread_id           text NOT NULL UNIQUE,
    status              text NOT NULL DEFAULT 'initializing',
    metadata            jsonb DEFAULT '{}',
    last_woke_at        timestamptz,
    last_acted_at       timestamptz,
    wake_count          integer DEFAULT 0,
    error_message       text,
    wake_schedule       text,
    organization_id     integer NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text,
    created_by_user_id  text,
    check_prompt        text,
    response_prompt     text,
    notify_chat_id      text,
    notify_topic_id     text,
    auto_complete       boolean DEFAULT false,
    user_context        jsonb DEFAULT '{}',
    weekly_summaries    jsonb DEFAULT '{}',
    last_compacted_at   timestamptz,
    subscribers         jsonb DEFAULT '[]',
    UNIQUE (expert_id, anchor_entity_id),
    CONSTRAINT valid_agent_status CHECK (status IN (
        'initializing', 'active', 'executing', 'paused', 'error', 'terminated'
    ))
);

CREATE INDEX IF NOT EXISTS persistent_agent_instances_active_idx
    ON persistent_agent_instances (status)
    WHERE status IN ('active', 'executing');
CREATE INDEX IF NOT EXISTS persistent_agent_instances_org_idx ON persistent_agent_instances (organization_id);

CREATE TABLE IF NOT EXISTS agent_events (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    target_instance_id  uuid NOT NULL REFERENCES persistent_agent_instances (id) ON DELETE CASCADE,
    event_type          text NOT NULL,
    event_data          jsonb NOT NULL DEFAULT '{}',
    source_message_id   text,
    status              text NOT NULL DEFAULT 'pending',
    processed_at        timestamptz,
    result              jsonb,
    error               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT valid_event_status CHECK (status IN ('pending', 'processing', 'done', 'failed'))
);

CREATE INDEX IF NOT EXISTS agent_events_pending_idx
    ON agent_events (target_instance_id, created_at)
    WHERE status = 'pending';

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

-- RPC: atomically claim agent events for processing
CREATE OR REPLACE FUNCTION claim_agent_events(p_instance_id UUID, batch_size INT DEFAULT 10)
RETURNS SETOF agent_events LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    UPDATE agent_events
    SET status = 'processing'
    WHERE id IN (
        SELECT id FROM agent_events
        WHERE target_instance_id = p_instance_id AND status = 'pending'
        ORDER BY created_at
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
    command                 text NOT NULL,
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
    user_context            jsonb DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS user_schedule_logs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id             uuid NOT NULL REFERENCES user_schedules (id) ON DELETE CASCADE,
    executed_at             timestamptz NOT NULL DEFAULT now(),
    status                  text NOT NULL,
    result_message          text,
    error_message           text,
    telegram_message_id     text,
    verification_passed     boolean,
    verification_feedback   text,
    execution_time_ms       integer
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
    metadata                jsonb DEFAULT '{}'
);

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

CREATE OR REPLACE FUNCTION search_chunks_with_permissions(
    query_embedding         vector(768),
    p_organization_id       integer DEFAULT NULL,
    match_count             int DEFAULT 10,
    similarity_threshold    float DEFAULT 0.5
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
    JOIN documents d ON c.document_id = d.id
    WHERE 1 - (c.embedding <=> query_embedding) > similarity_threshold
      AND (
          p_organization_id IS NULL
          OR p_organization_id::text::uuid = ANY(d.allowed_organization_ids)
      )
    ORDER BY c.embedding <=> query_embedding
    LIMIT match_count;
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
        ('entities'), ('relationships'), ('persistent_agent_instances'),
        ('bot_artifacts')
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS %I ON %I; CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION update_updated_at()',
            'trg_' || t || '_updated_at', t, 'trg_' || t || '_updated_at', t
        );
    END LOOP;
END $$;
