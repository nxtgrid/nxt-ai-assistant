-- 0024_fix_search_chunks_hybrid_score_type.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- 0022_search_chunks_hybrid.sql, applied 2026-08-19, fails on every call:
--
--   ERROR: structure of query does not match function result type
--   DETAIL: Returned type numeric does not match expected type double
--   precision in column 4.
--
-- Confirmed live: the RRF score expression
-- (COALESCE(1.0 / (rrf_k + d.rank), 0.0) + COALESCE(1.0 / (rrf_k + s.rank), 0.0))
-- evaluates to `numeric` in Postgres (dividing a numeric literal by an
-- integer), but the function's RETURNS TABLE declares `score float`
-- (double precision) -- and RETURNS TABLE requires an exact type match, it
-- does not implicitly widen numeric to double precision the way a plain
-- SELECT would. Fixed by casting the fused expression to float explicitly.
-- Same fix in db/schema/chat_db.sql so a fresh recreate doesn't reintroduce
-- this from 0022 alone.

BEGIN;

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

COMMIT;
