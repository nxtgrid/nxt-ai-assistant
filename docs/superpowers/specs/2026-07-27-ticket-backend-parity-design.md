# Ticket Backend Parity and Jira Type Selection Design

## Goal

Make notify-created tickets behave consistently whether their deployment uses
Jira or the internal `TKT-*` backend, and let an LLM choose a valid Jira issue
type from the configured alert project's currently creatable types.

## Scope

- One Jira alert project is configured per deployment; no code assumes its key
  is `OPS`.
- Jira issue-type selection is metadata-driven. The LLM may choose from every
  type Jira reports as creatable in the configured alert project.
- A selection that cannot satisfy the chosen type's required fields falls back
  to the configured default type, initially `Task`, and records why.
- `TKT-*` remains a first-class backend. Operators may change the deployment's
  backend setting in production; pre-existing Jira and internal tickets must
  continue to support common staff actions.
- Internal tickets retain a normalized local ticket type/classification so
  their information model can mirror the alert classification used for Jira.

## Non-goals

- Per-notification or per-tenant Jira-project selection. Backend and alert
  project configuration are deployment-wide.
- Pretending that Jira-only workflow capabilities, such as arbitrary Jira
  transitions or Jira account assignment, are supported by internal tickets.
- Changing the existing customer-escalation backend policy.

## Architecture

### Project-aware Jira alert metadata

Introduce a project-aware Jira metadata provider. It reads the configured
alert project, fetches create metadata with a bounded TTL cache, and produces
a normalized catalogue of creatable issue types and create fields. Every Jira
operation for notify alerts--type discovery, creation, open-ticket lookup,
and correlation--uses this same project identity.

The LLM receives a compact, current representation of the catalogue and the
normalized alert facts. It returns an issue-type ID and a structured field
selection. The server validates the type ID against the fetched catalogue and
validates selected values against field metadata. It never accepts a type name
or custom-field ID invented by the model.

If the selected type has a required field that cannot be derived and validated,
the server creates the configured fallback type (`Task`) using its valid
default/profile field payload. The decision and fallback reason are persisted
with the alert correlation audit record and included in structured logs.

### Backend-neutral ticket operations

`TicketService` becomes the sole routing point for common ticket operations:
get details/status, add a comment, close/resolve, update, and open-ticket
lookup. It determines the backend by the persisted reference, so a `TKT-*`
ticket remains internal after a later switch to Jira and a Jira key remains
Jira after a later switch to internal.

The MCP/command-facing ticket tools call this façade instead of invoking the
Jira MCP client directly. Jira-only actions remain Jira tools and return a
clear unsupported-backend response for internal references.

### Internal ticket parity

Add a nullable normalized `ticket_type` field to `internal_tickets`, populate
it when notify ticket classification runs, and return it through the shared
ticket detail model. The field is local metadata; it does not claim that an
internal ticket has a Jira workflow or Jira custom fields.

## Data Flow

1. `/chat/notify` resolves the deployment-wide backend setting.
2. It derives alert facts and correlation decision as it does today.
3. For a new ticket, it classifies the alert once into a normalized type.
4. The internal backend stores that type locally; the Jira backend validates
   the LLM's selection against current metadata and creates the selected or
   fallback Jira type.
5. Staff ticket commands receive a reference and call `TicketService`, which
   routes the operation to the stored backend.

## Error Handling

- Metadata fetch failure does not drop an alert: Jira creation falls back to
  the configured default Jira type where the existing profile can form a valid
  payload; otherwise existing backend-resolution failure behavior applies.
- Invalid LLM JSON, an unavailable type, unsupported field shape, or missing
  required values all select the default type and write an auditable reason.
- A Jira tool invoked for an internal ticket returns an explicit capability
  error rather than an incorrect "issue does not exist" response.
- Backend switches never reclassify or migrate existing tickets.

## Verification

- Unit-test metadata normalization, type validation, fallback selection, and
  configured-project propagation.
- Unit-test both ticket backends through the shared service for comment,
  status, close, and update operations before and after a backend switch.
- Add MCP/command tests that close an internal reference successfully and
  report Jira-only capabilities as unsupported for it.
- Regression-test an alert project key different from `JIRA_PROJECT_KEY` to
  ensure Jira create and correlation query the same project.
