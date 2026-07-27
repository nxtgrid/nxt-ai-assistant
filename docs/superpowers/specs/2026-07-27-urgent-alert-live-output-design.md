# Urgent alert live-output enrichment

## Goal

When Anansi sends an urgent external alert to Telegram, show the grid's live
inverter output in kW. The same live observation must be available to the LLM
before it performs alert correlation or Jira issue-type selection, without
making alert delivery depend on telemetry, the LLM, Jira, or the internal
ticket service.

The feature applies to every actual Telegram post for an urgent alert or an
urgent ticket: new-ticket cards, explicit ticket updates, correlated
amendments, duplicate roll-ups, and unticketed urgent pass-through alerts.
Silent duplicate suppressions remain silent and do not fetch telemetry.

## Definitions

- **Incoming urgency**: the explicit `alert.severity` when supplied, otherwise
  `derive_severity()` applied to the incoming alert subject (or its existing
  first-line fallback).
- **Ticket urgency**: `derive_severity()` applied to the live summary returned
  by the ticket's owning backend, whether Jira or internal.
- **Effective urgency**: `urgent` when either incoming urgency or ticket
  urgency is `urgent`. Urgent is therefore the worst severity and cannot be
  downgraded by a later warning alert.
- **Live output**: the current VRM inverter total output for the resolved grid,
  expressed in kW. A numeric zero is valid. Missing site mapping, stale source
  data, a timeout, or any lookup error produce the explicit unavailable state.

## Architecture

### Lightweight telemetry boundary

Add a narrow live-output operation next to the customer grid-status code. It
will resolve the already canonical grid name to its VRM site, call only the
current inverter-voltage/output endpoint, reject stale observations using the
existing 30-minute status rule, and return a small typed result:

- `output_kw: float` for a current reading, including `0.0`; or
- `output_kw: None` with an unavailable outcome.

It will not call the full `get_grid_status()` operation, TimescaleDB,
weather, downtime, battery, or PV APIs. The notification API invokes this
operation with a short, configurable timeout. All failures are caught at this
boundary and converted to unavailable, with structured logs for diagnostics.

### Alert context and LLM ordering

After `/chat/notify` resolves the canonical `GridNotificationTarget`, it builds
one immutable alert context. The context carries the canonical incoming
subject, incoming severity, and a lazily evaluated, request-local live-output
lookup. The first LLM decision or actual urgent Telegram delivery that needs
the observation resolves and caches it; all later consumers reuse that result.
An exact duplicate that remains silent never resolves the lookup.

For `ticket_id="auto"`, live output is obtained immediately before
`AlertCorrelator` assembles an LLM prompt. The deterministic duplicate
pre-check runs first, so it can suppress a silent re-fire without telemetry
I/O. The observation is added to the LLM's operational facts as live telemetry.
Jira's issue-type-selection LLM receives the same cached context as separate
decision input. This allows models to make better correlation,
amendment, and issue-type choices without copying an unverified telemetry line
into the persisted ticket body.

The stable ticket fallback remains deterministic: the incoming subject becomes
the ticket summary and the raw incoming alert text becomes the description.
The system does not require LLM-generated ticket prose to create an alert
ticket.

### Telegram rendering

Every actual outgoing Telegram post first determines effective urgency. For a
ticketed notification it reads the current backend-neutral ticket status and
summary through `TicketService`; the incoming alert and that summary are then
combined using the worst-severity rule. Auto-correlated tickets additionally
use their stored correlation severity if the live ticket read fails.

Urgent messages receive one extra line:

```
⚡ Live output: 4.2 kW
```

When no trustworthy reading is available, that line is instead:

```
⚡ Live output: unavailable
```

The line is added before existing Telegram Markdown conversion, so ticket links
and escaping continue to work. It is not added to non-urgent messages or to the
persisted ticket description.

## Fail-open delivery contract

The incoming alert is the source-of-truth fallback. Optional failures must
never prevent its Telegram delivery.

1. Telemetry failure becomes `Live output: unavailable`; all remaining work
   continues.
2. Correlation/LLM failure becomes the existing deterministic `new` decision;
   ticket summary is the incoming subject and description is the raw alert.
3. Jira ticket creation failure triggers one immediate retry of the same
   deterministic request with the internal backend. A successful fallback
   renders and returns the `TKT-*` reference normally.
4. If both the configured backend and internal fallback fail, the endpoint
   still schedules the base Telegram alert without a ticket link. It returns
   `202` with a machine-readable ticket error so the caller can observe the
   failed persistence while avoiding an unsafe retry that might duplicate a
   later-recovered ticket.
5. Telegram transport keeps its existing Markdown fallback and chunking
   behavior. A transport failure is logged but never rewrites or rolls back a
   ticket.

Ticket fallback is attempted only after an actual Jira creation failure; it
does not change the deployment-wide backend choice for successful requests.

## Configuration and observability

Add a documented `URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS` setting with a
short default. Include it in the application environment examples and Digital
Ocean manifest, alongside tests for invalid or non-positive values if the
project's flag/config conventions require parsing.

Log the canonical grid, urgent decision source(s), whether the telemetry
result is present/unavailable, and the chosen ticket backend/fallback outcome.
Do not log Telegram credentials, raw VRM payloads, or alert secrets.

## Verification

Unit and API tests will prove:

- live numeric output, including `0.0`, renders once and with kW units;
- stale, missing, timed-out, and failed reads render `unavailable`;
- `ticket_id="auto"` provides the same pre-fetched telemetry to the
  correlation and Jira issue-type LLM inputs, with no duplicate lookup;
- telemetry remains out of the deterministic ticket description;
- a warning update to an existing urgent Jira or internal ticket is still
  rendered as urgent, while non-urgent combinations are unchanged;
- all supported urgent render shapes receive the line and silent duplicates do
  not issue a lookup;
- Jira failure creates an internal fallback ticket; failure of both ticket
  backends still sends the subject-based Telegram alert and returns `202` with
  a ticket error;
- pre-existing ticket, Markdown, link-escaping, and oversized-message behavior
  remain covered by the existing notification tests.
