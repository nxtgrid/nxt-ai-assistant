# Jira Alert Settings Simplification Design

## Goal

Make alert ticketing work from the Jira project configured for a deployment,
without a second, mostly blank, n8n-specific Jira profile. The settings page
should expose only genuine deployment choices; alert correlation rules should
be versioned with the application.

## Evidence

The deployed application configures the generic Jira project key and issue
type, but none of the `JIRA_ALERT_*` profile fields. Consequently `/notify`
uses the old generic Jira path. The type-selection code is reachable only when
the entire profile is configured, so it is disconnected from the path that
creates the production tickets.

The profile duplicates the generic project configuration and requires Jira
field IDs, option IDs, a reporter account, a label, and priority IDs. Those
values encode one historic n8n workflow rather than portable application
behaviour. They are unnecessary for a project whose creatable metadata already
describes its available types and fields.

## Configuration contract

### Retained deployment choices

- `JIRA_PROJECT_KEY` identifies the Jira project used by Jira-backed tickets.
  It is the only alert-specific Jira value that must differ across deployments.
- `NOTIFY_TICKETS_BACKEND` continues to choose Jira, internal, or automatic
  notify ticketing. Jira project configuration must never prevent internal
  `TKT-*` creation.
- `ALERT_CORRELATION_ENABLED` remains the operational kill switch: disabling
  it turns `ticket_id="auto"` into a plain alert-ticket create without dropping
  the alert.

An urgent alert is the sole priority rule. When Jira is the owning backend,
the service resolves Jira's current `Highest` priority at runtime and applies
it to a new urgent ticket or promotes an existing ticket when an urgent
amendment arrives. This is not a configurable project-specific priority ID;
if Jira cannot expose a highest priority, filing and updating the ticket still
proceed without a priority change.

The existing generic `JIRA_ISSUE_TYPE` remains backward-compatible for
non-alert Jira callers. For alert filing it is a safe fallback only; it is not
another alert setting and is not required to be `Task` for a project that does
not provide Task.

### Removed configuration

Remove the complete `JIRA_ALERT_*` profile: project ID/key, legacy issue-type
ID, selection/cache switches, fallback type, reporter, labels, custom-field
IDs, option IDs, and priority IDs. Remove the profile module and its separate
ticket-creation path rather than retaining empty values or translating them
into hidden constants.

Remove the following alert-correlation overrides:

- `ALERT_CORRELATION_MODEL`: alert work uses the application's configured
  primary generation model.
- `ALERT_CORRELATION_DOC_ID` and `ALERT_CORRELATION_RAG_IDENTITY`: the
  correlation policy is the application-versioned rules file approved for all
  deployments.
- confidence, lookback, prompt-size, timeout, duplicate-roll-up, and
  component-count escalation knobs. These are implementation safety bounds or
  product rules, not deployment configuration. In particular duplicate alerts
  remain silent and notification significance is decided from the material
  alert change, never an occurrence counter.

The generated environment example, App Platform example, registry, settings
page, README, tests, and deployment documentation will no longer refer to
removed keys.

## Unified Jira creation

Both alert and ordinary Jira tickets will use one metadata-aware creation
builder.

1. Fetch the configured project's creatable issue types and their field
   contracts from Jira, using a bounded in-process cache.
2. Derive a candidate payload from the request: project key, summary,
   description, labels, and optional assignee/organisation. For a grid, inspect
   the selected type's metadata and add a matching selectable field only when
   Jira exposes one that can be safely populated. For an urgent alert, resolve
   the Jira `Highest` priority dynamically and include it only when available.
   No custom field ID or option ID is embedded in application configuration.
3. Exclude issue types whose required fields cannot be populated by that
   payload. The LLM is given only this eligible catalogue and must return one
   advertised type ID.
4. On unavailable metadata, invalid model output, or an incompatible chosen
   type, use the generic configured issue type when it is compatible; otherwise
   use Jira's first compatible type. If no compatible Jira type can be proved,
   preserve the existing fail-open notify behaviour by falling back to the
   internal backend.

This supports projects with keys other than `OPS`, including projects whose
valid alert types are Electricity Service Disruption or Comms Failure. It also
keeps `TKT-*` tickets backend-neutral when a deployment changes its notify
backend setting.

## Error handling and observability

Metadata and LLM selection failures are warnings with the project key and
reason, never a reason to discard an incoming alert. The selected issue type,
fallback source, and any omitted optional field are logged without alert text
or credentials. Existing ticket-link and Telegram resilience remain unchanged.
Failure to discover the highest Jira priority is handled as an omitted optional
field, never as a ticketing failure.

## Verification

Focused tests will verify that:

1. alert selection uses the configured generic Jira project and its creatable
   type catalogue, without any `JIRA_ALERT_*` configuration;
2. the LLM cannot choose an ineligible or unadvertised type;
3. `Task` is used only as a compatible fallback and a project without Task
   still files through another compatible type;
4. required unknown fields prevent that type from being selected rather than
   causing a malformed Jira request;
5. ordinary Jira creation and `/notify` share the metadata-aware field
   builder, including safe optional grid handling;
6. a new urgent Jira alert and an urgent amendment apply the dynamically
   discovered highest priority, while non-urgent updates leave priority alone;
7. an unavailable Jira priority catalogue does not block ticket creation or an
   amendment;
8. internal notify tickets still work with a configured Jira project; and
9. removed keys no longer appear in the settings UI or generated examples.

## Non-goals

This change does not infer arbitrary business-specific custom-field values
(such as a category taxonomy) from a field name. A project that requires such
fields without defaults is intentionally treated as incompatible for automated
alert creation; the notify resilience path creates an internal ticket instead
of guessing a value.
