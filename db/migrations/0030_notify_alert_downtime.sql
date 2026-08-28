-- Mark which /chat/notify deliveries reported the grid itself as down, so the
-- downtime floor (orchestrator/services/ticketing/downtime_alert_policy.py) has
-- a clock that only downtime alerts advance. Without a dedicated marker an
-- unrelated equipment alert would reset the "already told them today" window
-- and a dark grid could stay silent.
-- Apply against chat_db. Every DDL statement is intentionally idempotent.

BEGIN;

ALTER TABLE notify_alert_deliveries
    ADD COLUMN IF NOT EXISTS downtime boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS notify_alert_deliveries_grid_downtime_sent_idx
    ON notify_alert_deliveries (grid_name, sent_at DESC)
    WHERE downtime;

COMMIT;
