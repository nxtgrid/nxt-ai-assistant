"""Central registry for Anansi feature flags and tunable settings.

This module is the **single source of truth** for every operator-tunable
environment variable in Anansi (feature toggles, model knobs, layout
parameters, MCP server enables, etc.).

Credentials fall into two classes. *App-owned* integration secrets
(``GRAFANA_PASSWORD``, ``OPENROUTER_API_KEY``, ``TAVILY_API_KEY``) are
registered editable and are set through the settings UI, which writes them to
the deployment backend as secrets. *Host-owned* credentials and connection
strings (``AUTH_DB_*``, ``CHAT_DB_*``, ``DIGITALOCEAN_API_TOKEN`` …) are
registered ``editable=False, document=False`` so they can never be written
back; they exist here only so the deployment-readiness view can report
whether they are set.

Why this exists
---------------
Flag metadata used to be duplicated across three places that drifted apart:

* ``orchestrator/config/settings.py`` (pydantic ``BaseSettings``)
* ``mcp_servers/shared_code/config/action_flags.py`` (raw ``os.getenv``)
* ``anansi_app/services/settings_service.py`` (hand-maintained Python sets,
  including *two* copies of ``SERVICE_SPECIFIC_SETTINGS``)

Consumers now derive their behaviour from this registry, and a sync test
(`tests/test_flag_registry.py`) keeps ``shared/config/flags.env.example`` and the
settings service in lock-step with it.

Regenerating the env example
----------------------------
After editing ``FLAGS`` regenerate the committed example file::

    python -m shared.config.flag_registry > shared/config/flags.env.example
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

SCOPE_GLOBAL = "global"


class FlagType(str, Enum):
    """Supported value types for a flag (controls coercion and rendering)."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    JSON = "json"  # value is a JSON string; stored/returned verbatim


_TRUTHY = {"true", "1", "yes", "on"}


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


@dataclass(frozen=True)
class Flag:
    """Declarative description of a single tunable env var.

    Attributes:
        name: The environment variable name (verbatim, case-sensitive).
        type: How the raw string value is coerced.
        default: Default value as it would appear in the environment (a string,
            or a native bool/int/float — always rendered/coerced consistently).
        description: One-line human description (used in the generated example).
        scope: ``"global"`` or a DigitalOcean service name. Non-global flags are
            written to that service's ``envs[]`` rather than the app globals.
        editable: If False the flag is read-only in the settings UI and is never
            written back to the deployment backend (the ``DO_NOT_SAVE_TO_DO`` set).
        secret: Sensitive value — rendered with an empty placeholder in examples
            and (on DigitalOcean) stored with ``type: SECRET``.
        required: Deployment cannot function correctly until this is set. Surfaced
            by :func:`validate_required` for fail-loud startup checks.
        show_in_settings: If False the flag is excluded from the settings UI
            dictionary (routing-only or deployment-level flags).
        document: If False the flag is omitted from the generated example file
            (e.g. large machine-managed JSON blobs).
        group: Settings-page section id, drawn from :data:`GROUPS`.
        label: Human label for the settings UI; falls back to ``name`` via
            :attr:`display_label` when unset.
        choices: When set, the settings UI renders a select restricted to these
            values instead of free text, and a value outside this set is
            rejected on save.
        advanced: If True the flag is collapsed under "Show advanced" within
            its group instead of always visible.
        restart_required: If True, changing this flag needs a bot restart to
            take effect; the settings UI offers "Save & Restart" instead of
            "Save" when any changed flag sets this.
        depends_on: Name of a boolean flag this one (or its whole group, when
            every flag in a group shares the same value here) is inert without.
            The settings UI hides a dependent flag/group while the dependency
            is false.
        minimum: Inclusive lower bound for INT/FLOAT flags (UI + save validation).
        maximum: Inclusive upper bound for INT/FLOAT flags (UI + save validation).
        set_via: For read-only flags, a short hint about where an operator sets
            this value (e.g. "Set in the DigitalOcean console.").
        model_picker: When set to ``"gemini"``, the settings UI renders this
            flag as a select populated from the live Gemini model list
            (fetched via the Google API) instead of free text. Unlike
            :attr:`choices`, the option list isn't known statically. Distinct
            from the provider-aware "role model" fields (``MODEL_THINKING``,
            ``MODEL_FAST``, ``MODEL_LITE``, ``FALLBACK_MODEL``), which switch
            between Gemini and OpenRouter model lists based on ``LLM_PROVIDER``.
    """

    name: str
    type: FlagType
    default: Any
    description: str
    scope: str = SCOPE_GLOBAL
    editable: bool = True
    secret: bool = False
    required: bool = False
    show_in_settings: bool = True
    document: bool = True
    group: str = "bot_control"
    label: str = ""
    choices: Optional[tuple[str, ...]] = None
    advanced: bool = False
    restart_required: bool = False
    depends_on: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    set_via: Optional[str] = None
    model_picker: Optional[str] = None

    def coerce(self, raw: Optional[str]) -> Any:
        """Coerce a raw string (or None) to this flag's typed value."""
        if raw is None:
            return self.coerce(_as_str(self.default)) if self.default is not None else None
        if self.type is FlagType.BOOL:
            return raw.strip().lower() in _TRUTHY
        if self.type is FlagType.INT:
            return int(raw)
        if self.type is FlagType.FLOAT:
            return float(raw)
        if self.type is FlagType.JSON:
            # Validate but return the verbatim string — downstream callers parse it.
            json.loads(raw)
            return raw
        return raw

    @property
    def default_str(self) -> str:
        """The default rendered as an environment-string."""
        return _as_str(self.default)

    @property
    def display_label(self) -> str:
        """Human label for the settings UI, falling back to the env var name."""
        return self.label or self.name


