-- 0026_user_schedules_skill_unique.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 3 (Task 7) of docs/superpowers/plans/2026-08-22-p3-skills-lifecycle-and-function-steps.md.
--
-- NOTE on numbering: 0025 is this same branch's Phase 1 migration, already
-- applied. main is at 0024 as of this migration (P4 merged) -- confirm
-- `ls db/migrations/` still shows 0025 as the highest applied before running
-- this, in case numbering has moved further by the time this lands.
--
-- SkillBuilderService.set_skill_schedule (anansi_app/services/skill_builder_service.py)
-- upserts on_conflict="skill_id" -- one schedule row per skill, editing the
-- modal's Schedule section replaces it rather than adding a second one.
-- postgrest's upsert requires a real unique constraint/index to target;
-- none existed on user_schedules.skill_id. A plain UNIQUE constraint can't
-- be used because skill_id is nullable (NULL for every command-type row,
-- per user_schedules_command_xor_skill_chk) and Postgres does not treat
-- repeated NULLs as conflicting -- a partial index scoped to
-- "skill_id IS NOT NULL" is both what upsert needs and the actual invariant
-- intended: at most one schedule per skill, unlimited command-type rows.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS user_schedules_skill_id_unique
    ON user_schedules (skill_id)
    WHERE skill_id IS NOT NULL;

COMMIT;
