-- Chat DB cleanup: drop dead tables/views/RPCs and redundant indexes.
-- Run in the Supabase SQL editor (prod Chat DB).
--
-- Evidence (2026-07-11 audit): every object dropped here has zero references
-- in active code (chat_orchestrator, mcp_servers, rag_pipeline, anansi_app,
-- shared, scripts) and zero index/table scans attributable to app traffic
-- since the stats reset on 2025-10-10. Non-empty legacy tables are MOVED to a
-- `graveyard` schema (out of the PostgREST-exposed `public` schema) instead of
-- dropped, so this is reversible with ALTER TABLE ... SET SCHEMA public.
--
-- Companion script: 2026-07-11-chat-db-retention.sql (data pruning — run that
-- one AFTER this one).

BEGIN;

-- ── 1. Dead views (legacy RAG sync / access-control / GraphRAG read models) ──
DROP VIEW IF EXISTS chunks_with_context;
DROP VIEW IF EXISTS documents_with_access;
DROP VIEW IF EXISTS entity_mentions_detailed;
DROP VIEW IF EXISTS entity_relationships_summary;
DROP VIEW IF EXISTS community_hierarchy;
DROP VIEW IF EXISTS latest_sync_status;
DROP VIEW IF EXISTS sync_run_stats;
DROP VIEW IF EXISTS telegram_topic_access;
DROP VIEW IF EXISTS user_telegram_access_flat;

-- ── 2. Dead RPCs (no callers anywhere in the codebase) ──
-- Dropped by name via pg_proc so we don't need exact signatures.
DO $$
DECLARE fn record;
BEGIN
    FOR fn IN
        SELECT p.oid::regprocedure AS sig
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname IN (
            -- document ACL family (documents.allowed_role_ids/allowed_user_ids path, never used)
            'add_organization_to_document', 'add_user_to_document',
            'can_user_access_document', 'check_document_access',
            'clear_document_user_allowlist',
            'get_documents_for_organization', 'get_documents_for_user',
            'remove_organization_from_document', 'remove_user_from_document',
            -- legacy RAG incremental-sync family
            'get_or_create_source', 'start_sync_run', 'complete_sync_run',
            'get_last_successful_sync', 'upsert_artifact_from_sync',
            -- legacy access-control lookups
            'get_source_access', 'get_user_telegram_chat_ids',
            'get_user_telegram_chat_topics', 'search_chunks_for_current_user',
            -- GraphRAG graph queries never wired up
            'search_communities', 'search_entities', 'get_entity_graph',
            -- packet summary helper, no callers
            'get_packet_summary_for_session',
            -- schedule helper superseded by direct table reads
            'get_chat_schedules'
        )
    LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', fn.sig);
    END LOOP;
END $$;

-- ── 3. Non-empty legacy tables → graveyard schema (reversible archive) ──
CREATE SCHEMA IF NOT EXISTS graveyard;
ALTER TABLE IF EXISTS source_access_control SET SCHEMA graveyard;  -- 67 rows, legacy access control
ALTER TABLE IF EXISTS ingestion_sources     SET SCHEMA graveyard;  -- 6 rows, legacy sync registry
ALTER TABLE IF EXISTS artifact_versions     SET SCHEMA graveyard;  -- 289 rows, bot_artifacts version sidecar (bot_artifacts itself stays)

-- ── 4. Empty dead tables ──
-- CASCADE because some of these legacy tables have FKs to each other
-- (e.g. batch_sync_runs -> sync_runs). CASCADE only removes dependent objects
-- such as FK constraints/views, never a whole referencing table — and every
-- table that references one of these is itself in this drop list.
DROP TABLE IF EXISTS sync_runs CASCADE;
DROP TABLE IF EXISTS sync_state CASCADE;
DROP TABLE IF EXISTS batch_sync_runs CASCADE;
DROP TABLE IF EXISTS batch_ingestion_runs CASCADE;
DROP TABLE IF EXISTS gdrive_metadata CASCADE;
DROP TABLE IF EXISTS github_metadata CASCADE;
DROP TABLE IF EXISTS meeting_metadata CASCADE;
DROP TABLE IF EXISTS telegram_metadata CASCADE;
DROP TABLE IF EXISTS mqtt_rate_limits CASCADE;
DROP TABLE IF EXISTS user_telegram_access CASCADE;
DROP TABLE IF EXISTS source_access_control_backup CASCADE;
DROP TABLE IF EXISTS source_access_control_backup_uuid CASCADE;
DROP TABLE IF EXISTS artifact_sync_log CASCADE;
DROP TABLE IF EXISTS community_members CASCADE;   -- before communities (FK)
DROP TABLE IF EXISTS communities CASCADE;

-- ── 5. Redundant / never-scanned indexes ──
-- Exact duplicates of UNIQUE constraints on the same column (unique index stays):
DROP INDEX IF EXISTS idx_chat_sessions_session_id;         -- dup of chat_sessions_session_id_key
DROP INDEX IF EXISTS idx_escalation_mappings_message_id;   -- dup of escalation_mappings_escalation_message_id_key
-- 0 scans since 2025-10-10:
DROP INDEX IF EXISTS idx_entities_embedding;               -- 2 MB; search_entities RPC (its only consumer) is dropped above
DROP INDEX IF EXISTS idx_chat_messages_thread;
DROP INDEX IF EXISTS idx_grafana_panels_gin;
DROP INDEX IF EXISTS idx_documents_allowed_roles;
DROP INDEX IF EXISTS idx_documents_allowed_users;
DROP INDEX IF EXISTS idx_documents_metadata;
DROP INDEX IF EXISTS idx_bot_artifacts_tags;

-- ── 6. New index: second partial for the claim_scheduled_messages OR-branch ──
-- The RPC polls (status='pending' AND scheduled_for<=now()) OR
-- (status='processing' AND processed_at<now()-5min). The pending branch has a
-- partial index; the processing branch forced a seq scan on every poll
-- (536k seq scans since the stats reset).
CREATE INDEX IF NOT EXISTS idx_scheduled_messages_processing
    ON scheduled_messages (processed_at) WHERE status = 'processing';

COMMIT;
