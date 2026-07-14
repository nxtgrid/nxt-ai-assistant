-- Chat DB retention: prune bloat that makes up ~1.2 GB of the 1.42 GB database.
-- Run in the Supabase SQL editor (prod Chat DB), AFTER 2026-07-11-chat-db-cleanup.sql.
--
-- Where the weight is (2026-07-11 audit, sizes are TOAST-inclusive):
--   checkpoints + checkpoint_writes + checkpoint_blobs ... ~640 MB
--     (583 MB belongs to 6 thread_ids with NO persistent_agent_instances row;
--      LangGraph never prunes)
--   agent_work_packets.packet_state .................... ~256 MB in 346 rows
--     (126 MB cancelled, 102 MB completed — pre-Drive-ID base64 blobs)
--   agent_work_packet_logs input/output_data ........... ~270 MB (201 MB from March 2026)
--   pending_decisions.context ........................... 52 MB in 28 rows (24h TTL!)
--   chat_messages.tool_result ........................... ~170 MB (optional prune, see §6)
--
-- Steps 1–5 are plain DML in one transaction. Step 7 (VACUUM FULL) must run
-- statement-by-statement OUTSIDE a transaction and takes brief exclusive locks
-- — run it off-peak. Space is only returned to disk after step 7.

BEGIN;

-- ── 1. LangGraph checkpoints ──
-- 1a. Threads with no agent instance (orphans) or terminated agents: delete all.
-- A thread is "alive" iff it has a persistent_agent_instances row that is not
-- terminated; everything else is dead. The condition is inlined per-statement
-- (no temp table) so the script survives the Supabase SQL editor running
-- statements in separate implicit transactions.
DELETE FROM checkpoint_writes w
WHERE NOT EXISTS (
    SELECT 1 FROM persistent_agent_instances p
    WHERE p.thread_id = w.thread_id AND p.status <> 'terminated'
);
DELETE FROM checkpoint_blobs b
WHERE NOT EXISTS (
    SELECT 1 FROM persistent_agent_instances p
    WHERE p.thread_id = b.thread_id AND p.status <> 'terminated'
);
DELETE FROM checkpoints c
WHERE NOT EXISTS (
    SELECT 1 FROM persistent_agent_instances p
    WHERE p.thread_id = c.thread_id AND p.status <> 'terminated'
);

-- 1b. Live threads: keep only the 20 most recent checkpoints per thread.
--     (LangGraph checkpoint_ids are lexically monotonic.)
WITH ranked AS (
    SELECT thread_id, checkpoint_ns, checkpoint_id,
           row_number() OVER (PARTITION BY thread_id, checkpoint_ns
                              ORDER BY checkpoint_id DESC) AS rn
    FROM checkpoints
),
to_delete AS (
    SELECT thread_id, checkpoint_ns, checkpoint_id
    FROM ranked
    WHERE rn > 20
)
DELETE FROM checkpoint_writes w
USING to_delete d
WHERE w.thread_id = d.thread_id
  AND w.checkpoint_ns = d.checkpoint_ns
  AND w.checkpoint_id = d.checkpoint_id;

WITH ranked AS (
    SELECT thread_id, checkpoint_ns, checkpoint_id,
           row_number() OVER (PARTITION BY thread_id, checkpoint_ns
                              ORDER BY checkpoint_id DESC) AS rn
    FROM checkpoints
)
DELETE FROM checkpoints c
USING ranked r
WHERE c.thread_id = r.thread_id
  AND c.checkpoint_ns = r.checkpoint_ns
  AND c.checkpoint_id = r.checkpoint_id
  AND r.rn > 20;
-- checkpoint_blobs for live threads are intentionally NOT pruned (only ~12 MB;
-- blob versions are shared across checkpoints and GC is not worth the risk).

-- ── 2. agent_work_packets: strip heavyweight packet_state from terminal packets ──
-- packet_outputs/packet_inputs are preserved; only the intermediate state blob
-- (historically containing base64 images) is cleared. Failed packets keep
-- state for 30 days so ask_resume_failed still works on recent ones.
UPDATE agent_work_packets
SET packet_state = '{}'::jsonb
WHERE (
        packet_status IN ('cancelled', 'completed') AND created_at < now() - interval '30 days'
     OR packet_status = 'failed'                    AND created_at < now() - interval '30 days'
  )
  AND pg_column_size(packet_state) > 8192;

