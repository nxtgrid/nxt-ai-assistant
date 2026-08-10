-- Migration: ticket schema validate and contract (SQL 2 of 2)
--
-- Companion to 0005a_ticket_schema_expand_and_backfill.sql. That migration
-- created the canonical tickets / escalations / ticket_comments /
-- message_deliveries tables and backfilled them from the legacy relations
-- while leaving the legacy relations and their sync triggers in place.
--
-- This migration is the destructive half: once the application has fully
-- cut over to TicketRepository / EscalationRepository / DeliveryRepository /
-- CorrelationStore, it validates that cutover actually happened, drops the
-- now-redundant legacy columns, and archives the superseded tables.
--
-- ── Cutover status as of 2026-08-10 ───────────────────────────────────
-- No code path writes to `escalation_mappings` / `internal_tickets` /
-- `internal_ticket_comments` anymore -- STOP_LEGACY_ESCALATION_WRITES is
-- "true" in production as of this writing. The only remaining references
-- are read-only legacy fallbacks, gated behind
-- CANONICAL_ESCALATION_READS_ENABLED (also "true" in production) and only
-- reached when the canonical lookup is inconclusive:
--   chat_orchestrator/orchestrator/services/supabase_client.py
--   chat_orchestrator/orchestrator/services/metrics_service.py
--   mcp_servers/servers/customer_server/customer_mcp_server.py
--   mcp_servers/servers/meta_server/meta_mcp_server.py (5 call sites)
-- escalation_service.py, callback_handlers.py, ticketing/service.py, and
-- handler.py mention the legacy table only in comments now -- no live code
-- touches it there.
--
-- Not verified here: the row-parity assertions in step 1 and the actual
-- write-recency check in step 2 both require a live DB connection. The
-- production deployment where both flags above went active has only been
-- up since 2026-08-09 12:25 UTC -- close to the 24h RECENT_WRITE_WINDOW in
-- step 2. Run this and let it self-check rather than assuming the window
-- has cleared.
--
-- Update: first run (2026-08-10) failed step 1's escalation_mappings check
-- with 15 unmirrored rows. 12 turned out to be a historical bug -- session
-- resolved via chat_sessions.id instead of chat_sessions.session_id, all
-- reason='negative_feedback', 2026-01-15 through 2026-03-09, none since --
-- now recovered by step 0 below. The remaining 3 (session
-- 'telegram_5d3ac3abfd3a79e4', 2026-03-16) reference a session that was
-- never persisted to chat_sessions at all -- confirmed unrecoverable and
-- explicitly excluded in step 1 below.
--
-- Update 2: second run hit step 5's chat_messages check (242 rows with a
-- resolvable ticket_ref but no ticket_id). Confirmed nothing live still
-- reads metadata.ticket_ref/ticket_role -- the two reader methods that did
-- (list_tickets/_attach_comment_counts and get_ticket_detail/
-- _fetch_ticket_comments, both in anansi_app/services/supabase_reader.py)
-- have zero live callers, superseded by list_ticket_page/
-- get_canonical_ticket_detail, which are ticket_id-only already -- so step
-- 5's metadata strip is safe. The actual gap: tag_message_as_ticket_comment
-- (chat_orchestrator) never set ticket_id at all. Fixed in code (it now
-- resolves and sets it) and backfilled for pre-fix rows by step 0b below.
--
-- Idempotent: safe to run twice. The second run finds the legacy tables
-- already archived/columns already dropped and no-ops those steps.
--
-- Usage:
--   psql "$CHAT_DB_URL" -f db/migrations/0005b_ticket_schema_validate_and_contract.sql

BEGIN;

-- ── Step 0: repair legacy rows keyed by internal session id ─────────────────
-- 0005a's backfill and the sync_legacy_escalation() trigger both resolve
-- chat_session_id by inner-joining chat_sessions.session_id (the external
-- string key, e.g. "telegram_abc123") against
-- escalation_mappings.session_id. A historical bug put
-- chat_sessions.id (the internal uuid) in that column instead for a batch
-- of escalations, so the join silently excluded them -- not because the
-- session was ever missing, but because the wrong key was stored. Recover
-- them here, before step 1's mirror-completeness assertion runs.
--
-- Confirmed by manual review before this step was added: 12 rows, all
-- reason='negative_feedback', 2026-01-15 through 2026-03-09 (none since),
-- each resolving to a live chat_sessions.id.