def _as_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _b(name: str, default: bool, description: str, **kw: Any) -> Flag:
    return Flag(name, FlagType.BOOL, default, description, **kw)


def _i(name: str, default: int, description: str, **kw: Any) -> Flag:
    return Flag(name, FlagType.INT, default, description, **kw)


def _f(name: str, default: float, description: str, **kw: Any) -> Flag:
    return Flag(name, FlagType.FLOAT, default, description, **kw)


def _s(name: str, default: str, description: str, **kw: Any) -> Flag:
    return Flag(name, FlagType.STR, default, description, **kw)


def _j(name: str, default: str, description: str, **kw: Any) -> Flag:
    return Flag(name, FlagType.JSON, default, description, **kw)


# Canonical list of MCP servers that can be toggled with {NAME}_ENABLED.
#
# This is the single source of truth for the set. It used to be written out
# three times -- here, in mcp_servers/shared_code/config/action_flags.py as
# CONFIGURABLE_SERVERS, and implicitly in server_registry.SERVER_METADATA --
# and they drifted: all three still listed "codebase" after that server was
# deleted. action_flags now imports this list, and
# mcp_servers/tests/test_server_list_sync.py asserts SERVER_METADATA matches.
#
# Names are lowercase to match server_registry keys; the env var name is the
# uppercase form (grid_design -> GRID_DESIGN_ENABLED).
MCP_SERVER_NAMES: List[str] = [
    "equipment_diagnostics",
    "jira",
    "meters",
    "equipment_control",
    "payment_processor",
    "customer",
    "grafana",
    "schedule",
    "meta",
    "grid_design",
    "solar",
    "knowledge",
    "reference",
]


def _mcp_enable_flags() -> List[Flag]:
    return [
        _b(
            f"{srv.upper()}_ENABLED",
            True,
            f"Enable the {srv.replace('_', ' ')} MCP server "
            "(disabling hides all of its tools).",
            group="tools",
        )
        for srv in MCP_SERVER_NAMES
    ]


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


def _connection(name: str, description: str, set_via: str = "", secret: bool = True) -> Flag:
    """A credential or endpoint this deployment depends on but does not manage.

    Status-only: shown in the Connections group as configured/not configured so
    the readiness panel can explain what a deployment is still missing. Never
    editable, so it can never be written back to the deployment.
    """
    return Flag(
        name,
        FlagType.STR,
        "",
        description,
        editable=False,
        secret=secret,
        document=False,
        group="connections",
        set_via=set_via or "Set in the deployment environment (DigitalOcean app spec or .env).",
    )


