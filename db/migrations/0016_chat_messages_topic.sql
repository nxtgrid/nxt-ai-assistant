-- 0016_chat_messages_topic.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe
-- to re-run.
--
-- ChatWatermarkRepository (chat_orchestrator/orchestrator/services/ticketing/
-- chat_watermark.py) counted "messages since" chat-wide, but every grid
-- resolves to one shared Telegram group with a *topic per grid* (see
-- shared/auth/auth_service.py's grid->target resolution) -- production ids
-- ran 65876->65882 in 40 seconds across five grids sharing one group, so any
-- anchor read as "scrolled past" within seconds even when the operator's own
-- topic sat silent all day. That's why a ticket-status "in progress" update
-- posted a fresh reply instead of editing in place. chat_messages carries no
-- topic today (only chat_sessions does) -- this backfills it so the
-- watermark can filter by topic instead of by chat alone.
BEGIN;

ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS telegram_topic_id text;

CREATE INDEX IF NOT EXISTS chat_messages_group_topic_msg_idx
    ON chat_messages (group_id, telegram_topic_id, telegram_message_id DESC);

UPDATE chat_messages message_row
SET telegram_topic_id = session_row.telegram_topic_id
FROM chat_sessions session_row
WHERE message_row.session_id = session_row.id
  AND message_row.telegram_topic_id IS NULL
  AND session_row.telegram_topic_id IS NOT NULL;

COMMIT;
