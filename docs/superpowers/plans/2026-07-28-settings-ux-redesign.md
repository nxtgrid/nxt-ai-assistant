# Settings Page UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Anansi setting findable, correctly grouped, validated, and completable from the admin UI, and make the minimum environment for a new deployment explicit both in-app and in the README.

**Architecture:** All UI knowledge (grouping, labels, enum choices, advanced tiering, restart scope, conditional visibility, numeric bounds) moves from hardcoded tables inside `pages/settings.py` onto the `Flag` dataclass in `shared/config/flag_registry.py`, which is already the single source of truth for flag existence and type. The settings page becomes a generic renderer over registry groups with search, advanced tiering, and conditional collapse. A new capability-based readiness model consumes the long-dormant `required` field to answer "what is still missing for this deployment".

**Tech Stack:** Python 3.11, NiceGUI 2.x (Quasar widgets), dataclasses, pytest, DigitalOcean App Platform API.

---

## Global Constraints

- `shared/config/flag_registry.py` must not import NiceGUI or anything from `anansi_app`. It stays host-agnostic.
- After this work, `anansi_app/nicegui_app/pages/settings.py` must contain **no per-flag knowledge**. Adding a flag to the registry must require zero edits to the page. Grep for any remaining flag-name literal in the page as a completion check, with the sole exception of the Grafana dashboard/panel picker, which is a genuine bespoke widget.
- Every newly registered flag keeps its existing consumer-side default **verbatim**. Registration must change no runtime behaviour. Task 4 is the only task allowed to change a default, and it does so to make the registry match the code that actually runs.
- Do not re-introduce anything PR #33 removed. `ALERT_CORRELATION_*` policy values and `JIRA_ALERT_*` profile fields are versioned in `correlation_rules.py` as `CorrelationPolicy` and stay out of the registry. `ALERT_CORRELATION_ENABLED` (the kill switch) is the only survivor.
- Secrets never round-trip to the browser. A secret widget renders a placeholder derived from "is it set", never the value.
- Host-owned credentials (`AUTH_DB_*`, `DIGITALOCEAN_API_TOKEN`, `SESSION_ID_SECRET`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `CHAT_DB_*`) are registered `editable=False` so `_filter_writable` drops them by construction. They exist in the registry only so readiness can report on them.
- Do not touch `anansi_app/nicegui_app/pages/tickets.py` or `anansi_app/services/supabase_reader.py`. The tickets view is under parallel development on another branch.

## Environment

All commands assume you are at the worktree root:

```
/Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.worktrees/settings-ux-redesign
```

The virtualenv lives in the main checkout and is shared. Define this once per shell:

```bash
export PY=/Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.venv/bin/python
```

Test invocations differ per package, matching `.github/workflows/ci.yml`:

| Suite | Command |
|---|---|
| Flag registry | `cd chat_orchestrator && $PY -m pytest tests/test_flag_registry.py -q` |
| Shared | `cd chat_orchestrator && $PY -m pytest ../shared -q` |
| anansi_app | `PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests -q` (from worktree root) |

---

## File Structure

- Modify: `shared/config/flag_registry.py` — add UI metadata fields to `Flag`, the ordered `GROUPS` table, the `Capability` readiness model, and the ~40 newly registered flags. This file grows substantially; keep the flag list itself grouped by `GROUPS` order with section comments so it stays navigable.
- Modify: `shared/config/flags.env.example` — regenerated output, never hand-edited.
- Modify: `anansi_app/services/settings_service.py` — add provenance-aware reads and secret-presence reporting.
- Rewrite: `anansi_app/nicegui_app/pages/settings.py` — generic registry-driven renderer with toolbar, groups, advanced tiering, conditional collapse.
- Create: `anansi_app/nicegui_app/pages/settings_readiness.py` — the Deployment Readiness panel, kept out of the main page file.
- Create: `anansi_app/nicegui_app/pages/settings_widgets.py` — one widget factory per `FlagType`/render mode (enum select, masked secret, read-only display, bounded number, chip list), so the page file stays a layout concern.
- Modify: `chat_orchestrator/tests/test_flag_registry.py` — extend registry invariants.
- Create: `chat_orchestrator/tests/test_flag_readiness.py` — capability computation.
- Create: `anansi_app/tests/test_settings_page.py` — grouping, search, conditional collapse, secret masking, enum validation.
- Create: `anansi_app/tests/test_settings_readiness_panel.py` — panel rendering logic.
- Modify: `README.md` — rewrite the "Environment Variables" section.
- Modify: `.do/app.example.yaml` — add newly registered flags with their defaults.
- Create: `docs/superpowers/plans/2026-07-28-settings-ux-redesign.md` — this file.

---

## Task 0: Baseline

**Files:** none

- [ ] **Step 1: Install the missing anansi_app dependency**

The shared venv lacks `requests`, which `anansi_app/requirements.txt` declares. Four tests in `anansi_app/tests/test_model_settings.py` fail without it. This is an environment gap, not a code defect.

```bash
export PY=/Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant/.venv/bin/python
$PY -m pip install "requests>=2.33.1"
```

- [ ] **Step 2: Confirm a green baseline**

```bash
cd chat_orchestrator && $PY -m pytest tests/test_flag_registry.py -q && cd ..
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests -q
```

Expected: 25 passed for the registry suite; 43 passed for anansi_app. If `test_model_settings.py` still fails, stop and report — every later task depends on this baseline.

---

## Task 1: UI metadata on `Flag` and the `GROUPS` table

**Files:**
- Modify: `shared/config/flag_registry.py`
- Modify: `chat_orchestrator/tests/test_flag_registry.py`

**Interfaces produced:**
- `Flag.group: str`, `Flag.label: str`, `Flag.choices: tuple[str, ...] | None`, `Flag.advanced: bool`, `Flag.restart_required: bool`, `Flag.depends_on: str | None`, `Flag.minimum: float | None`, `Flag.maximum: float | None`, `Flag.set_via: str | None`
- `Flag.display_label -> str`
- `GROUPS: tuple[Group, ...]` where `Group` is `(id, title, description)`
- `groups() -> tuple[Group, ...]`, `flags_in_group(group_id) -> list[Flag]`

- [ ] **Step 1: Write the failing tests**

Append to `chat_orchestrator/tests/test_flag_registry.py`:

```python
class TestFlagUIMetadata:
    def test_group_ids_are_all_known(self):
        known = {group.id for group in fr.GROUPS}
        for name, flag in fr.FLAGS.items():
            assert flag.group in known, f"{name} has unknown group {flag.group!r}"

    def test_groups_have_unique_ids_and_are_ordered(self):
        ids = [group.id for group in fr.GROUPS]
        assert len(ids) == len(set(ids))
        assert ids[0] == "bot_control", "Bot Control must render first"

    def test_flags_in_group_returns_registration_order(self):
        names = [flag.name for flag in fr.flags_in_group("bot_control")]
        assert names[0] == "BOT_ENABLED"

    def test_display_label_falls_back_to_name(self):
        assert fr.FLAGS["BOT_ENABLED"].display_label == "BOT_ENABLED"

    def test_choices_always_contain_the_default(self):
        for name, flag in fr.FLAGS.items():
            if flag.choices:
                assert flag.default_str in flag.choices, (
                    f"{name} default {flag.default_str!r} is not among {flag.choices}"
                )

    def test_depends_on_targets_an_existing_bool_flag(self):
        for name, flag in fr.FLAGS.items():
            if flag.depends_on is None:
                continue
            target = fr.FLAGS.get(flag.depends_on)
            assert target is not None, f"{name} depends on unknown flag {flag.depends_on}"
            assert target.type is fr.FlagType.BOOL, (
                f"{name} depends on {flag.depends_on}, which is not a boolean"
            )

    def test_numeric_defaults_are_inside_declared_bounds(self):
        for name, flag in fr.FLAGS.items():
            if flag.type not in (fr.FlagType.INT, fr.FlagType.FLOAT):
                continue
            value = flag.coerce(flag.default_str)
            if flag.minimum is not None:
                assert value >= flag.minimum, f"{name} default below minimum"
            if flag.maximum is not None:
                assert value <= flag.maximum, f"{name} default above maximum"

    def test_read_only_flags_explain_where_to_set_them(self):
        for name, flag in fr.FLAGS.items():
            if not flag.editable and flag.show_in_settings:
                assert flag.set_via, f"{name} is read-only but has no set_via hint"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd chat_orchestrator && $PY -m pytest tests/test_flag_registry.py::TestFlagUIMetadata -q
```

Expected: FAIL — `AttributeError: module 'shared.config.flag_registry' has no attribute 'GROUPS'`.

- [ ] **Step 3: Add the `Group` type and `GROUPS` table**

In `shared/config/flag_registry.py`, after the `FlagType` enum:

```python
@dataclass(frozen=True)
class Group:
    """A settings-page section. Order in :data:`GROUPS` is render order."""

    id: str
    title: str
    description: str


# Ordered by how often an operator touches the group. There is deliberately no
# catch-all: an unknown group id fails the registry test rather than silently
# collecting unrelated flags, which is how the previous prefix-matching
# `_section_of` accumulated 24 unrelated flags in one section.
GROUPS: tuple[Group, ...] = (
    Group("bot_control", "Bot Control", "Master switch and core request handling."),
    Group("models", "AI Models & Providers", "Which model answers, and how it generates."),
    Group("conversation", "Conversation Experience", "Threading, context, and chat interaction."),
    Group("ticketing", "Escalations & Ticketing", "Where escalations and alerts are filed."),
    Group("alerts", "Alerts & Notifications", "Inbound /notify and outbound Telegram targets."),
    Group("tools", "Tools & Integrations", "Which MCP servers and tools the bot may use."),
    Group("knowledge", "Knowledge & RAG", "Retrieval-augmented generation."),
    Group("grafana", "Grafana Dashboards", "Dashboard and panel exposure as tools."),
    Group("layout", "Site Layout Engine", "Geometry and sizing for generated site layouts."),
    Group("documents", "Documents & Templates", "Google Doc, Slides, Sheet and Drive ids."),
    Group("access", "Access Control", "Who may use the admin app and staff tools."),
    Group("connections", "Connections & Credentials", "External services this deployment talks to."),
    Group("metrics", "Metrics & Scheduling", "Scheduled collection jobs."),
    Group("deployment", "Deployment", "Platform values set outside this app."),
)
```

- [ ] **Step 4: Extend the `Flag` dataclass**

Add these fields to `Flag` after `document`, and add `display_label`. Every field has a default, so all existing `_b`/`_i`/`_f`/`_s`/`_j` call sites keep working:

```python
    group: str = "bot_control"
    label: str = ""
    choices: Optional[tuple[str, ...]] = None
    advanced: bool = False
    restart_required: bool = False
    depends_on: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    set_via: Optional[str] = None
```

And as a property alongside `default_str`:

```python
    @property
    def display_label(self) -> str:
        """Human label for the settings UI, falling back to the env var name."""
        return self.label or self.name
```

Extend the class docstring's Attributes block to describe each new field, matching the existing style.

- [ ] **Step 5: Add the group accessors**

Next to `settings_defaults`:

```python
def groups() -> tuple[Group, ...]:
    """Ordered settings-page sections."""
    return GROUPS


def flags_in_group(group_id: str) -> List[Flag]:
    """Flags declared for ``group_id``, in registration order."""
    return [flag for flag in FLAGS.values() if flag.group == group_id]
```

- [ ] **Step 6: Run the tests**

```bash
cd chat_orchestrator && $PY -m pytest tests/test_flag_registry.py -q
```

Expected: `test_group_ids_are_all_known` PASSES (every flag defaults to `bot_control`, which is a known id), `test_flags_in_group_returns_registration_order` FAILS because `BOT_ENABLED` is not first in registration order yet. That is expected — Task 2 assigns real groups. Leave it failing and note it; do not weaken the test.

