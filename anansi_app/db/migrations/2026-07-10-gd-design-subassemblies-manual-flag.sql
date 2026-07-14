-- Add a manually_edited flag to gd_design_subassemblies (run in the Chat DB /
-- Supabase SQL editor).
--
-- shared/grid_design/auto_designer.py's auto_design() soft-deletes ALL existing
-- subassembly rows for a design and regenerates them from scratch (see
-- auto_designer.py lines ~399-403). As the grid_design MCP server grows
-- fine-grained tools that let an LLM/user hand-edit or add individual
-- subassembly rows outside of auto_design, a future guard needs a way to tell
-- "was this row produced by the last auto_design run, or edited by hand
-- afterwards" so it can refuse to silently wipe manual edits without an
-- explicit force flag. This column is that marker; it defaults to false so
-- existing/auto-generated rows are unaffected.

ALTER TABLE gd_design_subassemblies
    ADD COLUMN IF NOT EXISTS manually_edited boolean NOT NULL DEFAULT false;
