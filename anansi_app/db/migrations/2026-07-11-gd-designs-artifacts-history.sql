-- Add an artifacts jsonb column to gd_designs (run in the Chat DB /
-- Supabase SQL editor).
--
-- The LPP (Light Preliminary Package) workflow generates artifacts per design
-- (distribution maps, site layout renders, QGIS project files, etc.) and
-- uploads them to Google Drive during handler execution, but today the Drive
-- file IDs only ever land in ephemeral `packet_state` jsonb -- once the packet
-- finishes (or is cleaned up), there is no durable, per-design record of what
-- was generated, when, or whether it's still the latest version. As the
-- grid_design MCP server grows read-side tools that let an LLM/user query a
-- design's artifact history (e.g. "show me the last 3 distribution maps for
-- this design"), there needs to be a queryable, versioned home for this data
-- that outlives any single packet's `packet_state`. This column is that home:
-- a jsonb object keyed by artifact type (e.g. "map_image",
-- "site_layout_png"), each value a newest-first list of version entries
-- ({drive_file_id, web_view_link, created_at, packet_id, label, mime_type,
-- stale}), capped at a small number of versions per type by the application
-- layer (see shared/grid_design/artifact_log.py). Defaults to an empty object
-- so existing rows are unaffected.

ALTER TABLE gd_designs
    ADD COLUMN IF NOT EXISTS artifacts jsonb NOT NULL DEFAULT '{}';