- [ ] **Step 7: Commit**

```bash
git add shared/config/flag_registry.py chat_orchestrator/tests/test_flag_registry.py
git commit -m "feat(settings): add declarative UI metadata to the flag registry"
```

---

## Task 2: Assign every existing flag to a group

**Files:**
- Modify: `shared/config/flag_registry.py`
- Modify: `shared/config/flags.env.example` (regenerated)

No new tests here; Task 1's invariants are the tests. This task makes them all pass.

- [ ] **Step 1: Reorder `_FLAGS` and assign `group=` to every entry**

Reorder the `_FLAGS` list to follow `GROUPS` order, with a section comment per group. Assign exactly these memberships. Every existing flag appears exactly once.

**`bot_control`** — in this order, `BOT_ENABLED` first:
`BOT_ENABLED`, `LOG_LEVEL`, `MAX_TOOL_ROUNDS`, `ALLOW_PARALLEL_CALLS`

**`models`:**
`LLM_PROVIDER`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `GEMINI_DEEP_THINKING_MODEL`, `INTENT_ROUTER_MODEL`, `VERIFICATION_MODEL`, `EMBEDDING_MODEL`, `GEMINI_TEMPERATURE`, `GEMINI_MAX_OUTPUT_TOKENS`, `GEMINI_LITE_MAX_OUTPUT_TOKENS`, `OPENROUTER_MODEL`, `OPENROUTER_PROVIDER_ORDER`, `OPENROUTER_ALLOW_FALLBACKS`, `OPENROUTER_REQUIRE_PARAMETERS`

**`conversation`:**
`WORKFLOW_PARAMETER_CONFIRMATION`, `INLINE_BUTTONS_ENABLED`, `PROCEDURE_BUTTONS_ENABLED`, `MINI_APP_FORMS_ENABLED`, `CONTEXT_FILTER_ENABLED`, `THREAD_DISENTANGLEMENT_ENABLED`, `ACTIVE_THREAD_WINDOW_MINUTES`, `CONVERSATION_SUMMARY_ENABLED`, `AWAITING_INPUT_TIMEOUT_MINUTES`, `PERSISTENT_AGENTS_ENABLED`, `VERIFICATION_ENABLED`, `VERIFICATION_DOC_ID`

**`ticketing`:**
`TICKET_BACKEND_OVERRIDE`, `NOTIFY_TICKETS_BACKEND`, `INTERNAL_TICKET_PREFIX`, `JIRA_PROJECT_KEY`, `JIRA_HEALTHCHECK_TTL_SECONDS`, `ALERT_CORRELATION_ENABLED`, `URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS`

**`alerts`:**
`NOTIFY_ENDPOINT_ENABLED`, `ESCALATION_TELEGRAM_CHAT_ID`, `DEBUG_TELEGRAM_CHAT_ID`

**`tools`:**
the 14 `*_ENABLED` MCP flags from `_mcp_enable_flags()`, then `MCP_DISABLED_TOOLS`

**`knowledge`:**
`rag__enabled`, `rag__top_k`

**`grafana`:**
`GRAFANA_URL`, `GRAFANA_USERNAME`, `GRAFANA_PASSWORD`, `GRAFANA_FOLDER_NAME`, `GRAFANA_ENABLED_DASHBOARDS`, `GRAFANA_ENABLED_PANELS`, `GRAFANA_SYNC_HOUR`, `GRAFANA_FORCE_FULL_REINDEX`, `GRAFANA_PANEL_DESCRIPTION_PROMPT`, `GRAFANA_PANELS_METADATA`, `GRAFANA_AVAILABLE_DASHBOARDS`

**`layout`:**
all 20 existing `LAYOUT_*` flags

**`documents`:**
`EXPERT_INSTRUCTIONS_DOC_ID`, `CUSTOMER_SUPPORT_DOC_ID`, `STAFF_SUPPORT_DOC_ID`, `TROUBLESHOOTING_PROCEDURES_DOC_ID`, `LPP_TEMPLATE_ID`, `QGIS_TEMPLATE_FILE_ID`, `LPP_OUTPUT_FOLDER_ID`, `NIGERIA_IMPORT_TARIFF_SHEET_ID`, `NIGERIA_IMPORT_STANDARDS_PDF_ID`

**`access`:**
`ALLOWED_VIEWER_EMAILS`, `EQUIPMENT_CONTROL_ALLOWED_USERS`, `GRID_DESIGN_ALLOWED_USERS`, `GRID_DESIGN_EDITORS`, `GRID_PROCUREMENT_EDITORS`, `STAFF_ORG_ID`

**`connections`:** (populated in Task 3; empty after this task)

**`metrics`:**
`METRICS_ENABLED`, `METRICS_SCHEDULE_HOUR`

**`deployment`:**
`DEFAULT_TIMEZONE`, `SETTINGS_BACKEND`, `SETTINGS_FILE`

- [ ] **Step 2: Set `label=` on flags whose env var name is not self-explanatory**

These replace the deleted `_MODEL_LABELS` table and cover the worst offenders. Leave every other flag's `label` unset so it falls back to the name.

```
GEMINI_MODEL                  -> "Main model"
GEMINI_FALLBACK_MODEL         -> "Fallback model"
GEMINI_DEEP_THINKING_MODEL    -> "Deep-thinking model"
GEMINI_TEMPERATURE            -> "Temperature"
GEMINI_MAX_OUTPUT_TOKENS      -> "Main model max output tokens"
GEMINI_LITE_MAX_OUTPUT_TOKENS -> "Lite model max output tokens"
INTENT_ROUTER_MODEL           -> "Intent router model"
VERIFICATION_MODEL            -> "Response verification model"
EMBEDDING_MODEL               -> "Embedding model"
LLM_PROVIDER                  -> "Generation provider"
rag__enabled                  -> "Enable RAG"
rag__top_k                    -> "Chunks retrieved per query"
MCP_DISABLED_TOOLS            -> "Individually disabled tools"
TICKET_BACKEND_OVERRIDE       -> "Customer escalation ticket backend"
NOTIFY_TICKETS_BACKEND        -> "Alert (/notify) ticket backend"
ALERT_CORRELATION_ENABLED     -> "Group related alerts onto one ticket"
ALLOWED_VIEWER_EMAILS         -> "Admin app access (email allow-list)"
STAFF_ORG_ID                  -> "Staff organization id"
```

- [ ] **Step 3: Set `choices=` on the enum flags**

These are free-text inputs today, where a typo silently reroutes traffic:

```python
_s("LOG_LEVEL", "INFO", ..., choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
_s("LLM_PROVIDER", "gemini", ..., choices=("gemini", "openrouter"))
_s("TICKET_BACKEND_OVERRIDE", "auto", ..., choices=("auto", "jira", "internal"))
_s("NOTIFY_TICKETS_BACKEND", "internal", ..., choices=("internal", "auto"))
_s("SETTINGS_BACKEND", "auto", ..., choices=("auto", "digitalocean", "envfile"))
```

- [ ] **Step 4: Set `restart_required=True`, replacing the page-local set**

On exactly these five, which are read once at process startup:
`PERSISTENT_AGENTS_ENABLED`, `METRICS_ENABLED`, `METRICS_SCHEDULE_HOUR`, `GRAFANA_SYNC_HOUR`, `MINI_APP_FORMS_ENABLED`

- [ ] **Step 5: Set `depends_on=`**

```
VERIFICATION_DOC_ID                     -> "VERIFICATION_ENABLED"
ACTIVE_THREAD_WINDOW_MINUTES            -> "THREAD_DISENTANGLEMENT_ENABLED"
rag__top_k                              -> "rag__enabled"
URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS -> "ALERT_CORRELATION_ENABLED"
every GRAFANA_* flag                    -> "GRAFANA_ENABLED"
every LAYOUT_* flag                     -> "GRID_DESIGN_ENABLED"
OPENROUTER_PROVIDER_ORDER, OPENROUTER_ALLOW_FALLBACKS, OPENROUTER_REQUIRE_PARAMETERS, OPENROUTER_MODEL
                                        -> None  (handled by the provider select, not a bool)
METRICS_SCHEDULE_HOUR                   -> "METRICS_ENABLED"
```

`LLM_PROVIDER` is a string enum, not a bool, so the OpenRouter fields cannot use `depends_on`. Keep the existing provider-driven filtering (`_model_section_plan`) for those four and note it in a comment.

- [ ] **Step 6: Set numeric bounds**

```
METRICS_SCHEDULE_HOUR   -> minimum=0,  maximum=23
GRAFANA_SYNC_HOUR       -> minimum=0,  maximum=23
GEMINI_TEMPERATURE      -> minimum=0.0, maximum=2.0
MAX_TOOL_ROUNDS         -> minimum=1,  maximum=20
rag__top_k              -> minimum=1,  maximum=50
LAYOUT_TARGET_COVERAGE_PCT -> minimum=0.0, maximum=100.0
```

- [ ] **Step 6b: Mark the Layout Engine advanced**

Set `advanced=True` on all 20 existing `LAYOUT_*` flags. There is deliberately no
group-level advanced flag — flag-level tiering is the single mechanism, and a
group whose every flag is advanced simply renders empty and is skipped, which is
the desired "Site Layout Engine hidden until asked for" behaviour.

Also set `advanced=True` on `MCP_DISABLED_TOOLS` (per-tool surgery, rarely used)
and `GRAFANA_PANEL_DESCRIPTION_PROMPT` (a wall of prompt text).

- [ ] **Step 7: Set `set_via=` on every read-only flag**

```
DEFAULT_TIMEZONE, STAFF_ORG_ID, SETTINGS_BACKEND, SETTINGS_FILE
    -> "Set in the deployment environment (DigitalOcean app spec or .env)."
ESCALATION_TELEGRAM_CHAT_ID, DEBUG_TELEGRAM_CHAT_ID
    -> "Set in the deployment environment; changing it re-points Telegram delivery."
CUSTOMER_SUPPORT_DOC_ID, STAFF_SUPPORT_DOC_ID, TROUBLESHOOTING_PROCEDURES_DOC_ID
    -> "Set in the deployment environment alongside GOOGLE_SERVICE_ACCOUNT_JSON."
EMBEDDING_MODEL
    -> "Changing this invalidates every stored embedding; re-ingest required."
GRAFANA_PANELS_METADATA, GRAFANA_AVAILABLE_DASHBOARDS
    -> "Machine-managed by the Grafana indexer; use Sync Now."
GEMINI_LITE_MAX_OUTPUT_TOKENS
    -> "Set in the deployment environment."
```

- [ ] **Step 8: Make `show_in_settings=True` on the four deployment flags**

`DEFAULT_TIMEZONE`, `STAFF_ORG_ID`, `SETTINGS_BACKEND`, `SETTINGS_FILE` currently carry `show_in_settings=False`. Remove that argument; they stay `editable=False` and now render read-only in the Deployment group. `OPENROUTER_MODEL` keeps `show_in_settings=False` — it is a legacy fallback the orchestrator no longer consults for role models.

- [ ] **Step 9: Regenerate the example file and run the registry tests**

```bash
$PY -m shared.config.flag_registry > shared/config/flags.env.example
cd chat_orchestrator && $PY -m pytest tests/test_flag_registry.py -q
```

Expected: all pass, including `test_flags_in_group_returns_registration_order` which failed at the end of Task 1.

- [ ] **Step 10: Commit**

```bash
git add shared/config/flag_registry.py shared/config/flags.env.example
git commit -m "refactor(settings): assign every flag a declarative group, label and validation"
```

---

## Task 3: Register the missing operator-tunable flags

