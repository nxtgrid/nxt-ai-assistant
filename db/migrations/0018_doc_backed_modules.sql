-- 0018_doc_backed_modules.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- docs/superpowers/plans/2026-08-20-doc-backed-context-modules.md.
-- A gdoc module stores no body and carries an explicit audience decision.
-- Also renames the catch-all scope from 'sector' to 'global'.

BEGIN;

ALTER TABLE knowledge_modules ADD COLUMN IF NOT EXISTS source_tab          text;
ALTER TABLE knowledge_modules ADD COLUMN IF NOT EXISTS doc_audience        text;
ALTER TABLE knowledge_modules ADD COLUMN IF NOT EXISTS doc_audience_set_by text;

-- Safe default for anything that predates this migration. acl_mirror
-- tightens (fewer callers see it), never loosens.
UPDATE knowledge_modules
    SET doc_audience = 'acl_mirror'
    WHERE source = 'gdoc' AND doc_audience IS NULL;

-- Today's constraint exempts only graph/directory/episodic, forcing a stored
-- body on exactly the source that must not have one.
ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_body_required_chk;
ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_body_required_chk
        CHECK (source IN ('gdoc', 'graph', 'directory', 'episodic') OR body IS NOT NULL);

ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_doc_audience_chk;
ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_doc_audience_chk
        CHECK ((source = 'gdoc' AND doc_audience IN ('acl_mirror', 'published'))
               OR (source <> 'gdoc' AND doc_audience IS NULL));

ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_gdoc_ref_chk;
ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_gdoc_ref_chk
        CHECK (source <> 'gdoc' OR source_ref IS NOT NULL);

-- 'sector' -> 'global'. RequestScope.matches() accepts both permanently, so
-- a row missed here still resolves rather than going silently dark.
UPDATE knowledge_modules SET scope = 'global' WHERE scope = 'sector';
ALTER TABLE knowledge_modules ALTER COLUMN scope SET DEFAULT 'global';

COMMIT;

-- Report what changed. Any pre-existing gdoc row tightens from "everyone"
-- to "ACL-gated" -- a real behaviour change. No code path can currently
-- create one, so the expected count is 0.
SELECT source, doc_audience, count(*)
    FROM knowledge_modules
    GROUP BY source, doc_audience
    ORDER BY source;
