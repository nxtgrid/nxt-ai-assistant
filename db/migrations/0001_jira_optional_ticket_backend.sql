-- Migration: Jira-optional ticket backend
--
-- Adds a backend-agnostic ticket reference to escalation_mappings and
-- introduces the internal_tickets / internal_ticket_comments tables so
-- Anansi can track tickets without a Jira project configured.
--
-- Idempotent: safe to run multiple times, and safe to run against a
-- database that already has db/schema/chat_db.sql applied in full (every
-- statement below is also present there under the same IF NOT EXISTS /
-- ADD COLUMN IF NOT EXISTS idiom, so this file is a no-op in that case).
--
-- Usage:
--   psql "$CHAT_DB_URL" -f db/migrations/0001_jira_optional_ticket_backend.sql
--
-- jira_ticket_key is left untouched — it's still used by the Jira inbound
-- webhook lookup (get_escalation_mapping_by_jira_key). ticket_ref/ticket_backend
-- are additive columns that generalize over both backends.

BEGIN;

-- ── escalation_mappings: add backend-agnostic ticket reference ───────────────

ALTER TABLE escalation_mappings ADD COLUMN IF NOT EXISTS ticket_ref text;
ALTER TABLE escalation_mappings ADD COLUMN IF NOT EXISTS ticket_backend text CHECK (ticket_backend IN ('jira', 'internal'));

-- Built inside this transaction for atomicity with the column/table additions
-- above; Postgres disallows CREATE INDEX CONCURRENTLY inside a transaction
-- block, so this takes a normal (blocking) lock on escalation_mappings for the
-- duration of the build -- worth knowing if that table is large/high-traffic.
CREATE INDEX IF NOT EXISTS escalation_mappings_ticket_ref_idx ON escalation_mappings (ticket_ref);

-- Defensive: installs that already ran the ADD COLUMN IF NOT EXISTS above
-- from before this CHECK constraint was added will have ticket_backend
-- without it. ADD COLUMN IF NOT EXISTS skips the whole clause (including
-- the inline CHECK) when the column already exists, so it won't retrofit
-- the constraint on its own -- add it explicitly if missing.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'escalation_mappings_ticket_backend_check'
    ) THEN
        ALTER TABLE escalation_mappings
            ADD CONSTRAINT escalation_mappings_ticket_backend_check
            CHECK (ticket_backend IN ('jira', 'internal'));
    END IF;
END $$;

-- Backfill: for escalations already resolved via Jira, ticket_ref/ticket_backend
-- mirror jira_ticket_key so callers can query either column going forward.
-- Invariant: ticket_backend = 'jira'     => ticket_ref = jira_ticket_key (both populated).
--            ticket_backend = 'internal' => jira_ticket_key stays NULL, ticket_ref is set.
-- (The 'internal' case only arises for tickets created going forward by the
-- new TicketService, so there is nothing to backfill for it here.)
UPDATE escalation_mappings
    SET ticket_ref = jira_ticket_key, ticket_backend = 'jira'
    WHERE jira_ticket_key IS NOT NULL AND ticket_ref IS NULL;

-- ── internal_tickets / internal_ticket_comments ──────────────────────────────

CREATE SEQUENCE IF NOT EXISTS internal_ticket_seq;

CREATE TABLE IF NOT EXISTS internal_tickets (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref              text UNIQUE NOT NULL,              -- e.g. 'TKT-000123'
    escalation_mapping_id   uuid,                              -- nullable (notify tickets have none)
    session_id              text,
    organization_id         integer,
    grid_name               text,
    summary                 text NOT NULL,
    description             text,
    status                  text NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open','in_progress','done')),
    assignee_email          text,
    labels                  jsonb DEFAULT '[]',
    source                  text NOT NULL DEFAULT 'escalation' -- 'escalation' | 'notify'
                            CHECK (source IN ('escalation','notify')),
    created_at              timestamptz DEFAULT now(),
    updated_at              timestamptz DEFAULT now(),
    resolved_at             timestamptz
);
CREATE INDEX IF NOT EXISTS internal_tickets_mapping_idx ON internal_tickets (escalation_mapping_id);
CREATE INDEX IF NOT EXISTS internal_tickets_status_idx ON internal_tickets (status);
CREATE INDEX IF NOT EXISTS internal_tickets_org_idx ON internal_tickets (organization_id);

CREATE TABLE IF NOT EXISTS internal_ticket_comments (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref    text NOT NULL REFERENCES internal_tickets(ticket_ref) ON DELETE CASCADE,
    author        text,               -- staff name / source system
    body          text NOT NULL,
    is_public     boolean DEFAULT false,   -- mirrors Jira jsdPublic (public = forward to customer)
    source        text DEFAULT 'staff',    -- 'staff' | 'customer' | 'notify' | 'system'
    created_at    timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS internal_ticket_comments_ref_idx ON internal_ticket_comments (ticket_ref, created_at);

-- Keep internal_tickets.updated_at current, matching the repo's existing
-- update_updated_at() trigger convention (see db/schema/chat_db.sql).
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS trg_internal_tickets_updated_at ON internal_tickets;
CREATE TRIGGER trg_internal_tickets_updated_at
    BEFORE UPDATE ON internal_tickets
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMIT;
