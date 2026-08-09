-- 0015_prompt_model_overrides.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe
-- to re-run. No application code requires this table to exist -- resolution
-- degrades to each prompt's frontmatter `model` field when absent, same
-- pattern as prompt_doc_bindings (0006_prompt_library.sql).

BEGIN;

CREATE TABLE IF NOT EXISTS prompt_model_overrides (
    prompt_id    text PRIMARY KEY,
    tier         text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    CONSTRAINT prompt_model_overrides_tier_chk CHECK (tier IN ('thinking', 'fast', 'lite'))
);

DROP TRIGGER IF EXISTS trg_prompt_model_overrides_updated_at ON prompt_model_overrides;
CREATE TRIGGER trg_prompt_model_overrides_updated_at
    BEFORE UPDATE ON prompt_model_overrides
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMIT;