**Files:**
- Modify: `shared/config/flag_registry.py`
- Modify: `chat_orchestrator/tests/test_flag_registry.py`
- Modify: `shared/config/flags.env.example` (regenerated)

Every default below was read from the consuming code. Registering a flag must not change behaviour, so these must match exactly.

- [ ] **Step 1: Write the failing test that pins new defaults to their consumers**

Append to `chat_orchestrator/tests/test_flag_registry.py`:

```python
class TestNewlyRegisteredFlagsMatchTheirConsumers:
    """Registering a flag must not change runtime behaviour.

    Each expected value here was read from the module that actually consumes the
    variable. If a consumer's default changes, this test fails and the registry
    gets updated with it -- which is the drift these assertions exist to stop.
    """

    EXPECTED = {
        "AGENT_MAX_ACTIONS_PER_WAKE": 10,
        "AGENT_MAX_TOOL_ROUNDS": 5,
        "LOOP_DETECTION_ENABLED": True,
        "LOOP_DETECTION_THRESHOLD": 2,
        "MULTI_SITE_MAX_CONCURRENCY": 5,
        "STARTUP_RECOVERY_ENABLED": True,
        "JIRA_SWEEP_ENABLED": True,
        "JIRA_ISSUE_TYPE": "Task",
        "METRICS_TIMEZONE": "UTC",
        "AFTER_HOURS_START_HOUR": 19,
        "GEMINI_THINKING_BUDGET": 4096,
        "GEMINI_AGENT_PRO_MODEL": "gemini-2.5-pro",
        "THREAD_CLASSIFIER_MODEL": "gemini-2.5-flash-lite",
        "GOOGLE_SEARCH_GROUNDING": True,
        "GRAFANA_ACTIONS_ENABLED": False,
        "GRAFANA_QUERY_TIMEOUT": 180,
        "GRAFANA_METADATA_TIMEOUT": 30,
        "GRAFANA_VARIABLE_TIMEOUT": 60,
        "ORGANIZATION_NAME": "the operator",
        "DOC_CODE_PREFIX": "DOC",
        "STAFF_ORG_NAME": "Staff",
        "MANAGED_GENERATION_COLUMN": "is_generation_managed_by_nxt_grid",
        "LAYOUT_KW_PER_HOUSEHOLD": 0.0,
        "LAYOUT_MAX_BRIDGE_DISTANCE_M": 200.0,
        "LAYOUT_PATH_REDUNDANCY_DISTANCE_M": 22.5,
        "LAYOUT_PATH_WEIGHT_PENALTY": 3.0,
        "LAYOUT_PLANT_CONNECT_DISTANCE_M": 150.0,
        "LAYOUT_PLANT_CONNECT_K": 5,
        "LAYOUT_POWER_FACTOR": 0.95,
        "LAYOUT_ROAD_CLIP_BUFFER_M": 100.0,
        "LAYOUT_WATERWAY_BUFFER_M": 200.0,
    }

    def test_each_new_flag_keeps_its_consumer_default(self):
        for name, expected in self.EXPECTED.items():
            assert name in fr.FLAGS, f"{name} is not registered"
            assert fr.get(name, env={}) == expected, name

    def test_every_mcp_server_has_a_write_gate(self):
        for server in fr.MCP_SERVER_NAMES:
            name = f"{server.upper()}_ACTIONS_ENABLED"
            assert name in fr.FLAGS, f"{name} missing -- write gating is invisible"
            assert fr.FLAGS[name].group == "tools"

    def test_after_hours_timezone_defaults_to_empty_not_utc(self):
        # The consumer falls back to DEFAULT_TIMEZONE at read time; baking "UTC"
        # into the registry would silently override a deployment's own timezone.
        assert fr.get("AFTER_HOURS_TIMEZONE", env={}) == ""
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd chat_orchestrator && $PY -m pytest tests/test_flag_registry.py::TestNewlyRegisteredFlagsMatchTheirConsumers -q
```

Expected: FAIL — `AGENT_MAX_ACTIONS_PER_WAKE is not registered`.

- [ ] **Step 3: Add a write-gate flag generator next to `_mcp_enable_flags`**

```python
def _mcp_actions_flags() -> List[Flag]:
    """Per-server write gates ({SERVER}_ACTIONS_ENABLED).

    ``mcp_servers/shared_code/config/action_flags.py`` has always honoured these
    -- they are the read-only vs read-write switch for each server -- but they
    were never registered, so they were invisible in the settings UI and absent
    from the generated env example.
    """
    return [
        _b(
            f"{srv.upper()}_ACTIONS_ENABLED",
            False,
            f"Allow write/action tools on the {srv.replace('_', ' ')} MCP server "
            "(read-only tools are unaffected).",
            group="tools",
            depends_on=f"{srv.upper()}_ENABLED",
            advanced=True,
        )
        for srv in MCP_SERVER_NAMES
    ]
```

Add `*_mcp_actions_flags(),` to `_FLAGS` immediately after `*_mcp_enable_flags(),`.

Note: `GRAFANA_ACTIONS_ENABLED` is generated by this helper (grafana is in `MCP_SERVER_NAMES`) and must therefore **not** also be declared by hand in Step 5. Its default of `False` matches `grafana_mcp_server.py`.

- [ ] **Step 4: Add the conversation, ticketing, alerts and metrics flags**

Into their group sections in `_FLAGS`:

```python
    # --- conversation ---
    _i("AGENT_MAX_ACTIONS_PER_WAKE", 10,
       "Maximum actions a persistent agent may take in one wake cycle.",
       scope=SERVICE_BOT, group="conversation", depends_on="PERSISTENT_AGENTS_ENABLED",
       minimum=1, maximum=100, advanced=True),
    _i("AGENT_MAX_TOOL_ROUNDS", 5,
       "Maximum sequential tool-call rounds inside one persistent-agent action.",
       scope=SERVICE_BOT, group="conversation", depends_on="PERSISTENT_AGENTS_ENABLED",
       minimum=1, maximum=20, advanced=True),
    _b("LOOP_DETECTION_ENABLED", True,
       "Detect and break repeated identical tool calls within a turn.",
       scope=SERVICE_BOT, group="conversation", advanced=True),
    _i("LOOP_DETECTION_THRESHOLD", 2,
       "Identical repeats before a tool call is treated as a loop.",
       scope=SERVICE_BOT, group="conversation", depends_on="LOOP_DETECTION_ENABLED",
       minimum=2, maximum=10, advanced=True),
    _i("MULTI_SITE_MAX_CONCURRENCY", 5,
       "Maximum sites evaluated in parallel by multi-site workflows.",
       scope=SERVICE_BOT, group="conversation", minimum=1, maximum=20, advanced=True),

    # --- ticketing ---
    _b("JIRA_SWEEP_ENABLED", True,
       "Run the periodic Jira sweep that reconciles ticket state.",
       scope=SERVICE_BOT, group="ticketing"),
    _s("JIRA_ISSUE_TYPE", "Task",
       "Jira issue type used for tickets when the project offers no better match.",
       scope=SERVICE_BOT, group="ticketing"),
    _b("STARTUP_RECOVERY_ENABLED", True,
       "Scan for orphaned work on startup. Must be false when running more than "
       "one orchestrator instance.",
       scope=SERVICE_BOT, group="ticketing", advanced=True),

    # --- alerts ---
    _s("NO_REPLY_CHAT_IDS", "",
       "Comma-separated Telegram chat ids the bot never replies in.",
       scope=SERVICE_BOT, group="alerts"),
    _s("AFTER_HOURS_TIMEZONE", "",
       "Timezone for after-hours escalation logic. Empty falls back to DEFAULT_TIMEZONE.",
       scope=SERVICE_BOT, group="alerts"),
    _i("AFTER_HOURS_START_HOUR", 19,
       "Hour (0-23) when after-hours escalation handling begins.",
       scope=SERVICE_BOT, group="alerts", minimum=0, maximum=23),

    # --- metrics ---
    _s("METRICS_TIMEZONE", "UTC",
       "Timezone used to schedule metrics collection.",
       group="metrics", depends_on="METRICS_ENABLED"),
```

- [ ] **Step 5: Add the model, Grafana, layout, access and deployment flags**

```python
    # --- models ---
    _i("GEMINI_THINKING_BUDGET", 4096,
       "Thinking-token budget for Gemini 2.5 models (-1 dynamic, 0 off, >0 cap).",
       group="models", minimum=-1, maximum=24576, advanced=True),
    _s("GEMINI_AGENT_PRO_MODEL", "gemini-2.5-pro",
       "Model for complex agent tasks (analysis, multi-step reasoning).",
       group="models"),
    _s("THREAD_CLASSIFIER_MODEL", "gemini-2.5-flash-lite",
       "Model that assigns incoming messages to conversation threads.",
       scope=SERVICE_BOT, group="models",
       depends_on="THREAD_DISENTANGLEMENT_ENABLED"),
    _b("GOOGLE_SEARCH_GROUNDING", True,
       "Allow Google Search grounding for staff users.",
       group="models"),

    # --- grafana (GRAFANA_ACTIONS_ENABLED comes from _mcp_actions_flags) ---
    _i("GRAFANA_QUERY_TIMEOUT", 180, "Seconds allowed for a Grafana panel query.",
       group="grafana", depends_on="GRAFANA_ENABLED", minimum=1, maximum=600, advanced=True),
    _i("GRAFANA_METADATA_TIMEOUT", 30, "Seconds allowed for a Grafana metadata fetch.",
       group="grafana", depends_on="GRAFANA_ENABLED", minimum=1, maximum=600, advanced=True),
    _i("GRAFANA_VARIABLE_TIMEOUT", 60, "Seconds allowed for a Grafana variable lookup.",
       group="grafana", depends_on="GRAFANA_ENABLED", minimum=1, maximum=600, advanced=True),

    # --- layout: read from shared/layout/* but never registered until now ---
    _f("LAYOUT_KW_PER_HOUSEHOLD", 0.0,
       "Explicit kW per household. 0 lets the pipeline derive it.",
       scope=SERVICE_BOT, group="layout", depends_on="GRID_DESIGN_ENABLED", advanced=True),
    _f("LAYOUT_MAX_BRIDGE_DISTANCE_M", 200.0,
       "Longest gap the distribution network may bridge (m).",
       scope=SERVICE_BOT, group="layout", depends_on="GRID_DESIGN_ENABLED", advanced=True),
    _f("LAYOUT_PATH_REDUNDANCY_DISTANCE_M", 22.5,
       "Distance under which parallel road paths are treated as redundant (m).",
       scope=SERVICE_BOT, group="layout", depends_on="GRID_DESIGN_ENABLED", advanced=True),
    _f("LAYOUT_PATH_WEIGHT_PENALTY", 3.0,
       "Routing penalty applied to building-adjacent paths.",
       scope=SERVICE_BOT, group="layout", depends_on="GRID_DESIGN_ENABLED", advanced=True),
    _f("LAYOUT_PLANT_CONNECT_DISTANCE_M", 150.0,
       "Search radius when connecting the plant to the network (m).",
       scope=SERVICE_BOT, group="layout", depends_on="GRID_DESIGN_ENABLED", advanced=True),
    _i("LAYOUT_PLANT_CONNECT_K", 5,
       "Number of candidate connection points evaluated for the plant.",
       scope=SERVICE_BOT, group="layout", depends_on="GRID_DESIGN_ENABLED",
       minimum=1, advanced=True),
    _f("LAYOUT_POWER_FACTOR", 0.95,
       "Power factor used to convert kVA to kW.",
       scope=SERVICE_BOT, group="layout", depends_on="GRID_DESIGN_ENABLED",
       minimum=0.1, maximum=1.0, advanced=True),
    _f("LAYOUT_ROAD_CLIP_BUFFER_M", 100.0,
       "Buffer around the site used to clip the road network (m).",
       scope=SERVICE_BOT, group="layout", depends_on="GRID_DESIGN_ENABLED", advanced=True),
    _f("LAYOUT_WATERWAY_BUFFER_M", 200.0,
       "Exclusion buffer around waterways (m).",
       scope=SERVICE_BOT, group="layout", depends_on="GRID_DESIGN_ENABLED", advanced=True),

    # --- access ---
    _s("STAFF_ORG_NAME", "Staff",
       "Display name for the internal staff organization.",
       group="access"),
    _s("MANAGED_GENERATION_COLUMN", "is_generation_managed_by_nxt_grid",
       "Grids-table column marking operator-managed generation. Interpolated into "
       "SQL, so use only valid PostgreSQL identifier characters.",
       group="access", advanced=True),

    # --- deployment ---
    _s("ORGANIZATION_NAME", "the operator",
       "Operator name shown in chart watermarks and equipment messages.",
       group="deployment"),
    _s("DOC_CODE_PREFIX", "DOC",
       "Prefix for generated document reference codes.",
       group="deployment"),
```

