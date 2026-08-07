-- 0012_message_archive.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe
-- to re-run -- ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
--
-- Phase 4 of docs/superpowers/plans/2026-08-06-user-designed-skills.md.
-- Backs the skill builder's Rewind button: archiving a message (and every
-- message after it in the session) is how "rewind and rerun" works -- there
-- is no LangGraph checkpoint to unwind (full_conversation_graph.py's builder
-- is a bare .compile(), no checkpointer), so archival is the entire
-- mechanism. NULL means "live"; a timestamp means "excluded from every
-- history read as of that time" -- see get_messages/get_messages_filtered/
-- get_messages_around_timestamp in
-- chat_orchestrator/orchestrator/services/supabase_client.py, all three of
-- which filter archived_at IS NULL at the query level so no caller (direct
-- or via init_services.py's three load sites) can see an archived row by
-- forgetting to ask.
--
-- Archiving is permanent and non-branching: rewinding to step 2 of a 5-step
-- session archives steps 3-5, and there is no "unarchive" -- the user
-- re-does steps 3+ by hand. Side effects those steps already caused (a
-- ticket filed, a message sent) are not rolled back.

BEGIN;

ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS archived_at timestamptz;

CREATE INDEX IF NOT EXISTS chat_messages_archived_idx
    ON chat_messages (session_id) WHERE archived_at IS NULL;

COMMIT;