DO $$
DECLARE
    recovered_rows integer;
BEGIN
    IF to_regclass('public.escalation_mappings') IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO escalations (
        id, chat_session_id, thread_id, ticket_id, state,
        customer_username, customer_email, org_hashtag, reason, action_type,
        question_text, created_at, resolved_at
    )
    SELECT
        mapping.id,
        session_row.id,
        mapping.thread_id,
        ticket_row.id,
        CASE
            WHEN mapping.resolved_at IS NOT NULL OR NOT coalesce(mapping.is_active, true) THEN 'resolved'
            WHEN ticket_row.id IS NOT NULL THEN 'tracked'
            ELSE 'open'
        END,
        mapping.customer_username,
        mapping.customer_email,
        mapping.org_hashtag,
        mapping.reason,
        mapping.action_type,
        mapping.question_text,
        coalesce(mapping.created_at, now()),
        mapping.resolved_at
    FROM escalation_mappings mapping
    JOIN chat_sessions session_row ON session_row.id::text = mapping.session_id
    LEFT JOIN tickets ticket_row
        ON ticket_row.ticket_ref = coalesce(mapping.ticket_ref, mapping.jira_ticket_key)
    WHERE NOT EXISTS (SELECT 1 FROM escalations e WHERE e.id = mapping.id);

    GET DIAGNOSTICS recovered_rows = ROW_COUNT;
    IF recovered_rows > 0 THEN
        RAISE NOTICE 'Step 0: recovered % mis-keyed escalation_mappings row(s) into escalations', recovered_rows;
    END IF;
END $$;

-- ── Step 0b: backfill chat_messages.ticket_id for pre-fix tagged rows ───────
-- tag_message_as_ticket_comment (chat_orchestrator/orchestrator/services/
-- supabase_client.py) only ever wrote metadata.ticket_ref/ticket_role until
-- 2026-08-10 -- it now also resolves and sets ticket_id going forward, but
-- every message it tagged before that fix landed is still sitting on
-- ticket_id = NULL. Same UPDATE 0005a ran once already, reapplied to catch
-- everything that's drifted since.

DO $$
DECLARE
    backfilled_rows integer;
BEGIN
    UPDATE chat_messages message_row
    SET ticket_id = ticket_row.id
    FROM tickets ticket_row
    WHERE message_row.ticket_id IS NULL
      AND message_row.metadata ->> 'ticket_ref' = ticket_row.ticket_ref;

    GET DIAGNOSTICS backfilled_rows = ROW_COUNT;
    IF backfilled_rows > 0 THEN
        RAISE NOTICE 'Step 0b: backfilled ticket_id on % chat_messages row(s)', backfilled_rows;
    END IF;
END $$;

-- ── Step 1: rerun count and referential-integrity assertions ────────────────
-- Re-checks the invariants 0005a established, in case backfilled data has
-- drifted (e.g. a legacy row inserted after 0005a ran but before this
-- migration, which the sync triggers should have mirrored forward).

DO $$
DECLARE
    unmirrored_internal_tickets integer;
    unmirrored_escalations integer;
    unmirrored_comments integer;
    duplicate_refs integer;
    incomplete_active_tickets integer;