- [ ] **Step 6: Fix the `ORGANIZATION_NAME` default that disagrees with the other two**

`mcp_servers/servers/grafana_server/grafana_mcp_server.py:1468` defaults to `"Anansi"` while `equipment_control_mcp_server.py:801` and `equipment_diagnostics_mcp_server.py:22` default to `"the operator"`, as does `ServerSettings.organization_name` in `shared/config/settings.py`. Three of four agree; align the outlier:

```python
        org_name = os.getenv("ORGANIZATION_NAME", "the operator")
```

- [ ] **Step 7: Add the connections entries (status only, never written)**

These are registered solely so the readiness panel can report configured/not-configured. `editable=False` means `_filter_writable` drops them, so they can never be written back. `document=False` keeps them out of `flags.env.example`, which stays a tunables-only file.

```python
def _connection(name: str, description: str, set_via: str, secret: bool = True) -> Flag:
    """A credential or endpoint this deployment depends on but does not manage.

    Status-only: shown in the Connections group as configured/not configured so
    the readiness panel can explain what a deployment is still missing.
    """
    return Flag(
        name, FlagType.STR, "", description,
        editable=False, secret=secret, document=False,
        group="connections", set_via=set_via,
    )
```

Add these, in this order, in the `connections` section of `_FLAGS`:

```
GOOGLE_API_KEY              "Google AI Studio key for Gemini generation and embeddings."
GOOGLE_SERVICE_ACCOUNT_JSON "Service account JSON used to read Google Docs and Drive."
TELEGRAM_BOT_TOKEN          "Telegram bot token."
TELEGRAM_BOT_USERNAME       "Telegram bot username."           (secret=False)
CHAT_DB_URL                 "Chat database (Supabase) URL."     (secret=False)
CHAT_DB_SERVICE_KEY         "Chat database service-role key."
AUTH_DB_HOST                "Auth database host."
API_KEY                     "Shared key authenticating calls to the orchestrator."
SESSION_ID_SECRET           "Secret used to derive session identifiers."
CHAT_ORCHESTRATOR_URL       "Orchestrator chat endpoint."       (secret=False)
DIGITALOCEAN_APP_ID         "DigitalOcean app id for the settings backend."  (secret=False)
DIGITALOCEAN_API_TOKEN      "DigitalOcean API token for the settings backend."
JIRA_BASE_URL               "Jira Cloud base URL."              (secret=False)
JIRA_USERNAME               "Jira account email."               (secret=False)
JIRA_API_TOKEN              "Jira API token."
NOTIFY_SHARED_SECRET        "Shared secret required on POST /chat/notify."
LANGFUSE_DASHBOARD_URL      "Langfuse dashboard link shown in the sidebar."  (secret=False)
```

`set_via` for all of them: `"Set in the deployment environment (DigitalOcean app spec or .env)."`

- [ ] **Step 8: Make the app-owned secrets editable**

`GRAFANA_PASSWORD` already exists with `secret=True`; add `editable=True` explicitly is unnecessary (it is the default) — instead confirm it stays editable and add these two, which are app-owned integration credentials an operator turns on from this page:

```python
    _s("OPENROUTER_API_KEY", "", "OpenRouter API key.", scope=SERVICE_BOT,
       group="connections", secret=True),
    _s("TAVILY_API_KEY", "", "Tavily web-search key for the knowledge server.",
       group="connections", secret=True, depends_on="KNOWLEDGE_ENABLED"),
```

- [ ] **Step 9: Update the module docstring**

The docstring at the top of `flag_registry.py` currently states credentials "are intentionally *not* registered here". That is no longer true. Replace that paragraph with:

```
Credentials fall into two classes. *App-owned* integration secrets
(``GRAFANA_PASSWORD``, ``OPENROUTER_API_KEY``, ``TAVILY_API_KEY``) are registered
editable and are set through the settings UI, which writes them to the deployment
backend as secrets. *Host-owned* credentials and connection strings
(``AUTH_DB_*``, ``CHAT_DB_*``, ``DIGITALOCEAN_API_TOKEN`` …) are registered
``editable=False, document=False`` so they can never be written back; they exist
here only so the deployment-readiness view can report whether they are set.
```

- [ ] **Step 10: Regenerate and run the tests**

```bash
$PY -m shared.config.flag_registry > shared/config/flags.env.example
cd chat_orchestrator && $PY -m pytest tests/test_flag_registry.py -q
```

Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add shared/config/flag_registry.py shared/config/flags.env.example \
        chat_orchestrator/tests/test_flag_registry.py \
        mcp_servers/servers/grafana_server/grafana_mcp_server.py
git commit -m "feat(settings): register the operator flags that were invisible to the UI"
```

---

## Task 4: Reconcile registry defaults with the code that runs

**Files:**
- Modify: `shared/config/flag_registry.py`
- Modify: `chat_orchestrator/tests/test_flag_registry.py`

Three registry defaults disagree with `chat_orchestrator/orchestrator/config/settings.py`, so the settings page can display a value the bot is not using. The orchestrator is authoritative — it is what runs.

- [ ] **Step 1: Write the failing test**

```python
class TestRegistryMatchesOrchestratorDefaults:
    """The settings page must not display a value the orchestrator ignores."""

    def test_fallback_model_matches_the_orchestrator(self):
        assert fr.get("GEMINI_FALLBACK_MODEL", env={}) == "gemini-2.5-flash-lite"

    def test_deep_thinking_model_matches_the_orchestrator(self):
        assert fr.get("GEMINI_DEEP_THINKING_MODEL", env={}) == "gemini-pro-latest"

    def test_temperature_is_editable(self):
        # Rendered read-only at 0.2 while the orchestrator treats empty as "use
        # the model default" -- an operator could not reach the documented
        # behaviour from the UI at all.
        assert fr.FLAGS["GEMINI_TEMPERATURE"].editable is True

    def test_main_max_output_tokens_is_editable(self):
        assert fr.FLAGS["GEMINI_MAX_OUTPUT_TOKENS"].editable is True
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd chat_orchestrator && $PY -m pytest tests/test_flag_registry.py::TestRegistryMatchesOrchestratorDefaults -q
```

Expected: FAIL — `assert 'gemini-2.5-flash' == 'gemini-2.5-flash-lite'`.

- [ ] **Step 3: Apply the three corrections**

In `_FLAGS`, change `GEMINI_FALLBACK_MODEL`'s default to `"gemini-2.5-flash-lite"` and `GEMINI_DEEP_THINKING_MODEL`'s to `"gemini-pro-latest"`, and drop `editable=False` from `GEMINI_TEMPERATURE` and `GEMINI_MAX_OUTPUT_TOKENS`. Add `maximum=65536, minimum=64` to `GEMINI_MAX_OUTPUT_TOKENS`, matching the orchestrator's `Field(ge=64, le=65536)`.

- [ ] **Step 4: Run the tests and regenerate**

```bash
$PY -m shared.config.flag_registry > shared/config/flags.env.example
cd chat_orchestrator && $PY -m pytest tests/test_flag_registry.py -q && $PY -m pytest tests -q
```

Expected: all pass. The full orchestrator suite must also pass — these defaults feed real code paths.

- [ ] **Step 5: Commit**

```bash
git add shared/config/flag_registry.py shared/config/flags.env.example chat_orchestrator/tests/test_flag_registry.py
git commit -m "fix(settings): align registry model defaults with the orchestrator"
```

---

## Task 5: Deployment readiness capabilities

**Files:**
- Modify: `shared/config/flag_registry.py`
- Create: `chat_orchestrator/tests/test_flag_readiness.py`

**Interfaces produced:**
- `Requirement = Union[str, tuple[str, ...]]` — a name, or alternatives where any one satisfies
- `Capability(key, title, description, requires, severity)` with `severity` in `{"required", "recommended"}`
- `CAPABILITIES: tuple[Capability, ...]`
- `readiness(env=None) -> list[CapabilityStatus]` where `CapabilityStatus(capability, missing, satisfied)`

- [ ] **Step 1: Write the failing tests**

Create `chat_orchestrator/tests/test_flag_readiness.py`:

```python
"""Deployment-readiness capability computation.

`validate_required` and the `required` flag field existed for months with zero
callers and zero flags using them. These tests pin the behaviour now that the
settings page reports on it.
"""

from shared.config import flag_registry as fr


def _status(env, key):
    return next(s for s in fr.readiness(env=env) if s.capability.key == key)


class TestAdminLogin:
    def test_dev_bypass_alone_satisfies_login(self):
        status = _status({"GRID_DESIGN_DEV_NO_AUTH": "1"}, "admin_login")
        assert status.satisfied
        assert status.missing == []

    def test_empty_env_reports_every_login_requirement_missing(self):
        status = _status({}, "admin_login")
        assert not status.satisfied
        assert "GOOGLE_CLIENT_ID" in status.missing[0]

    def test_auth_client_id_alias_satisfies_the_client_id_requirement(self):
        env = {
            "AUTH_CLIENT_ID": "x",
            "AUTH_CLIENT_SECRET": "y",
            "ALLOWED_VIEWER_EMAILS": "a@example.com",
        }
        assert _status(env, "admin_login").satisfied

    def test_whitespace_only_value_does_not_count_as_configured(self):
        env = {"GOOGLE_CLIENT_ID": "   ", "GOOGLE_CLIENT_SECRET": "y",
               "ALLOWED_VIEWER_EMAILS": "a@example.com"}
        assert not _status(env, "admin_login").satisfied


class TestBotReplies:
    def test_chat_db_legacy_supabase_names_are_accepted(self):
        env = {
            "GOOGLE_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "t",
            "SUPABASE_URL": "u", "SUPABASE_KEY": "s",
            "API_KEY": "a", "SESSION_ID_SECRET": "z", "AUTH_DB_HOST": "h",
        }
        assert _status(env, "bot_replies").satisfied

    def test_missing_telegram_token_is_reported_by_name(self):
        env = {
            "GOOGLE_API_KEY": "k", "CHAT_DB_URL": "u", "CHAT_DB_SERVICE_KEY": "s",
            "API_KEY": "a", "SESSION_ID_SECRET": "z", "AUTH_DB_HOST": "h",
        }
        assert _status(env, "bot_replies").missing == ["TELEGRAM_BOT_TOKEN"]


class TestSeverity:
    def test_optional_integrations_are_recommended_not_required(self):
        for key in ("escalations_to_jira", "grafana_tools"):
            status = _status({}, key)
            assert status.capability.severity == "recommended"

    def test_core_capabilities_are_required(self):
        for key in ("admin_login", "bot_replies", "system_instructions"):
            assert _status({}, key).capability.severity == "required"


