BEGIN;

-- Media attachments captured from the chat turn that triggers an escalation.
-- Keyed by escalation_id (not ticket_id) because an escalation can sit
-- unfiled for a while, or never get filed -- the media is captured at
-- escalation time, well before any ticket exists. ticket_id is filled in by
-- TicketService.create_ticket() once the ticket is created.
CREATE TABLE IF NOT EXISTS escalation_attachments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    escalation_id uuid NOT NULL REFERENCES escalations(id) ON DELETE CASCADE,
    ticket_id uuid REFERENCES tickets(id),
    storage_path text NOT NULL,
    media_type text NOT NULL,
    mime_type text NOT NULL,
    size_bytes integer NOT NULL,
    jira_attachment_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_escalation_attachments_escalation_id
    ON escalation_attachments(escalation_id);
CREATE INDEX IF NOT EXISTS idx_escalation_attachments_ticket_id
    ON escalation_attachments(ticket_id);

-- Private bucket -- objects are only ever read/written server-side via the
-- service-role client (same client every other table in this migration is
-- accessed through), so no storage.objects RLS policies are needed.
INSERT INTO storage.buckets (id, name, public)
VALUES ('escalation-media', 'escalation-media', false)
ON CONFLICT (id) DO NOTHING;

COMMIT;
