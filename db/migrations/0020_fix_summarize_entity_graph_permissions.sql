-- 0020_fix_summarize_entity_graph_permissions.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 0 of docs/superpowers/plans/2026-08-23-p4-hybrid-agentic-retrieval.md
-- (found while starting Phase 1, not part of the original plan -- see the
-- real-permission-model-is-chunk-metadata-not-documents-column memory).
--
-- 0018_summarize_entity_graph.sql shipped with p_org_ids uuid[], joining
-- entity_mentions -> documents.allowed_organization_ids. That assumption was
-- wrong on two counts, confirmed by pulling the real production
-- search_chunks_with_permissions body with pg_get_functiondef:
--
--   1. documents.allowed_organization_ids is never read by the actual
--      permission-filtered search function -- it appears to be dead schema.
--      Every document's array is '{}' (the column default) in production.
--   2. The real filter lives on chunks.chunk_metadata as JSONB
--      allowed_org_ids/allowed_role_ids arrays (integer, not uuid),
--      absent-or-both-empty meaning public, a present non-empty array
--      meaning "caller's ids must overlap it".
--
-- Consequence: summarize_entity_graph(p_org_ids => ARRAY['some-integer-org-id'])
-- has been crashing outright since 0018 shipped -- "invalid input syntax for
-- type uuid" -- for any non-staff caller with a real (integer) organization
-- id. Latent only because nothing has attached the entity-graph module to a
-- live prompt yet.
--
-- This migration: p_org_ids becomes integer[] (matching every other org id
-- in this system), and the visibility join goes through
-- entity_mentions -> chunks.chunk_metadata instead of documents. The
-- role-based branch is included for structural parity with
-- search_chunks_with_permissions but is not yet drivable by any caller (no
-- client-side role-name -> numeric-role-id mapping exists) -- same honest
-- limitation RAGProvider.build_search_arguments already documents for
-- search_chunks_with_permissions itself. p_org_ids IS NULL still means
-- unrestricted (staff).

BEGIN;

CREATE OR REPLACE FUNCTION summarize_entity_graph(
    p_org_ids    integer[] DEFAULT NULL,
    p_max_types  int       DEFAULT 20,
    p_examples   int       DEFAULT 3
)
RETURNS TABLE (
    kind          text,     -- 'entity' | 'relationship'
    type_name     text,
    item_count    bigint,
    examples      text[]
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    WITH visible_entities AS (
        SELECT DISTINCT e.id, e.name, e.type
        FROM entities e
        WHERE p_org_ids IS NULL
           OR EXISTS (
               SELECT 1
               FROM entity_mentions em
               JOIN chunks c ON c.id = em.chunk_id
               WHERE em.entity_id = e.id
                 AND (
                     -- Role-restricted: structurally present for parity with
                     -- search_chunks_with_permissions, but '{}'::integer[]
                     -- (no caller can supply real role ids yet) means this
                     -- branch can never fire today -- see the migration note.
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
           )
    ),
    entity_types AS (
        SELECT 'entity'::text AS kind,
               ve.type        AS type_name,
               count(*)       AS item_count,
               (array_agg(ve.name ORDER BY ve.name))[1:p_examples] AS examples
        FROM visible_entities ve
        GROUP BY ve.type
        ORDER BY count(*) DESC
        LIMIT p_max_types
    ),
    rel_types AS (
        SELECT 'relationship'::text  AS kind,
               r.relationship_type   AS type_name,
               count(*)              AS item_count,
               ARRAY[]::text[]       AS examples
        FROM relationships r
        JOIN visible_entities s ON s.id = r.source_entity_id
        JOIN visible_entities t ON t.id = r.target_entity_id
        GROUP BY r.relationship_type
        ORDER BY count(*) DESC
        LIMIT p_max_types
    )
    SELECT * FROM entity_types
    UNION ALL
    SELECT * FROM rel_types;
END;
$$;

COMMIT;
