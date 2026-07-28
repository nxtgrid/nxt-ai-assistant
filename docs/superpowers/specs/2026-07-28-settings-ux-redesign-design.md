# Settings Page UX Redesign — Design

**Status:** approved
**Date:** 2026-07-28
**Branch:** `codex/settings-ux-redesign`

## Problem

The Bot Settings page renders 87 controls with no search, no tiering, and
sections inferred from environment-variable name prefixes. Operators cannot find
a setting they know exists, cannot tell a default from a deliberate choice, and
cannot complete some integrations at all. Separately, ~40 operator-tunable
environment variables are read by the code but absent from the registry, so they
are invisible everywhere, and the README documents variables that do not exist.

### Evidence

Grouping is guessed. `_section_of` (`anansi_app/nicegui_app/pages/settings.py:65`)
assigns each flag to a section by matching its name prefix. Results:

- 24 of 87 visible flags fall through to the `🤖 Bot Behavior & Core` catch-all,
  which therefore mixes Nigerian import-tariff sheet IDs, LPP/QGIS Drive template
  IDs, ticket-backend routing, Telegram button toggles and `LOG_LEVEL`.
- `JIRA_PROJECT_KEY` lands in the catch-all; `JIRA_ENABLED` lands in MCP Servers.
- `settings.py:81` is unreachable: it claims `KNOWLEDGE_ENABLED` and
  `REFERENCE_ENABLED` for the RAG section, but line 69 already claimed them.
- `_MCP_SERVER_KEYS` (`settings.py:45`) re-hardcodes `MCP_SERVER_NAMES`, the exact
  duplication the registry was introduced to eliminate. `RESTART_REQUIRED_KEYS`
  and `_MODEL_LABELS` are likewise page-local knowledge about specific flags.

Enums render as free-text inputs. `TICKET_BACKEND_OVERRIDE` (`auto|jira|internal`)
and `NOTIFY_TICKETS_BACKEND` (`internal|auto`) accept any string; a typo silently
reroutes every escalation. `LOG_LEVEL` and `LLM_PROVIDER` get dropdowns only
because `_render_flag` special-cases them by name.

Secrets are silently skipped (`settings.py:307`), so `GRAFANA_PASSWORD` never
renders. The Grafana section looks configurable but cannot be completed from the
UI.

Deployment readiness is inert. `validate_required()` has zero callers and zero
flags set `required=True`.

The page can display values the bot is not using. Registry defaults have drifted
from the orchestrator's:

| Flag | Registry default | `orchestrator/config/settings.py` |
|---|---|---|
| `GEMINI_FALLBACK_MODEL` | `gemini-2.5-flash` | `gemini-2.5-flash-lite` |
| `GEMINI_DEEP_THINKING_MODEL` | `""` | `gemini-pro-latest` |
| `GEMINI_TEMPERATURE` | `0.2`, rendered read-only | empty/`auto` means "model default" |

Also: no search across 87 controls; no basic/advanced tiering (20 Layout Engine
floats carry the same visual weight as `BOT_ENABLED`); conditional visibility
exists only for OpenRouter (`_model_section_plan`); no distinction between an
inherited default and an explicitly set value, because `settings_defaults()`
coerces missing values to defaults; and `get_openrouter_models()` performs a
network request on every page render even when the provider is Gemini.

## Goals

1. Make any setting findable in seconds and make each group's purpose obvious.
2. Make grouping, labelling, validation and visibility declarative properties of
   a flag, so they cannot drift from the registry again.
3. Surface the operator-tunable environment variables that are currently invisible.
4. Let a new deployment reach a working state from the UI, and state plainly what
   must be set outside it first.

## Non-goals

- Changing the save model. Explicit save-all stays; the DigitalOcean backend
  redeploys the app on write, so per-keystroke autosave would thrash it.
- Re-introducing anything PR #33 removed. `ALERT_CORRELATION_*` policy and the
  `JIRA_ALERT_*` profile are now versioned in application code
  (`CorrelationPolicy` in `correlation_rules.py`) and are deliberately not
  operator-overridable. A regression test asserts they stay out.
