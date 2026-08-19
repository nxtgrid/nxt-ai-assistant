-- 0025_skill_draft_status.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 1 of docs/superpowers/plans/2026-08-22-p3-skills-lifecycle-and-function-steps.md.
--
-- NOTE on numbering: this branch (P3) forked from main before P4's branch
-- merged, so from here 0020 looked like the next free number -- but P4's
-- 0020-0024 are already applied to production (see that branch/PR) and will
-- keep those numbers, since renumbering already-applied migrations would be
-- disruptive. Using 0025 to avoid the collision when both merge. Confirm
-- `ls db/migrations/` still shows 0024 as the highest applied before running
-- this, in case numbering has moved further by the time this lands.
--
-- Adds 'draft' so the builder can save unfinished work without it entering
-- anyone's context. No code change is needed for the invisibility itself:
-- SkillCatalogStore.all_skills() already filters .eq("status", "active").
--
-- Existing rows are unaffected -- this only widens what is allowed.

BEGIN;

ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_status_chk;

ALTER TABLE skills
    ADD CONSTRAINT skills_status_chk
        CHECK (status IN ('draft', 'active', 'disabled', 'unusable'));

COMMIT;