BEGIN
    -- A second run of this migration finds the legacy tables already
    -- archived (step 7) -- nothing left to reconcile them against, so skip
    -- rather than erroring on a relation that no longer exists.
    IF to_regclass('public.internal_tickets') IS NOT NULL THEN
        SELECT count(*) INTO unmirrored_internal_tickets
        FROM internal_tickets legacy
        WHERE NOT EXISTS (
            SELECT 1 FROM tickets t WHERE t.ticket_ref = legacy.ticket_ref
        );
        IF unmirrored_internal_tickets > 0 THEN
            RAISE EXCEPTION 'contract precondition failed: % internal_tickets row(s) have no matching tickets row', unmirrored_internal_tickets;
        END IF;
    END IF;

    IF to_regclass('public.escalation_mappings') IS NOT NULL THEN
        SELECT count(*) INTO unmirrored_escalations
        FROM escalation_mappings legacy
        WHERE NOT EXISTS (
            SELECT 1 FROM escalations e WHERE e.id = legacy.id
        )
        -- Reviewed and accepted 2026-08-10: session 'telegram_5d3ac3abfd3a79e4'
        -- was never persisted to chat_sessions -- escalation_mappings.session_id
        -- has no FK, so nothing prevented these 3 rows from being written
        -- against a session that doesn't exist. Confirmed not a formatting
        -- mismatch (no near-match in chat_sessions) and not a wider outage
        -- (other sessions were being created normally in the same window).
        -- Unrecoverable, not an unexamined gap -- still preserved via step 7's
        -- archive/rename, just excluded from this assertion.
        AND legacy.session_id IS DISTINCT FROM 'telegram_5d3ac3abfd3a79e4';
        IF unmirrored_escalations > 0 THEN
            RAISE EXCEPTION 'contract precondition failed: % escalation_mappings row(s) have no matching escalations row', unmirrored_escalations;
        END IF;
    END IF;

    IF to_regclass('public.internal_ticket_comments') IS NOT NULL THEN
        SELECT count(*) INTO unmirrored_comments
        FROM internal_ticket_comments legacy
        WHERE NOT EXISTS (
            SELECT 1 FROM ticket_comments c
            JOIN tickets t ON t.id = c.ticket_id
            WHERE t.ticket_ref = legacy.ticket_ref
              AND c.body = legacy.body
              AND c.created_at = coalesce(legacy.created_at, c.created_at)
        );
        IF unmirrored_comments > 0 THEN
            RAISE EXCEPTION 'contract precondition failed: % internal_ticket_comments row(s) have no matching ticket_comments row', unmirrored_comments;
        END IF;
    END IF;

    SELECT count(*) INTO duplicate_refs
    FROM (
        SELECT ticket_ref FROM tickets WHERE ticket_ref IS NOT NULL
        GROUP BY ticket_ref HAVING count(*) > 1
    ) dupes;
    IF duplicate_refs > 0 THEN
        RAISE EXCEPTION 'contract precondition failed: % duplicate ticket_ref value(s) in tickets', duplicate_refs;
    END IF;

    SELECT count(*) INTO incomplete_active_tickets
    FROM tickets
    WHERE provisioning_state = 'active'
      AND (ticket_ref IS NULL OR backend IS NULL OR activated_at IS NULL);
    IF incomplete_active_tickets > 0 THEN
        RAISE EXCEPTION 'contract precondition failed: % active ticket(s) missing ticket_ref/backend/activated_at', incomplete_active_tickets;
    END IF;
END $$;

-- ── Step 2: require zero recent legacy-only writes ──────────────────────────
-- A direct write to a legacy table this recently means some caller is still
-- bypassing the repositories (the sync triggers keep the canonical rows
-- correct either way, but their continued existence is exactly what this
-- migration must not assume away). Adjust RECENT_WRITE_WINDOW only after
-- confirming via application logs/metrics that no legacy writers remain.

DO $$
DECLARE
    recent_write_window interval := interval '24 hours';
    recent_legacy_writes integer := 0;
BEGIN
    -- Already archived by a prior run of this migration: nothing left to
    -- write to, so there is nothing to check.
    IF to_regclass('public.internal_tickets') IS NULL THEN
        RETURN;
    END IF;

    SELECT
        (SELECT count(*) FROM internal_tickets WHERE created_at > now() - recent_write_window OR updated_at > now() - recent_write_window)
      + (SELECT count(*) FROM escalation_mappings WHERE created_at > now() - recent_write_window OR resolved_at > now() - recent_write_window)
      + (SELECT count(*) FROM internal_ticket_comments WHERE created_at > now() - recent_write_window)
    INTO recent_legacy_writes;

    IF recent_legacy_writes > 0 THEN
        RAISE EXCEPTION 'contract precondition failed: % legacy-table write(s) within the last %; application has not fully cut over', recent_legacy_writes, recent_write_window;
    END IF;
END $$;

-- ── Step 3: rename final tables where expansion used temporary names ────────
-- Not applicable here: 0005a created tickets/escalations/ticket_comments/
-- message_deliveries directly under their final names, so there is nothing
-- to rename.