- Editing credentials that belong to the host platform (`DIGITALOCEAN_API_TOKEN`,
  `AUTH_DB_*`). Those get read-only configured/not-configured status.
- Any change to the tickets view, which is under parallel development.

## Design

### 1. Declarative UI metadata on `Flag`

`shared/config/flag_registry.py` remains the single source of truth. The `Flag`
dataclass gains fields that today live as hardcoded tables inside the page:

| Field | Purpose | Replaces |
|---|---|---|
| `group: str` | Explicit section id | `_section_of` prefix matching |
| `label: str` | Human label; falls back to `name` | `_MODEL_LABELS` |
| `choices: tuple[str, ...] \| None` | Renders a select; validated on save | name special-cases in `_render_flag` |
| `advanced: bool` | Collapsed under "Advanced" within its group | — |
| `restart_required: bool` | Drives the Save & Restart affordance | `RESTART_REQUIRED_KEYS` |
| `depends_on: str \| None` | Group/field is inert when the named flag is falsy | `_model_section_plan` (OpenRouter only) |
| `minimum` / `maximum` | Numeric bounds (e.g. hours 0–23) | — |
| `set_via: str \| None` | For read-only flags: where the operator sets it | — |
| `required: bool` | Already exists; finally consumed | — |

Group ids are validated against an ordered `GROUPS` tuple in the registry, so an
unknown or missing group is a test failure rather than a silent fall-through to a
catch-all. There is no catch-all group.

### 2. Information architecture

Fourteen task-oriented groups replace the ten prefix-derived sections, ordered by
how often an operator touches them:

1. **Bot Control** — master switch, log level, tool-round budget
2. **AI Models & Providers** — provider, role models, generation knobs
3. **Conversation Experience** — threading, context filtering, summaries, Telegram UI
4. **Escalations & Ticketing** — ticket backend routing, Jira project, correlation kill switch
5. **Alerts & Notifications** — `/notify` endpoint, escalation and debug chat targets
6. **Tools & Integrations** — MCP server enable × actions-enabled matrix, disabled tools
7. **Knowledge & RAG**
8. **Grafana Dashboards**
9. **Site Layout Engine** — advanced by default
10. **Documents & Templates** — all Google Doc / Drive / Sheet IDs
11. **Access Control** — the four email whitelists
12. **Connections & Credentials** — secret status and masked entry
13. **Metrics & Scheduling**
14. **Deployment** — read-only platform values

### 3. Page patterns

**Deployment Readiness panel** at the top of the page. Consumes
`validate_required()` and reports per *capability* rather than per variable:
"Bot can reply", "Escalations reach Jira", "Grafana tools available". Each
capability lists what is missing and whether it is settable here or must be set
in the host environment. Green capabilities collapse to a single line.

**Sticky toolbar** with a search box filtering by name, label and description
across all groups (matching groups auto-expand), plus "Show advanced" and
"Only changed / Only unset" filters.

**Conditional groups.** A group whose master toggle is off collapses to a
"disabled" chip rather than showing dead knobs — Grafana's 12 fields when
`GRAFANA_ENABLED` is false, Layout's 30 when `GRID_DESIGN_ENABLED` is false.

**Provenance chip** on each field: `default`, `set`, or `changed here`. Requires
`get_current_settings` to return which names were explicitly present in the
environment or backend, rather than collapsing missing to default.

