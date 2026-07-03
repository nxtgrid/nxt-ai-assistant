-- Fix mistyped gd_designs parameter columns (run in the Chat DB / Supabase SQL editor).
--
-- AppSheet's Designs form had "Wp per conn override?" as a Number (Wp per
-- connection, e.g. 850) and "Constrain design to known regulation?" as an Enum
-- ("None" / "Nigeria - DARES"). The generated schema mistyped both as boolean,
-- which silently disabled the Wp/conn override and the DARES sizing constraint
-- in shared/grid_design/auto_designer.py (it reads them as number / text).
--
-- All existing rows hold false/NULL for both columns (verified 2026-07-03), so
-- the conversion below cannot lose data: a legacy `true` in the regulation
-- column maps to 'Nigeria - DARES' (its AppSheet default), everything else NULL.

ALTER TABLE gd_designs
    ALTER COLUMN wp_per_conn_override DROP DEFAULT,
    ALTER COLUMN wp_per_conn_override TYPE numeric USING NULL,
    ALTER COLUMN constrain_design_to_known_regulation DROP DEFAULT,
    ALTER COLUMN constrain_design_to_known_regulation TYPE text
        USING CASE WHEN constrain_design_to_known_regulation THEN 'Nigeria - DARES' END;
