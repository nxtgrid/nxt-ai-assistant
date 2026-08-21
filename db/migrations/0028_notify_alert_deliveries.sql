-- Durable successful-delivery ledger for /chat/notify alert messages.
-- Apply against chat_db. Every DDL statement is intentionally idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS notify_alert_deliveries (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    grid_name               text NOT NULL,
    external_chat_id        text NOT NULL,
    external_topic_id       text,
    external_message_id     bigint NOT NULL,
    sent_at                 timestamptz NOT NULL DEFAULT now(),
    source                  text,
    dedup_key               text,
    ticket_id               uuid REFERENCES tickets(id) ON DELETE SET NULL,
    ticket_ref              text,
    rendered_text           text NOT NULL,
    alert                   jsonb NOT NULL DEFAULT '{}',
    CONSTRAINT notify_alert_deliveries_chat_message_uniq
        UNIQUE (external_chat_id, external_message_id)
);

CREATE INDEX IF NOT EXISTS notify_alert_deliveries_grid_sent_idx
    ON notify_alert_deliveries (grid_name, sent_at DESC);

ALTER TABLE ticket_correlation_events
    ADD COLUMN IF NOT EXISTS judgment jsonb,
    ADD COLUMN IF NOT EXISTS context_availability jsonb,
    ADD COLUMN IF NOT EXISTS send_decision boolean,
    ADD COLUMN IF NOT EXISTS send_forced_by jsonb NOT NULL DEFAULT '[]';

COMMIT;
