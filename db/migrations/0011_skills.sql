-- 0011_skills.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe
-- to re-run -- CREATE TABLE IF NOT EXISTS.
--
-- Phase 3 of docs/superpowers/plans/2026-08-06-user-designed-skills.md.
-- A skill is a user-designed, ordered list of LLM steps, saved from the
-- interactive builder (Phase 4) and run as an expert workflow (Phase 5).
--
-- `steps` element shape (see skill_step_bindings.py, Phase 2):
--   {"index": 0, "name": "find_tickets", "instruction": "...",
--    "output_var": "open_tickets", "allow_write": false,
--    "is_response_step": false}
--
-- `status = 'unusable'` is set automatically when a scheduled run finds the
-- creating account deleted (Phase 5). Nothing auto-deletes a skill; an
-- admin removes it later after seeing that status.
--
-- `staff_only` gates catalog visibility (shared/prompts/skills.py) the same
-- way command_registry.py already gates slash commands -- a customer-org
-- request never sees a staff_only=True skill's title/summary in context.

BEGIN;

CREATE TABLE IF NOT EXISTS skills (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug           text NOT NULL UNIQUE,
    title          text NOT NULL,
    summary        text NOT NULL,
    steps          jsonb NOT NULL DEFAULT '[]',
    inputs         jsonb NOT NULL DEFAULT '[]',
    staff_only     boolean NOT NULL DEFAULT true,
    status         text NOT NULL DEFAULT 'active',
    created_by     text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT skills_status_chk CHECK (status IN ('active', 'disabled', 'unusable'))
);

COMMIT;
