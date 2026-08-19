-- 0018_summarize_entity_graph.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 6 of docs/superpowers/plans/2026-08-20-p1-resolvable-context-modules.md.
--
-- entities/relationships/entity_mentions carry NO permission columns. The only
-- path to row-level permission is
--   entities -> entity_mentions -> documents.allowed_organization_ids
-- so every aggregate here goes through that join. p_org_ids IS NULL means
-- unrestricted (staff), matching search_chunks_with_permissions' convention.

BEGIN;

CREATE OR REPLACE FUNCTION summarize_entity_graph(
    p_org_ids    uuid[] DEFAULT NULL,
    p_max_types  int    DEFAULT 20,
    p_examples   int    DEFAULT 3
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
               JOIN documents d ON d.id = em.document_id
               WHERE em.entity_id = e.id
                 AND d.allowed_organization_ids && p_org_ids
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
