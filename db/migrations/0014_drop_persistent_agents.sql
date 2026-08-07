-- Phase 6 of docs/superpowers/plans/2026-08-06-user-designed-skills.md:
-- persistent agents are fully removed from the codebase (agent_worker.py,
-- persistent_agent_graph.py, user_agent_service.py, expert_tool_runner.py,
-- the messaging MCP server, and everything that dispatched to them). This
-- drops what they left behind in the Chat DB.
--
-- agent_work_packets is NOT touched -- skills (docs/superpowers/plans/
-- 2026-08-06-user-designed-skills.md) execute through it too.
--
-- Run in the Supabase SQL editor (prod Chat DB). Prerequisite per the plan's
-- "before you write any code" section: confirmed via
-- `SELECT expert_id, status, count(*) FROM persistent_agent_instances GROUP
-- BY 1, 2 ORDER BY 3 DESC` that no instance was `active`/`executing` at the
-- time this shipped, and the operator confirmed the 9 `paused` grid_monitor
-- rows were safe to let go.

BEGIN;

-- claim_agent_events operated on agent_events; drop before the table so
-- there's no window with a function referencing a gone relation.
DROP FUNCTION IF EXISTS claim_agent_events(uuid, int);

-- agent_events.target_instance_id FKs to persistent_agent_instances, so drop
-- it first even though CASCADE below would handle the order regardless.
DROP TABLE IF EXISTS agent_events;
DROP TABLE IF EXISTS persistent_agent_instances CASCADE;

-- LangGraph's own checkpoint tables. Never declared in db/schema/chat_db.sql
-- (AsyncPostgresSaver.setup() created them at runtime, only when
-- PERSISTENT_AGENTS_ENABLED=true) -- agent_worker.py's checkpointer was
-- their only reader or writer anywhere in this codebase; the main
-- conversation graph has never used a checkpointer. Per the 2026-07-11
-- retention audit these three tables were ~640 MB, the single largest chunk
-- of Chat DB bloat identified at the time -- IF NOT EXISTS makes this safe
-- to run even somewhere they were never created.
DROP TABLE IF EXISTS checkpoint_writes;
DROP TABLE IF EXISTS checkpoint_blobs;
DROP TABLE IF EXISTS checkpoints;
DROP TABLE IF EXISTS checkpoint_migrations;

COMMIT;

-- No VACUUM FULL needed afterward: unlike the UPDATE/DELETE-based
-- 2026-07-11 retention pass, DROP TABLE reclaims its disk space immediately.
