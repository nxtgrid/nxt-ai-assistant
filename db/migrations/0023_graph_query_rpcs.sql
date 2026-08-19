-- 0023_graph_query_rpcs.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 2 of docs/superpowers/plans/2026-08-23-p4-hybrid-agentic-retrieval.md.
--
-- Same real permission model as search_chunks_with_permissions/
-- summarize_entity_graph/search_chunks_hybrid: chunks.chunk_metadata's
-- allowed_org_ids/allowed_role_ids JSONB arrays (integer, absent-or-both-
-- empty = public), never documents.allowed_organization_ids -- confirmed
-- dead schema (every row is '{}', its own default; the real function never
-- reads it). See the real-permission-model-is-chunk-metadata-not-documents-
-- column memory. p_org_ids IS NULL means unrestricted (staff).
--
-- Three functions here all need the identical per-chunk visibility check;
-- factored into chunk_permission_visible() rather than pasting it a third
-- time in one file. 0020/0022 predate this helper and still inline their
-- own copy -- a worthwhile follow-up cleanup, not done here to keep this
-- migration's diff to what Phase 2 actually needs.
--
-- A relationship is visible only when BOTH endpoints are: an edge whose far
-- end is only mentioned in a chunk this caller can't see must not be
-- traversable.

BEGIN;

CREATE OR REPLACE FUNCTION chunk_permission_visible(
    p_chunk_metadata jsonb,
    p_org_ids        integer[]
) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT
        (
            p_chunk_metadata ? 'allowed_role_ids'
            AND p_chunk_metadata->'allowed_role_ids' IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM unnest('{}'::integer[]) AS ur(rid)
                WHERE p_chunk_metadata->'allowed_role_ids' @> to_jsonb(ur.rid)
            )
        )
        OR (
            p_chunk_metadata ? 'allowed_org_ids'
            AND p_chunk_metadata->'allowed_org_ids' IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM unnest(coalesce(p_org_ids, '{}'::integer[])) AS uo(oid)
                WHERE p_chunk_metadata->'allowed_org_ids' @> to_jsonb(uo.oid)
            )
        )
        OR (
            NOT (p_chunk_metadata ? 'allowed_role_ids')
            AND NOT (p_chunk_metadata ? 'allowed_org_ids')
        )
        OR (
            p_chunk_metadata->'allowed_role_ids' = '[]'::jsonb
            AND p_chunk_metadata->'allowed_org_ids' = '[]'::jsonb
        );
$$;

CREATE OR REPLACE FUNCTION search_entities_permitted(
    p_query    text,
    p_org_ids  integer[] DEFAULT NULL,
    p_type     text      DEFAULT NULL,
    p_limit    int       DEFAULT 10
)
RETURNS TABLE (id uuid, name text, type text, description text)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT e.id, e.name, e.type, e.description
    FROM entities e
    WHERE (p_type IS NULL OR e.type = p_type)
      AND e.name ILIKE '%' || p_query || '%'
      AND (
          p_org_ids IS NULL
          OR EXISTS (
              SELECT 1 FROM entity_mentions em
              JOIN chunks c ON c.id = em.chunk_id
              WHERE em.entity_id = e.id
                AND chunk_permission_visible(c.chunk_metadata, p_org_ids)
          )
      )
    ORDER BY length(e.name), e.name
    LIMIT p_limit;
END;
$$;

CREATE OR REPLACE FUNCTION get_entity_neighbors_permitted(
    p_entity_id  uuid,
    p_org_ids    integer[] DEFAULT NULL,
    p_rel_type   text      DEFAULT NULL,
    p_limit      int       DEFAULT 25
)
RETURNS TABLE (
    neighbor_id       uuid,
    neighbor_name     text,
    neighbor_type     text,
    relationship_type text,
    description       text,
    direction         text
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    WITH visible AS (
        SELECT e.id
        FROM entities e
        WHERE p_org_ids IS NULL
           OR EXISTS (
               SELECT 1 FROM entity_mentions em
               JOIN chunks c ON c.id = em.chunk_id
               WHERE em.entity_id = e.id
                 AND chunk_permission_visible(c.chunk_metadata, p_org_ids)
           )
    )
    SELECT t.id, t.name, t.type, r.relationship_type, r.description, 'outgoing'::text
    FROM relationships r
    JOIN entities t ON t.id = r.target_entity_id
    WHERE r.source_entity_id = p_entity_id
      AND (p_rel_type IS NULL OR r.relationship_type = p_rel_type)
      AND r.source_entity_id IN (SELECT id FROM visible)
      AND r.target_entity_id IN (SELECT id FROM visible)
    UNION ALL
    SELECT s.id, s.name, s.type, r.relationship_type, r.description, 'incoming'::text
    FROM relationships r
    JOIN entities s ON s.id = r.source_entity_id
    WHERE r.target_entity_id = p_entity_id
      AND (p_rel_type IS NULL OR r.relationship_type = p_rel_type)
      AND r.source_entity_id IN (SELECT id FROM visible)
      AND r.target_entity_id IN (SELECT id FROM visible)
    LIMIT p_limit;
END;
$$;

CREATE OR REPLACE FUNCTION get_entity_evidence_permitted(
    p_entity_id uuid,
    p_org_ids   integer[] DEFAULT NULL,
    p_limit     int       DEFAULT 5
)
RETURNS TABLE (chunk_id uuid, document_id uuid, document_title text, excerpt text)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT em.chunk_id, em.document_id, d.title, coalesce(em.context, c.content)
    FROM entity_mentions em
    JOIN documents d ON d.id = em.document_id
    JOIN chunks c ON c.id = em.chunk_id
    WHERE em.entity_id = p_entity_id
      AND (p_org_ids IS NULL OR chunk_permission_visible(c.chunk_metadata, p_org_ids))
    ORDER BY em.confidence DESC
    LIMIT p_limit;
END;
$$;

COMMIT;