-- ── Step 4: remove legacy ticket identity, status, and message-coordinate
--            columns from ticket_correlations / ticket_correlation_events ───
-- Current ticket summary, status, backend, organization, and grid are read
-- by joining `tickets`; delivery coordinates are read from
-- `message_deliveries`. ticket_id becomes the primary key since one ticket
-- has at most one correlation row.

DO $$
DECLARE
    missing_ticket_id integer;
BEGIN
    SELECT count(*) INTO missing_ticket_id FROM ticket_correlations WHERE ticket_id IS NULL;
    IF missing_ticket_id > 0 THEN
        RAISE EXCEPTION 'contract precondition failed: % ticket_correlations row(s) missing ticket_id', missing_ticket_id;
    END IF;
END $$;

DROP INDEX IF EXISTS ticket_correlations_grid_idx;

ALTER TABLE ticket_correlations DROP CONSTRAINT IF EXISTS ticket_correlations_pkey;
ALTER TABLE ticket_correlations DROP CONSTRAINT IF EXISTS ticket_correlations_ticket_ref_key;
ALTER TABLE ticket_correlations ALTER COLUMN ticket_id SET NOT NULL;
ALTER TABLE ticket_correlations ADD CONSTRAINT ticket_correlations_pkey PRIMARY KEY (ticket_id);
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS id;
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS ticket_ref;
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS ticket_backend;
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS grid_name;
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS organization_id;
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS summary_current;
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS status;
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS telegram_chat_id;
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS telegram_topic_id;
ALTER TABLE ticket_correlations DROP COLUMN IF EXISTS telegram_message_id;

-- ticket_correlation_events: replace the text ticket_ref with the ticket_id
-- FK added in 0005a. Events that never resolved to a ticket keep ticket_id
-- null (valid per design -- they remain event-time evidence).
ALTER TABLE ticket_correlation_events DROP COLUMN IF EXISTS ticket_ref;

-- ── Step 5: remove JSON ticket annotations after their FK backfill is
--            verified ────────────────────────────────────────────────────────
-- chat_messages.ticket_id (backfilled in 0005a) replaces
-- metadata.ticket_ref / metadata.ticket_role as the relationship source of
-- truth. Only strip the JSON keys once every annotated row that could be
-- resolved to a real ticket has the FK set, so this can't silently discard
-- an un-backfilled relationship.

DO $$
DECLARE
    unresolved_annotations integer;
BEGIN
    SELECT count(*) INTO unresolved_annotations
    FROM chat_messages m
    WHERE m.metadata ? 'ticket_ref'
      AND m.ticket_id IS NULL
      AND EXISTS (SELECT 1 FROM tickets t WHERE t.ticket_ref = m.metadata ->> 'ticket_ref');
    IF unresolved_annotations > 0 THEN
        RAISE EXCEPTION 'contract precondition failed: % chat_messages row(s) have a resolvable ticket_ref annotation but no ticket_id', unresolved_annotations;
    END IF;
END $$;

UPDATE chat_messages
SET metadata = metadata - 'ticket_ref' - 'ticket_role'
WHERE metadata ?| array['ticket_ref', 'ticket_role'];

-- ── Step 6: remove redundant session escalation columns ─────────────────────
-- Session escalation state is derived from whether an open/processing
-- blocking escalation exists for that session (escalations.state).

DO $$
DECLARE
    mismatched_sessions integer;
BEGIN
    -- Already dropped by a prior run of this migration.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chat_sessions' AND column_name = 'is_escalated'
    ) THEN
        RETURN;
    END IF;

    SELECT count(*) INTO mismatched_sessions
    FROM chat_sessions s
    WHERE s.is_escalated
      AND NOT EXISTS (
          SELECT 1 FROM escalations e
          WHERE e.chat_session_id = s.id
            AND e.state IN ('open', 'processing')
      );
    IF mismatched_sessions > 0 THEN
        RAISE EXCEPTION 'contract precondition failed: % chat_sessions row(s) marked is_escalated with no corresponding open/processing escalations row', mismatched_sessions;
    END IF;
END $$;

ALTER TABLE chat_sessions DROP COLUMN IF EXISTS is_escalated;
ALTER TABLE chat_sessions DROP COLUMN IF EXISTS escalated_at;
ALTER TABLE chat_sessions DROP COLUMN IF EXISTS escalation_message_id;

