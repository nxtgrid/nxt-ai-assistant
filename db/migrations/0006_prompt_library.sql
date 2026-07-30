-- 0006_prompt_library.sql
-- Prompt library: versioned overrides, labels, doc bindings, knowledge modules.
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe to
-- re-run. No application code requires these tables to exist — every DB path
-- degrades to bundled prompt resolution when they are absent.

BEGIN;

-- ── Prompt versions (append-only) ────────────────────────────────────────────
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

-- ── Labels (which version is live) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_labels (
    prompt_id    text NOT NULL,
    label        text NOT NULL,
    version      integer NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    PRIMARY KEY (prompt_id, label)
);

-- ── Google Doc bindings (one adapter, configured per prompt) ─────────────────
CREATE TABLE IF NOT EXISTS prompt_doc_bindings (
    prompt_id      text PRIMARY KEY,
    doc_id         text NOT NULL,
    last_synced_at timestamptz
);

-- ── Knowledge modules ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_modules (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug         text NOT NULL UNIQUE,
    title        text NOT NULL,
    summary      text NOT NULL,
    body         text NOT NULL,
    tags         text[] NOT NULL DEFAULT '{}',
    scope        text NOT NULL DEFAULT 'sector',
    mode         text NOT NULL DEFAULT 'pinned',
    source       text NOT NULL DEFAULT 'manual',
    source_ref   text,
    edit_groups  text[] NOT NULL DEFAULT '{}',
    version      integer NOT NULL DEFAULT 1,
    is_active    boolean NOT NULL DEFAULT true,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text,
    CONSTRAINT knowledge_modules_mode_chk CHECK (mode IN ('pinned', 'on_demand')),
    CONSTRAINT knowledge_modules_source_chk
        CHECK (source IN ('manual', 'gdoc', 'ingested'))
);

CREATE INDEX IF NOT EXISTS knowledge_modules_tags_idx
    ON knowledge_modules USING gin (tags);
CREATE INDEX IF NOT EXISTS knowledge_modules_active_idx
    ON knowledge_modules (is_active) WHERE is_active = true;

-- ── Per-prompt knowledge overrides ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_knowledge_overrides (
    prompt_id    text NOT NULL,
    module_id    uuid NOT NULL REFERENCES knowledge_modules (id) ON DELETE CASCADE,
    pinned       boolean NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text,
    PRIMARY KEY (prompt_id, module_id)
);

-- ── Auto-update triggers ──────────────────────────────────────────────────────
-- Reuses update_updated_at(), created by an earlier migration and already
-- live in the database (see db/schema/chat_db.sql's own trigger block).
-- prompt_versions is append-only (no updated_at) and prompt_doc_bindings
-- tracks last_synced_at instead, so neither needs one.

DROP TRIGGER IF EXISTS trg_prompt_labels_updated_at ON prompt_labels;
CREATE TRIGGER trg_prompt_labels_updated_at
    BEFORE UPDATE ON prompt_labels
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

DROP TRIGGER IF EXISTS trg_knowledge_modules_updated_at ON knowledge_modules;
CREATE TRIGGER trg_knowledge_modules_updated_at
    BEFORE UPDATE ON knowledge_modules
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

DROP TRIGGER IF EXISTS trg_prompt_knowledge_overrides_updated_at ON prompt_knowledge_overrides;
CREATE TRIGGER trg_prompt_knowledge_overrides_updated_at
    BEFORE UPDATE ON prompt_knowledge_overrides
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMIT;
