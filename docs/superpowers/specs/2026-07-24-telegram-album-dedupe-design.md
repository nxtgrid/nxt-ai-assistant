# Telegram Album Deduplication Repair

## Goal

Ensure an addressed Telegram photo album is processed exactly once after its
photos are merged, while retaining retry deduplication for the original
Telegram updates.

## Cause

`_buffer_media_group_message` records every incoming album photo in the
process-local deduplication cache. `_flush_media_group` then re-enters
`async_main` with the last photo's message ID. `_handle_webhook_async` applies
the same deduplication check a second time and rejects the merged album.

## Design

`_flush_media_group` will attach a module-private object sentinel to the
in-memory re-entry. The normalizer will tag an album only when that sentinel
has been verified, and the async webhook handler will skip its second
deduplication check only for that tag. Raw Telegram retries remain protected
by the deduplication check in `_buffer_media_group_message`; ordinary Telegram
messages retain the existing handler-level check.

The regression test will send a group album through the buffer and its merged
re-entry, verify that one background processing task is created, and verify
that the retry cache still contains the original photo ID.

Deployment manifests will declare `TELEGRAM_BOT_USERNAME` so a future app
creation or manifest-based update retains the existing production setting.

## Non-goals

This repair does not change the scheduled-message system-error behavior. That
issue has separate runtime evidence and needs its own log-led investigation.