def test_every_capability_requirement_names_a_real_env_var():
    """A typo in a requirement would silently make a capability unsatisfiable."""
    for capability in fr.CAPABILITIES:
        for requirement in capability.requires:
            names = (requirement,) if isinstance(requirement, str) else requirement
            assert names, f"{capability.key} has an empty requirement"
            for name in names:
                assert name.isupper() or "_" in name, name
```

- [ ] **Step 2: Run to verify failure**

```bash
cd chat_orchestrator && $PY -m pytest tests/test_flag_readiness.py -q
```

Expected: FAIL — `AttributeError: module 'shared.config.flag_registry' has no attribute 'readiness'`.

- [ ] **Step 3: Implement the capability model**

Append to `shared/config/flag_registry.py`:

```python
# ---------------------------------------------------------------------------
# Deployment readiness
# ---------------------------------------------------------------------------
# A capability is something the deployment can *do*. Operators reason about
# "the bot cannot reply", not about a list of 40 unset variables, so readiness
# is reported per capability with the specific missing names attached.

Requirement = Any  # str, or tuple[str, ...] meaning "any one of these"


@dataclass(frozen=True)
class Capability:
    key: str
    title: str
    description: str
    requires: tuple[Requirement, ...]
    severity: str = "required"  # or "recommended"


@dataclass(frozen=True)
class CapabilityStatus:
    capability: Capability
    missing: List[str]

    @property
    def satisfied(self) -> bool:
        return not self.missing


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "admin_login",
        "Admins can sign in",
        "Google OAuth plus at least one allow-listed address. "
        "GRID_DESIGN_DEV_NO_AUTH bypasses all of it for local development only.",
        (
            ("GOOGLE_CLIENT_ID", "AUTH_CLIENT_ID", "GRID_DESIGN_DEV_NO_AUTH"),
            ("GOOGLE_CLIENT_SECRET", "AUTH_CLIENT_SECRET", "GRID_DESIGN_DEV_NO_AUTH"),
            ("ALLOWED_VIEWER_EMAILS", "GRID_DESIGN_DEV_NO_AUTH"),
        ),
    ),
    Capability(
        "settings_persist",
        "Settings changes reach the live app",
        "Without DigitalOcean credentials, changes are written to the local "
        "SETTINGS_FILE and apply on the next restart of this process only.",
        ("DIGITALOCEAN_APP_ID", "DIGITALOCEAN_API_TOKEN"),
        severity="recommended",
    ),
    Capability(
        "bot_replies",
        "The bot can answer messages",
        "Generation, Telegram delivery, chat storage and authentication.",
        (
            "GOOGLE_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            ("CHAT_DB_URL", "SUPABASE_URL"),
            ("CHAT_DB_SERVICE_KEY", "SUPABASE_KEY"),
            "API_KEY",
            "SESSION_ID_SECRET",
            ("AUTH_DB_HOST", "AUTH_SUPABASE_URL"),
        ),
    ),
    Capability(
        "system_instructions",
        "The bot loads its instructions",
        "Google Docs holding the customer and staff system prompts.",
        (
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "CUSTOMER_SUPPORT_DOC_ID",
            "STAFF_SUPPORT_DOC_ID",
        ),
    ),
    Capability(
        "escalations_to_jira",
        "Escalations reach Jira",
        "Without these, escalations still post to Telegram and are tracked in "
        "the internal ticket ledger.",
        ("JIRA_BASE_URL", "JIRA_USERNAME", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"),
        severity="recommended",
    ),
    Capability(
        "grafana_tools",
        "Grafana panels are available as tools",
        "Needed before Sync Now can index dashboards.",
        ("GRAFANA_URL", "GRAFANA_USERNAME", "GRAFANA_PASSWORD"),
        severity="recommended",
    ),
    Capability(
        "notify_endpoint",
        "External systems can post alerts",
        "POST /chat/notify for Grafana, n8n and VRM passthrough.",
        ("NOTIFY_SHARED_SECRET",),
        severity="recommended",
    ),
)


def _is_set(name: str, source: Mapping[str, str]) -> bool:
    return bool((source.get(name) or "").strip())


def readiness(env: Optional[Mapping[str, str]] = None) -> List[CapabilityStatus]:
    """Per-capability status for the deployment described by ``env``.

    A requirement given as a tuple is satisfied by any one of its names, which
    is how legacy aliases (SUPABASE_URL for CHAT_DB_URL) and bypasses
    (GRID_DESIGN_DEV_NO_AUTH) are expressed without special cases.
    """
    source = env if env is not None else os.environ
    statuses: List[CapabilityStatus] = []
    for capability in CAPABILITIES:
        missing: List[str] = []
        for requirement in capability.requires:
            names = (requirement,) if isinstance(requirement, str) else tuple(requirement)
            if not any(_is_set(name, source) for name in names):
                missing.append(" or ".join(names))
        statuses.append(CapabilityStatus(capability, missing))
    return statuses
```

- [ ] **Step 4: Run the tests**

```bash
cd chat_orchestrator && $PY -m pytest tests/test_flag_readiness.py -q
```

Expected: PASS.

- [ ] **Step 5: Force-add the new test file and commit**

The repo's `.gitignore` denies `tests/` — a plain `git add` on a new test file is a silent no-op that CI will never catch, because the suite simply will not exist. See `CLAUDE.md`.

```bash
git add -f chat_orchestrator/tests/test_flag_readiness.py
git add shared/config/flag_registry.py
git status --short   # confirm test_flag_readiness.py is staged
git commit -m "feat(settings): add deployment-readiness capabilities"
```

---

## Task 6: Provenance and secret handling in the settings service

**Files:**
- Modify: `anansi_app/services/settings_service.py`
- Create: `anansi_app/tests/test_settings_service_provenance.py`

**Interfaces produced:**
- `ValueSource` enum: `DEFAULT`, `ENVIRONMENT`, `BACKEND`
- `SettingsService.get_settings_with_provenance(fetch_from_do=False) -> dict[str, SettingValue]`
- `SettingValue(name, value, source, secret_is_set)`

- [ ] **Step 1: Write the failing tests**

Create `anansi_app/tests/test_settings_service_provenance.py`:

```python
"""An unset flag and a flag deliberately set to its default look identical in
`settings_defaults()`. Operators need to tell them apart, and secrets must never
round-trip their value to the browser."""

from services.settings_service import SettingsService, ValueSource


class FakeBackend:
    name = "fake"

    def __init__(self, values):
        self._values = values

    def available(self):
        return True

    def get_all(self):
        return dict(self._values)

    def update(self, settings, restart=True):
        return True, None


def _service(backend_values, monkeypatch, env=None):
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    service = SettingsService()
    service.backend = FakeBackend(backend_values)
    return service


def test_unset_flag_is_reported_as_default(monkeypatch):
    monkeypatch.delenv("MAX_TOOL_ROUNDS", raising=False)
    values = _service({}, monkeypatch).get_settings_with_provenance()
    assert values["MAX_TOOL_ROUNDS"].value == 5
    assert values["MAX_TOOL_ROUNDS"].source is ValueSource.DEFAULT


def test_env_value_equal_to_the_default_is_still_reported_as_set(monkeypatch):
    values = _service({}, monkeypatch, env={"MAX_TOOL_ROUNDS": "5"}).get_settings_with_provenance()
    assert values["MAX_TOOL_ROUNDS"].source is ValueSource.ENVIRONMENT


def test_backend_value_wins_and_is_labelled_backend(monkeypatch):
    service = _service({"MAX_TOOL_ROUNDS": "9"}, monkeypatch, env={"MAX_TOOL_ROUNDS": "5"})
    values = service.get_settings_with_provenance(fetch_from_do=True)
    assert values["MAX_TOOL_ROUNDS"].value == 9
    assert values["MAX_TOOL_ROUNDS"].source is ValueSource.BACKEND


def test_secret_value_is_never_returned(monkeypatch):
    service = _service({"GRAFANA_PASSWORD": "hunter2"}, monkeypatch)
    values = service.get_settings_with_provenance(fetch_from_do=True)
    entry = values["GRAFANA_PASSWORD"]
    assert entry.value == ""
    assert entry.secret_is_set is True


def test_unset_secret_reports_not_set(monkeypatch):
    monkeypatch.delenv("GRAFANA_PASSWORD", raising=False)
    values = _service({}, monkeypatch).get_settings_with_provenance()
    assert values["GRAFANA_PASSWORD"].secret_is_set is False
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests/test_settings_service_provenance.py -q
```

Expected: FAIL — `ImportError: cannot import name 'ValueSource'`.

- [ ] **Step 3: Implement**

Add to `anansi_app/services/settings_service.py`:

```python
class ValueSource(Enum):
    """Where a setting's effective value came from."""

    DEFAULT = "default"
    ENVIRONMENT = "environment"
    BACKEND = "backend"


@dataclass(frozen=True)
class SettingValue:
    name: str
    value: Any
    source: ValueSource
    secret_is_set: bool = False
```

And the method on `SettingsService`:

```python
    def get_settings_with_provenance(
        self, fetch_from_do: bool = False
    ) -> Dict[str, SettingValue]:
        """Current values annotated with where each one came from.

        ``get_current_settings`` collapses "unset" into "default", so the UI
        cannot show whether a value was chosen or merely inherited. Secrets
        report only whether they are set; their value never leaves this method.
        """
        remote: Dict[str, str] = {}
        if fetch_from_do and self.backend.available():
            remote = self.backend.get_all()

        out: Dict[str, SettingValue] = {}
        for name, flag in registry.FLAGS.items():
            if not flag.show_in_settings:
                continue
            if name in remote:
                raw, source = remote[name], ValueSource.BACKEND
            elif name in os.environ:
                raw, source = os.environ[name], ValueSource.ENVIRONMENT
            else:
                raw, source = None, ValueSource.DEFAULT

            is_set = bool((raw or "").strip())
            if flag.secret:
                out[name] = SettingValue(name, "", source, secret_is_set=is_set)
            else:
                out[name] = SettingValue(name, flag.coerce(raw), source)
        return out
```

Add `from dataclasses import dataclass` and `from enum import Enum` to the imports, and export `ValueSource` and `SettingValue` in `__all__`.

- [ ] **Step 4: Make the OpenRouter fetch lazy**

In `anansi_app/nicegui_app/pages/settings.py`, `_model_select_options` currently calls `svc.get_openrouter_models()` unconditionally, so every page render makes a network request to openrouter.ai even on a Gemini deployment. Guard it:

```python
    openrouter_models = svc.get_openrouter_models() if provider == "openrouter" else []
```

and move the `provider = _selected_provider(current)` line above it. `_role_model_options` already falls back to `GEMINI_MODEL` options when the OpenRouter list is empty, so the Gemini path is unaffected.

- [ ] **Step 5: Run the tests**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests -q
```

Expected: all pass.

- [ ] **Step 6: Force-add and commit**

```bash
git add -f anansi_app/tests/test_settings_service_provenance.py
git add anansi_app/services/settings_service.py anansi_app/nicegui_app/pages/settings.py
git status --short
git commit -m "feat(settings): report value provenance and secret presence"
```

---

## Task 7: Widget factories

**Files:**
- Create: `anansi_app/nicegui_app/pages/settings_widgets.py`
- Create: `anansi_app/tests/test_settings_widgets.py`

Pure functions that decide *what* to render go here and are unit-testable without a browser. Only the thin `ui.*` construction stays untested.

**Interfaces produced:**
- `RenderMode` enum: `SWITCH`, `NUMBER`, `SELECT`, `MULTI_SELECT`, `TEXT`, `TEXTAREA`, `SECRET`, `READ_ONLY`
- `render_mode(flag) -> RenderMode`
- `secret_placeholder(is_set) -> str`
- `validate(flag, value) -> str | None` returning an error message or `None`

- [ ] **Step 1: Write the failing tests**

Create `anansi_app/tests/test_settings_widgets.py`:

```python
from nicegui_app.pages.settings_widgets import (
    RenderMode,
    render_mode,
    secret_placeholder,
    validate,
)
from shared.config import flag_registry as fr


class TestRenderMode:
    def test_bool_renders_a_switch(self):
        assert render_mode(fr.FLAGS["BOT_ENABLED"]) is RenderMode.SWITCH

    def test_enum_renders_a_select_not_a_text_box(self):
        assert render_mode(fr.FLAGS["TICKET_BACKEND_OVERRIDE"]) is RenderMode.SELECT

    def test_secret_renders_masked(self):
        assert render_mode(fr.FLAGS["GRAFANA_PASSWORD"]) is RenderMode.SECRET

    def test_read_only_wins_over_type(self):
        assert render_mode(fr.FLAGS["DEFAULT_TIMEZONE"]) is RenderMode.READ_ONLY

    def test_json_renders_a_textarea(self):
        assert render_mode(fr.FLAGS["MCP_DISABLED_TOOLS"]) is RenderMode.TEXTAREA

    def test_email_lists_render_as_chips(self):
        assert render_mode(fr.FLAGS["ALLOWED_VIEWER_EMAILS"]) is RenderMode.MULTI_SELECT


class TestSecretPlaceholder:
    def test_set_secret_shows_a_masked_marker_and_no_value(self):
        text = secret_placeholder(True)
        assert "set" in text.lower()
        assert "•" in text

    def test_unset_secret_says_so(self):
        assert secret_placeholder(False) == "not set"


class TestValidate:
    def test_enum_rejects_an_unlisted_value(self):
        error = validate(fr.FLAGS["TICKET_BACKEND_OVERRIDE"], "jra")
        assert error is not None and "auto" in error

    def test_enum_accepts_a_listed_value(self):
        assert validate(fr.FLAGS["TICKET_BACKEND_OVERRIDE"], "internal") is None

    def test_number_below_minimum_is_rejected(self):
        assert validate(fr.FLAGS["METRICS_SCHEDULE_HOUR"], -1) is not None

    def test_number_above_maximum_is_rejected(self):
        assert validate(fr.FLAGS["METRICS_SCHEDULE_HOUR"], 24) is not None

    def test_number_inside_bounds_is_accepted(self):
        assert validate(fr.FLAGS["METRICS_SCHEDULE_HOUR"], 9) is None

    def test_invalid_json_is_rejected(self):
        assert validate(fr.FLAGS["MCP_DISABLED_TOOLS"], "[not json") is not None

    def test_valid_json_is_accepted(self):
        assert validate(fr.FLAGS["MCP_DISABLED_TOOLS"], '["jira:create_issue"]') is None
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests/test_settings_widgets.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'nicegui_app.pages.settings_widgets'`.

- [ ] **Step 3: Implement**

Create `anansi_app/nicegui_app/pages/settings_widgets.py`:

```python
"""Widget selection and validation for the settings page.

Kept free of ``ui.*`` construction so the decisions -- which widget, which
placeholder, which error -- are unit-testable without a browser.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional

from shared.config.flag_registry import Flag, FlagType

# Flags whose value is a comma-separated list and which read far better as
# removable chips than as one long comma-run in a text box.
_CSV_LIST_FLAGS = frozenset(
    {
        "ALLOWED_VIEWER_EMAILS",
        "EQUIPMENT_CONTROL_ALLOWED_USERS",
        "GRID_DESIGN_ALLOWED_USERS",
        "GRID_DESIGN_EDITORS",
        "GRID_PROCUREMENT_EDITORS",
        "NO_REPLY_CHAT_IDS",
    }
)
# OPENROUTER_PROVIDER_ORDER is deliberately absent: it is also a comma-separated
# list, but its options are fetched live from OpenRouter per selected model, so
# it keeps the bespoke picker in settings.py rather than a generic chip input
# that would drop the discovered routes.


class RenderMode(Enum):
    SWITCH = "switch"
    NUMBER = "number"
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    TEXT = "text"
    TEXTAREA = "textarea"
    SECRET = "secret"
    READ_ONLY = "read_only"


def render_mode(flag: Flag) -> RenderMode:
    """Which widget a flag gets. Read-only and secret win over the value type."""
    if not flag.editable:
        return RenderMode.READ_ONLY
    if flag.secret:
        return RenderMode.SECRET
    if flag.type is FlagType.BOOL:
        return RenderMode.SWITCH
    if flag.type is FlagType.JSON:
        return RenderMode.TEXTAREA
    if flag.choices:
        return RenderMode.SELECT
    if flag.name in _CSV_LIST_FLAGS:
        return RenderMode.MULTI_SELECT
    if flag.type in (FlagType.INT, FlagType.FLOAT):
        return RenderMode.NUMBER
    return RenderMode.TEXT


def secret_placeholder(is_set: bool) -> str:
    """Placeholder for a secret field. Never derived from the actual value."""
    return "••••••••  (set)" if is_set else "not set"


def validate(flag: Flag, value: Any) -> Optional[str]:
    """Return an error message for ``value``, or None when it is acceptable."""
    if flag.choices and str(value) not in flag.choices:
        return f"{flag.display_label}: must be one of {', '.join(flag.choices)}"
    if flag.type is FlagType.JSON:
        try:
            json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return f"{flag.display_label}: invalid JSON"
    if flag.type in (FlagType.INT, FlagType.FLOAT) and value not in (None, ""):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f"{flag.display_label}: must be a number"
        if flag.minimum is not None and number < flag.minimum:
            return f"{flag.display_label}: must be at least {flag.minimum:g}"
        if flag.maximum is not None and number > flag.maximum:
            return f"{flag.display_label}: must be at most {flag.maximum:g}"
    return None
```

- [ ] **Step 4: Run the tests**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests/test_settings_widgets.py -q
```

Expected: PASS.

- [ ] **Step 5: Force-add and commit**

```bash
git add -f anansi_app/tests/test_settings_widgets.py
git add anansi_app/nicegui_app/pages/settings_widgets.py
git status --short
git commit -m "feat(settings): add testable widget selection and validation"
```

---

## Task 8: Readiness panel

**Files:**
- Create: `anansi_app/nicegui_app/pages/settings_readiness.py`
- Create: `anansi_app/tests/test_settings_readiness_panel.py`

**Interfaces produced:**
- `PanelRow(title, description, missing, severity, satisfied, settable_here)`
- `build_rows(env=None) -> list[PanelRow]`
- `render_panel(rows) -> None` (NiceGUI construction; not unit-tested)

- [ ] **Step 1: Write the failing tests**

Create `anansi_app/tests/test_settings_readiness_panel.py`:

```python
from nicegui_app.pages.settings_readiness import build_rows


def _row(env, title_fragment):
    return next(r for r in build_rows(env=env) if title_fragment in r.title)


def test_satisfied_rows_sort_below_unsatisfied():
    env = {"GRID_DESIGN_DEV_NO_AUTH": "1"}
    rows = build_rows(env=env)
    first_satisfied = next(i for i, r in enumerate(rows) if r.satisfied)
    assert all(not r.satisfied for r in rows[:first_satisfied])


def test_required_rows_sort_above_recommended():
    rows = [r for r in build_rows(env={}) if not r.satisfied]
    severities = [r.severity for r in rows]
    assert severities == sorted(severities, key=lambda s: 0 if s == "required" else 1)


def test_grafana_password_is_marked_settable_here():
    # An app-owned secret the operator can finish configuring on this page.
    assert _row({}, "Grafana").settable_here is True


def test_auth_database_is_not_settable_here():
    # Host-owned; the panel must send the operator to the deployment env.
    assert _row({}, "bot can answer").settable_here is False


def test_missing_names_are_listed_verbatim():
    row = _row({}, "Grafana")
    assert "GRAFANA_URL" in row.missing
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests/test_settings_readiness_panel.py -q
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `anansi_app/nicegui_app/pages/settings_readiness.py`:

```python
"""Deployment Readiness panel.

Answers "what is this deployment still missing" in terms of capabilities rather
than a list of unset variables, and says for each one whether the operator can
fix it on this page or must set it in the host environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional

from nicegui import ui

from shared.config import flag_registry as registry

_SEVERITY_RANK = {"required": 0, "recommended": 1}
_SEVERITY_COLOR = {"required": "#ef4444", "recommended": "#f59e0b"}


@dataclass(frozen=True)
class PanelRow:
    title: str
    description: str
    missing: List[str]
    severity: str
    satisfied: bool
    settable_here: bool


def _settable_here(missing: List[str]) -> bool:
    """True when every missing name is an editable flag in the registry.

    Host-owned credentials are registered ``editable=False``, so this is exactly
    the question "can the operator finish this without leaving the app".
    """
    if not missing:
        return False
    for entry in missing:
        # A requirement may be "A or B"; it is settable if any alternative is.
        alternatives = [part.strip() for part in entry.split(" or ")]
        flags = [registry.FLAGS.get(name) for name in alternatives]
        if not any(flag is not None and flag.editable for flag in flags):
            return False
    return True


def build_rows(env: Optional[Mapping[str, str]] = None) -> List[PanelRow]:
    """Readiness rows, unsatisfied first, required before recommended."""
    rows = [
        PanelRow(
            title=status.capability.title,
            description=status.capability.description,
            missing=list(status.missing),
            severity=status.capability.severity,
            satisfied=status.satisfied,
            settable_here=_settable_here(list(status.missing)),
        )
        for status in registry.readiness(env=env)
    ]
    rows.sort(key=lambda row: (row.satisfied, _SEVERITY_RANK.get(row.severity, 9)))
    return rows


def render_panel(rows: List[PanelRow]) -> None:
    """Render the readiness card. Fully-ready deployments collapse to one line."""
    outstanding = [row for row in rows if not row.satisfied]
    with ui.card().classes("w-full q-mb-md"):
        if not outstanding:
            ui.label("✅ Deployment ready — every capability is configured.").classes(
                "text-subtitle1 text-weight-bold"
            )
            return

        ui.label("Deployment readiness").classes("text-subtitle1 text-weight-bold")
        ui.label(
            f"{len(outstanding)} of {len(rows)} capabilities are not configured yet."
        ).classes("text-caption").style("color: #64748b")

        for row in outstanding:
            with ui.row().classes("items-start gap-2 w-full no-wrap q-mt-sm"):
                ui.element("div").style(
                    "width: 8px; height: 8px; border-radius: 9999px; margin-top: 6px;"
                    f" background-color: {_SEVERITY_COLOR.get(row.severity, '#64748b')};"
                    " flex: 0 0 auto;"
                )
                with ui.column().classes("gap-0"):
                    ui.label(row.title).classes("text-weight-medium")
                    ui.label(row.description).classes("text-caption").style("color: #64748b")
                    ui.label("Missing: " + ", ".join(row.missing)).classes("text-caption")
                    ui.label(
                        "Set below on this page."
                        if row.settable_here
                        else "Set in the deployment environment, then reload."
                    ).classes("text-caption").style("color: #64748b")

        with ui.expansion(f"Configured ({len(rows) - len(outstanding)})").classes("w-full"):
            for row in rows:
                if row.satisfied:
                    ui.label(f"✅ {row.title}").classes("text-caption")
```