-- ── 3. agent_work_packet_logs: drop step input/output blobs older than 90 days ──
-- Keeps the log rows themselves (message, step_name, timings, error_data).
UPDATE agent_work_packet_logs
SET input_data = NULL, output_data = NULL
WHERE created_at < now() - interval '90 days'
  AND (input_data IS NOT NULL OR output_data IS NOT NULL);

-- ── 4. pending_decisions: rows expire after 24h but are never deleted ──
DELETE FROM pending_decisions
WHERE coalesce(resolved_at, expires_at) < now() - interval '7 days';

-- ── 5. scheduled_messages: processed rows accumulate forever and the claim
--       RPC scans this table every few seconds ──
DELETE FROM scheduled_messages
WHERE status NOT IN ('pending', 'processing')
  AND created_at < now() - interval '60 days';

COMMIT;

-- ── 6. OPTIONAL: strip old tool_result blobs from chat history ──
-- ~170 MB total. tool_result is only replayed when building recent
-- conversation context; the /conversations admin browser would show old tool
-- payloads as null. Uncomment if you're comfortable with that.
-- UPDATE chat_messages
-- SET tool_result = NULL
-- WHERE created_at < now() - interval '120 days'
--   AND tool_result IS NOT NULL
--   AND pg_column_size(tool_result) > 8192;

-- ── 7. Reclaim disk (run each line separately, off-peak; brief exclusive locks;
--       tables are small after the deletes so each completes in seconds) ──
-- VACUUM FULL checkpoints;
-- VACUUM FULL checkpoint_writes;
-- VACUUM FULL checkpoint_blobs;
-- VACUUM FULL agent_work_packets;
-- VACUUM FULL agent_work_packet_logs;
-- VACUUM FULL pending_decisions;
-- VACUUM FULL scheduled_messages;
-- VACUUM FULL grafana_dashboard_metadata;
-- VACUUM FULL chat_messages;          -- only worthwhile if §6 was run

-- ── 8. OPTIONAL: keep it clean automatically with pg_cron ──
-- Enable the pg_cron extension in Supabase (Dashboard → Database → Extensions),
-- then:
--
-- CREATE OR REPLACE FUNCTION chat_db_weekly_retention() RETURNS void LANGUAGE sql AS $$
--     DELETE FROM pending_decisions
--     WHERE coalesce(resolved_at, expires_at) < now() - interval '7 days';
--     DELETE FROM scheduled_messages
--     WHERE status NOT IN ('pending','processing') AND created_at < now() - interval '60 days';
--     UPDATE agent_work_packets SET packet_state = '{}'::jsonb
--     WHERE packet_status IN ('cancelled','completed','failed')
--       AND created_at < now() - interval '30 days'
--       AND pg_column_size(packet_state) > 8192;
--     UPDATE agent_work_packet_logs SET input_data = NULL, output_data = NULL
--     WHERE created_at < now() - interval '90 days'
--       AND (input_data IS NOT NULL OR output_data IS NOT NULL);
--     DELETE FROM checkpoint_writes w USING (
--         SELECT thread_id, checkpoint_ns, checkpoint_id,
--                row_number() OVER (PARTITION BY thread_id, checkpoint_ns
--                                   ORDER BY checkpoint_id DESC) AS rn
--         FROM checkpoints
--     ) r
--     WHERE w.thread_id = r.thread_id AND w.checkpoint_ns = r.checkpoint_ns
--       AND w.checkpoint_id = r.checkpoint_id AND r.rn > 20;
--     DELETE FROM checkpoints c USING (
--         SELECT thread_id, checkpoint_ns, checkpoint_id,
--                row_number() OVER (PARTITION BY thread_id, checkpoint_ns
--                                   ORDER BY checkpoint_id DESC) AS rn
--         FROM checkpoints
--     ) r
--     WHERE c.thread_id = r.thread_id AND c.checkpoint_ns = r.checkpoint_ns
--       AND c.checkpoint_id = r.checkpoint_id AND r.rn > 20;
-- $$;
--
-- SELECT cron.schedule('chat-db-retention', '0 3 * * 0', 'SELECT chat_db_weekly_retention()');
