BEGIN;

-- Canonical Anansi ticket schema: expand phase.
--
-- This migration deliberately keeps legacy ticket relations intact.  The
-- application switches to the canonical relations before the companion
-- contract migration removes obsolete storage.
CREATE TABLE IF NOT EXISTS tickets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref text,
    backend text,
    created_via text NOT NULL,
    provisioning_state text NOT NULL DEFAULT 'pending',
    status text NOT NULL DEFAULT 'open',
    backend_status text,
    summary text NOT NULL,
    description text,
    ticket_type text,
    organization_id integer,
    grid_name text,
    assignee_email text,
    labels jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    backend_synced_at timestamptz
);

CREATE TABLE IF NOT EXISTS escalations (
    id uuid PRIMARY KEY,
    chat_session_id uuid NOT NULL REFERENCES chat_sessions(id),
    thread_id text REFERENCES chat_threads(thread_id),
    ticket_id uuid REFERENCES tickets(id),
    state text NOT NULL DEFAULT 'open',
    customer_username text,
    customer_email text,
    org_hashtag text,
    reason text,
    action_type text,
    question_text text,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id uuid NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    backend_comment_id text,
    author text,
    body text NOT NULL,
    is_public boolean NOT NULL DEFAULT false,
    source text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS message_deliveries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id uuid REFERENCES tickets(id) ON DELETE CASCADE,
    escalation_id uuid REFERENCES escalations(id) ON DELETE CASCADE,
    chat_message_id uuid REFERENCES chat_messages(id) ON DELETE SET NULL,
    purpose text NOT NULL,
    channel text NOT NULL DEFAULT 'telegram',
    external_chat_id text NOT NULL,
    external_topic_id text,
    external_message_id bigint NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS ticket_id uuid;
ALTER TABLE ticket_correlations ADD COLUMN IF NOT EXISTS ticket_id uuid;
ALTER TABLE ticket_correlation_events ADD COLUMN IF NOT EXISTS ticket_id uuid;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tickets_backend_check') THEN
        ALTER TABLE tickets ADD CONSTRAINT tickets_backend_check
            CHECK (backend IN ('jira', 'internal'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tickets_created_via_check') THEN
        ALTER TABLE tickets ADD CONSTRAINT tickets_created_via_check
            CHECK (created_via IN ('escalation', 'notification', 'adopted', 'legacy'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tickets_provisioning_state_check') THEN
        ALTER TABLE tickets ADD CONSTRAINT tickets_provisioning_state_check
            CHECK (provisioning_state IN ('pending', 'active', 'failed'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tickets_status_check') THEN
        ALTER TABLE tickets ADD CONSTRAINT tickets_status_check
            CHECK (status IN ('open', 'in_progress', 'done'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tickets_active_requires_backend_ref') THEN
        ALTER TABLE tickets ADD CONSTRAINT tickets_active_requires_backend_ref
            CHECK (
                provisioning_state <> 'active'
                OR (ticket_ref IS NOT NULL AND backend IS NOT NULL AND activated_at IS NOT NULL)
            );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'escalations_state_check') THEN
        ALTER TABLE escalations ADD CONSTRAINT escalations_state_check
            CHECK (state IN ('open', 'processing', 'tracked', 'resolved'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ticket_comments_source_check') THEN
        ALTER TABLE ticket_comments ADD CONSTRAINT ticket_comments_source_check
            CHECK (source IN ('customer', 'staff', 'notify', 'jira', 'system'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'message_deliveries_purpose_check') THEN
        ALTER TABLE message_deliveries ADD CONSTRAINT message_deliveries_purpose_check
            CHECK (purpose IN ('escalation', 'notification', 'update'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'message_deliveries_channel_check') THEN
        ALTER TABLE message_deliveries ADD CONSTRAINT message_deliveries_channel_check
            CHECK (channel IN ('telegram'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'message_deliveries_owner_required') THEN
        ALTER TABLE message_deliveries ADD CONSTRAINT message_deliveries_owner_required
            CHECK (ticket_id IS NOT NULL OR escalation_id IS NOT NULL);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chat_messages_ticket_id_fkey') THEN
        ALTER TABLE chat_messages ADD CONSTRAINT chat_messages_ticket_id_fkey
            FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ticket_correlations_ticket_id_fkey') THEN
        ALTER TABLE ticket_correlations ADD CONSTRAINT ticket_correlations_ticket_id_fkey
            FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ticket_correlation_events_ticket_id_fkey') THEN
        ALTER TABLE ticket_correlation_events ADD CONSTRAINT ticket_correlation_events_ticket_id_fkey
            FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS tickets_ticket_ref_unique
    ON tickets (ticket_ref) WHERE ticket_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS tickets_status_created_at_idx
    ON tickets (status, created_at DESC);
CREATE INDEX IF NOT EXISTS tickets_backend_status_created_at_idx
    ON tickets (backend, status, created_at DESC);
CREATE INDEX IF NOT EXISTS tickets_created_via_status_created_at_idx
    ON tickets (created_via, status, created_at DESC);
CREATE INDEX IF NOT EXISTS tickets_organization_id_idx ON tickets (organization_id);
CREATE INDEX IF NOT EXISTS tickets_grid_name_status_idx ON tickets (grid_name, status);
CREATE INDEX IF NOT EXISTS escalations_ticket_id_idx ON escalations (ticket_id);
CREATE INDEX IF NOT EXISTS escalations_state_created_at_idx ON escalations (state, created_at DESC);
CREATE INDEX IF NOT EXISTS ticket_comments_ticket_id_created_at_idx
    ON ticket_comments (ticket_id, created_at);
CREATE INDEX IF NOT EXISTS message_deliveries_ticket_id_sent_at_idx
    ON message_deliveries (ticket_id, sent_at);
CREATE INDEX IF NOT EXISTS message_deliveries_escalation_id_sent_at_idx
    ON message_deliveries (escalation_id, sent_at);
CREATE UNIQUE INDEX IF NOT EXISTS message_deliveries_external_identity_unique
    ON message_deliveries (channel, external_chat_id, external_message_id);
CREATE INDEX IF NOT EXISTS chat_messages_ticket_id_created_at_idx
    ON chat_messages (ticket_id, created_at);

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_tickets_updated_at ON tickets;
CREATE TRIGGER trg_tickets_updated_at
    BEFORE UPDATE ON tickets
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Backfill canonical ticket identity first.  The two inserts intentionally
-- give internal-ticket evidence precedence over mappings and correlations.
INSERT INTO tickets (
    ticket_ref, backend, created_via, provisioning_state, status, backend_status,
    summary, description, organization_id, grid_name, assignee_email, labels,
    created_at, activated_at, updated_at, resolved_at, backend_synced_at
)
SELECT
    legacy.ticket_ref,
    'internal',
    CASE WHEN legacy.source = 'notify' THEN 'notification' ELSE 'escalation' END,
    'active',
    legacy.status,
    legacy.status,
    legacy.summary,
    legacy.description,
    legacy.organization_id,
    legacy.grid_name,
    legacy.assignee_email,
    coalesce(legacy.labels, '[]'::jsonb),
    coalesce(legacy.created_at, now()),
    coalesce(legacy.created_at, now()),
    coalesce(legacy.updated_at, now()),
    legacy.resolved_at,
    coalesce(legacy.updated_at, now())
FROM internal_tickets legacy
ON CONFLICT (ticket_ref) WHERE ticket_ref IS NOT NULL DO NOTHING;

INSERT INTO tickets (
    ticket_ref, backend, created_via, provisioning_state, status, backend_status,
    summary, description, organization_id, grid_name, labels,
    created_at, activated_at, updated_at, resolved_at, backend_synced_at
)
SELECT
    candidate.ticket_ref,
    candidate.backend,
    candidate.created_via,
    'active',
    candidate.status,
    candidate.backend_status,
    candidate.summary,
    candidate.description,
    candidate.organization_id,
    candidate.grid_name,
    candidate.labels,
    candidate.created_at,
    candidate.created_at,
    candidate.updated_at,
    candidate.resolved_at,
    NULL
FROM (
    SELECT
        coalesce(mapping.ticket_ref, mapping.jira_ticket_key) AS ticket_ref,
        'jira'::text AS backend,
        'escalation'::text AS created_via,
        CASE WHEN mapping.resolved_at IS NULL THEN 'open' ELSE 'done' END AS status,
        NULL::text AS backend_status,
        coalesce(mapping.question_text, mapping.reason, coalesce(mapping.ticket_ref, mapping.jira_ticket_key)) AS summary,
        mapping.question_text AS description,
        mapping.organization_id,
        NULL::text AS grid_name,
        '[]'::jsonb AS labels,
        coalesce(mapping.created_at, now()) AS created_at,
        coalesce(mapping.created_at, now()) AS updated_at,
        mapping.resolved_at
    FROM escalation_mappings mapping
    WHERE coalesce(mapping.ticket_ref, mapping.jira_ticket_key) IS NOT NULL

    UNION ALL

    SELECT
        correlation.ticket_ref,
        coalesce(correlation.ticket_backend, 'jira') AS backend,
        CASE
            WHEN EXISTS (
                SELECT 1 FROM ticket_correlation_events event_row
                WHERE event_row.ticket_ref = correlation.ticket_ref
                  AND event_row.source = 'notify'
            ) THEN 'notification'
            WHEN EXISTS (
                SELECT 1 FROM ticket_correlation_events event_row
                WHERE event_row.ticket_ref = correlation.ticket_ref
                  AND event_row.decision = 'amend'
                  AND event_row.candidate_refs ? correlation.ticket_ref
            ) THEN 'adopted'
            ELSE 'legacy'
        END AS created_via,
        CASE WHEN correlation.status = 'done' THEN 'done' ELSE 'open' END AS status,
        NULL::text AS backend_status,
        coalesce(correlation.summary_current, correlation.summary_base, correlation.ticket_ref) AS summary,
        correlation.description_base AS description,
        correlation.organization_id,
        correlation.grid_name,
        '[]'::jsonb AS labels,
        coalesce(correlation.created_at, now()) AS created_at,
        coalesce(correlation.updated_at, correlation.created_at, now()) AS updated_at,
        NULL::timestamptz AS resolved_at
    FROM ticket_correlations correlation
) candidate
WHERE candidate.ticket_ref IS NOT NULL
ON CONFLICT (ticket_ref) WHERE ticket_ref IS NOT NULL DO NOTHING;

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
JOIN chat_sessions session_row ON session_row.session_id = mapping.session_id
LEFT JOIN tickets ticket_row
    ON ticket_row.ticket_ref = coalesce(mapping.ticket_ref, mapping.jira_ticket_key)
ON CONFLICT (id) DO UPDATE
SET ticket_id = EXCLUDED.ticket_id,
    state = EXCLUDED.state,
    resolved_at = EXCLUDED.resolved_at;

INSERT INTO ticket_comments (ticket_id, author, body, is_public, source, created_at)
SELECT
    ticket_row.id,
    legacy.author,
    legacy.body,
    coalesce(legacy.is_public, false),
    CASE WHEN legacy.source IN ('customer', 'staff', 'notify', 'system') THEN legacy.source ELSE 'staff' END,
    coalesce(legacy.created_at, now())
FROM internal_ticket_comments legacy
JOIN tickets ticket_row ON ticket_row.ticket_ref = legacy.ticket_ref
WHERE NOT EXISTS (
    SELECT 1 FROM ticket_comments existing
    WHERE existing.ticket_id = ticket_row.id
      AND existing.body = legacy.body
      AND existing.created_at = coalesce(legacy.created_at, now())
);

UPDATE chat_messages message_row
SET ticket_id = ticket_row.id
FROM tickets ticket_row
WHERE message_row.ticket_id IS NULL
  AND message_row.metadata ->> 'ticket_ref' = ticket_row.ticket_ref;

UPDATE ticket_correlations correlation
SET ticket_id = ticket_row.id
FROM tickets ticket_row
WHERE correlation.ticket_id IS NULL
  AND correlation.ticket_ref = ticket_row.ticket_ref;

UPDATE ticket_correlation_events event_row
SET ticket_id = ticket_row.id
FROM tickets ticket_row
WHERE event_row.ticket_id IS NULL
  AND event_row.ticket_ref = ticket_row.ticket_ref;

INSERT INTO message_deliveries (
    ticket_id, purpose, channel, external_chat_id, external_topic_id,
    external_message_id, sent_at
)
SELECT
    ticket_row.id,
    'notification',
    'telegram',
    correlation.telegram_chat_id,
    correlation.telegram_topic_id,
    correlation.telegram_message_id,
    coalesce(correlation.created_at, now())
FROM ticket_correlations correlation
JOIN tickets ticket_row ON ticket_row.id = correlation.ticket_id
WHERE correlation.telegram_chat_id IS NOT NULL
  AND correlation.telegram_message_id IS NOT NULL
ON CONFLICT (channel, external_chat_id, external_message_id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM tickets
        WHERE provisioning_state = 'active'
          AND (ticket_ref IS NULL OR backend IS NULL OR activated_at IS NULL)
    ) THEN
        RAISE EXCEPTION 'canonical active ticket invariant failed';
    END IF;
    IF EXISTS (
        SELECT ticket_ref FROM tickets WHERE ticket_ref IS NOT NULL
        GROUP BY ticket_ref HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'canonical ticket reference uniqueness invariant failed';
    END IF;
END $$;

-- Keep the canonical model complete while an older application release may
-- still write the legacy relations.  These one-way triggers disappear in the
-- contract migration once all runtime writers use repositories.
CREATE OR REPLACE FUNCTION sync_legacy_internal_ticket()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO tickets (
        ticket_ref, backend, created_via, provisioning_state, status, backend_status,
        summary, description, ticket_type, organization_id, grid_name, assignee_email,
        labels, created_at, activated_at, updated_at, resolved_at, backend_synced_at
    ) VALUES (
        NEW.ticket_ref,
        'internal',
        CASE WHEN NEW.source = 'notify' THEN 'notification' ELSE 'escalation' END,
        'active',
        NEW.status,
        NEW.status,
        NEW.summary,
        NEW.description,
        NULL,
        NEW.organization_id,
        NEW.grid_name,
        NEW.assignee_email,
        coalesce(NEW.labels, '[]'::jsonb),
        coalesce(NEW.created_at, now()),
        coalesce(NEW.created_at, now()),
        coalesce(NEW.updated_at, now()),
        NEW.resolved_at,
        now()
    )
    ON CONFLICT (ticket_ref) WHERE ticket_ref IS NOT NULL DO UPDATE
    SET backend = EXCLUDED.backend,
        created_via = CASE
            WHEN tickets.created_via = 'legacy' THEN EXCLUDED.created_via
            ELSE tickets.created_via
        END,
        status = EXCLUDED.status,
        backend_status = EXCLUDED.backend_status,
        summary = EXCLUDED.summary,
        description = EXCLUDED.description,
        organization_id = EXCLUDED.organization_id,
        grid_name = EXCLUDED.grid_name,
        assignee_email = EXCLUDED.assignee_email,
        labels = EXCLUDED.labels,
        resolved_at = EXCLUDED.resolved_at,
        backend_synced_at = EXCLUDED.backend_synced_at;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sync_legacy_escalation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    canonical_session_id uuid;
    canonical_ticket_id uuid;
BEGIN
    SELECT id INTO canonical_session_id
    FROM chat_sessions
    WHERE session_id = NEW.session_id;

    -- A legacy mapping without its source session cannot satisfy the canonical
    -- foreign key.  Leave it for the contract migration's validation instead
    -- of making an old writer fail during the compatibility window.
    IF canonical_session_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT id INTO canonical_ticket_id
    FROM tickets
    WHERE ticket_ref = coalesce(NEW.ticket_ref, NEW.jira_ticket_key);

    INSERT INTO escalations (
        id, chat_session_id, thread_id, ticket_id, state,
        customer_username, customer_email, org_hashtag, reason, action_type,
        question_text, created_at, resolved_at
    ) VALUES (
        NEW.id,
        canonical_session_id,
        NEW.thread_id,
        canonical_ticket_id,
        CASE
            WHEN NEW.resolved_at IS NOT NULL THEN 'resolved'
            WHEN coalesce(NEW.is_active, true) THEN 'open'
            ELSE 'resolved'
        END,
        NEW.customer_username,
        NEW.customer_email,
        NEW.org_hashtag,
        NEW.reason,
        NEW.action_type,
        NEW.question_text,
        coalesce(NEW.created_at, now()),
        NEW.resolved_at
    )
    ON CONFLICT (id) DO UPDATE
    SET thread_id = EXCLUDED.thread_id,
        ticket_id = EXCLUDED.ticket_id,
        state = EXCLUDED.state,
        customer_username = EXCLUDED.customer_username,
        customer_email = EXCLUDED.customer_email,
        org_hashtag = EXCLUDED.org_hashtag,
        reason = EXCLUDED.reason,
        action_type = EXCLUDED.action_type,
        question_text = EXCLUDED.question_text,
        resolved_at = EXCLUDED.resolved_at;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sync_legacy_internal_ticket_comment()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO ticket_comments (ticket_id, author, body, is_public, source, created_at)
    SELECT ticket_row.id, NEW.author, NEW.body, coalesce(NEW.is_public, false),
           CASE WHEN NEW.source IN ('customer', 'staff', 'notify', 'system') THEN NEW.source ELSE 'staff' END,
           coalesce(NEW.created_at, now())
    FROM tickets ticket_row
    WHERE ticket_row.ticket_ref = NEW.ticket_ref
      AND NOT EXISTS (
          SELECT 1 FROM ticket_comments comment_row
          WHERE comment_row.ticket_id = ticket_row.id
            AND comment_row.created_at = coalesce(NEW.created_at, now())
            AND comment_row.body = NEW.body
      );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_legacy_internal_ticket_to_ticket ON internal_tickets;
CREATE TRIGGER trg_legacy_internal_ticket_to_ticket
    AFTER INSERT OR UPDATE ON internal_tickets
    FOR EACH ROW EXECUTE FUNCTION sync_legacy_internal_ticket();

DROP TRIGGER IF EXISTS trg_legacy_escalation_to_escalation ON escalation_mappings;
CREATE TRIGGER trg_legacy_escalation_to_escalation
    AFTER INSERT OR UPDATE ON escalation_mappings
    FOR EACH ROW EXECUTE FUNCTION sync_legacy_escalation();

DROP TRIGGER IF EXISTS trg_legacy_internal_ticket_comment_to_ticket_comment ON internal_ticket_comments;
CREATE TRIGGER trg_legacy_internal_ticket_comment_to_ticket_comment
    AFTER INSERT ON internal_ticket_comments
    FOR EACH ROW EXECUTE FUNCTION sync_legacy_internal_ticket_comment();

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
    WHERE ticket_id IS NOT NULL
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