- [ ] **Step 4: Run the tests**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests/test_settings_readiness_panel.py -q
```

Expected: PASS. If `test_auth_database_is_not_settable_here` fails, check that the Task 3 connections entries really carry `editable=False`.

- [ ] **Step 5: Force-add and commit**

```bash
git add -f anansi_app/tests/test_settings_readiness_panel.py
git add anansi_app/nicegui_app/pages/settings_readiness.py
git status --short
git commit -m "feat(settings): add the deployment readiness panel"
```

---

## Task 9: Rewrite the settings page over registry groups

**Files:**
- Modify: `anansi_app/nicegui_app/pages/settings.py`
- Create: `anansi_app/tests/test_settings_page.py`

**Interfaces produced:**
- `visible_flags(group_id, pending, show_advanced, query) -> list[Flag]` — the pure filtering decision, unit-tested
- `group_is_inert(group_id, pending) -> bool`
- `render(log_levels=None)` — unchanged signature, so `main.py` needs no edit

- [ ] **Step 1: Write the failing tests**

Create `anansi_app/tests/test_settings_page.py`:

```python
import sys
from types import SimpleNamespace

sys.modules.setdefault("nicegui", SimpleNamespace(run=SimpleNamespace(), ui=SimpleNamespace()))

from nicegui_app.pages import settings as page
from shared.config import flag_registry as fr


def _pending(**overrides):
    values = {name: flag.coerce(None) for name, flag in fr.FLAGS.items()}
    values.update(overrides)
    return values


class TestVisibleFlags:
    def test_advanced_flags_are_hidden_by_default(self):
        names = [f.name for f in page.visible_flags("layout", _pending(), False, "")]
        assert "LAYOUT_POWER_FACTOR" not in names

    def test_advanced_flags_appear_when_requested(self):
        names = [f.name for f in page.visible_flags("layout", _pending(), True, "")]
        assert "LAYOUT_POWER_FACTOR" in names

    def test_search_matches_the_env_var_name(self):
        names = [f.name for f in page.visible_flags("ticketing", _pending(), True, "prefix")]
        assert names == ["INTERNAL_TICKET_PREFIX"]

    def test_search_matches_the_description(self):
        names = [f.name for f in page.visible_flags("bot_control", _pending(), True, "telegram")]
        assert "BOT_ENABLED" in names

    def test_search_matches_the_human_label(self):
        names = [f.name for f in page.visible_flags("models", _pending(), True, "main model")]
        assert "GEMINI_MODEL" in names

    def test_search_is_case_insensitive(self):
        assert page.visible_flags("bot_control", _pending(), True, "LOG_LEVEL")
        assert page.visible_flags("bot_control", _pending(), True, "log_level")

    def test_a_flag_whose_dependency_is_off_is_hidden(self):
        pending = _pending(rag__enabled=False)
        names = [f.name for f in page.visible_flags("knowledge", pending, True, "")]
        assert names == ["rag__enabled"]

    def test_a_flag_whose_dependency_is_on_is_shown(self):
        pending = _pending(rag__enabled=True)
        names = [f.name for f in page.visible_flags("knowledge", pending, True, "")]
        assert "rag__top_k" in names


class TestGroupIsInert:
    def test_grafana_group_is_inert_when_the_server_is_disabled(self):
        assert page.group_is_inert("grafana", _pending(GRAFANA_ENABLED=False)) is True

    def test_grafana_group_is_active_when_the_server_is_enabled(self):
        assert page.group_is_inert("grafana", _pending(GRAFANA_ENABLED=True)) is False

    def test_a_group_with_no_shared_dependency_is_never_inert(self):
        assert page.group_is_inert("bot_control", _pending()) is False


def test_page_contains_no_hardcoded_flag_names():
    """Adding a flag to the registry must require no edit to this page.

    The Grafana dashboard/panel picker is a genuine bespoke widget and is the
    only permitted exception.
    """
    import inspect

    source = inspect.getsource(page)
    permitted = {
        "GRAFANA_ENABLED_DASHBOARDS",
        "GRAFANA_ENABLED_PANELS",
        "GRAFANA_SYNC_HOUR",
        "GRAFANA_FORCE_FULL_REINDEX",
        "GRAFANA_PANELS_METADATA",
        "GRAFANA_AVAILABLE_DASHBOARDS",
        "GRAFANA_URL",
        "GRAFANA_USERNAME",
        "GRAFANA_FOLDER_NAME",
        "GRAFANA_PANEL_DESCRIPTION_PROMPT",
        "LLM_PROVIDER",
        "GEMINI_MODEL",
        "GEMINI_FALLBACK_MODEL",
        "GEMINI_DEEP_THINKING_MODEL",
        "INTENT_ROUTER_MODEL",
        "VERIFICATION_MODEL",
        "OPENROUTER_MODEL",
        "OPENROUTER_PROVIDER_ORDER",
        "OPENROUTER_ALLOW_FALLBACKS",
        "OPENROUTER_REQUIRE_PARAMETERS",
    }
    leaked = sorted(
        name for name in fr.FLAGS
        if name not in permitted and f'"{name}"' in source
    )
    assert leaked == [], f"page still hardcodes {leaked}; move it to the registry"
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests/test_settings_page.py -q
```

Expected: FAIL — `AttributeError: module has no attribute 'visible_flags'`.

- [ ] **Step 3: Delete the four hardcoded tables**

Remove from `anansi_app/nicegui_app/pages/settings.py`: `RESTART_REQUIRED_KEYS`, `_MCP_SERVER_KEYS`, `_section_of`, `_SECTION_ORDER`, and `_MODEL_LABELS`. Replace `_flag_label` with a call to `flag.display_label`. Every one of these is now registry data. Keep `_ROLE_MODEL_KEYS`, `_OPENROUTER_ONLY_KEYS`, `_OPENROUTER_ROUTE_FALLBACKS`, `_model_section_plan` and the Grafana picker — those are genuine bespoke behaviour, and the test above permits their names.

- [ ] **Step 4: Add the pure filtering functions**

```python
def group_is_inert(group_id: str, pending: dict[str, Any]) -> bool:
    """True when every flag in the group hangs off one switch that is off.

    Showing Grafana's twelve fields when the Grafana server is disabled, or the
    Layout Engine's twenty-nine when grid design is off, is the single largest
    source of noise on this page.
    """
    flags = [f for f in registry.flags_in_group(group_id) if f.show_in_settings]
    dependencies = {f.depends_on for f in flags if f.depends_on}
    if len(dependencies) != 1:
        return False
    dependency = dependencies.pop()
    if any(f.name == dependency for f in flags):
        return False  # the master switch lives in this group; keep it reachable
    return not bool(pending.get(dependency))


def _matches(flag, query: str) -> bool:
    if not query:
        return True
    needle = query.strip().lower()
    return (
        needle in flag.name.lower()
        or needle in flag.display_label.lower()
        or needle in flag.description.lower()
    )


def visible_flags(
    group_id: str,
    pending: dict[str, Any],
    show_advanced: bool,
    query: str,
) -> list:
    """Flags to render in ``group_id`` under the current filters.

    A search query overrides the advanced filter -- if you searched for it by
    name you want to see it, wherever it sits in the tiering.
    """
    out = []
    for flag in registry.flags_in_group(group_id):
        if not flag.show_in_settings:
            continue
        if flag.depends_on and not pending.get(flag.depends_on):
            continue
        if flag.advanced and not show_advanced and not query:
            continue
        if not _matches(flag, query):
            continue
        out.append(flag)
    return out
```

- [ ] **Step 5: Rewrite `render` around groups and the toolbar**

Replace the section-building block (currently `sections: dict[str, list[str]]` and the `for title in _SECTION_ORDER` loop) with:

```python
    state = {"query": "", "advanced": False}
    groups_container: Any = None

    def _rebuild_groups() -> None:
        groups_container.clear()
        with groups_container:
            for group in registry.groups():
                flags = visible_flags(group.id, pending, state["advanced"], state["query"])
                if not flags:
                    continue
                inert = group_is_inert(group.id, pending)
                changed_here = sum(1 for f in flags if f.name in _changed())
                header = group.title
                if changed_here:
                    header += f"  ·  {changed_here} changed"
                expanded = bool(state["query"]) or group.id in ("bot_control", "models")
                section = ui.expansion(header, value=expanded and not inert).classes("w-full q-mb-sm")
                section.props(
                    'header-class="text-h6 text-weight-bold" expand-icon-class="text-h5" '
                    "dense-toggle switch-toggle-side"
                )
                with section:
                    ui.label(group.description).classes("text-caption").style("color: #64748b")
                    if inert:
                        ui.label(
                            "Disabled — turn the corresponding server on in "
                            "Tools & Integrations to configure this."
                        ).classes("text-caption text-warning")
                        continue
                    if group.id == "grafana":
                        _render_grafana_section(
                            [f.name for f in flags], pending, log_levels, model_options,
                            _on_change, grafana_dashboards, grafana_panels,
                        )
                    elif group.id == "models":
                        _render_models_section(
                            [f.name for f in flags], pending, log_levels,
                            model_options, _on_change,
                        )
                    else:
                        with ui.grid(columns=2).classes("w-full gap-x-6 gap-y-0"):
                            for flag in flags:
                                _render_flag(
                                    flag.name, pending, log_levels, model_options, _on_change
                                )

    # Readiness first: what is missing matters more than what is tunable.
    readiness_panel.render_panel(readiness_panel.build_rows())

    with ui.row().classes("items-center gap-3 w-full q-mb-sm").style(
        "position: sticky; top: 0; z-index: 10; background: #f0f2f6; padding: 0.5rem 0"
    ):
        def _on_search(event) -> None:
            state["query"] = event.value or ""
            _rebuild_groups()

        def _on_advanced(event) -> None:
            state["advanced"] = bool(event.value)
            _rebuild_groups()

        ui.input(placeholder="Search settings…", on_change=_on_search).props(
            "outlined dense clearable"
        ).classes("flex-grow")
        ui.switch("Show advanced", value=False, on_change=_on_advanced).props("dense")

    groups_container = ui.column().classes("w-full")
    _rebuild_groups()
```

Add `from nicegui_app.pages import settings_readiness as readiness_panel` and
`from nicegui_app.pages.settings_widgets import RenderMode, render_mode, secret_placeholder, validate`
to the imports. `_rerender_models_section` keeps working — point `models_container` at the container created inside `_rebuild_groups`, or simply call `_rebuild_groups()` from `_on_change` when the provider changes.

- [ ] **Step 6: Route `_render_flag` through the widget factory**

Replace the if/elif ladder in `_render_flag` with a `render_mode(flag)` switch. The new branches:

```python
        mode = render_mode(flag)
        if mode is RenderMode.READ_ONLY:
            ui.label(f"{label}: {value if value not in (None, '') else '—'}").classes(
                "text-body2"
            )
            if flag.set_via:
                ui.label(flag.set_via).classes("text-caption").style("color: #64748b")
            return
        if mode is RenderMode.SECRET:
            is_set = bool(secret_state.get(name))
            w = ui.input(
                label, value="", password=True,
                placeholder=secret_placeholder(is_set),
                on_change=handler,
            ).classes("w-full")
            ui.label(
                "Leave blank to keep the current value."
            ).classes("text-caption").style("color: #64748b")
```

The remaining branches, replacing the type checks and name special-cases:

```python
        elif mode is RenderMode.SWITCH:
            w = ui.switch(label, value=bool(value), on_change=handler)
        elif mode is RenderMode.SELECT:
            w = ui.select(
                list(flag.choices), value=value, label=label, on_change=handler
            ).classes("w-full")
        elif mode is RenderMode.NUMBER:
            number_args: dict[str, Any] = {}
            if flag.minimum is not None:
                number_args["min"] = flag.minimum
            if flag.maximum is not None:
                number_args["max"] = flag.maximum
            if flag.type is FlagType.INT:
                number_args["precision"] = 0
            w = ui.number(label, value=value, on_change=handler, **number_args).classes("w-full")
        elif mode is RenderMode.MULTI_SELECT:
            entries = _csv_to_list(value)
            w = (
                ui.select(
                    {entry: entry for entry in entries},
                    value=entries,
                    label=label,
                    multiple=True,
                    with_input=True,
                    new_value_mode="add-unique",
                    on_change=lambda e, n=name: on_change(n, ",".join(e.value or [])),
                )
                .props("use-chips outlined dense clearable")
                .classes("w-full")
            )
        elif mode is RenderMode.TEXTAREA:
            w = ui.textarea(label, value=str(value or ""), on_change=handler).classes("w-full")
        elif name in _ROLE_MODEL_KEYS or name in model_options:
            # Live-fetched model and provider-route pickers keep their bespoke
            # option building; everything else is registry-driven.
            opts = _options_with_current(
                _role_model_options(model_options, pending)
                if name in _ROLE_MODEL_KEYS
                else model_options[name],
                value,
            )
            w = (
                ui.select(opts, value=value, label=label, with_input=True, on_change=handler)
                .props("outlined dense clearable")
                .classes("w-full")
            )
        else:
            w = ui.input(label, value=str(value or ""), on_change=handler).classes("w-full")
