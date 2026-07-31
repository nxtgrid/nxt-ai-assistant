-- 0007_backfill_ticket_correlations_ticket_id.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent: safe
-- to re-run -- only touches rows where ticket_id IS NULL.
--
-- 0005a_ticket_schema_expand_and_backfill.sql added ticket_correlations.ticket_id
-- and backfilled it once for rows that existed when that migration ran.
-- CorrelationStore.upsert_correlation() never set ticket_id on new writes, so
-- every ticket_correlations row created since then has ticket_id = NULL. This
-- is a plain data backfill (no schema change) closing that gap for existing
-- rows; going forward, upsert_correlation() populates ticket_id itself.
--
-- Required before db/migrations/0005b_ticket_schema_validate_and_contract.sql
-- can make ticket_id the table's primary key (that step fails if any row
-- still has ticket_id = NULL).

BEGIN;

UPDATE ticket_correlations correlation
SET ticket_id = tickets.id
FROM tickets
WHERE correlation.ticket_id IS NULL
  AND tickets.ticket_ref = correlation.ticket_ref
  AND tickets.backend = correlation.ticket_backend;

COMMIT;
