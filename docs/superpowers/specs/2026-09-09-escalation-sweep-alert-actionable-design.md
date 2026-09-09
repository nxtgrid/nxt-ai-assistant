# Making the stale-escalation sweep alert readable and actionable

**Date:** 2026-09-09
**Status:** Approved, ready for planning

## Problem

The daily escalation sweep posts an admin alert listing every `state='open'`,
`ticket_id IS NULL` escalation older than 24h (excluding `safety_escalation`).
In production this alert has two failure modes:

1. **Every entry renders as a bare UUID.** The bullet label is
   `customer_username or masked_email or f"id:{esc['id']}"`, with `(#org)` only
   ever appended as a suffix. A row whose `customer_username`, `customer_email`
   *and* `org_hashtag` are all absent falls through to `id:<uuid>`. PR #192
   (2026-09-07) made `_record_canonical_escalation` persist those three fields
   for **new** escalations, but rows created before that deploy still have all
   three NULL, so they keep showing as UUIDs every day.

2. **The listed escalations cannot be closed by an admin.** The two rows stuck
   in the alert today are both `reason='verification_failed'`.
   `escalate_verification_failure` sends its Telegram message with **no
   `reply_markup`** — unlike `_escalate_to_telegram`, which attaches
   Track / Close-silently / Close-&-notify buttons. So the alert's `View` link
   lands on a button-less message, and there is no supported admin path to
   resolve the escalation. The close machinery itself already exists
   (`_handle_escalation_close_callback` → `EscalationRepository.resolve()`); it
   is simply never wired to this class of escalation.

### What is recoverable for the stuck rows

`chat_sessions.metadata->>'organization_short_name'` holds the org short name
for both stuck sessions, so `org_hashtag` can be backfilled. There is no
durable source for `customer_username` / `customer_email` on these rows, so
those stay NULL.

## Goals

- The sweep alert stops showing bare UUIDs whenever *any* human-meaningful
  identifier (name, email, or org) is known.
- Existing open, unfiled escalations missing `org_hashtag` get it backfilled
  from the session's stored org short name.
- An admin can close (fully resolve) a listed escalation in one tap, directly
  from the alert.
- `verification_failed` escalations carry action buttons going forward, like
  every other escalation.

## Non-goals

- Whether `verification_failed` escalations should be auto-filed as tickets, or
  be part of this sweep at all. Separate concern, untouched here.
- Backfilling `customer_username` / `customer_email` (no reliable source).
- Any "snooze / acknowledge but keep open" state. Close means fully resolve.

## Design

Four pieces. One PR. The backfill script is run once by hand after deploy.

### Piece 1 — Alert label precedence (`run_escalation_ticket_sweep`)

In the per-entry label construction inside `run_escalation_ticket_sweep`
(`chat_orchestrator/orchestrator/services/escalation_service.py`), change the
precedence so the org hashtag is used as the primary label when name and email
are both absent, and `id:<uuid>` becomes a true last resort:

```
primary   = customer_username or masked_email or org_hashtag or f"id:{esc['id']}"
label     = _escape_telegram_markdown(primary)
org_part  = f" ({_escape_telegram_markdown(org_raw)})"  if org_raw and org_raw != primary  else ""
```

Behaviour:

| Row state | Before | After |
|---|---|---|
| name + org | `Jane Doe (#ExampleOrg)` | `Jane Doe (#ExampleOrg)` (unchanged) |
| org only (backfilled) | `id:<uuid> (#ExampleOrg)` | `#ExampleOrg` |
| nothing known | `id:<uuid>` | `id:<uuid>` (unchanged) |

Applies to both the linkable-bullet branch and the unlinkable-footer branch of
the alert (they build labels the same way).

### Piece 2 — One-tap Close button in the alert

The alert is a single `_send_telegram_message` with plain text and N bullet
lines. Attach an inline keyboard: one button per **linkable** listed entry:

- text: `✓ Close — <same label as the bullet>`
- `callback_data`: `f"{ESCALATION_ALERT_DISMISS_PREFIX}:{esc_id}"`

New constant `ESCALATION_ALERT_DISMISS_PREFIX = "ex"` in
`shared/utils/telegram_buttons.py` (`es` / `ec` / `en` are taken). Payload is
`"ex:" + 36-char uuid` = 39 bytes, within Telegram's 64-byte `callback_data`
limit. A small keyboard-builder helper (e.g. `build_stale_alert_keyboard(rows)`)
lives next to `build_escalation_track_keyboard`.