```

Keep the trailing description label. Below it, add the provenance chip so an
operator can tell an inherited default from a deliberate choice — the single
piece of information `settings_defaults()` throws away today:

```python
        if flag.description:
            ui.label(flag.description).classes("text-caption").style("color: #64748b")

        source = provenance.get(name)
        if name in _changed_names:
            chip_text, chip_color = "changed here", "#f59e0b"
        elif source is ValueSource.DEFAULT:
            chip_text, chip_color = "default", "#94a3b8"
        elif source is ValueSource.BACKEND:
            chip_text, chip_color = "set in deployment", "#22c55e"
        else:
            chip_text, chip_color = "set in environment", "#22c55e"
        ui.label(chip_text).classes("text-caption").style(
            f"color: {chip_color}; font-size: 0.7rem; letter-spacing: 0.03em;"
        )
```

`_render_flag` gains three parameters: `secret_state: dict[str, bool]`,
`provenance: dict[str, ValueSource]`, and `_changed_names: set[str]`. Build all
three once in `render` from a single
`svc.get_settings_with_provenance(fetch_from_do=True)` call, and pass
`set(_changed())` for the third on each `_rebuild_groups()` pass so chips stay
current as the operator edits.

Import `_csv_to_list` is already defined in this module. Add
`from nicegui_app.pages.settings_widgets import _CSV_LIST_FLAGS` only if you need
it here — `render_mode` already consults it, so the page should not.

- [ ] **Step 7: Validate on save and skip blank secrets**

In `_save`, replace the JSON-only validation loop with the general validator, and drop untouched secrets so a blank field never wipes a stored credential:

```python
        for name, val in list(changed.items()):
            flag = registry.FLAGS[name]
            if flag.secret and not str(val or "").strip():
                # A blank secret field means "leave it alone", never "delete it".
                changed.pop(name)
                continue
            error = validate(flag, val)
            if error:
                ui.notify(error + " — not saved.", type="negative")
                return
        if not changed:
            ui.notify("No changes to save.")
            return
```

Replace `needs_restart = any(k in RESTART_REQUIRED_KEYS for k in changed)` in `_refresh_bar` with `any(registry.FLAGS[k].restart_required for k in changed)`.

- [ ] **Step 8: Run the tests**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests -q
```

Expected: all pass, including `test_page_contains_no_hardcoded_flag_names`. If that one fails, the listed names belong in the registry, not the page — move them rather than widening the permitted set.

- [ ] **Step 9: Force-add and commit**

```bash
git add -f anansi_app/tests/test_settings_page.py
git add anansi_app/nicegui_app/pages/settings.py
git status --short
git commit -m "feat(settings): rebuild the settings page over registry groups"
```

---

## Task 10: Verify the minimum environment by booting the app

**Files:**
- Create: `docs/superpowers/plans/2026-07-28-settings-minimum-env-verification.md`

This task produces evidence, not code. The tiers in the design were derived by reading code; this proves them. Record what actually happened, including anything that contradicts the design — a contradiction here is a finding, not a failure.

- [ ] **Step 1: Verify Tier 0 (dev bypass, nothing else)**

```bash
cd anansi_app
env -i PATH="$PATH" HOME="$HOME" GRID_DESIGN_DEV_NO_AUTH=1 PORT=8599 \
  PYTHONPATH="$(cd .. && pwd):$(pwd)" \
  $PY -m nicegui_app.main > /tmp/anansi-tier0.log 2>&1 &
sleep 12
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8599/healthz
curl -s http://localhost:8599/settings | head -c 400
kill %1
```

Expected: `200` from `/healthz`, and the `/settings` response is an HTML document rather than a redirect to `/login` or a traceback. Record the actual output. If the page 500s, capture the traceback from `/tmp/anansi-tier0.log` — that is the real Tier 0 requirement list and the design must be corrected to match.

- [ ] **Step 2: Verify Tier 0′ (real auth configuration, no bypass)**

Repeat with `GRID_DESIGN_DEV_NO_AUTH` unset and these set to dummy values:
`GOOGLE_CLIENT_ID=test-id`, `GOOGLE_CLIENT_SECRET=test-secret`,
`AUTH_REDIRECT_URI=http://localhost:8599/oauth2callback`,
`ALLOWED_VIEWER_EMAILS=you@example.com`.

Expected: `/healthz` returns 200; `/settings` redirects to `/login`; `/login` renders the sign-in button and does **not** show "Google OAuth is not configured on this server". A real sign-in cannot be completed with dummy credentials — reaching a correctly-configured login page is the assertion.

- [ ] **Step 3: Confirm the readiness panel agrees with the tiers**

```bash
cd chat_orchestrator && $PY -c "
from shared.config import flag_registry as fr
for s in fr.readiness(env={'GRID_DESIGN_DEV_NO_AUTH': '1'}):
    print(('OK ' if s.satisfied else '-- '), s.capability.key, s.missing)
"
```

Expected: `admin_login` satisfied; `bot_replies` and `system_instructions` unsatisfied with their full missing lists. This is exactly what a fresh operator would see, so read it as a fresh operator would and fix any wording that would confuse them.

- [ ] **Step 4: Write the verification record**

Create `docs/superpowers/plans/2026-07-28-settings-minimum-env-verification.md` containing, for each tier: the exact environment used, the exact commands, the observed HTTP codes and any traceback, and a final confirmed variable list. Where observation contradicted the design, state the contradiction plainly and note which one is now authoritative.

- [ ] **Step 5: Commit**

`docs/superpowers/plans/` is gitignored, exactly like `tests/`. A plain `git add`
on a new file there is a silent no-op — the commit succeeds and the document
never reaches the remote. Force-add it and confirm:

```bash
git add -f docs/superpowers/plans/2026-07-28-settings-minimum-env-verification.md
git status --short   # confirm the file is staged
git commit -m "docs: record verified minimum environment per deployment tier"
git show --stat HEAD | grep verification   # confirm it actually landed
```

---

## Task 11: Documentation

**Files:**
- Modify: `README.md`
- Modify: `.do/app.example.yaml`
- Modify: `anansi_app/.env.example`

- [ ] **Step 1: Rewrite the README "Environment Variables" section**

The section beginning at the `### Environment Variables` heading currently documents variables that are read nowhere in the codebase: `JIRA_DOMAIN`, `JIRA_EMAIL`, `SUPABASE_ACTIONS_ENABLED`, `TIMESCALE_ACTIONS_ENABLED`, and `TIMESCALE_CONNECTION_STRING`. Verify each with `grep -rn 'JIRA_DOMAIN' --include='*.py' .` before deleting it, then replace the whole section with the four tiers as confirmed in Task 10.

Structure it as: a one-paragraph explanation that tunable flags live in the registry and are set through the settings UI while credentials come from the host environment; then Tier 0 / Tier 0′ / Tier 1 / Tier 2 / Tier 3 as fenced `bash` blocks with a sentence each; then a pointer to `shared/config/flags.env.example` for the generated list of every tunable. Use the real names Jira uses in this codebase: `JIRA_BASE_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`.

- [ ] **Step 2: Add the newly registered flags to the DigitalOcean example spec**

`.do/app.example.yaml` should carry the newly registered flags that a real deployment would want to set explicitly rather than inherit: `ORGANIZATION_NAME`, `STAFF_ORG_NAME`, `METRICS_TIMEZONE`, `AFTER_HOURS_START_HOUR`, `JIRA_ISSUE_TYPE`. Put each in the block matching its registry `scope` — `SERVICE_BOT`-scoped flags go under the `anansi-bot` service `envs[]`, the rest under the app-level `envs[]`. Do not add the `*_ACTIONS_ENABLED` flags; their `false` default is the safe one and listing fourteen of them would bury the spec.

- [ ] **Step 3: Fix the stale entries in `anansi_app/.env.example`**

It lists `GEMINI_MODEL`, `LLM_PROVIDER`, `OPENROUTER_*`, `GRAFANA_*`, `STAFF_ORG_ID` and `DEFAULT_TIMEZONE`, all of which are now registry-managed and documented in `flags.env.example`. Remove them and add a comment pointing at `shared/config/flags.env.example`. Keep only what this app genuinely needs from the host: the `AUTH_*`/`GOOGLE_CLIENT_*` OAuth block, `GOOGLE_API_KEY`, `SUPABASE_*`, `TELEGRAM_*`, `API_KEY`, `CHAT_ORCHESTRATOR_URL`, `ANANSI_BOT_HEALTH_URL`, `MINI_APP_BASE_URL`, `DIGITALOCEAN_*`, `GRID_DESIGN_DEV_NO_AUTH`, `LANGFUSE_DASHBOARD_URL`.

- [ ] **Step 4: Commit**

```bash
git add README.md .do/app.example.yaml anansi_app/.env.example
git commit -m "docs: document the tiered minimum environment and drop stale variables"
```

---

## Task 12: Full verification

**Files:** none

- [ ] **Step 1: Run every affected suite**

```bash
cd chat_orchestrator && $PY -m pytest tests -q && $PY -m pytest ../shared -q && cd ..
PYTHONPATH="$PWD:$PWD/mcp_servers" $PY -m pytest mcp_servers/tests -q
PYTHONPATH="$PWD:$PWD/anansi_app" $PY -m pytest anansi_app/tests -q
```

Expected: all green. The mcp_servers suite matters because Task 3 changed `grafana_mcp_server.py`.

- [ ] **Step 2: Run pre-commit across the whole tree**

```bash
pre-commit run --all-files
```

This is the only check that catches a new test file missing from the commit. `git status`, `ruff check .` and `pytest` all pass while a `tests/` file is silently untracked — see `CLAUDE.md` for the incident this rule came from.

- [ ] **Step 3: If the test-wiring hook reports untracked test files**

Vet each one for operator data, then force-add it and re-run:

```bash
git add -f <path>
pre-commit run --all-files
```

- [ ] **Step 4: Confirm the generated example file is current**

```bash
$PY -m shared.config.flag_registry > shared/config/flags.env.example
git diff --exit-code shared/config/flags.env.example
```

Expected: no diff. A diff means an earlier task edited the registry without regenerating.

- [ ] **Step 5: Confirm what actually got committed**

```bash
git log --oneline main..HEAD
git diff --stat main..HEAD
```

Verify all five new test files appear in the diffstat: `test_flag_readiness.py`, `test_settings_service_provenance.py`, `test_settings_widgets.py`, `test_settings_readiness_panel.py`, `test_settings_page.py`. Also verify `2026-07-28-settings-minimum-env-verification.md` appears — both `tests/` and `docs/superpowers/plans/` are gitignored, so either can be silently dropped by a plain `git add`.

- [ ] **Step 6: Report**

State plainly: which suites ran and their counts, whether `pre-commit run --all-files` passed, and what Task 10's boot verification actually observed for each tier. If any tier's confirmed variable list differs from this plan's design, say so explicitly rather than quietly conforming.
