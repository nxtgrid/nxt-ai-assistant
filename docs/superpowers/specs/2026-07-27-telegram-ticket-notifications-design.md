# Telegram Ticket Notifications Design

## Goal

Make `POST /chat/notify` messages immediately actionable in Telegram. A
ticketed alert must show its subject, retain its details, display an urgent
red indicator when appropriate, and make the ticket reference itself the
only visible link for either ticket backend.

## Configuration

`APP_URL` is the canonical public Anansi application URL. The production
manifest will define it at app scope so both the chat orchestrator and the
Anansi UI inherit the same value. Existing local environment examples will
document it. The value is normalized by removing a trailing slash.

## Notification rendering

For a notification associated with a ticket, the renderer produces Telegram
Markdown in this order:

1. `🔴 ` when the alert's severity is `urgent` (including severity derived
   from an urgent subject), otherwise no indicator.
2. A bold subject. It uses `alert.subject` when present, otherwise the first
   non-empty line of the notification text, and finally `Notification`.
3. The original notification text as the body.
4. `🎫 Ticket: OPS-3124` (or its internal equivalent), with the reference
   itself as the linked text.

Jira uses the URL returned by the Jira backend. Internal tickets use
`{APP_URL}/tickets/{ref}`. If no URL is available, the reference is rendered
as bold text rather than sending an invalid link. Markdown-special characters
in all dynamic text are escaped before sending. Existing plain passthrough
notifications (no ticket reference) retain their current body-only output.

## Data flow

Ticket resolution already returns a backend-neutral `TicketResult` with
`ref`, `backend`, and an optional URL. The notify flow will pass that result
to delivery rather than only the reference. Delivery resolves an internal
ticket URL from `APP_URL` when needed, formats the notification once, then
uses the existing Telegram Markdown conversion and plain-text fallback.

The alert-facts enrichment used by `ticket_id="auto"` will supply derived
urgency. Explicitly created ticket notifications use the supplied alert
severity when available and otherwise derive it from the supplied subject.

## Error handling

An unset `APP_URL` never prevents delivery: internal ticket references fall
back to bold, unlinked text. Jira continues to use its backend-provided URL.
Telegram Markdown parse failures keep the existing plain-text fallback.

## Tests

Focused `/chat/notify` delivery tests will cover:

- Jira subject/body rendering with its reference as a Markdown link.
- Internal ticket deep links via `APP_URL`.
- Urgent rendering from an explicit or derived alert severity.
- Graceful internal fallback when `APP_URL` is missing.
- Existing non-ticket passthrough behavior.

Deployment-manifest tests will require the canonical `APP_URL` setting to be
available to the chat orchestrator.
