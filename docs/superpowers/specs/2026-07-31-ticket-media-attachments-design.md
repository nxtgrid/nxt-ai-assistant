# Ticket Media Attachments — Phase 1 (Escalation-Triggering Media)

**Status:** Approved for planning
**Branch:** `feat/ticket-media-attachments` (worktree: `.worktrees/media-tickets`, based on `fix/canonical-escalation-dual-write-phase1`)
**Date:** 2026-07-31

## Problem

Images/video/audio a customer sends in chat can trigger an escalation, but that media
has no path into the resulting support ticket today, let alone into Jira when the
ticket syncs there. It's used once for an LLM vision prompt (if present) and then
discarded — never persisted, never attached to anything downstream.

## Scope (phase 1)

In scope: media present on the message that triggers an escalation (the turn where
`escalate_to_support()` is called) flows through to:
1. The internal ticket record, always.
2. The Jira issue, when the ticket backend is Jira.

Out of scope (deferred to a later phase):
- Media sent as a *follow-up* to an already-active escalation (`forward_customer_message()`
  / `_forward_telegram_media()`), which today re-forwards Telegram `file_id`s
  peer-to-peer into the internal Telegram group without ever downloading bytes.
- Media from earlier in the conversation history, before the escalating turn
  (`chat_messages` gains no new columns in this phase; media stays un-persisted at
  the message level in general).

## Why this shape

Ticket creation (`track_as_ticket()`) never runs synchronously in the same request as
the escalating chat turn — it's only invoked later, from an operator's "File as
ticket" Telegram callback (`callback_handlers.py`) or a periodic auto-file sweep
(`escalation_service.py: run_escalation_ticket_sweep`). Both re-fetch conversation
history from the DB at that later point; the original request's in-memory
`state["media"]` is long gone by then.

`escalate_to_support()`, in contrast, *is* called synchronously from within the chat
graph (`conversation_graph.py`, `prepare_tools.py`, `safety_check.py`), where
`state["metadata"]`'s raw Telegram file_ids for the current turn are still available.

So capture happens at escalation time (persist to storage + a new table keyed by
`escalation_id`), and attachment happens at ticket-filing time (read that stored
media back out, possibly much later, from a different process).

## Architecture

```
[chat turn w/ media] → prepare_media (existing, unchanged — LLM vision path untouched)
        ↓ state["metadata"] still has raw file_ids
[escalate_to_support() called] → NEW: download file_ids (own size cap) → upload to
                                       Supabase Storage bucket `escalation-media`
                                       → insert row(s) into escalation_attachments
        ↓ (later, different request — operator click or sweep job)
[track_as_ticket()] → NEW: look up escalation_attachments by escalation_id
                            → internal backend: link (ticket_id column), no re-upload
                            → Jira backend: upload each as a Jira issue attachment,
                              stamp jira_attachment_id (idempotency marker)
```

## Data model

New migration (next number in sequence, e.g. `0008_escalation_attachments.sql`):

```sql
CREATE TABLE IF NOT EXISTS escalation_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    escalation_id UUID NOT NULL REFERENCES escalations(id) ON DELETE CASCADE,
    ticket_id UUID REFERENCES tickets(id),   -- NULL until the ticket is filed
    storage_path TEXT NOT NULL,              -- path within the Storage bucket
    media_type TEXT NOT NULL,                -- 'image' | 'video' | 'audio' | 'document'
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    jira_attachment_id TEXT,                 -- set once synced to Jira; NULL until then
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON escalation_attachments(escalation_id);
CREATE INDEX ON escalation_attachments(ticket_id);
```

Keyed by `escalation_id` because attachments can exist before any ticket does — an
escalation may sit unfiled for a while, or never get filed at all. `ticket_id` is
filled in once `track_as_ticket()` runs. `jira_attachment_id` NULL/non-NULL is the
Jira-sync idempotency marker, so the sweep job can retry without double-uploading.

**Storage:** one new private Supabase Storage bucket, `escalation-media`. Objects
addressed as `{escalation_id}/{attachment_id}.{ext}`. Accessed server-side only via
the existing service-role Supabase client — no signed URLs handed to end users, no
new auth mechanism.

**Size limit:** a new constant, independent of `MAX_MEDIA_SIZE_BYTES` (which stays
5MB and untouched, for the LLM-vision download path in `telegram_transport.py`).
The ticket-attachment cap is set to match Jira Cloud's own per-file attachment limit
(confirm the exact figure against the target Jira instance — typically 10MB on
standard Cloud plans) so nothing is accepted here that Jira would later reject
anyway.

## Capture path

