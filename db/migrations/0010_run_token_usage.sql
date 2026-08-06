-- 0010_run_token_usage.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe
-- to re-run -- ADD COLUMN IF NOT EXISTS.
--
-- Adds per-run LLM token/cost accounting to agent_work_packets, written once
-- per workflow run by WorkflowExecutor._persist_token_usage
-- (chat_orchestrator/orchestrator/experts/workflow_executor.py). DEFAULT '{}'
-- means "not yet recorded" for every existing row -- this migration changes
-- no packet's live behavior, it only adds a column new runs will populate.
--
-- Shape written: {"input_tokens": int, "output_tokens": int, "rounds": int,
-- "model": str, "cost_usd": str}. "cost_usd" is a string (decimal), not a
-- float -- do not cast it to numeric for display without checking it's
-- present first; it is OMITTED (not null, not 0) when the model isn't in
-- shared/llm/pricing.py's PRICES table. Treat cost as an estimate -- see that
-- module's docstring.

BEGIN;

ALTER TABLE agent_work_packets
    ADD COLUMN IF NOT EXISTS token_usage jsonb NOT NULL DEFAULT '{}';

COMMIT;
