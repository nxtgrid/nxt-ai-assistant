-- 0021_chunks_fulltext.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 1 of docs/superpowers/plans/2026-08-23-p4-hybrid-agentic-retrieval.md.
--
-- Dense vectors are poor at exact token match, and this corpus is full of part
-- numbers, serial codes and error codes (QH611A, E-402, RP1000). A query for
-- "E-402" retrieves chunks *about errors* rather than the chunk containing
-- E-402. A generated tsvector backfills on creation and stays correct without
-- any ingestion change.
--
-- NOTE: this rewrites the chunks table. 1,147 rows as of 2026-08-19 -- instant.
-- Re-check size before running if the corpus has grown by orders of magnitude.

BEGIN;

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED;

CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx ON chunks USING gin (content_tsv);

COMMIT;
