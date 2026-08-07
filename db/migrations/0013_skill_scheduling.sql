-- 0013_skill_scheduling.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe
-- to re-run -- ADD COLUMN IF NOT EXISTS.
--
-- Phase 5 of docs/superpowers/plans/2026-08-06-user-designed-skills.md.
-- Extends the existing user_schedules mechanism to fan a skill out across
-- every eligible entity of a given type (see
-- orchestrator/experts/entity_fanout.py), rather than adding a fifth
-- scheduler -- see the plan's Phase 5, item 1.
--
-- skill_id set = this schedule runs a skill (fanned out per
-- anchor_entity_type) rather than the pre-existing single-chat `command`
-- text. Both mechanisms share the same table; a row uses exactly one.
--
-- skill_inputs is scoped to ONE schedule (e.g. fixed parameters an author
-- configured when scheduling this particular skill), separate from a
-- skill's own `skills.inputs` (which describes what inputs a skill accepts
-- at all -- Phase 3).
--
-- anchor_entity_type has no default: a skill_id row must set it to a value
-- entity_fanout.SUPPORTED_ANCHOR_ENTITY_TYPES recognizes ("grid" or
-- "organization" as of this phase) for fan-out to mean anything; the CHECK
-- constraint enforces that in the DB, the application-level set in
-- entity_fanout.py is the source of truth for what's actually wired up.

BEGIN;

ALTER TABLE user_schedules
    ADD COLUMN IF NOT EXISTS skill_id uuid REFERENCES skills (id),
    ADD COLUMN IF NOT EXISTS anchor_entity_type text,
    ADD COLUMN IF NOT EXISTS skill_inputs jsonb NOT NULL DEFAULT '{}';

-- A skill-based row has no single `command` text -- the skill's own steps
-- are what runs. Loosening NOT NULL is safe for existing rows (every one
-- already has a command; this only lets *new* rows omit it).
ALTER TABLE user_schedules
    ALTER COLUMN command DROP NOT NULL;

ALTER TABLE user_schedules
    ADD CONSTRAINT user_schedules_anchor_entity_type_chk
        CHECK (anchor_entity_type IS NULL OR anchor_entity_type IN ('grid', 'organization'));

-- Exactly one of command / skill_id per row -- a schedule is either the
-- pre-existing single-chat command mechanism or a fanned-out skill run,
-- never both, never neither.
ALTER TABLE user_schedules
    ADD CONSTRAINT user_schedules_command_xor_skill_chk
        CHECK ((command IS NOT NULL) <> (skill_id IS NOT NULL));

-- A skill row must declare how to fan out; a command row has no use for
-- anchor_entity_type at all (it already names one chat_id directly).
ALTER TABLE user_schedules
    ADD CONSTRAINT user_schedules_skill_requires_anchor_chk
        CHECK ((skill_id IS NULL) = (anchor_entity_type IS NULL));

-- Per-target-entity outcome for a skill's fan-out run (Phase 5, item 3:
-- "a skipped chat sends nothing to Telegram but must appear in the web run
-- history with its reason"). A single user_schedules tick now produces one
-- user_schedule_logs row per eligible entity, not one row for the whole
-- tick -- anchor_entity_id/anchor_entity_name identify which. NULL on both
-- for the pre-existing single-chat command path, which has only ever had
-- one target and needs no entity identification.
--
-- status gains 'skipped' as a value alongside the pre-existing
-- 'success'/'failed' (enforced in application code, not a DB constraint --
-- user_schedule_logs.status was never DB-constrained to begin with).
-- error_message is reused for a skip's reason too (e.g. "creator's org
-- doesn't match this chat") rather than adding a parallel skip_reason
-- column for what is, from a run-history reader's perspective, the same
-- kind of "why didn't this happen" text.
ALTER TABLE user_schedule_logs
    ADD COLUMN IF NOT EXISTS anchor_entity_id text,
    ADD COLUMN IF NOT EXISTS anchor_entity_name text;

COMMIT;
