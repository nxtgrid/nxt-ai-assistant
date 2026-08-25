-- 0029: retire knowledge_modules.mode.
--
-- Every module attached to a prompt is now inlined into that prompt in full.
-- The old 'on_demand' tier -- which contributed a summary line to a catalog
-- the model could fetch from with get_knowledge_module -- is gone: attaching
-- a module and having its content actually reach the prompt were two
-- different things, which is not what attaching a module reads as.
--
-- The column is NOT dropped. Nothing in the code reads it (see
-- shared/prompts/knowledge.py) and nothing writes it any more either -- the
-- NOT NULL DEFAULT 'pinned' covers new inserts -- so this migration is purely
-- so the stored value stops disagreeing with actual behaviour for anyone
-- reading the table directly. The application behaves identically whether or
-- not this has been applied, which matters here: migrations in this repo are
-- applied by hand and merging one does not run it.
--
-- Safe to re-run.

UPDATE knowledge_modules
   SET mode = 'pinned'
 WHERE mode <> 'pinned';

COMMENT ON COLUMN knowledge_modules.mode IS
    'Retired (migration 0029). Every attached module is inlined in full; '
    'nothing reads this column. Kept only so existing rows stay valid '
    'against knowledge_modules_mode_chk.';