# ---------------------------------------------------------------------------
# The registry. Order here is the order used when rendering the example file.
# ---------------------------------------------------------------------------
_FLAGS: List[Flag] = [
    # --- Bot Control --------------------------------------------------------
    _b("BOT_ENABLED", True, "Master switch for the Telegram bot.", group="bot_control"),
    _s(
        "LOG_LEVEL",
        "INFO",
        "Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
        group="bot_control",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    ),
    _i(
        "MAX_TOOL_ROUNDS",
        5,
        "Maximum sequential tool-call rounds per turn.",
        group="bot_control",
        minimum=1,
        maximum=20,
    ),
    _b(
        "ALLOW_PARALLEL_CALLS",
        True,
        "Allow Gemini to request parallel tool calls.",
        group="bot_control",
    ),
    # --- AI Models & Providers -----------------------------------------------
    _s(
        "LLM_PROVIDER",
        "gemini",
        "Generation provider: 'gemini' for direct Google Gemini or 'openrouter' for OpenRouter.",
        group="models",
        label="Generation provider",
        choices=("gemini", "openrouter"),
    ),
    _s(
        "MODEL_THINKING",
        "gemini-pro-latest",
        "Model for complex-reasoning tasks (deep analysis, multi-step agent work).",
        group="models",
        label="Thinking-tier model",
    ),
    _s(
        "MODEL_FAST",
        "gemini-flash-latest",
        "Model for the general-purpose default tier.",
        group="models",
        label="Fast-tier model",
    ),
    _s(
        "MODEL_LITE",
        "gemini-2.5-flash-lite",
        "Model for lightweight/high-volume tasks (classification, verification, routing).",
        group="models",
        label="Lite-tier model",
    ),
    _s(
        "FALLBACK_MODEL",
        "gemini-2.5-flash-lite",
        "Fallback generation model id, used when the primary call fails. Not a quality tier.",
        group="models",
        label="Fallback model",
    ),
    _s(
        "EMBEDDING_MODEL",
        "gemini-embedding-001",
        "Embedding model used for RAG ingestion/retrieval.",
        editable=False,
        group="models",
        label="Embedding model",
        set_via="Changing this invalidates every stored embedding; re-ingest required.",
    ),
    _f(
        "GEMINI_TEMPERATURE",
        0.2,
        "Generation temperature where supported by the selected model.",
        group="models",
        label="Temperature",
        minimum=0.0,
        maximum=2.0,
    ),
    _i(
        "GEMINI_MAX_OUTPUT_TOKENS",
        8192,
        "Max output tokens for the primary model.",
        group="models",
        label="Main model max output tokens",
        minimum=64,
        maximum=65536,
    ),
    _i(
        "GEMINI_LITE_MAX_OUTPUT_TOKENS",
        1024,
        "Max output tokens for the lite/verification model.",
        editable=False,
        group="models",
        label="Lite model max output tokens",
        set_via="Set in the deployment environment.",
    ),
    _s(
        "OPENROUTER_MODEL",
        "google/gemini-2.5-flash",
        "Legacy OpenRouter default model fallback. Role-specific model flags are used by the orchestrator.",
        show_in_settings=False,
        document=False,
        group="models",
    ),
    _s(
        "OPENROUTER_PROVIDER_ORDER",
        "",
        "Optional comma-separated OpenRouter provider slugs to try first, e.g. 'google-vertex' for Google Vertex BYOK.",
        group="models",
    ),
    _b(
        "OPENROUTER_ALLOW_FALLBACKS",
        True,
        "Allow OpenRouter to fall back to other endpoints when provider routing is configured.",
        group="models",
    ),
    _b(
        "OPENROUTER_REQUIRE_PARAMETERS",
        False,
        "Require OpenRouter endpoints that support the requested parameters/tool schema.",
        group="models",
    ),
    _i(
        "GEMINI_THINKING_BUDGET",
        4096,
        "Thinking-token budget for Gemini 2.5 models (-1 dynamic, 0 off, >0 cap).",
        group="models",
        minimum=-1,
        maximum=24576,
        advanced=True,
    ),
    _b(
        "GOOGLE_SEARCH_GROUNDING",
        True,
        "Allow Google Search grounding for staff users.",
        group="models",
    ),
    # --- Conversation Experience ----------------------------------------------
    _b(
        "WORKFLOW_PARAMETER_CONFIRMATION",
        True,
        "Prompt the user to confirm editable workflow parameters.",
        group="conversation",
    ),
    _b(
        "INLINE_BUTTONS_ENABLED",
        False,
        "Telegram inline buttons for decision prompts.",
        group="conversation",
    ),
    _b(
        "PROCEDURE_BUTTONS_ENABLED",
        False,
        "Procedure buttons in customer support chats.",
        group="conversation",
    ),
    _b(
        "MINI_APP_FORMS_ENABLED",
        False,
        "Telegram WebApp popups for workflow parameters.",
        group="conversation",
        restart_required=True,
    ),
    _b(
        "CONTEXT_FILTER_ENABLED",
        False,
        "Conversation context filtering.",
        group="conversation",
    ),
    _b(
        "THREAD_DISENTANGLEMENT_ENABLED",
        False,
        "Multi-thread conversation disentanglement.",
        group="conversation",
    ),
    _i(
        "ACTIVE_THREAD_WINDOW_MINUTES",
        60,
        "Window (minutes) a thread stays active.",
        group="conversation",
        depends_on="THREAD_DISENTANGLEMENT_ENABLED",
    ),
    _b(
        "CONVERSATION_SUMMARY_ENABLED",
        False,
        "Rolling conversation summarization.",
        group="conversation",
    ),
    _i(
        "AWAITING_INPUT_TIMEOUT_MINUTES",
        180,
        "Timeout (minutes) for an expert awaiting user input.",
        group="conversation",
    ),
    _b(
        "VERIFICATION_ENABLED",
        False,
        "Enable LLM-as-judge verification of outgoing customer messages.",
        group="conversation",
    ),
    _s(
        "VERIFICATION_DOC_ID",
        "",
        "Google Doc id holding verification criteria.",
        group="conversation",
        depends_on="VERIFICATION_ENABLED",
    ),
    _b(
        "LANGFUSE_ENABLED",
        False,
        "Enable Langfuse LLM observability tracing.",
        group="conversation",
        advanced=True,
    ),
    _b(
        "LOOP_DETECTION_ENABLED",
        True,
        "Detect and break repeated identical tool calls within a turn.",
        group="conversation",
        advanced=True,
    ),
    _i(
        "LOOP_DETECTION_THRESHOLD",
        2,
        "Identical repeats before a tool call is treated as a loop.",
        group="conversation",
        depends_on="LOOP_DETECTION_ENABLED",
        minimum=2,
        maximum=10,
        advanced=True,
    ),
    _i(
        "MULTI_SITE_MAX_CONCURRENCY",
        5,
        "Maximum sites evaluated in parallel by multi-site workflows.",
        group="conversation",
        minimum=1,
        maximum=20,
        advanced=True,
    ),
    # --- Escalations & Ticketing ----------------------------------------------
    _s(
        "TICKET_BACKEND_OVERRIDE",
        "auto",
        "Which ticket backend customer escalations use: 'auto' (Jira if configured "
        "and healthy, else internal), 'jira' (Jira if creds present, else internal -- "
        "never hard-fails), or 'internal' (always internal). Ops kill-switch / forcing.",
        group="ticketing",
        label="Customer escalation ticket backend",
        choices=("auto", "jira", "internal"),
    ),
    _s(
        "NOTIFY_TICKETS_BACKEND",
        "internal",
        "Which ticket backend /notify-originated tickets use: 'internal' (default -- "
        "ops alerts from Grafana/n8n/VRM never land in the Jira OPS project) or 'auto' "
        "(Jira if configured and healthy, else internal, same as customer escalations).",
        group="ticketing",
        label="Alert (/notify) ticket backend",
        choices=("internal", "auto"),
    ),
    _s(
        "INTERNAL_TICKET_PREFIX",
        "TKT",
        "Prefix for internal ticket refs allocated when the internal backend is used, "
        "e.g. 'TKT' -> 'TKT-000123'.",
        group="ticketing",
    ),
    _s(
        "JIRA_PROJECT_KEY",
        "OPS",
        "Jira project key used when the Jira ticket backend is selected.",
        group="ticketing",
    ),
    _i(
        "JIRA_HEALTHCHECK_TTL_SECONDS",
        60,
        "How long JiraTicketBackend caches its Jira health probe result before "
        "re-checking (seconds).",
        group="ticketing",
        advanced=True,
    ),
    _b(
        "ALWAYS_FILE_ESCALATION_AS_TICKET",
        False,
        "File a ticket automatically for every new escalation, same as the existing "
        "after-hours behavior, instead of showing staff the Track button to opt in. "
        "Off (default) keeps today's staff-triggered tracking.",
        group="ticketing",
        label="Always file escalations as tickets",
    ),
    _b(
        "ALERT_CORRELATION_ENABLED",
        False,
        "Master switch for /notify ticket_id='auto' correlation. Off: 'auto' behaves "
        "exactly like '' (plain create) -- the kill switch can never drop an alert.",
        group="ticketing",
        label="Group related alerts onto one ticket",
    ),
    _b(
        "ALERT_CASCADE_MERGE_ENABLED",
        False,
        "Merge a cascading equipment failure (e.g. battery/BMS communication loss "
        "causing an inverter protective shutdown) onto the root-cause ticket instead "
        "of filing it separately. Off (default): a same-grid, different-equipment-kind "
        "alert always files its own ticket, even when the correlation prompt's "
        "failure-topology guidance says it is a likely symptom.",
        group="ticketing",
        label="Merge cascading equipment failures onto the root-cause ticket",
        depends_on="ALERT_CORRELATION_ENABLED",
    ),
    _i(
        "URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS",
        3,
        "Maximum seconds to wait for the one live VRM inverter-output lookup before "
        "reporting it as unavailable.",
        group="ticketing",
        depends_on="ALERT_CORRELATION_ENABLED",
        advanced=True,
    ),
    _b(
        "JIRA_SWEEP_ENABLED",
        True,
        "Run the periodic Jira sweep that reconciles ticket state.",
        group="ticketing",
    ),
    _s(
        "JIRA_ISSUE_TYPE",
        "Task",
        "Jira issue type used for tickets when the project offers no better match.",
        group="ticketing",
    ),
    _b(
        "STARTUP_RECOVERY_ENABLED",
        True,
        "Scan for orphaned work on startup. Must be false when running more than "
        "one orchestrator instance.",
        group="ticketing",
        advanced=True,
    ),
    _b(
        "CANONICAL_ESCALATION_READS_ENABLED",
        False,
        "Read escalation state from the canonical `escalations` table instead "
        "of legacy escalation_mappings/chat_sessions columns (ticket-schema "
        "cutover Phase 2). Off until escalation receipts from the Phase 1 "
        "dual-write have covered every escalation open at deploy time -- "
        "flipping this on too early can make an escalation opened before "
        "cutover look unescalated.",
        group="ticketing",
        label="Read escalations from canonical tables",
        advanced=True,
    ),
    _b(
        "STOP_LEGACY_ESCALATION_WRITES",
        False,
        "Stop writing escalation_mappings/chat_sessions.is_escalated "
        "(ticket-schema cutover Phase 3) -- escalation creation, claim, "
        "close, reopen, and the ticket-filing sweep all become canonical-"
        "only. Requires CANONICAL_ESCALATION_READS_ENABLED to have been on "
        "and verified first: once this is on, canonical-reads consumers "
        "stop falling back to legacy (which is now guaranteed stale) on an "
        "inconclusive lookup, rather than risk serving stale data.",
        group="ticketing",
        label="Stop legacy escalation writes",
        advanced=True,
        depends_on="CANONICAL_ESCALATION_READS_ENABLED",
    ),
    # --- Alerts & Notifications ------------------------------------------------
    _b(
        "NOTIFY_ENDPOINT_ENABLED",
        False,
        "Accept external notifications on POST /chat/notify and forward them to "
        "Telegram (n8n / VRM / Grafana passthrough). When off the endpoint returns 503.",
        group="alerts",
    ),
    _s(
        "ESCALATION_TELEGRAM_CHAT_ID",
        "",
        "Telegram chat id for the escalation group.",
        editable=False,
        group="alerts",
        set_via="Set in the deployment environment; changing it re-points Telegram delivery.",
    ),
    _s(
        "DEBUG_TELEGRAM_CHAT_ID",
        "",
        "Telegram chat id for debug output.",
        editable=False,
        group="alerts",
        set_via="Set in the deployment environment; changing it re-points Telegram delivery.",
    ),
    _s(
        "NO_REPLY_CHAT_IDS",
        "",
        "Comma-separated Telegram chat ids the bot never replies in.",
        group="alerts",
    ),
    _s(
        "AFTER_HOURS_TIMEZONE",
        "",
        "Timezone for after-hours escalation logic. Empty falls back to DEFAULT_TIMEZONE.",
        group="alerts",
    ),
    _i(
        "AFTER_HOURS_START_HOUR",
        19,
        "Hour (0-23) when after-hours escalation handling begins.",
        group="alerts",
        minimum=0,
        maximum=23,
    ),
    # --- Tools & Integrations ---------------------------------------------------
    *_mcp_enable_flags(),
    *_mcp_actions_flags(),
    _j(
        "MCP_DISABLED_TOOLS",
        "[]",
        'JSON array of "server:tool" strings to disable individual tools.',
        group="tools",
        label="Individually disabled tools",
        advanced=True,
    ),
    # --- Knowledge & RAG ---------------------------------------------------------
    _b(
        "rag__enabled",
        False,
        "Enable retrieval-augmented generation.",
        group="knowledge",
        label="Enable RAG",
    ),
    _i(
        "rag__top_k",
        5,
        "Number of RAG chunks to retrieve per query.",
        group="knowledge",
        label="Chunks retrieved per query",
        depends_on="rag__enabled",
        minimum=1,
        maximum=50,
    ),
    # --- Grafana Dashboards -------------------------------------------------------
    _s("GRAFANA_URL", "http://localhost:3000", "Grafana base URL.", group="grafana"),
    _s("GRAFANA_USERNAME", "", "Grafana username.", group="grafana"),
    _s("GRAFANA_PASSWORD", "", "Grafana password.", secret=True, group="grafana"),
    _s(
        "GRAFANA_FOLDER_NAME",
        "",
        "Grafana folder to source dashboards from.",
        group="grafana",
    ),
    _s(
        "GRAFANA_ENABLED_DASHBOARDS",
        "",
        "Comma-separated dashboard UIDs to expose as tools.",
        group="grafana",
        depends_on="GRAFANA_ENABLED",
    ),
    _s(
        "GRAFANA_ENABLED_PANELS",
        "",
        "Comma-separated list of enabled Grafana panels.",
        group="grafana",
        depends_on="GRAFANA_ENABLED",
    ),
    _i(
        "GRAFANA_SYNC_HOUR",
        2,
        "Hour of day (0-23) to sync Grafana panel metadata.",
        group="grafana",
        depends_on="GRAFANA_ENABLED",
        restart_required=True,
        minimum=0,
        maximum=23,
    ),
    _b(
        "GRAFANA_FORCE_FULL_REINDEX",
        False,
        "Force a full Grafana panel reindex on next sync.",
        group="grafana",
        depends_on="GRAFANA_ENABLED",
    ),
    _j(
        "GRAFANA_PANELS_METADATA",
        "{}",
        "Machine-managed Grafana panel metadata (synced by scripts, not the UI).",
        editable=False,
        document=False,
        group="grafana",
        depends_on="GRAFANA_ENABLED",
        set_via="Machine-managed by the Grafana indexer; use Sync Now.",
    ),
    _j(
        "GRAFANA_AVAILABLE_DASHBOARDS",
        "{}",
        "Machine-managed Grafana dashboard catalog (synced by scripts, not the UI).",
        editable=False,
        document=False,
        group="grafana",
        depends_on="GRAFANA_ENABLED",
        set_via="Machine-managed by the Grafana indexer; use Sync Now.",
    ),
    _i(
        "GRAFANA_QUERY_TIMEOUT",
        180,
        "Seconds allowed for a Grafana panel query.",
        group="grafana",
        depends_on="GRAFANA_ENABLED",
        minimum=1,
        maximum=600,
        advanced=True,
    ),
    _i(
        "GRAFANA_METADATA_TIMEOUT",
        30,
        "Seconds allowed for a Grafana metadata fetch.",
        group="grafana",
        depends_on="GRAFANA_ENABLED",
        minimum=1,
        maximum=600,
        advanced=True,
    ),
    _i(
        "GRAFANA_VARIABLE_TIMEOUT",
        60,
        "Seconds allowed for a Grafana variable lookup.",
        group="grafana",
        depends_on="GRAFANA_ENABLED",
        minimum=1,
        maximum=600,
        advanced=True,
    ),
    # --- Site Layout Engine -------------------------------------------------------
    _f(
        "LAYOUT_POLE_SPACING_M",
        45.0,
        "Spacing between poles along roads (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_MAX_DROP_DISTANCE_M",
        40.0,
        "Max drop-cable length to a building (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_TARGET_COVERAGE_PCT",
        90.0,
        "Target building coverage (%).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
        minimum=0.0,
        maximum=100.0,
    ),
    _f(
        "LAYOUT_SQM_PER_KWP",
        15.5,
        "Roof area per kWp (m²/kWp).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_KWP_PER_BUILDING",
        0.25,
        "Estimated kWp per building.",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_MIN_ESTIMATED_KWP",
        30.0,
        "Minimum estimated site kWp.",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_BUILDING_BUFFER_M",
        15.0,
        "Buffer around buildings (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_SITE_SETBACK_M",
        5.0,
        "Setback from site boundary (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_ROAD_SETBACK_M",
        5.0,
        "Setback from roads (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_CORRIDOR_CLEARANCE_M",
        10.0,
        "Corridor clearance (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_CANOPY_THRESHOLD_M",
        5.0,
        "Tree-canopy avoidance threshold (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_MIN_CANDIDATE_SEPARATION_M",
        100.0,
        "Min separation between candidate sites (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _i(
        "LAYOUT_MAX_CANDIDATES",
        3,
        "Max candidate sites to evaluate.",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_POLE_DEDUP_DISTANCE_M",
        5.0,
        "Distance under which poles are deduplicated (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_SNAP_NODE_TOLERANCE_M",
        1.0,
        "Node-snapping tolerance (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_MERGE_GAP_THRESHOLD_M",
        5.0,
        "Gap under which segments merge (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_REDISTRIBUTE_GAP_MAX_M",
        10.0,
        "Max gap for pole redistribution (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_LIGHTNING_RADIUS_M",
        13.5,
        "Lightning-protection radius (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_KW_PER_HOUSEHOLD",
        0.0,
        "Explicit kW per household. 0 lets the pipeline derive it.",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_MAX_BRIDGE_DISTANCE_M",
        200.0,
        "Longest gap the distribution network may bridge (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_PATH_REDUNDANCY_DISTANCE_M",
        22.5,
        "Distance under which parallel road paths are treated as redundant (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_PATH_WEIGHT_PENALTY",
        3.0,
        "Routing penalty applied to building-adjacent paths.",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_PLANT_CONNECT_DISTANCE_M",
        150.0,
        "Search radius when connecting the plant to the network (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _i(
        "LAYOUT_PLANT_CONNECT_K",
        5,
        "Number of candidate connection points evaluated for the plant.",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        minimum=1,
        advanced=True,
    ),
    _f(
        "LAYOUT_POWER_FACTOR",
        0.95,
        "Power factor used to convert kVA to kW.",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        minimum=0.1,
        maximum=1.0,
        advanced=True,
    ),
    _f(
        "LAYOUT_ROAD_CLIP_BUFFER_M",
        100.0,
        "Buffer around the site used to clip the road network (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    _f(
        "LAYOUT_WATERWAY_BUFFER_M",
        200.0,
        "Exclusion buffer around waterways (m).",
        group="layout",
        depends_on="GRID_DESIGN_ENABLED",
        advanced=True,
    ),
    # --- Documents & Templates -----------------------------------------------------
    _s(
        "EXPERT_INSTRUCTIONS_DOC_ID",
        "",
        "Google Doc id holding expert/workflow definitions.",
        group="documents",
    ),
    _s(
        "CUSTOMER_SUPPORT_DOC_ID",
        "",
        "Google Doc id for customer-mode system instructions.",
        editable=False,
        group="documents",
        set_via="Set in the deployment environment alongside GOOGLE_SERVICE_ACCOUNT_JSON.",
    ),
    _s(
        "STAFF_SUPPORT_DOC_ID",
        "",
        "Google Doc id for staff-mode system instructions.",
        editable=False,
        group="documents",
        set_via="Set in the deployment environment alongside GOOGLE_SERVICE_ACCOUNT_JSON.",
    ),
    _s(
        "TROUBLESHOOTING_PROCEDURES_DOC_ID",
        "",
        "Google Doc id for shared troubleshooting procedures.",
        editable=False,
        group="documents",
        set_via="Set in the deployment environment alongside GOOGLE_SERVICE_ACCOUNT_JSON.",
    ),
    _s(
        "PROMPT_EDITORS_OPS",
        "",
        "Comma-separated emails in the ops prompt-editor group.",
        editable=False,
        group="documents",
        set_via="Set in the deployment environment alongside the other access whitelists.",
    ),
    _s(
        "PROMPT_EDITORS_ENG",
        "",
        "Comma-separated emails in the engineering prompt-editor group.",
        editable=False,
        group="documents",
        set_via="Set in the deployment environment alongside the other access whitelists.",
    ),
    _s(
        "PROMPT_ADMINS",
        "",
        "Comma-separated emails with full access to every prompt.",
        editable=False,
        group="documents",
        set_via="Set in the deployment environment alongside the other access whitelists.",
    ),
    # These MUST stay show_in_settings=True (the default). The settings page
    # renders them as editable text inputs; when show_in_settings is False the
    # overlay in get_current_settings drops their real DO values, the inputs
    # render blank, and the next "Save changes" writes the blanks back to the
    # live spec — silently wiping the IDs and breaking the whole LPP workflow.
    _s(
        "LPP_TEMPLATE_ID",
        "",
        "Google Slides template id for LPP output.",
        group="documents",
    ),
    _s(
        "QGIS_TEMPLATE_FILE_ID",
        "",
        "QGIS template file id for site layouts.",
        group="documents",
    ),
    _s(
        "LPP_OUTPUT_FOLDER_ID",
        "",
        "Drive folder id for LPP output.",
        group="documents",
    ),
    _s(
        "NIGERIA_IMPORT_TARIFF_SHEET_ID",
        "",
        "Sheet id for import tariff reference data.",
        group="documents",
    ),
    _s(
        "NIGERIA_IMPORT_STANDARDS_PDF_ID",
        "",
        "PDF id for import standards reference data.",
        group="documents",
    ),
    # --- Access Control -------------------------------------------------------------
    _s(
        "ALLOWED_VIEWER_EMAILS",
        "",
        "Comma-separated emails allowed to view the admin app.",
        group="access",
        label="Admin app access (email allow-list)",
    ),
    _s(
        "EQUIPMENT_CONTROL_ALLOWED_USERS",
        "",
        "Comma-separated emails allowed to issue equipment-control commands.",
        group="access",
    ),
    _s(
        "GRID_DESIGN_ALLOWED_USERS",
        "",
        "Comma-separated emails with view-only access to the Grid Design tables.",
        group="access",
    ),
    _s(
        "GRID_DESIGN_EDITORS",
        "",
        "Comma-separated emails allowed to edit all Grid Design tables except Procurements.",
        group="access",
    ),
    _s(
        "GRID_PROCUREMENT_EDITORS",
        "",
        "Comma-separated emails allowed to edit only the Procurements (Purchases/BoS) table.",
        group="access",
    ),
    _i(
        "STAFF_ORG_ID",
        2,
        "Organization id treated as internal staff (full tool access).",
        editable=False,
        group="access",
        label="Staff organization id",
        set_via="Set in the deployment environment.",
    ),
    _s(
        "STAFF_ORG_NAME",
        "Staff",
        "Display name for the internal staff organization.",
        group="access",
    ),
    _s(
        "MANAGED_GENERATION_COLUMN",
        "is_generation_managed_by_nxt_grid",
        "Grids-table column marking operator-managed generation. Interpolated into "
        "SQL, so use only valid PostgreSQL identifier characters.",
        group="access",
        advanced=True,
    ),
    # --- Metrics & Scheduling ---------------------------------------------------------
    _b(
        "METRICS_ENABLED",
        True,
        "Enable scheduled metrics collection.",
        group="metrics",
        restart_required=True,
    ),
    _i(
        "METRICS_SCHEDULE_HOUR",
        9,
        "Hour of day (0-23) to run metrics collection.",
        group="metrics",
        depends_on="METRICS_ENABLED",
        restart_required=True,
        minimum=0,
        maximum=23,
    ),
    _s(
        "METRICS_TIMEZONE",
        "UTC",
        "Timezone used to schedule metrics collection.",
        group="metrics",
        depends_on="METRICS_ENABLED",
    ),
    # --- Deployment (read-only; set outside this app) --------------------------------
    _s(
        "DEFAULT_TIMEZONE",
        "UTC",
        "IANA timezone used as the fallback for display/scheduling when a grid "
        "has no timezone of its own (e.g. 'Africa/Lagos', 'UTC').",
        editable=False,
        group="deployment",
        set_via="Set in the deployment environment.",
    ),
    _s(
        "SETTINGS_BACKEND",
        "auto",
        "Backend for runtime settings management: 'auto' (DigitalOcean if "
        "DIGITALOCEAN_APP_ID + token are set, else env-file), 'digitalocean', or 'envfile'.",
        editable=False,
        group="deployment",
        choices=("auto", "digitalocean", "envfile"),
        set_via="Set in the deployment environment.",
    ),
    _s(
        "SETTINGS_FILE",
        ".env.settings",
        "Path the env-file settings backend reads/writes when not on DigitalOcean.",
        editable=False,
        group="deployment",
        set_via="Set in the deployment environment.",
    ),
    _s(
        "ORGANIZATION_NAME",
        "the operator",
        "Operator name shown in chart watermarks and equipment messages.",
        group="deployment",
    ),
    _s(
        "DOC_CODE_PREFIX",
        "DOC",
        "Prefix for generated document reference codes.",
        group="deployment",
    ),
    # --- Connections & Credentials (status-only; never written back) -----------------
    _connection(
        "GOOGLE_API_KEY",
        "Google AI Studio key for Gemini generation and embeddings.",
    ),
    _connection(
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "Service account JSON used to read Google Docs and Drive.",
    ),
    _connection("TELEGRAM_BOT_TOKEN", "Telegram bot token."),
    _connection("TELEGRAM_BOT_USERNAME", "Telegram bot username.", secret=False),
    _connection("CHAT_DB_URL", "Chat database (Supabase) URL.", secret=False),
    _connection("CHAT_DB_SERVICE_KEY", "Chat database service-role key."),
    _connection("AUTH_DB_HOST", "Auth database host."),
    _connection("API_KEY", "Shared key authenticating calls to the orchestrator."),
    _connection(
        "IDENTITY_ASSERTION_KEY",
        "Distinct from API_KEY -- lets the skill builder assert a caller's "
        "user_email when auth-DB lookup misses. Unset means nobody can.",
    ),
    _connection("SESSION_ID_SECRET", "Secret used to derive session identifiers."),
    _connection("CHAT_ORCHESTRATOR_URL", "Orchestrator chat endpoint.", secret=False),
    _connection(
        "DIGITALOCEAN_APP_ID",
        "DigitalOcean app id for the settings backend.",
        secret=False,
    ),
    _connection(
        "DIGITALOCEAN_API_TOKEN",
        "DigitalOcean API token for the settings backend.",
    ),
    _connection("JIRA_BASE_URL", "Jira Cloud base URL.", secret=False),
    _connection("JIRA_USERNAME", "Jira account email.", secret=False),
    _connection("JIRA_API_TOKEN", "Jira API token."),
    _connection("NOTIFY_SHARED_SECRET", "Shared secret required on POST /chat/notify."),
    _connection(
        "LANGFUSE_DASHBOARD_URL",
        "Langfuse dashboard link shown in the sidebar.",
        secret=False,
    ),
    # App-owned integration secrets: editable, unlike the host-owned credentials
    # above -- an operator turns these on from the settings UI.
    _s(
        "OPENROUTER_API_KEY",
        "",
        "OpenRouter API key.",
        group="connections",
        secret=True,
    ),
    _s(
        "TAVILY_API_KEY",
        "",
        "Tavily web-search key for the knowledge server.",
        group="connections",
        secret=True,
        depends_on="KNOWLEDGE_ENABLED",
    ),
]

