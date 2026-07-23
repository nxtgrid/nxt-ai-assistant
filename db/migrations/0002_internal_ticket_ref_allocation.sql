-- Migration: internal ticket ref allocation RPC
--
-- Task 1 (0001_jira_optional_ticket_backend.sql) created internal_ticket_seq
-- and the internal_tickets table. Postgres sequences are already race-free
-- under concurrency on their own -- nextval() atomically increments and
-- returns a unique value per call no matter how many callers invoke it
-- concurrently, so two callers can never receive the same value. The only
-- reason this needs a SQL-side function at all is that Supabase's PostgREST
-- layer only exposes functions you've explicitly created for RPC use --
-- there's no way to call the built-in nextval() directly through the
-- Supabase client without some SQL-side wrapper.
--
-- next_internal_ticket_ref() is that minimal wrapper: it allocates and
-- formats a ref and nothing else. It does not touch internal_tickets --
-- InternalTicketBackend.create_ticket() calls this RPC to get a ref, then
-- does a normal .table("internal_tickets").insert(...) as a second,
-- ordinary round-trip (see internal_backend.py). At worst, a failure
-- between the two round-trips leaves an unused sequence number, which is
-- an expected, harmless gap -- the same as any SERIAL/sequence-backed
-- primary key under a failed insert.
--
-- Idempotent: CREATE OR REPLACE FUNCTION is safe to re-run.
--
-- Usage:
--   psql "$CHAT_DB_URL" -f db/migrations/0002_internal_ticket_ref_allocation.sql

BEGIN;

CREATE OR REPLACE FUNCTION next_internal_ticket_ref(p_prefix text DEFAULT 'TKT')
RETURNS text LANGUAGE sql AS $$
    SELECT p_prefix || '-' || lpad(nextval('internal_ticket_seq')::text, 6, '0');
$$;

COMMIT;
