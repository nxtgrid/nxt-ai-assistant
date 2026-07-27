-- Migration: Alert correlation for /notify smart ticketing
--
-- Adds ticket_correlations / ticket_correlation_events so the /notify
-- endpoint can group incoming alerts (from n8n/VRM/Grafana) against a
-- grid's already-open tickets instead of filing one ticket per alert.
-- See docs/superpowers/plans/2026-07-27-smart-alert-correlation-notify.md.
--
-- Deliberately backend-agnostic and un-FK'd to internal_tickets:
-- ticket_correlations.ticket_ref may point at either an internal_tickets
-- row (e.g. 'TKT-000123') or a Jira key (e.g. 'OPS-456') -- the correlation
-- layer must work identically whether or not Jira is configured, and it is
-- the durable, backend-agnostic state a ticket's summary/description are
-- *rendered from* (never parsed back out of Jira ADF).
--
-- Idempotent: safe to run multiple times, and safe to run against a
-- database that already has db/schema/chat_db.sql applied in full (every
-- statement below is also present there under the same IF NOT EXISTS
-- idiom, so this file is a no-op in that case).
--
-- Usage:
--   psql "$CHAT_DB_URL" -f db/migrations/0003_alert_correlation.sql

BEGIN;

-- ── ticket_correlations ──────────────────────────────────────────────────────
-- One row per ticket (either backend) that alert correlation has ever
-- touched. summary_base/description_base capture the ticket exactly as
-- first filed; summary_current and the rendered description are recomputed
-- from affected_keys on every amend (see correlation_render.py), never
-- parsed back out of the ticket itself.

CREATE TABLE IF NOT EXISTS ticket_correlations (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref           text UNIQUE NOT NULL,        -- Jira key OR internal ref; no FK (see header)
    ticket_backend       text CHECK (ticket_backend IN ('jira', 'internal')),
    grid_name            text NOT NULL,
    organization_id      integer,
    root_cause_kind      text,                        -- 'grid_off' | 'grid_isolated' | 'component' | 'other'
    primary_signature    text,
    signatures           jsonb NOT NULL DEFAULT '[]',  -- every signature folded into this ticket
    affected_keys        jsonb NOT NULL DEFAULT '[]',  -- [{kind,key,label,first_seen,last_seen,count}]
    summary_base         text,                         -- summary as first filed (never overwritten)
    summary_current      text,
    description_base     text,                         -- description as first filed (never overwritten)
    severity             text,
    occurrence_count     integer NOT NULL DEFAULT 1,
    escalated_at         timestamptz,
    status               text NOT NULL DEFAULT 'open', -- cached mirror; the ticket backend is authoritative
    telegram_chat_id     text,
    telegram_topic_id    text,
    telegram_message_id  bigint,                       -- first post, for reply threading
    last_alert_at        timestamptz DEFAULT now(),
    created_at           timestamptz DEFAULT now(),
    updated_at           timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ticket_correlations_grid_idx
    ON ticket_correlations (grid_name, status, last_alert_at DESC);
CREATE INDEX IF NOT EXISTS ticket_correlations_sig_idx
    ON ticket_correlations USING gin (signatures jsonb_path_ops);

-- ── ticket_correlation_events ────────────────────────────────────────────────
-- Full audit trail of every correlation decision -- what was decided, by
-- which rung of the pipeline (signature match / LLM / no-candidates /
-- fallback / flag-off / replay), and why. dedup_key backs real idempotency:
-- an n8n retry with the same dedup_key replays the prior decision instead
-- of creating a second ticket or a second Telegram post.

CREATE TABLE IF NOT EXISTS ticket_correlation_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref      text,                 -- resolved target; null only on hard failure
    grid_name       text NOT NULL,
    source          text,
    signature       text,
    dedup_key       text,
    decision        text NOT NULL,        -- 'new' | 'amend' | 'duplicate'
    decided_by      text NOT NULL,        -- 'signature'|'llm'|'no_candidates'|'fallback'|'flag_off'|'replay'
    confidence      real,
    reason          text,
    candidate_refs  jsonb NOT NULL DEFAULT '[]',
    alert           jsonb,                -- AlertFacts as received
    llm_raw         text,
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ticket_correlation_events_grid_idx
    ON ticket_correlation_events (grid_name, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ticket_correlation_events_dedup_idx
    ON ticket_correlation_events (dedup_key) WHERE dedup_key IS NOT NULL;

-- Keep ticket_correlations.updated_at current, matching the repo's existing
-- update_updated_at() trigger convention (see db/schema/chat_db.sql). The
-- function is CREATE OR REPLACE'd there already; re-declare defensively here
-- too so this migration is runnable standalone against a bare database.
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS trg_ticket_correlations_updated_at ON ticket_correlations;
CREATE TRIGGER trg_ticket_correlations_updated_at
    BEFORE UPDATE ON ticket_correlations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMIT;
