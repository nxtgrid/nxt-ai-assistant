-- Migration: internal ticket ref allocation RPC
--
-- Task 1 (0001_jira_optional_ticket_backend.sql) created internal_ticket_seq
-- and the internal_tickets table but not a way to atomically allocate a ref
-- and insert the row in one round-trip. Reading nextval() in application
-- code and then inserting separately is a read-then-write race (two
-- concurrent callers could both read N, then both try to insert -- or worse,
-- insert out of order and leave a gap that looks like a lost ticket).
--
-- create_internal_ticket() does both atomically, following this repo's
-- existing RPC convention (see claim_scheduled_messages, get_bot_artifacts,
-- etc. in db/schema/chat_db.sql) -- called via
-- supabase_client.rpc("create_internal_ticket", {...}).execute() from
-- InternalTicketBackend.create_ticket().
--
-- Idempotent: CREATE OR REPLACE FUNCTION is safe to re-run.
--
-- Usage:
--   psql "$CHAT_DB_URL" -f db/migrations/0002_internal_ticket_ref_allocation.sql

BEGIN;

CREATE OR REPLACE FUNCTION create_internal_ticket(
    p_summary               text,
    p_description           text DEFAULT NULL,
    p_escalation_mapping_id uuid DEFAULT NULL,
    p_session_id            text DEFAULT NULL,
    p_organization_id       integer DEFAULT NULL,
    p_grid_name             text DEFAULT NULL,
    p_assignee_email        text DEFAULT NULL,
    p_labels                jsonb DEFAULT '[]',
    p_source                text DEFAULT 'escalation',
    p_prefix                text DEFAULT 'TKT'
)
RETURNS SETOF internal_tickets LANGUAGE plpgsql AS $$
DECLARE
    v_ref text;
BEGIN
    v_ref := p_prefix || '-' || lpad(nextval('internal_ticket_seq')::text, 6, '0');
    RETURN QUERY
    INSERT INTO internal_tickets (
        ticket_ref, escalation_mapping_id, session_id, organization_id,
        grid_name, summary, description, assignee_email, labels, source
    ) VALUES (
        v_ref, p_escalation_mapping_id, p_session_id, p_organization_id,
        p_grid_name, p_summary, p_description, p_assignee_email, p_labels, p_source
    )
    RETURNING *;
END;
$$;

COMMIT;