Handler: extend `callback_handlers.py`'s escalation-callback dispatch to
recognise the `ex` prefix and call a new
`_handle_stale_alert_dismiss_callback(...)` which:

1. Resolves the canonical row: `_canonical_escalations(supabase_client).resolve(mapping_id)`
   (`state → 'resolved'`, `resolved_at` set; reversible with `reopen()`).
2. Answers the callback query with a toast: `✓ Closed — gone from tomorrow's alert.`
3. Does **not** edit the alert message (toast-only feedback; the alert is
   regenerated fresh each day). Pressing an already-resolved row's button again
   is a harmless no-op re-resolve.

Unlinkable entries (rendered only in the footer, no `escalation_message_id`)
get no button — same as today they get no `View` link. They remain the domain
of `scripts/resolve_stale_swept_escalations.py`.

### Piece 3 — Verification-failure escalations carry the standard keyboard

In `escalate_verification_failure`
(`chat_orchestrator/orchestrator/services/escalation_service.py`):

- Generate the escalation id **before** the `_send_telegram_message` call
  (today `saved_verification_id` is minted after the send). The other two
  escalation-creation paths already pre-generate their `mapping_id`.
- Build `build_escalation_track_keyboard(verification_id, include_track=True)`
  and pass it as `reply_markup`.
- **Drop the "Close & inform customer" button for this path.** A verification
  failure is an internal AI-quality flag; sending the customer a "your issue
  has been resolved" message for something they never saw is wrong. This needs
  a small change to `build_escalation_track_keyboard` — either a
  `include_close_notify: bool = True` parameter, or a dedicated
  `build_verification_failure_keyboard`. Parameter is lighter; decide in
  planning.
- Reuse `verification_id` for the `_record_canonical_escalation` call so the
  keyboard's id and the canonical row's id match (they must, for the close
  callback to find the row).

Normal escalations are unchanged — they keep all three buttons.

### Piece 4 — Backfill script

`scripts/backfill_escalation_org_hashtag.py`, modelled on
`scripts/resolve_stale_swept_escalations.py`:

- Dry-run by default; `--apply` to write. Preview-first, like the resolve
  script — these are real support records.
- Selects `escalations` rows where `state='open'` AND `ticket_id IS NULL` AND
  `org_hashtag IS NULL`.
- For each: read the linked `chat_sessions` row, take
  `metadata->>'organization_short_name'`, run it through the shared
  `_org_hashtag_from_short_name` helper (introduced in PR #192), and
  `UPDATE escalations SET org_hashtag = <value> WHERE id = <id> AND org_hashtag IS NULL`.
- Rows with no resolvable org short name are printed and skipped, not failed.
- Idempotent: a second run is a no-op; already-populated rows are never
  touched.
- Requires `CHAT_DB_URL` / `CHAT_DB_SERVICE_KEY` and
  `PYTHONPATH=<repo-root>` (per `scripts/` run conventions). Documented in the
  module docstring.

## Testing

TDD, per piece:

- **Piece 1:** `run_escalation_ticket_sweep` alert tests — org-only row →
  bullet reads `#Org — [View](...)`, no `id:`; name+org row → unchanged; row
  with nothing → still `id:<uuid>`; unlinkable org-only row → footer breadcrumb
  uses `#Org`, not `id:`.
- **Piece 2:** alert-render test → keyboard has exactly one `ex:<id>` button
  per linkable entry, none for unlinkable ones. Callback-handler test → `ex`
  prefix resolves the canonical row and answers the callback; second press on a
  resolved row does not raise.
- **Piece 3:** `escalate_verification_failure` test → message sent **with**
  `reply_markup`; keyboard contains Track + Close-silently, **not**
  Close-&-notify; the canonical row id equals the id embedded in the keyboard
  callbacks.
- **Piece 4:** script test in `shared/tests/` (mirroring
  `test_resolve_stale_swept_escalations.py`) — dry-run lists and writes
  nothing; `--apply` sets `org_hashtag` from session metadata; a row with no
  session org is skipped; a second `--apply` run is a no-op; a row with a
  pre-existing `org_hashtag` is never modified.

All fixtures use synthetic values (`Jane Doe`, `jane@example.com`,
`#ExampleOrg`) — this repo is public.

## Rollout

1. Merge the PR; GHCR build; deploy to DO (per `deployDO`).
2. Run `python scripts/backfill_escalation_org_hashtag.py` (dry-run), eyeball
   the matches, then `--apply`.
3. Next daily alert (≈09:00 WAT) shows `#Org — View` lines with a `✓ Close`
   button each; tapping one clears it.
