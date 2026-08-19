-- 0017_context_module_providers.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 1 of docs/superpowers/plans/2026-08-20-p1-resolvable-context-modules.md.
-- Lets a knowledge module declare that its body comes from a provider resolved
-- at render time rather than from the `body` column. Existing rows are all
-- 'manual' or 'ingested' and are unaffected.

BEGIN;

ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_source_chk;

ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_source_chk
        CHECK (source IN ('manual', 'gdoc', 'ingested', 'graph', 'directory', 'episodic'));

-- A provider-backed module stores no body.
ALTER TABLE knowledge_modules
    ALTER COLUMN body DROP NOT NULL;

ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_body_required_chk;

ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_body_required_chk
        CHECK (source IN ('graph', 'directory', 'episodic') OR body IS NOT NULL);

COMMIT;