- `download_telegram_photo()` in `telegram_transport.py` gains an optional
  `max_size_bytes` parameter, defaulting to today's `MAX_MEDIA_SIZE_BYTES` so every
  existing caller (the vision path) is unaffected.
- `escalate_to_support()` gains an optional `media_file_ids` param — a list of
  `{type, file_id}` pulled from `state["metadata"]` by the calling graph node (not
  from `state["media"]`, which has already been silently capped at 5MB by the time
  it's populated — see note below).
- The three graph call sites (`conversation_graph.py` ~1011/1074/1466 and
  `safety_check.py` ~190) pass `media_file_ids` through from `state["metadata"]`.
- Each file_id is downloaded fresh (own higher cap), uploaded to
  `escalation-media`, and inserted into `escalation_attachments` — *after* the
  escalation row itself is created/claimed, so a media failure never blocks the
  escalation from going through. Per-file failures are logged and skipped, not
  fatal.

**Note on why this re-downloads rather than reusing `state["media"]`:**
`download_telegram_photo()`'s existing 5MB cap is checked *before* anything is
added to `state["media"]` — a 7MB video is silently dropped upstream, with only its
raw `file_id` surviving in `state["metadata"]`. Reusing `state["media"]` for ticket
attachments would silently lose anything already dropped by the tighter vision-path
cap, so the capture step re-downloads independently at the higher cap instead.

## Ticket-creation path (internal)

- `TicketBackend` Protocol (`backend.py`) gains one method:
  `add_attachments(ticket_ref: str, attachments: List[EscalationAttachment]) -> AttachmentSyncResult`,
  called after `create_ticket()` succeeds — not folded into `TicketCreateRequest`,
  since it's a distinct operation per backend with its own partial-failure mode.
- `TicketService.create_ticket()` (`service.py`): after a successful
  `create_ticket()`, if `escalation_attachments` rows exist for this
  `escalation_id`, calls `backend.add_attachments(...)`.
- **Internal backend**: "attaching" means writing the new ticket's `id` onto the
  existing `escalation_attachments` rows — the file already lives in Storage,
  nothing to re-upload. Always happens, independent of Jira sync.
- Ticket UI needs a small read-side addition to list/link attachments (exact
  location TBD during implementation — will check the current ticket view first).

## Jira sync path

- New `_upload_jira_attachment(issue_key, filename, content, mime_type)` in
  `jira_backend.py`, reusing `_get_jira_session()` and the Basic-auth credentials
  from `_jira_auth_headers()`, but with its own headers dict (`X-Atlassian-Token:
  no-check`, no fixed `Content-Type` — aiohttp sets the multipart boundary) since
  the existing helper hardcodes `application/json`.
- `JiraBackend.add_attachments()` fetches each attachment's bytes from Storage,
  `POST`s to `/rest/api/3/issue/{key}/attachments`, and on success writes the
  returned Jira attachment id onto `escalation_attachments.jira_attachment_id`.
- `jira_attachment_id` NULL is the retry signal — safe to re-run from
  `run_escalation_jira_sweep` without double-uploading, mirroring the existing
  pattern that sweep already uses for ticket creation itself.

## Error handling

- Download/upload failures are per-file, logged, and skipped — never block
  escalation creation or ticket filing. A ticket with 2-of-3 attachments
  successfully attached is an acceptable degraded state.
- Jira upload failure leaves `jira_attachment_id` NULL, naturally retried by the
  existing sweep-job cadence — no new retry mechanism.

## Testing

- Unit tests: `download_telegram_photo(max_size_bytes=...)` cap enforcement;
  `escalation_attachments` repository methods (insert, mark-synced, list-by-
  escalation).
- `escalate_to_support()`: escalation succeeds even when media download/upload
  fails.
- `TicketService.create_ticket()`: internal backend links existing rows; Jira
  backend uploads and stamps `jira_attachment_id`; partial Jira failure leaves the
  ticket and other attachments intact.
- `_upload_jira_attachment()`: correct multipart headers/endpoint, mocked aiohttp
  session (matching existing `jira_backend.py` test patterns).
- One integration-style test: fake Telegram media → `escalate_to_support()` →
  `track_as_ticket()` (internal) → attachments present on the ticket record.
- New test files force-added (`git add -f`) per this repo's `tests/` `.gitignore`
  convention; `pre-commit run --all-files` run before considering any of this done.

## Explicitly out of scope (phase 1)

- Mid-escalation follow-up media (`forward_customer_message()` /
  `_forward_telegram_media()`) getting downloaded and attached, rather than just
  forwarded into the internal Telegram group.
- Persisting media on every `chat_messages` row / full conversation-history media.
- Any new UI for uploading media directly (this is entirely about media that
  arrives via the existing Telegram chat path).
