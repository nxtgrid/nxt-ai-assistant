# Alert Correlation Noise Reduction Design

## Goal

Prevent duplicate `/chat/notify` tickets during transient Jira read failures and
only notify the grid's Telegram topic when an alert creates a ticket or adds
meaningful equipment information to an existing one.

## Evidence and root cause

Production logs show both `OPS-3355` and `OPS-3361` being created after
`JiraTicketBackend.find_open_by_grid` returned a non-200 HTTP response. The
current search log uses percent-style placeholders with Loguru, so it hides the
status and response body that would identify the Jira API failure.

The more important unsafe behaviour is in `AlertCorrelator._assemble_candidates`:
it handles `ticket_service.get_status(ref) is None` exactly as it handles an
explicitly done ticket. For a correlation row this also calls `mark_closed`.
Thus a temporary Jira status-read error can remove the durable,
backend-neutral correlation candidate, allowing the same component alert to be
decided as `new` and create another ticket. A failed Jira search removes the
secondary candidate source at the same time.

The repeated `still firing — N occurrences` messages are deliberate periodic
duplicate roll-ups. `ALERT_CORRELATION_ROLLUP_EVERY` currently defaults to 10,
and `N` counts received alert events rather than distinct failed equipment.

## Design

### Candidate availability is fail-safe

`ticket_correlations` remains the durable source of correlation state for both
Jira and internal tickets. Candidate confirmation will behave as follows:

- an explicit `TicketStatus(is_done=True)` marks a stored candidate done and
  removes it from the decision set;
- an available open status keeps the candidate;
- an unavailable status (`None`) keeps a stored candidate open, logs a warning,
  and lets its cached summary, signatures, and affected keys participate in
  deterministic duplicate matching and LLM correlation.

This does not claim that a ticket is open during an outage; it preserves the
last known open state rather than turning a read failure into a closure. A
later successful status read remains authoritative.

### Jira discovery is repaired and observable

The Jira open-ticket lookup will use Jira's supported issue-search endpoint and
its response shape. Its non-success log will use the repository's Loguru
formatting convention so the HTTP status and a bounded response body are
visible. This lookup continues to find manually filed Jira tickets; it is not
required for tickets already represented in `ticket_correlations`.

### Telegram sends communicate changes, not heartbeat volume

Set `ALERT_CORRELATION_ROLLUP_EVERY` to `0` by default and explicitly in the
production App Platform environment. A duplicate decision always suppresses
Telegram delivery. The existing resilience promise remains: when ticket
creation or correlation itself fails, the base incoming alert still has a
delivery path.

New tickets retain their subject/grid/ticket-link notification. An amendment
continues to notify only when it adds a component and will describe that
component plus the current number of distinct affected components, for example
`Added MPPT Q7II (2 affected components)`. It will never render the total
number of received occurrences as the update text. Escalations keep their
top-level urgent message and live-output enrichment.

## Scope and boundaries

This deliberately does not introduce a new rate limiter, database schema,
digest job, or another LLM decision. The present `new`/`amend`/`duplicate`
model already distinguishes significant changes. The work corrects its
candidate availability and removes the one notification mode that bypasses
that meaning.

## Verification

Focused tests will prove that:

1. a stored Jira candidate remains eligible when its status read fails, and
   an exact same-component alert is a suppressed duplicate rather than a new
   ticket;
2. an explicit done status still closes the cached candidate;
3. Jira search uses the supported endpoint and reports a useful non-success
   failure;
4. duplicate alerts remain silent at every occurrence count;
5. an amendment containing a new equipment key sends one concise update with
   the distinct affected-component count.

Existing alert-correlation and `/chat/notify` suites will run alongside the
focused tests to preserve the ticket-creation and fail-open guarantees.
