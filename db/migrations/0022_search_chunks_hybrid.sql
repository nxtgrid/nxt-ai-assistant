-- 0022_search_chunks_hybrid.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
-- Requires 0021_chunks_fulltext.sql and 0020_fix_summarize_entity_graph_permissions.sql
-- (the latter not for a functional dependency, but because both share the
-- same permission-filtering pattern below -- apply 0020 first so a reviewer
-- sees the pattern already established once).
--
-- Runs a dense (pgvector) and a sparse (tsvector/BM25-ish) ranker over the same
-- permission-filtered candidate set and fuses them with Reciprocal Rank Fusion.
--
-- RRF rather than a weighted score blend: cosine similarity and ts_rank are on
-- incomparable scales, so any weighting would need retuning per corpus. RRF
-- uses only rank position, so it needs no normalisation and no tuning.
--
-- Permission filtering matches search_chunks_with_permissions exactly -- NOT
-- documents.allowed_organization_ids, which the real function never reads
-- (every document's array is '{}', confirmed empty in production; see the
-- real-permission-model-is-chunk-metadata-not-documents-column memory). The
-- actual mechanism is chunks.chunk_metadata's allowed_org_ids/allowed_role_ids
-- JSONB arrays: absent-or-both-empty means public, a present non-empty array
-- means the caller's ids must overlap it. p_org_ids integer[] (not uuid[]) to
-- match every other org id in this system. The role branch is structural
-- parity with search_chunks_with_permissions but not yet drivable by any
-- caller (no client-side role-name -> numeric-id mapping exists).

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
               COALESCE(1.0 / (rrf_k + d.rank), 0.0)
             + COALESCE(1.0 / (rrf_k + s.rank), 0.0) AS score
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