-- ── Step 7: drop or archive replaced tables ──────────────────────────────────
-- Archived rather than dropped outright: renaming preserves the data (and
-- lets an operator inspect/recover it) while still freeing the canonical
-- table names from the sync triggers. A follow-up cleanup migration can
-- DROP TABLE these once retention has passed and the archive has been
-- confirmed unneeded.

DROP TRIGGER IF EXISTS trg_legacy_internal_ticket_to_ticket ON internal_tickets;
DROP TRIGGER IF EXISTS trg_legacy_escalation_to_escalation ON escalation_mappings;
DROP TRIGGER IF EXISTS trg_legacy_internal_ticket_comment_to_ticket_comment ON internal_ticket_comments;

DROP FUNCTION IF EXISTS sync_legacy_internal_ticket();
DROP FUNCTION IF EXISTS sync_legacy_escalation();
DROP FUNCTION IF EXISTS sync_legacy_internal_ticket_comment();

ALTER TABLE IF EXISTS internal_tickets RENAME TO archived_internal_tickets;
ALTER TABLE IF EXISTS internal_ticket_comments RENAME TO archived_internal_ticket_comments;
ALTER TABLE IF EXISTS escalation_mappings RENAME TO archived_escalation_mappings;

-- internal_ticket_seq / next_internal_ticket_ref intentionally remain: the
-- internal backend still allocates TKT-* refs from that sequence.

-- ── Step 8: recreate the final views and constraints against final names ────
-- ticket_list_view already reads from the final table names (tickets,
-- escalations, ticket_comments, message_deliveries, ticket_correlations,
-- chat_messages), none of which were renamed by this migration, so the view
-- from 0005a is already correct. Recreate it anyway so its definition is
-- re-validated against the post-contract column set.

CREATE OR REPLACE VIEW ticket_list_view AS
WITH escalation_counts AS (
    SELECT ticket_id, count(*)::integer AS escalation_count
    FROM escalations
    WHERE ticket_id IS NOT NULL
    GROUP BY ticket_id
), comment_activity AS (
    SELECT ticket_id, count(*)::integer AS activity_count, max(created_at) AS latest_comment_at
    FROM ticket_comments
    GROUP BY ticket_id
), message_activity AS (
    SELECT ticket_id, count(*)::integer AS activity_count, max(created_at) AS latest_message_at
    FROM chat_messages
    WHERE ticket_id IS NOT NULL
    GROUP BY ticket_id
), delivery_activity AS (
    SELECT ticket_id, count(*)::integer AS activity_count, max(sent_at) AS latest_delivery_at
    FROM message_deliveries
    WHERE ticket_id IS NOT NULL
    GROUP BY ticket_id
), correlation_activity AS (
    SELECT
        ticket_id,
        coalesce(jsonb_array_length(affected_keys), 0) AS affected_count,
        occurrence_count,
        last_alert_at
    FROM ticket_correlations
)
SELECT
    t.id,
    t.ticket_ref,
    t.backend,
    t.created_via,
    t.provisioning_state,
    t.status,
    t.backend_status,
    t.summary,
    t.ticket_type,
    t.organization_id,
    t.grid_name,
    t.assignee_email,
    t.created_at,
    t.updated_at,
    t.resolved_at,
    t.backend_synced_at,
    coalesce(e.escalation_count, 0) AS escalation_count,
    coalesce(e.escalation_count, 0) > 0 AS has_escalation,
    coalesce(c.activity_count, 0) + coalesce(m.activity_count, 0) + coalesce(d.activity_count, 0)
        AS activity_count,
    coalesce(r.affected_count, 0) AS affected_count,
    coalesce(r.occurrence_count, 0) AS occurrence_count,
    greatest(t.updated_at, c.latest_comment_at, m.latest_message_at, d.latest_delivery_at, r.last_alert_at)
        AS latest_activity_at
FROM tickets t
LEFT JOIN escalation_counts e ON e.ticket_id = t.id
LEFT JOIN comment_activity c ON c.ticket_id = t.id
LEFT JOIN message_activity m ON m.ticket_id = t.id
LEFT JOIN delivery_activity d ON d.ticket_id = t.id
LEFT JOIN correlation_activity r ON r.ticket_id = t.id;

COMMIT;