# Name -> Flag, with a duplicate-name guard.
FLAGS: Dict[str, Flag] = {}
for _flag in _FLAGS:
    if _flag.name in FLAGS:
        raise ValueError(f"Duplicate flag registered: {_flag.name}")
    FLAGS[_flag.name] = _flag
del _flag


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def get_flag(name: str) -> Flag:
    """Return the :class:`Flag` for ``name`` or raise ``KeyError``."""
    return FLAGS[name]


def get(name: str, env: Optional[Mapping[str, str]] = None) -> Any:
    """Read ``name`` from the environment, coerced to its registered type."""
    flag = FLAGS[name]
    source = env if env is not None else os.environ
    return flag.coerce(source.get(name))


def service_specific_settings() -> Dict[str, str]:
    """Map of non-global flag name -> DigitalOcean service name."""
    return {f.name: f.scope for f in FLAGS.values() if f.scope != SCOPE_GLOBAL}


def non_editable_settings() -> set[str]:
    """Names that must never be written back to the deployment (read-only)."""
    return {f.name for f in FLAGS.values() if not f.editable}


def secret_settings() -> set[str]:
    """Names that must be stored encrypted (DigitalOcean ``type: SECRET``)."""
    return {f.name for f in FLAGS.values() if f.secret}


def groups() -> tuple[Group, ...]:
    """Ordered settings-page sections."""
    return GROUPS