**Read-only flags** render as static text plus a `set_via` hint ("set in the
DigitalOcean console"), not a greyed-out input that reads as broken.

**Secrets** render masked and write-only: the field shows `••••••• (set)` or
`not set`, never echoes a stored value back to the browser, and writes to the
DigitalOcean backend with `type: SECRET` (already supported by `_merge_env_vars`).
An empty submission is a no-op, not a deletion.

**Enums** become selects; bounded integers get `min`/`max`; email lists become
chip inputs; JSON keeps the existing validated textarea.

### 4. Registry coverage

Roughly 180 environment variables are read across the codebase; 94 are
registered. Most of the gap is correctly excluded — secrets, connection strings,
dataset paths, per-process `HOST`/`PORT`. These are operator-tunable, non-secret,
and get registered:

- `{SERVER}_ACTIONS_ENABLED` for each of the 14 MCP servers. This is the
  read-only versus read-write safety gate; `mcp_servers/shared_code/config/action_flags.py`
  has always supported it, the registry has never known about it, and it is
  therefore invisible in every interface.
- Nine further `LAYOUT_*` variables read directly from `shared/layout/*`
  (`LAYOUT_POWER_FACTOR`, `LAYOUT_WATERWAY_BUFFER_M`, `LAYOUT_PLANT_CONNECT_K`,
  and others). The Layout section currently looks complete while showing about
  two thirds of the knobs.
- Model knobs: `GEMINI_THINKING_BUDGET`, `GEMINI_AGENT_PRO_MODEL`,
  `THREAD_CLASSIFIER_MODEL`, `GOOGLE_SEARCH_GROUNDING`.
- Ops behaviour: `JIRA_SWEEP_ENABLED`, `JIRA_ISSUE_TYPE`, `LOOP_DETECTION_ENABLED`,
  `LOOP_DETECTION_THRESHOLD`, `STARTUP_RECOVERY_ENABLED`, `NO_REPLY_CHAT_IDS`,
  `AFTER_HOURS_TIMEZONE`, `AFTER_HOURS_START_HOUR`, `METRICS_TIMEZONE`.
- Agent budgets: `AGENT_MAX_ACTIONS_PER_WAKE`, `AGENT_MAX_TOOL_ROUNDS`,
  `MULTI_SITE_MAX_CONCURRENCY`.
- Grafana runtime: `GRAFANA_ACTIONS_ENABLED`, `GRAFANA_QUERY_TIMEOUT`,
  `GRAFANA_METADATA_TIMEOUT`, `GRAFANA_VARIABLE_TIMEOUT`.
- Deployment identity: `ORGANIZATION_NAME`, `DOC_CODE_PREFIX`,
  `MANAGED_GENERATION_COLUMN`, `STAFF_ORG_NAME`.

Every newly registered flag keeps its existing consumer-side default verbatim, so
registration changes nothing at runtime. This is asserted by test.

Secrets split into two classes, and the distinction is what `editable` means:

- **App-owned secrets** (`GRAFANA_PASSWORD`, `JIRA_API_TOKEN`, `OPENROUTER_API_KEY`,
  `TAVILY_API_KEY`) — `secret=True, editable=True`. Masked, write-only entry as
  described above. These complete an integration the operator turns on here.
- **Host-owned secrets and connection strings** (`DIGITALOCEAN_API_TOKEN`,
  `AUTH_DB_*`, `CHAT_DB_SERVICE_KEY`, `SESSION_ID_SECRET`,
  `GOOGLE_SERVICE_ACCOUNT_JSON`) — `secret=True, editable=False` with a `set_via`
  hint. Status only: configured or not configured, never an input. Writing these
  from the app that reads them is a bootstrap hazard, and the readiness panel
  needs to know they exist in order to report on Tier 2.

The four `show_in_settings=False` deployment flags (`DEFAULT_TIMEZONE`,
`STAFF_ORG_ID`, `SETTINGS_BACKEND`, `SETTINGS_FILE`) become visible in the
Deployment group as read-only values with `set_via` hints. They are currently
hidden entirely, which means an operator debugging why settings are not
persisting cannot see which backend is active — the single most useful fact on
the page for that failure.

### 5. Minimum environment for a new deployment

Stated as tiers, each verified by actually booting the app rather than inferred
from reading code:

- **Tier 0 — settings page loads (local).** `GRID_DESIGN_DEV_NO_AUTH=1` and
  nothing else. Every service call on the render path degrades gracefully: the
  env-file settings backend needs no credentials, the Grafana catalogue load is
  wrapped in `try/except`, the bot-status probe returns `down`, and the grid nav
  reads `db/entities.json` from disk.
- **Tier 0′ — settings page loads with real Google auth.** `GOOGLE_CLIENT_ID`,
  `GOOGLE_CLIENT_SECRET`, `AUTH_REDIRECT_URI` (must match the OAuth client
  registration), `ALLOWED_VIEWER_EMAILS` containing your address.
  `AUTH_COOKIE_SECRET` is optional — derived from the client id when unset.
- **Tier 1 — changes persist.** Nothing further: the env-file backend writes
  `.env.settings`. To drive a live DigitalOcean app spec instead, add
  `DIGITALOCEAN_APP_ID` and `DIGITALOCEAN_API_TOKEN`.
- **Tier 2 — the bot answers.** `GOOGLE_API_KEY`, `TELEGRAM_BOT_TOKEN`,
  `CHAT_DB_URL` + `CHAT_DB_SERVICE_KEY`, `API_KEY`, `SESSION_ID_SECRET`, an auth
  database (`AUTH_DB_*` or `AUTH_SUPABASE_*`), `GOOGLE_SERVICE_ACCOUNT_JSON` with
  `CUSTOMER_SUPPORT_DOC_ID` and `STAFF_SUPPORT_DOC_ID`, and
  `CHAT_ORCHESTRATOR_URL`.
- **Tier 3 — per integration.** Grafana, Jira, VRM, Calin, metering, and the rest,
  each configurable through the UI once Tier 0 is up.

This lands in two places: the in-app Deployment Readiness panel, and a rewritten
README "Environment Variables" section. The current README section documents
`JIRA_DOMAIN`, `JIRA_EMAIL`, `SUPABASE_ACTIONS_ENABLED` and
`TIMESCALE_ACTIONS_ENABLED` — none of which are read anywhere in the codebase.

### 6. Correctness fixes bundled in

- Reconcile the three registry/orchestrator default drifts, taking the
  orchestrator's value as authoritative since it is what actually runs.
- Delete `_section_of`, `_MCP_SERVER_KEYS`, `RESTART_REQUIRED_KEYS` and
  `_MODEL_LABELS` from the page; all four become registry data.
- Call `get_openrouter_models()` only when the selected provider is OpenRouter.
- Make `GEMINI_TEMPERATURE` and `GEMINI_MAX_OUTPUT_TOKENS` editable; they are
  ordinary operator knobs marked read-only for no discoverable reason.

## Component boundaries

- `shared/config/flag_registry.py` — declares flags and their UI metadata.
  Knows nothing about NiceGUI. Gains `groups()`, `flags_in_group()`, and
  `readiness()` returning capability status.
- `anansi_app/services/settings_service.py` — persistence plus value provenance.
  Gains `get_current_settings_with_provenance()`. Knows nothing about widgets.
- `anansi_app/nicegui_app/pages/settings.py` — renders whatever the registry
  declares. Contains no per-flag knowledge after this change; a new flag needs no
  page edit.
- `anansi_app/nicegui_app/pages/settings_readiness.py` (new) — the readiness
  panel, so the main page file does not grow further.

## Testing

- Registry: every flag has a group drawn from `GROUPS`; `choices` contains the
  flag's own default; `depends_on` names an existing boolean flag; numeric
  defaults fall inside `minimum`/`maximum`; newly registered flags match their
  consumer-side defaults; `JIRA_ALERT_*` and `ALERT_CORRELATION_*` policy flags
  stay absent; `flags.env.example` regenerated and byte-identical.
- Settings page: sections are built from registry groups; search filters and
  auto-expands; a group with a falsy `depends_on` collapses; a secret's stored
  value never reaches the rendered widget; an invalid enum value is rejected on
  save; readiness computation returns the expected missing set for a given env.
- Boot verification: the app starts and `/settings` renders under Tier 0 and
  Tier 0′ environments, executed and recorded rather than assumed.

New test files must be added with `git add -f` — the repo's `.gitignore` denies
`tests/` by default, and a plain `git add` is a silent no-op (see `CLAUDE.md`).
`pre-commit run --all-files` gates completion.

## Rollout

Single branch `codex/settings-ux-redesign` in `.worktrees/settings-ux-redesign`.
Touches `shared/config/flag_registry.py`, `shared/config/flags.env.example`,
`anansi_app/nicegui_app/pages/settings.py`, `anansi_app/services/settings_service.py`,
`README.md`, `.do/app.example.yaml`, and tests. No overlap with the tickets view.

The registry change is additive and the page is rewritten behind the same route,
so there is no migration step and no data to back-fill. Rollback is a branch
revert.
