-- 0009_prompt_doc_override.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe
-- to re-run -- ADD COLUMN IF NOT EXISTS.
--
-- Adds the "override" flag to prompt_doc_bindings (0006_prompt_library.sql):
-- when true, a prompt's attached Google Doc outranks a live DB version
-- instead of losing to it. DEFAULT false preserves today's resolution order
-- (DB, then doc, then bundled) for every existing binding -- this migration
-- changes no prompt's live behavior by itself.

BEGIN;

ALTER TABLE prompt_doc_bindings
    ADD COLUMN IF NOT EXISTS is_override boolean NOT NULL DEFAULT false;

COMMIT;
