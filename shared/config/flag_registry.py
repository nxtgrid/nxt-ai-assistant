"""Central registry for Anansi feature flags and tunable settings.

This module is the **single source of truth** for every operator-tunable
environment variable in Anansi (feature toggles, model knobs, layout
parameters, MCP server enables, etc.). Credentials and connection strings
(``GOOGLE_API_KEY``, ``AUTH_DB_*``, ``CHAT_DB_*`` …) are intentionally *not*
registered here — those are secrets documented per-service in each
``.env.example`` and are never managed through the settings UI.

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

# DigitalOcean scope name for the consolidated orchestrator + MCP service.
# Flags scoped here are written to the service ``envs[]`` block rather than the
# global ``spec.envs[]`` block. Kept as a constant so the value lives in one place.
SERVICE_BOT = "anansi-bot"
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
    "vrm",
    "jira",
    "logs",
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
    "messaging",
    "reference",
]


def _mcp_enable_flags() -> List[Flag]:
    return [
        _b(
            f"{srv.upper()}_ENABLED",
            True,
            f"Enable the {srv.replace('_', ' ')} MCP server "
            "(disabling hides all of its tools).",
        )
        for srv in MCP_SERVER_NAMES
    ]


# ---------------------------------------------------------------------------
# The registry. Order here is the order used when rendering the example file.
# ---------------------------------------------------------------------------
_FLAGS: List[Flag] = [
    # --- Deployment & portability -----------------------------------------
    _s(
        "DEFAULT_TIMEZONE",
        "UTC",
        "IANA timezone used as the fallback for display/scheduling when a grid "
        "has no timezone of its own (e.g. 'Africa/Lagos', 'UTC').",
        editable=False,
        show_in_settings=False,
    ),
    _i(
        "STAFF_ORG_ID",
        2,
        "Organization id treated as internal staff (full tool access).",
        editable=False,
        show_in_settings=False,
    ),
    _s(
        "SETTINGS_BACKEND",
        "auto",
        "Backend for runtime settings management: 'auto' (DigitalOcean if "
        "DIGITALOCEAN_APP_ID + token are set, else env-file), 'digitalocean', or 'envfile'.",
        editable=False,
        show_in_settings=False,
    ),
    _s(
        "SETTINGS_FILE",
        ".env.settings",
        "Path the env-file settings backend reads/writes when not on DigitalOcean.",
        editable=False,
        show_in_settings=False,
    ),
    # --- Bot behaviour & core ---------------------------------------------
    _b("ALLOW_PARALLEL_CALLS", True, "Allow Gemini to request parallel tool calls."),
    _i("MAX_TOOL_ROUNDS", 5, "Maximum sequential tool-call rounds per turn."),
    _b("BOT_ENABLED", True, "Master switch for the Telegram bot."),
    _s("LOG_LEVEL", "INFO", "Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)."),
    # --- AI model settings -------------------------------------------------
    _s(
        "LLM_PROVIDER",
        "gemini",
        "Generation provider: 'gemini' for direct Google Gemini or 'openrouter' for OpenRouter.",
        scope=SERVICE_BOT,
    ),
    _s(
        "GEMINI_MODEL",
        "gemini-2.5-flash",
        "Primary generation model id for the selected provider.",
    ),
    _s(
        "GEMINI_FALLBACK_MODEL",
        "gemini-2.5-flash",
        "Fallback generation model id for the selected provider.",
    ),
    _s(
        "INTENT_ROUTER_MODEL",
        "gemini-2.5-flash-lite",
        "Lightweight model for structured natural-language expert routing.",
    ),
    _f("GEMINI_TEMPERATURE", 0.2, "Generation temperature where supported by the selected model."),
    _i(
        "GEMINI_MAX_OUTPUT_TOKENS",
        8192,
        "Max output tokens for the primary model.",
        editable=False,
    ),
    _i(
        "GEMINI_LITE_MAX_OUTPUT_TOKENS",
        1024,
        "Max output tokens for the lite/verification model.",
        editable=False,
    ),
    _s(
        "GEMINI_DEEP_THINKING_MODEL",
        "",
        "Model for deep-thinking tasks (document editing, complex analysis).",
        scope=SERVICE_BOT,
    ),
    _s(
        "OPENROUTER_MODEL",
        "google/gemini-2.5-flash",
        "Legacy OpenRouter default model fallback. Role-specific model flags are used by the orchestrator.",
        scope=SERVICE_BOT,
        show_in_settings=False,
        document=False,
    ),
    _s(
        "OPENROUTER_PROVIDER_ORDER",
        "",
        "Optional comma-separated OpenRouter provider slugs to try first, e.g. 'google-vertex' for Google Vertex BYOK.",
        scope=SERVICE_BOT,
    ),
    _b(
        "OPENROUTER_ALLOW_FALLBACKS",
        True,
        "Allow OpenRouter to fall back to other endpoints when provider routing is configured.",
        scope=SERVICE_BOT,
    ),
    _b(
        "OPENROUTER_REQUIRE_PARAMETERS",
        False,
        "Require OpenRouter endpoints that support the requested parameters/tool schema.",
        scope=SERVICE_BOT,
    ),
    _s(
        "EMBEDDING_MODEL",
        "gemini-embedding-001",
        "Embedding model used for RAG ingestion/retrieval.",
        editable=False,
    ),
    # --- MCP servers -------------------------------------------------------
    *_mcp_enable_flags(),
    _j(
        "MCP_DISABLED_TOOLS",
        "[]",
        'JSON array of "server:tool" strings to disable individual tools.',
    ),
    # --- RAG ---------------------------------------------------------------
    _b("rag__enabled", False, "Enable retrieval-augmented generation."),
    _i("rag__top_k", 5, "Number of RAG chunks to retrieve per query."),
    # --- Metrics & monitoring ---------------------------------------------
    _b("METRICS_ENABLED", True, "Enable scheduled metrics collection."),
    _i("METRICS_SCHEDULE_HOUR", 9, "Hour of day (0-23) to run metrics collection."),
    # --- Response verification --------------------------------------------
    _b(
        "VERIFICATION_ENABLED",
        False,
        "Enable LLM-as-judge verification of outgoing customer messages.",
        scope=SERVICE_BOT,
    ),
    _s(
        "VERIFICATION_DOC_ID",
        "",
        "Google Doc id holding verification criteria.",
        scope=SERVICE_BOT,
    ),
    _s(
        "VERIFICATION_MODEL",
        "gemini-2.5-flash-lite",
        "Model used for response verification.",
        scope=SERVICE_BOT,
    ),
    # --- External notifications (n8n / VRM / Grafana passthrough) ----------
    _b(
        "NOTIFY_ENDPOINT_ENABLED",
        False,
        "Accept external notifications on POST /chat/notify and forward them to "
        "Telegram (n8n / VRM / Grafana passthrough). When off the endpoint returns 503.",
        scope=SERVICE_BOT,
    ),
    # --- Expert workflow / interaction ------------------------------------
    _b(
        "WORKFLOW_PARAMETER_CONFIRMATION",
        True,
        "Prompt the user to confirm editable workflow parameters.",
        scope=SERVICE_BOT,
    ),
    _b(
        "INLINE_BUTTONS_ENABLED",
        False,
        "Telegram inline buttons for decision prompts.",
        scope=SERVICE_BOT,
    ),
    _b(
        "PROCEDURE_BUTTONS_ENABLED",
        False,
        "Procedure buttons in customer support chats.",
        scope=SERVICE_BOT,
    ),
    _b(
        "MINI_APP_FORMS_ENABLED",
        False,
        "Telegram WebApp popups for workflow parameters.",
        scope=SERVICE_BOT,
    ),
    _b("CONTEXT_FILTER_ENABLED", False, "Conversation context filtering.", scope=SERVICE_BOT),
    _b(
        "THREAD_DISENTANGLEMENT_ENABLED",
        False,
        "Multi-thread conversation disentanglement.",
        scope=SERVICE_BOT,
    ),
    _i(
        "ACTIVE_THREAD_WINDOW_MINUTES",
        60,
        "Window (minutes) a thread stays active.",
        scope=SERVICE_BOT,
    ),
    _b(
        "CONVERSATION_SUMMARY_ENABLED",
        False,
        "Rolling conversation summarization.",
        scope=SERVICE_BOT,
    ),
    _b(
        "PERSISTENT_AGENTS_ENABLED",
        False,
        "Enable user-created persistent monitoring agents.",
        scope=SERVICE_BOT,
    ),
    _i(
        "AWAITING_INPUT_TIMEOUT_MINUTES",
        180,
        "Timeout (minutes) for an expert awaiting user input.",
        scope=SERVICE_BOT,
    ),
    _s(
        "EXPERT_INSTRUCTIONS_DOC_ID",
        "",
        "Google Doc id holding expert/workflow definitions.",
        scope=SERVICE_BOT,
    ),
    # --- Observability -----------------------------------------------------
    _b("LANGFUSE_ENABLED", False, "Enable Langfuse LLM observability tracing.", scope=SERVICE_BOT),
    # --- Grafana -----------------------------------------------------------
    _s("GRAFANA_URL", "http://localhost:3000", "Grafana base URL."),
    _s("GRAFANA_USERNAME", "", "Grafana username."),
    _s("GRAFANA_PASSWORD", "", "Grafana password.", secret=True),
    _s("GRAFANA_FOLDER_NAME", "", "Grafana folder to source dashboards from."),
    _s(
        "GRAFANA_PANEL_DESCRIPTION_PROMPT",
        (
            "You are a system that generates tool descriptions for Grafana dashboard panels. "
            "Given a panel with title, description, query, and dashboard variables, create a "
            "concise tool description that explains what data this panel shows and what "
            "variables it requires. Format: A tool description suitable for an LLM to "
            "understand when to use this panel."
        ),
        "Prompt used to auto-generate Grafana panel tool descriptions.",
    ),
    _s("GRAFANA_ENABLED_PANELS", "", "Comma-separated list of enabled Grafana panels."),
    _s("GRAFANA_ENABLED_DASHBOARDS", "", "Comma-separated list of enabled Grafana dashboards."),
    _i("GRAFANA_SYNC_HOUR", 2, "Hour of day (0-23) to sync Grafana panel metadata."),
    _b("GRAFANA_FORCE_FULL_REINDEX", False, "Force a full Grafana panel reindex on next sync."),
    _j(
        "GRAFANA_PANELS_METADATA",
        "{}",
        "Machine-managed Grafana panel metadata (synced by scripts, not the UI).",
        editable=False,
        document=False,
    ),
    _j(
        "GRAFANA_AVAILABLE_DASHBOARDS",
        "{}",
        "Machine-managed Grafana dashboard catalog (synced by scripts, not the UI).",
        editable=False,
        document=False,
    ),
    # --- Access control ----------------------------------------------------
    _s("ALLOWED_VIEWER_EMAILS", "", "Comma-separated emails allowed to view the admin app."),
    _s(
        "EQUIPMENT_CONTROL_ALLOWED_USERS",
        "",
        "Comma-separated emails allowed to issue equipment-control commands.",
        scope=SERVICE_BOT,
    ),
    _s(
        "GRID_DESIGN_ALLOWED_USERS",
        "",
        "Comma-separated emails with view-only access to the Grid Design tables.",
    ),
    _s(
        "GRID_DESIGN_EDITORS",
        "",
        "Comma-separated emails allowed to edit all Grid Design tables except Procurements.",
    ),
    _s(
        "GRID_PROCUREMENT_EDITORS",
        "",
        "Comma-separated emails allowed to edit only the Procurements (Purchases/BoS) table.",
    ),
    # --- System config (read-only display) --------------------------------
    _s(
        "ESCALATION_TELEGRAM_CHAT_ID",
        "",
        "Telegram chat id for the escalation group.",
        editable=False,
    ),
    _s("DEBUG_TELEGRAM_CHAT_ID", "", "Telegram chat id for debug output.", editable=False),
    _s(
        "CUSTOMER_SUPPORT_DOC_ID",
        "",
        "Google Doc id for customer-mode system instructions.",
        editable=False,
    ),
    _s(
        "STAFF_SUPPORT_DOC_ID",
        "",
        "Google Doc id for staff-mode system instructions.",
        editable=False,
    ),
    _s(
        "TROUBLESHOOTING_PROCEDURES_DOC_ID",
        "",
        "Google Doc id for shared troubleshooting procedures.",
        editable=False,
    ),
    # --- Layout / site design (anansi-bot) --------------------------------
    _f("LAYOUT_POLE_SPACING_M", 45.0, "Spacing between poles along roads (m).", scope=SERVICE_BOT),
    _f(
        "LAYOUT_MAX_DROP_DISTANCE_M",
        40.0,
        "Max drop-cable length to a building (m).",
        scope=SERVICE_BOT,
    ),
    _f("LAYOUT_TARGET_COVERAGE_PCT", 90.0, "Target building coverage (%).", scope=SERVICE_BOT),
    _f("LAYOUT_SQM_PER_KWP", 15.5, "Roof area per kWp (m²/kWp).", scope=SERVICE_BOT),
    _f("LAYOUT_KWP_PER_BUILDING", 0.25, "Estimated kWp per building.", scope=SERVICE_BOT),
    _f("LAYOUT_MIN_ESTIMATED_KWP", 30.0, "Minimum estimated site kWp.", scope=SERVICE_BOT),
    _f("LAYOUT_BUILDING_BUFFER_M", 15.0, "Buffer around buildings (m).", scope=SERVICE_BOT),
    _f("LAYOUT_SITE_SETBACK_M", 5.0, "Setback from site boundary (m).", scope=SERVICE_BOT),
    _f("LAYOUT_ROAD_SETBACK_M", 5.0, "Setback from roads (m).", scope=SERVICE_BOT),
    _f("LAYOUT_CORRIDOR_CLEARANCE_M", 10.0, "Corridor clearance (m).", scope=SERVICE_BOT),
    _f("LAYOUT_CANOPY_THRESHOLD_M", 5.0, "Tree-canopy avoidance threshold (m).", scope=SERVICE_BOT),
    _f(
        "LAYOUT_MIN_CANDIDATE_SEPARATION_M",
        100.0,
        "Min separation between candidate sites (m).",
        scope=SERVICE_BOT,
    ),
    _i("LAYOUT_MAX_CANDIDATES", 3, "Max candidate sites to evaluate.", scope=SERVICE_BOT),
    _f(
        "LAYOUT_POLE_DEDUP_DISTANCE_M",
        5.0,
        "Distance under which poles are deduplicated (m).",
        scope=SERVICE_BOT,
    ),
    _f("LAYOUT_SNAP_NODE_TOLERANCE_M", 1.0, "Node-snapping tolerance (m).", scope=SERVICE_BOT),
    _f(
        "LAYOUT_MERGE_GAP_THRESHOLD_M",
        5.0,
        "Gap under which segments merge (m).",
        scope=SERVICE_BOT,
    ),
    _f(
        "LAYOUT_REDISTRIBUTE_GAP_MAX_M",
        10.0,
        "Max gap for pole redistribution (m).",
        scope=SERVICE_BOT,
    ),
    _f("LAYOUT_LIGHTNING_RADIUS_M", 13.5, "Lightning-protection radius (m).", scope=SERVICE_BOT),
    # --- Drive templates (editable in the settings UI Templates section) ---
    # These MUST stay show_in_settings=True (the default). The settings page
    # renders them as editable text inputs; when show_in_settings is False the
    # overlay in get_current_settings drops their real DO values, the inputs
    # render blank, and the next "Save changes" writes the blanks back to the
    # live spec — silently wiping the IDs and breaking the whole LPP workflow.
    _s(
        "LPP_TEMPLATE_ID",
        "",
        "Google Slides template id for LPP output.",
        scope=SERVICE_BOT,
    ),
    _s(
        "QGIS_TEMPLATE_FILE_ID",
        "",
        "QGIS template file id for site layouts.",
        scope=SERVICE_BOT,
    ),
    _s(
        "LPP_OUTPUT_FOLDER_ID",
        "",
        "Drive folder id for LPP output.",
        scope=SERVICE_BOT,
    ),
    # --- Reference server (regulatory data; staff only) -------------------
    _s(
        "NIGERIA_IMPORT_TARIFF_SHEET_ID",
        "",
        "Sheet id for import tariff reference data.",
        scope=SERVICE_BOT,
    ),
    _s(
        "NIGERIA_IMPORT_STANDARDS_PDF_ID",
        "",
        "PDF id for import standards reference data.",
        scope=SERVICE_BOT,
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


if __name__ == "__main__":  # pragma: no cover - CLI generator
    print(render_env_example(), end="")
