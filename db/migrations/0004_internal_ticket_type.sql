-- Normalized ticket type for Jira-independent internal tickets. Nullable keeps
-- pre-existing rows readable; application code treats NULL as the Task default.
ALTER TABLE internal_tickets
    ADD COLUMN IF NOT EXISTS ticket_type text;