def flags_in_group(group_id: str) -> List[Flag]:
    """Flags declared for ``group_id``, in registration order."""
    return [flag for flag in FLAGS.values() if flag.group == group_id]


def settings_defaults(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Typed settings dict for the admin UI (only ``show_in_settings`` flags)."""
    source = env if env is not None else os.environ
    return {f.name: f.coerce(source.get(f.name)) for f in FLAGS.values() if f.show_in_settings}


def validate_required(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Return names of required flags that are missing/empty in ``env``."""
    source = env if env is not None else os.environ
    missing: List[str] = []
    for f in FLAGS.values():
        if f.required and not (source.get(f.name) or "").strip():
            missing.append(f.name)
    return missing


def render_env_example() -> str:
    """Render the canonical ``flags.env.example`` content from the registry."""
    lines = [
        "# ============================================================================",
        "# Anansi feature flags & tunable settings",
        "# AUTOGENERATED from shared/config/flag_registry.py — DO NOT EDIT BY HAND.",
        "# Regenerate with:  python -m shared.config.flag_registry > shared/config/flags.env.example",
        "#",
        "# These are operator-tunable toggles and knobs only. Secrets and connection",
        "# strings (API keys, AUTH_DB_*, CHAT_DB_*, tokens) live in each service's own",
        "# .env.example and are never managed here.",
        "# ============================================================================",
        "",
    ]
    for flag in FLAGS.values():
        if not flag.document:
            continue
        scope = "" if flag.scope == SCOPE_GLOBAL else f" [{flag.scope}]"
        tags = []
        if not flag.editable:
            tags.append("read-only")
        if flag.required:
            tags.append("required")
        if flag.secret:
            tags.append("secret")
        suffix = f" ({', '.join(tags)})" if tags else ""
        lines.append(f"# {flag.description}{scope}{suffix}")
        value = "" if flag.secret else flag.default_str
        lines.append(f"{flag.name}={value}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


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


if __name__ == "__main__":  # pragma: no cover - CLI generator
    print(render_env_example(), end="")
