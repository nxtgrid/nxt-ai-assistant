"""
Settings page component for Anansi App.

Allows authorized users to view and modify bot configuration settings.
"""

import time
from typing import Any, Dict, FrozenSet

import streamlit as st
from services.grafana_metadata_service import load_available_dashboards, load_panels_metadata
from services.settings_service import SettingsService

# Settings that require a bot restart to take effect (read at process startup).
# Everything else is read per-request and takes effect immediately.
RESTART_REQUIRED_KEYS: FrozenSet[str] = frozenset(
    {
        "PERSISTENT_AGENTS_ENABLED",  # agent runner set up in lifespan
        "METRICS_ENABLED",  # scheduler registered at startup
        "METRICS_SCHEDULE_HOUR",  # scheduler registered at startup
        "GRAFANA_SYNC_HOUR",  # scheduler registered at startup
        "MINI_APP_FORMS_ENABLED",  # route registered at startup
    }
)

_RESTART_KEY_LABELS: Dict[str, str] = {
    "PERSISTENT_AGENTS_ENABLED": "Persistent Agents",
    "METRICS_ENABLED": "Weekly Metrics",
    "METRICS_SCHEDULE_HOUR": "Metrics Posting Hour",
    "GRAFANA_SYNC_HOUR": "Grafana Sync Hour",
    "MINI_APP_FORMS_ENABLED": "Mini App Forms",
}

_READONLY_KEYS: FrozenSet[str] = frozenset(
    {
        "ESCALATION_TELEGRAM_CHAT_ID",
        "DEBUG_TELEGRAM_CHAT_ID",
        "GEMINI_MODEL",
        "GEMINI_FALLBACK_MODEL",
        "GEMINI_DEEP_THINKING_MODEL",
        "VERIFICATION_MODEL",
        "EMBEDDING_MODEL",
        "GEMINI_MAX_OUTPUT_TOKENS",
        "GEMINI_LITE_MAX_OUTPUT_TOKENS",
        "EXPERT_INSTRUCTIONS_DOC_ID",
    }
)


def render_settings_page():
    """Render the settings configuration page."""
    st.title("⚙️ Bot Settings")
    st.markdown("Configure Anansi bot behavior and features.")

    settings_service = SettingsService()

    # Cache current_settings in session state to avoid re-fetching after save
    # fetch_from_do=True ensures we read service-specific settings from DO API
    # (e.g., EQUIPMENT_CONTROL_ALLOWED_USERS is in anansi-bot, not anansi-app)
    if "current_settings" not in st.session_state:
        st.session_state.current_settings = settings_service.get_current_settings(
            fetch_from_do=True
        )

    current_settings = st.session_state.current_settings

    # Initialize or refresh session state with current settings
    # Always refresh to pick up any changes from deployments
    if (
        "pending_settings" not in st.session_state
        or st.session_state.get("_last_settings_refresh") != current_settings
    ):
        st.session_state.pending_settings = current_settings.copy()
        st.session_state._last_settings_refresh = current_settings

    # Master Bot Switch - Standalone at top
    st.markdown("### 🔴 Master Bot Control")
    st.session_state.pending_settings["BOT_ENABLED"] = st.toggle(
        "Enable Bot",
        value=st.session_state.pending_settings["BOT_ENABLED"],
        help="Master switch to enable/disable the entire bot. When disabled, all API calls are immediately rejected with 200 responses.",
    )
    if not st.session_state.pending_settings["BOT_ENABLED"]:
        st.warning("⚠️ Bot is currently disabled. All incoming requests will be rejected silently.")

    st.divider()

    # Section 1: Bot Behavior & Core Settings
    with st.expander("🤖 Bot Behavior & Core Settings", expanded=True):
        # Log Level Setting
        log_level_options = ["DEBUG", "INFO", "WARNING", "ERROR"]
        current_log_level = st.session_state.pending_settings.get("LOG_LEVEL", "INFO")
        # Find index of current value, default to INFO (index 1) if not found
        try:
            current_index = log_level_options.index(current_log_level)
        except ValueError:
            current_index = 1  # Default to INFO
        st.session_state.pending_settings["LOG_LEVEL"] = st.selectbox(
            "Log Level",
            options=log_level_options,
            index=current_index,
            help="Set logging verbosity. DEBUG shows full Gemini API payloads. Use INFO for normal operation.",
        )
        if st.session_state.pending_settings["LOG_LEVEL"] == "DEBUG":
            st.warning(
                "⚠️ DEBUG logging is verbose and may impact performance. "
                "Use only for troubleshooting, then revert to INFO."
            )

        st.markdown("---")
        st.session_state.pending_settings["ALLOW_PARALLEL_CALLS"] = st.checkbox(
            "Allow Parallel Tool Calls",
            value=st.session_state.pending_settings["ALLOW_PARALLEL_CALLS"],
            help="Enable bot to execute multiple tools simultaneously for faster responses",
        )

        st.session_state.pending_settings["MAX_TOOL_ROUNDS"] = st.slider(
            "Max Tool Execution Rounds",
            min_value=1,
            max_value=50,
            value=st.session_state.pending_settings["MAX_TOOL_ROUNDS"],
            help="Maximum number of tool execution cycles per conversation",
        )

        # Response Verification (Customer Mode)
        st.markdown("---")
        st.session_state.pending_settings["VERIFICATION_ENABLED"] = st.checkbox(
            "Enable Response Verification (Customer Mode)",
            value=st.session_state.pending_settings["VERIFICATION_ENABLED"],
            help="Verify customer-facing responses using LLM-as-judge before sending. Failed verification triggers regeneration, then escalation if still fails. Only applies to customer mode.",
        )
        if st.session_state.pending_settings["VERIFICATION_ENABLED"]:
            if not st.session_state.pending_settings.get("VERIFICATION_DOC_ID"):
                st.warning(
                    "⚠️ Verification requires a Google Doc with verification criteria "
                    "(VERIFICATION_DOC_ID). Contact admin to configure."
                )
            else:
                st.success("✓ Verification configured and enabled")

        # Expert Workflow Settings
        st.markdown("---")
        st.session_state.pending_settings["WORKFLOW_PARAMETER_CONFIRMATION"] = st.checkbox(
            "Enable Workflow Parameter Confirmation",
            value=st.session_state.pending_settings.get("WORKFLOW_PARAMETER_CONFIRMATION", True),
            help="Show parameter confirmation prompts before each expert workflow step. Allows users to review/modify values before execution.",
        )

        # Telegram Inline Buttons
        st.markdown("---")
        st.markdown("**Telegram Inline Buttons**")
        st.session_state.pending_settings["INLINE_BUTTONS_ENABLED"] = st.checkbox(
            "Enable Telegram Inline Buttons (Expert Workflows)",
            value=st.session_state.pending_settings.get("INLINE_BUTTONS_ENABLED", False),
            help="Show inline keyboard buttons for expert workflow decisions (duplicate detection, resume). Users can tap buttons instead of typing 1/2/3. Text input always works as fallback.",
        )

        st.session_state.pending_settings["PROCEDURE_BUTTONS_ENABLED"] = st.checkbox(
            "Enable Telegram Inline Buttons (Customer Support)",
            value=st.session_state.pending_settings.get("PROCEDURE_BUTTONS_ENABLED", False),
            help="Show inline keyboard buttons when the LLM asks customers to choose between options during support procedures. Requires [BUTTONS]...[/BUTTONS] formatting in support doc procedures.",
        )

        st.session_state.pending_settings["MINI_APP_FORMS_ENABLED"] = st.checkbox(
            "Enable Mini App Forms (Expert Workflows)",
            value=st.session_state.pending_settings.get("MINI_APP_FORMS_ENABLED", False),
            help="Show a 'Edit Parameters' button that opens a Telegram Mini App popup form during expert workflow parameter confirmation. Users can edit values in a form instead of typing. Requires MINI_APP_BASE_URL to be set.",
        )

        # Conversation Intelligence
        st.markdown("---")
        st.markdown("**Conversation Intelligence**")
        st.session_state.pending_settings["CONTEXT_FILTER_ENABLED"] = st.checkbox(
            "Enable Context Filter (Topic Classification)",
            value=st.session_state.pending_settings.get("CONTEXT_FILTER_ENABLED", False),
            help="Use a lightweight LLM call (gemini-2.5-flash-lite) to classify incoming messages by topic and filter irrelevant conversation history before sending to Gemini. Fail-open: if classification fails, all history is kept. Mutually exclusive with Thread Disentanglement.",
        )

        st.session_state.pending_settings["THREAD_DISENTANGLEMENT_ENABLED"] = st.checkbox(
            "Enable Thread Disentanglement",
            value=st.session_state.pending_settings.get("THREAD_DISENTANGLEMENT_ENABLED", False),
            help="Classify messages into conversation threads and scope history to the current thread only. Uses deterministic rules first (reply chains, slash commands, active workflows), falling back to a lightweight LLM call when multiple threads are active. Mutually exclusive with Context Filter.",
        )

        st.session_state.pending_settings["ACTIVE_THREAD_WINDOW_MINUTES"] = st.slider(
            "Active Thread Window (minutes)",
            min_value=5,
            max_value=180,
            value=st.session_state.pending_settings.get("ACTIVE_THREAD_WINDOW_MINUTES", 60),
            step=5,
            help="How long a thread stays 'active' for disentanglement. Messages within this window are considered part of active conversations. Only applies when Thread Disentanglement is enabled.",
        )

        st.session_state.pending_settings["CONVERSATION_SUMMARY_ENABLED"] = st.checkbox(
            "Enable Conversation Summarization",
            value=st.session_state.pending_settings.get("CONVERSATION_SUMMARY_ENABLED", False),
            help="Progressively summarize older messages in long conversations (40+ messages). Reduces token usage by replacing old messages with a summary. Non-blocking: failures don't affect responses.",
        )

        # Persistent Agents
        st.markdown("---")
        st.markdown("**Persistent Agents**")
        st.session_state.pending_settings["PERSISTENT_AGENTS_ENABLED"] = st.checkbox(
            "Enable Persistent Agents",
            value=st.session_state.pending_settings.get("PERSISTENT_AGENTS_ENABLED", False),
            help="Enable long-running agents that respond to events (equipment alerts, JIRA notifications, scheduled wakes). When disabled, no new events are queued and running agents stop within 10 seconds.",
        )
        if not st.session_state.pending_settings["PERSISTENT_AGENTS_ENABLED"]:
            st.info("Persistent agents are disabled. Events will not be queued or processed.")

    # Section 2: MCP Servers & Tools
    with st.expander("🔌 MCP Servers & Tools", expanded=True):
        st.caption(
            "Enable/disable MCP servers and tools. Disabled servers won't appear to the LLM."
        )

        # Define configurable servers with display names and descriptions
        # This matches CONFIGURABLE_SERVERS in action_flags.py
        MCP_SERVERS = {
            "equipment_diagnostics": {
                "display": "Equipment Diagnostics",
                "desc": "Production equipment diagnostics, charts, and monitoring",
            },
            "jira": {
                "display": "Jira",
                "desc": "Jira ticket creation, management, and analysis",
            },
            "meters": {
                "display": "Meters",
                "desc": "Smart meter management and operations",
            },
            "equipment_control": {
                "display": "Equipment Control",
                "desc": "Equipment control operations (restart, reboot)",
            },
            "customer": {
                "display": "Customer",
                "desc": "Customer-facing tools for payment and status",
            },
            "grafana": {
                "display": "Grafana",
                "desc": "Grafana dashboard panel rendering",
            },
            "schedule": {
                "display": "Schedule",
                "desc": "Command scheduling for future execution",
            },
            "meta": {
                "display": "Meta",
                "desc": "Bot performance analytics",
            },
            "grid_design": {
                "display": "Grid Design",
                "desc": "Grid design and Bill of Materials via AppSheet",
            },
            "solar": {
                "display": "Solar",
                "desc": "Solar potential assessment via Global Solar Atlas",
            },
            "knowledge": {
                "display": "Knowledge",
                "desc": "Knowledge base summarization tools",
            },
            "logs": {
                "display": "Logs",
                "desc": "Backend service log analysis from Loki",
            },
            "codebase": {
                "display": "Codebase",
                "desc": "Codebase analysis and PR tracking",
            },
            "payment_processor": {
                "display": "Payment Processor",
                "desc": "Payment transaction status checks",
            },
            "messaging": {
                "display": "Messaging",
                "desc": "Send messages to registered staff Telegram groups",
            },
            "reference": {
                "display": "Reference (Staff)",
                "desc": "Nigerian import tariff, prohibition list, and standards lookups",
            },
        }

        # Display servers in a grid
        cols = st.columns(2)
        for i, (server_name, server_info) in enumerate(MCP_SERVERS.items()):
            env_key = f"{server_name.upper()}_ENABLED"
            with cols[i % 2]:
                # Get current value, default to True (enabled)
                current_value = st.session_state.pending_settings.get(env_key, True)
                if isinstance(current_value, str):
                    current_value = current_value.lower() in ("true", "1", "yes", "on")

                st.session_state.pending_settings[env_key] = st.checkbox(
                    server_info["display"],
                    value=current_value,
                    help=server_info["desc"],
                    key=f"server_{server_name}",
                )

    # Section 3: Grafana Settings
    with st.expander("📊 Grafana Dashboard Panels", expanded=True):
        st.session_state.pending_settings["GRAFANA_FOLDER_NAME"] = st.text_input(
            "Grafana Folder Name",
            value=st.session_state.pending_settings["GRAFANA_FOLDER_NAME"],
            help="Name of the Grafana folder containing dashboards to index",
        )

        st.session_state.pending_settings["GRAFANA_PANEL_DESCRIPTION_PROMPT"] = st.text_area(
            "Panel Description System Prompt",
            value=st.session_state.pending_settings["GRAFANA_PANEL_DESCRIPTION_PROMPT"],
            height=150,
            help="System instructions for Gemini LLM to generate panel tool descriptions",
        )

        # Parse available dashboards and panels metadata
        import json

        # Load dashboard names from DB (always fresh after sync)
        available_dashboards = load_available_dashboards()
        if not available_dashboards:
            # Fallback to env var for backwards compatibility
            try:
                available_dashboards = json.loads(
                    st.session_state.pending_settings["GRAFANA_AVAILABLE_DASHBOARDS"]
                )
            except (json.JSONDecodeError, KeyError):
                available_dashboards = {}

        # Load panels from Supabase (GRAFANA_PANELS_METADATA is too large for DO env vars)
        panels_metadata = load_panels_metadata()

        # Get currently enabled dashboards
        enabled_dashboards_str = st.session_state.pending_settings.get(
            "GRAFANA_ENABLED_DASHBOARDS", ""
        )
        currently_enabled_dashboards = [
            d.strip() for d in enabled_dashboards_str.split(",") if d.strip()
        ]

        # Get currently enabled panels
        enabled_panels_str = st.session_state.pending_settings.get("GRAFANA_ENABLED_PANELS", "")
        currently_enabled_panels = [p.strip() for p in enabled_panels_str.split(",") if p.strip()]

        # Side-by-side layout for dashboard and panel selectors
        if available_dashboards or panels_metadata:
            st.markdown("#### Dashboard & Panel Selection")

            # CSS + JS: truncate long chip text but show full name on hover via title attribute
            st.markdown(
                """
                <style>
                div[data-testid="stMultiSelect"] span[data-baseweb="tag"] {
                    max-width: none !important;
                }
                div[data-testid="stMultiSelect"] span[data-baseweb="tag"] span {
                    max-width: 340px !important;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }
                </style>
                <script>
                (function() {
                    // Guard: Streamlit rerenders inject this script tag repeatedly;
                    // only register one observer per page lifetime.
                    if (window._chipTitleObserverRegistered) return;
                    window._chipTitleObserverRegistered = true;
                    function addTitlesToChips() {
                        document.querySelectorAll('span[data-baseweb="tag"] span').forEach(function(el) {
                            if (el.textContent && !el.title) {
                                el.title = el.textContent.trim();
                            }
                        });
                    }
                    // Run on load and whenever the DOM changes (multiselect updates)
                    addTitlesToChips();
                    new MutationObserver(addTitlesToChips).observe(document.body, { childList: true, subtree: true });
                })();
                </script>
                """,
                unsafe_allow_html=True,
            )

            col1, col2 = st.columns(2)

            with col1:
                if available_dashboards:
                    selected_dashboards = st.multiselect(
                        "Enabled Dashboards",
                        options=list(available_dashboards.keys()),
                        default=currently_enabled_dashboards,
                        format_func=lambda x: available_dashboards.get(x, x),
                        help="Select dashboards to pull panels from. Only panels from selected dashboards appear in the panel selector.",
                    )
                    st.session_state.pending_settings["GRAFANA_ENABLED_DASHBOARDS"] = ",".join(
                        selected_dashboards
                    )
                else:
                    st.info("ℹ️ No dashboards available yet. Run sync to populate.")
                    st.session_state.pending_settings["GRAFANA_ENABLED_DASHBOARDS"] = ""
                    selected_dashboards = []

            with col2:
                if panels_metadata:
                    # Filter panels to only show those from selected dashboards
                    if selected_dashboards:
                        filtered_panel_options = {
                            panel_key: f"{info.get('dashboard_title', 'Unknown')} - {info.get('title', 'Untitled')}"
                            for panel_key, info in panels_metadata.items()
                            if info.get("dashboard_uid") in selected_dashboards
                        }
                    else:
                        # No dashboards selected - show empty panel list
                        filtered_panel_options = {}

                    if filtered_panel_options:
                        # Filter currently enabled to only valid panels in filtered list
                        valid_enabled = [
                            p for p in currently_enabled_panels if p in filtered_panel_options
                        ]

                        selected_panels = st.multiselect(
                            "Enabled Panels",
                            options=list(filtered_panel_options.keys()),
                            default=valid_enabled,
                            format_func=lambda x: filtered_panel_options.get(x, x),
                            help="Select panels to enable as MCP tools. Only panels from selected dashboards are shown.",
                        )
                        st.session_state.pending_settings["GRAFANA_ENABLED_PANELS"] = ",".join(
                            selected_panels
                        )
                    else:
                        if selected_dashboards:
                            st.info("ℹ️ No panels found for selected dashboards.")
                        else:
                            st.info("ℹ️ Select dashboards first to see available panels.")
                        st.session_state.pending_settings["GRAFANA_ENABLED_PANELS"] = ""
                else:
                    st.info("ℹ️ No panels indexed yet. Run sync to populate.")
                    st.session_state.pending_settings["GRAFANA_ENABLED_PANELS"] = ""
        else:
            st.info("ℹ️ No dashboards or panels available yet. Run the dashboard sync to populate.")
            st.session_state.pending_settings["GRAFANA_ENABLED_DASHBOARDS"] = ""
            st.session_state.pending_settings["GRAFANA_ENABLED_PANELS"] = ""

        st.session_state.pending_settings["GRAFANA_SYNC_HOUR"] = st.slider(
            "Nightly Sync Hour (UTC)",
            min_value=0,
            max_value=23,
            value=st.session_state.pending_settings["GRAFANA_SYNC_HOUR"],
            help="Hour of day (UTC) to run automatic panel indexing (0-23)",
        )

        st.session_state.pending_settings["GRAFANA_FORCE_FULL_REINDEX"] = st.checkbox(
            "Force Full Reindex",
            value=st.session_state.pending_settings.get("GRAFANA_FORCE_FULL_REINDEX", False),
            help="If enabled, the next sync will regenerate ALL panel descriptions (ignores incremental caching). Disable after sync completes.",
        )

        # Sync button — settings auto-save, so this is always ready to run
        st.markdown("---")
        if st.button(
            "🔄 Sync Now",
            use_container_width=True,
            help=(
                "Re-index Grafana dashboards and generate panel descriptions. "
                "Use when new panels are added in Grafana. "
                "Enabling/disabling existing panels is instant — no sync needed."
            ),
        ):
            _trigger_grafana_sync()

        # Display last sync logs if available
        if "grafana_sync_logs" in st.session_state:
            logs = st.session_state.grafana_sync_logs
            with st.expander(
                f"📋 Last Sync Logs ({logs.get('timestamp', 'unknown')})",
                expanded=logs.get("has_errors", False),
            ):
                if logs.get("stdout"):
                    st.markdown("**Output:**")
                    st.code("\n".join(logs["stdout"][-50:]), language="text")
                if logs.get("stderr"):
                    st.markdown("**Errors/Warnings:**")
                    st.error("\n".join(logs["stderr"]))
                if logs.get("exception"):
                    st.markdown("**Exception:**")
                    st.error(logs["exception"])
                st.caption(f"Exit code: {logs.get('exit_code', 'N/A')}")
                if st.button("🗑️ Clear Logs", key="clear_grafana_logs"):
                    del st.session_state.grafana_sync_logs
                    st.rerun()

    # Section 4: RAG Settings
    with st.expander("📚 Knowledge Base (RAG)", expanded=True):
        st.session_state.pending_settings["rag__enabled"] = st.checkbox(
            "Enable Knowledge Base Retrieval",
            value=st.session_state.pending_settings["rag__enabled"],
            help="Use RAG to retrieve relevant information from knowledge base",
        )

        st.session_state.pending_settings["rag__top_k"] = st.slider(
            "Number of Results to Retrieve",
            min_value=1,
            max_value=20,
            value=st.session_state.pending_settings["rag__top_k"],
            help="How many knowledge base entries to retrieve per query",
            disabled=not st.session_state.pending_settings["rag__enabled"],
        )

    # Section 4: Metrics & Monitoring
    with st.expander("📊 Metrics & Monitoring", expanded=True):
        st.session_state.pending_settings["METRICS_ENABLED"] = st.checkbox(
            "Enable Weekly Metrics",
            value=st.session_state.pending_settings["METRICS_ENABLED"],
            help="Post weekly statistics to Telegram every Monday",
        )

        st.session_state.pending_settings["METRICS_SCHEDULE_HOUR"] = st.slider(
            "Metrics Posting Hour (UTC)",
            min_value=0,
            max_value=23,
            value=st.session_state.pending_settings["METRICS_SCHEDULE_HOUR"],
            help="Hour of day (Monday) to post weekly metrics (24-hour format, UTC timezone)",
            disabled=not st.session_state.pending_settings["METRICS_ENABLED"],
        )

        st.markdown("---")
        st.session_state.pending_settings["LANGFUSE_ENABLED"] = st.checkbox(
            "Enable Langfuse Observability",
            value=st.session_state.pending_settings.get("LANGFUSE_ENABLED", False),
            help="Send LLM traces, token usage, and tool execution spans to Langfuse. Requires LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, and LANGFUSE_HOST to be configured.",
        )

    # Section 5: Access Control
    with st.expander("🔒 Access Control", expanded=True):
        st.session_state.pending_settings["ALLOWED_VIEWER_EMAILS"] = st.text_area(
            "Allowed users of the admin UI (emails separated by commas)",
            value=st.session_state.pending_settings["ALLOWED_VIEWER_EMAILS"],
            height=100,
            help="Email addresses allowed to access Anansi",
        )

        st.markdown("---")
        st.markdown("#### ⚡ Equipment Control Access")
        st.caption(
            "Users allowed to execute /inverters_restart and /comms_reboot commands. "
            "This is the first auth gate - users not in this list are rejected immediately."
        )
        st.session_state.pending_settings["EQUIPMENT_CONTROL_ALLOWED_USERS"] = st.text_area(
            "Equipment control allowed users (emails separated by commas)",
            value=st.session_state.pending_settings["EQUIPMENT_CONTROL_ALLOWED_USERS"],
            height=100,
            help="Email addresses allowed to use equipment control commands (/inverters_restart, /comms_reboot)",
        )

    # Section 6: Experts (Subagents)
    with st.expander("🧠 Experts (Subagents)", expanded=False):
        st.caption("Settings for expert workflow execution, timeouts, and parameter confirmation.")

        st.session_state.pending_settings["AWAITING_INPUT_TIMEOUT_MINUTES"] = st.slider(
            "Awaiting Input Timeout (minutes)",
            min_value=10,
            max_value=480,
            value=st.session_state.pending_settings.get("AWAITING_INPUT_TIMEOUT_MINUTES", 180),
            step=10,
            help="How long a workflow waits for user input (e.g. parameter confirmation) before auto-expiring. If a user takes longer than this to respond, the workflow is cancelled and their reply is treated as a new message.",
        )

    # Section 7: Layout & Site Selection
    with st.expander("🗺️ Automated Layout & Site Selection", expanded=False):
        st.caption(
            "Parameters for the automated distribution layout and solar site selection engine."
        )

        st.markdown("**Distribution Layout**")
        st.session_state.pending_settings["LAYOUT_POLE_SPACING_M"] = st.number_input(
            "Pole Spacing (m)",
            min_value=10.0,
            max_value=200.0,
            value=float(st.session_state.pending_settings.get("LAYOUT_POLE_SPACING_M", 45.0)),
            step=5.0,
            format="%.1f",
            help="Distance between consecutive poles along roads (meters).",
        )

        st.session_state.pending_settings["LAYOUT_MAX_DROP_DISTANCE_M"] = st.number_input(
            "Max Drop Cable Distance (m)",
            min_value=5.0,
            max_value=200.0,
            value=float(st.session_state.pending_settings.get("LAYOUT_MAX_DROP_DISTANCE_M", 40.0)),
            step=5.0,
            format="%.1f",
            help="Maximum distance from a pole to a building for a drop cable connection.",
        )

        st.session_state.pending_settings["LAYOUT_TARGET_COVERAGE_PCT"] = st.number_input(
            "Target Coverage (%)",
            min_value=10.0,
            max_value=100.0,
            value=float(st.session_state.pending_settings.get("LAYOUT_TARGET_COVERAGE_PCT", 90.0)),
            step=5.0,
            format="%.1f",
            help="Target percentage of buildings connected to the distribution network.",
        )

        st.markdown("---")
        st.markdown("**Power Plant Site Layout**")
        st.session_state.pending_settings["LAYOUT_LIGHTNING_RADIUS_M"] = st.number_input(
            "Lightning Arrester Radius (m)",
            min_value=1.0,
            max_value=100.0,
            value=float(st.session_state.pending_settings.get("LAYOUT_LIGHTNING_RADIUS_M", 13.5)),
            step=0.5,
            format="%.1f",
            help="Coverage radius per lightning arrester (metres). Arresters are placed on a grid with spacing = radius × √2 so circles fully intersect. First arrester is fixed at the south face of the energy cabin.",
        )

        st.markdown("---")
        st.markdown("**Site Selection**")

        col1, col2 = st.columns(2)
        with col1:
            st.session_state.pending_settings["LAYOUT_SQM_PER_KWP"] = st.number_input(
                "Area per kWp (sqm)",
                min_value=1.0,
                max_value=50.0,
                value=float(st.session_state.pending_settings.get("LAYOUT_SQM_PER_KWP", 13.0)),
                step=1.0,
                format="%.1f",
                help="Square meters of land required per kWp of solar capacity.",
            )
        with col2:
            st.session_state.pending_settings["LAYOUT_KWP_PER_BUILDING"] = st.number_input(
                "kWp per Building (estimate)",
                min_value=0.05,
                max_value=5.0,
                value=float(st.session_state.pending_settings.get("LAYOUT_KWP_PER_BUILDING", 0.25)),
                step=0.05,
                format="%.2f",
                help="Conservative kWp estimate per building when actual kWp is unknown.",
            )

        st.session_state.pending_settings["LAYOUT_MIN_ESTIMATED_KWP"] = st.number_input(
            "Min Estimated kWp",
            min_value=5.0,
            max_value=500.0,
            value=float(st.session_state.pending_settings.get("LAYOUT_MIN_ESTIMATED_KWP", 30.0)),
            step=5.0,
            format="%.1f",
            help="Minimum system size estimate used for site rectangle sizing.",
        )

        st.markdown("**Setbacks & Clearances**")
        col1, col2 = st.columns(2)
        with col1:
            st.session_state.pending_settings["LAYOUT_BUILDING_BUFFER_M"] = st.number_input(
                "Building Buffer (m)",
                min_value=0.0,
                max_value=100.0,
                value=float(
                    st.session_state.pending_settings.get("LAYOUT_BUILDING_BUFFER_M", 15.0)
                ),
                step=1.0,
                format="%.1f",
                help="Minimum distance from solar site border to nearest building.",
            )
            st.session_state.pending_settings["LAYOUT_ROAD_SETBACK_M"] = st.number_input(
                "Road Setback (m)",
                min_value=0.0,
                max_value=50.0,
                value=float(st.session_state.pending_settings.get("LAYOUT_ROAD_SETBACK_M", 5.0)),
                step=1.0,
                format="%.1f",
                help="Minimum distance from solar site to road edge.",
            )
        with col2:
            st.session_state.pending_settings["LAYOUT_SITE_SETBACK_M"] = st.number_input(
                "Boundary Setback (m)",
                min_value=0.0,
                max_value=50.0,
                value=float(st.session_state.pending_settings.get("LAYOUT_SITE_SETBACK_M", 5.0)),
                step=1.0,
                format="%.1f",
                help="Inset from community boundary edge for site placement.",
            )
            st.session_state.pending_settings["LAYOUT_CORRIDOR_CLEARANCE_M"] = st.number_input(
                "Corridor Clearance (m)",
                min_value=0.0,
                max_value=50.0,
                value=float(
                    st.session_state.pending_settings.get("LAYOUT_CORRIDOR_CLEARANCE_M", 10.0)
                ),
                step=1.0,
                format="%.1f",
                help="Half-width of clearance corridor from site to nearest road.",
            )

        st.session_state.pending_settings["LAYOUT_CANOPY_THRESHOLD_M"] = st.number_input(
            "Canopy Height Threshold (m)",
            min_value=0.0,
            max_value=30.0,
            value=float(st.session_state.pending_settings.get("LAYOUT_CANOPY_THRESHOLD_M", 5.0)),
            step=1.0,
            format="%.1f",
            help="Trees >= this height are excluded from site candidates. Set 0 to disable.",
        )

        col1, col2 = st.columns(2)
        with col1:
            st.session_state.pending_settings["LAYOUT_MIN_CANDIDATE_SEPARATION_M"] = (
                st.number_input(
                    "Min Candidate Separation (m)",
                    min_value=10.0,
                    max_value=500.0,
                    value=float(
                        st.session_state.pending_settings.get(
                            "LAYOUT_MIN_CANDIDATE_SEPARATION_M", 100.0
                        )
                    ),
                    step=10.0,
                    format="%.1f",
                    help="Minimum distance between selected site candidates.",
                )
            )
        with col2:
            st.session_state.pending_settings["LAYOUT_MAX_CANDIDATES"] = st.slider(
                "Max Site Candidates",
                min_value=1,
                max_value=10,
                value=int(st.session_state.pending_settings.get("LAYOUT_MAX_CANDIDATES", 3)),
                help="Maximum number of candidate solar sites to return.",
            )

        st.markdown("---")
        st.markdown("**Advanced (Pole Snapping)**")
        col1, col2 = st.columns(2)
        with col1:
            st.session_state.pending_settings["LAYOUT_POLE_DEDUP_DISTANCE_M"] = st.number_input(
                "Pole Dedup Distance (m)",
                min_value=0.5,
                max_value=20.0,
                value=float(
                    st.session_state.pending_settings.get("LAYOUT_POLE_DEDUP_DISTANCE_M", 5.0)
                ),
                step=0.5,
                format="%.1f",
                help="Minimum distance between poles before deduplication.",
            )
            st.session_state.pending_settings["LAYOUT_MERGE_GAP_THRESHOLD_M"] = st.number_input(
                "Merge Gap Threshold (m)",
                min_value=0.5,
                max_value=20.0,
                value=float(
                    st.session_state.pending_settings.get("LAYOUT_MERGE_GAP_THRESHOLD_M", 5.0)
                ),
                step=0.5,
                format="%.1f",
                help="Gap smaller than this: absorb last pole into intersection.",
            )
        with col2:
            st.session_state.pending_settings["LAYOUT_SNAP_NODE_TOLERANCE_M"] = st.number_input(
                "Snap Node Tolerance (m)",
                min_value=0.1,
                max_value=10.0,
                value=float(
                    st.session_state.pending_settings.get("LAYOUT_SNAP_NODE_TOLERANCE_M", 1.0)
                ),
                step=0.1,
                format="%.1f",
                help="Tolerance for matching edge endpoints to road network nodes.",
            )
            st.session_state.pending_settings["LAYOUT_REDISTRIBUTE_GAP_MAX_M"] = st.number_input(
                "Redistribute Gap Max (m)",
                min_value=1.0,
                max_value=30.0,
                value=float(
                    st.session_state.pending_settings.get("LAYOUT_REDISTRIBUTE_GAP_MAX_M", 10.0)
                ),
                step=0.5,
                format="%.1f",
                help="Gap between this and merge threshold: redistribute last two poles.",
            )

        st.markdown("---")
        st.markdown("**Templates**")
        st.caption(
            "Google Drive IDs for templates used by the LPP workflow. "
            "Update these to point at a new template without redeploying."
        )

        st.session_state.pending_settings["LPP_TEMPLATE_ID"] = st.text_input(
            "LPP Spreadsheet Template (Drive ID)",
            value=st.session_state.pending_settings.get("LPP_TEMPLATE_ID", ""),
            help="Google Drive file ID of the LPP spreadsheet template that gets copied per site.",
        )

        st.session_state.pending_settings["QGIS_TEMPLATE_FILE_ID"] = st.text_input(
            "QGIS Project Template (Drive ID)",
            value=st.session_state.pending_settings.get("QGIS_TEMPLATE_FILE_ID", ""),
            help="Google Drive file ID of the .qgs template used for distribution design export.",
        )

        st.session_state.pending_settings["LPP_OUTPUT_FOLDER_ID"] = st.text_input(
            "LPP Output Folder (Drive ID)",
            value=st.session_state.pending_settings.get("LPP_OUTPUT_FOLDER_ID", ""),
            help="Google Drive folder where per-site LPP output folders are created.",
        )

        if st.button("🔍 Verify template access", key="verify_templates"):
            _verify_template_access(current_settings)

    with st.expander("📦 Reference Server", expanded=False):
        st.caption("Nigerian import regulatory data — staff-only tools.")

        st.session_state.pending_settings["NIGERIA_IMPORT_TARIFF_SHEET_ID"] = st.text_input(
            "Nigeria Import Tariff Sheet ID",
            value=st.session_state.pending_settings.get("NIGERIA_IMPORT_TARIFF_SHEET_ID", ""),
            help="Google Sheets file ID for the Nigeria Customs import tariff schedule (columns: CET Code, Description, SU, ID, VAT, LVY, EXC, DOV).",
        )

        st.session_state.pending_settings["NIGERIA_IMPORT_STANDARDS_PDF_ID"] = st.text_input(
            "Nigeria Import Standards PDF ID",
            value=st.session_state.pending_settings.get("NIGERIA_IMPORT_STANDARDS_PDF_ID", ""),
            help="Google Drive file ID for the Nigeria Import Standards PDF (tables with Item, H S Codes, Remarks columns).",
        )

    # Section 7: System Info (Read-only)
    with st.expander("ℹ️ System Configuration (Read-Only)", expanded=True):
        # Row 1: Model Configuration
        st.markdown("**AI Models**")
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            st.text_input(
                "Primary Model",
                value=current_settings.get("GEMINI_MODEL", ""),
                disabled=True,
                help="Main Gemini model for chat responses",
            )

        with col2:
            st.text_input(
                "Fallback Model",
                value=current_settings.get("GEMINI_FALLBACK_MODEL", ""),
                disabled=True,
                help="Used when primary model is rate-limited",
            )

        with col3:
            st.text_input(
                "Verification Model",
                value=current_settings.get("VERIFICATION_MODEL", ""),
                disabled=True,
                help="LLM-as-judge model for response verification",
            )

        with col4:
            st.text_input(
                "Embedding Model",
                value=current_settings.get("EMBEDDING_MODEL", ""),
                disabled=True,
                help="Google AI Studio model for RAG vector embeddings",
            )

        col5, col6, col7, col8 = st.columns(4)
        with col5:
            st.text_input(
                "Deep Thinking Model",
                value=current_settings.get("GEMINI_DEEP_THINKING_MODEL", ""),
                disabled=True,
                help="Model for complex tasks (doc editing, analysis). No thinking budget cap.",
            )

        # Row 1b: Output Token Limits
        st.markdown("**Output Token Limits**")
        col1, col2 = st.columns(2)

        with col1:
            st.text_input(
                "Primary Model Max Tokens",
                value=str(current_settings.get("GEMINI_MAX_OUTPUT_TOKENS", "8192")),
                disabled=True,
                help="Max output tokens for primary model (includes thinking tokens)",
            )

        with col2:
            st.text_input(
                "Lite Model Max Tokens",
                value=str(current_settings.get("GEMINI_LITE_MAX_OUTPUT_TOKENS", "1024")),
                disabled=True,
                help="Max output tokens for lite/verification model",
            )

        # Row 2: System Instruction Documents
        st.markdown("**System Instruction Documents**")
        col1, col2, col3 = st.columns(3)

        with col1:
            st.text_input(
                "Customer Support Doc",
                value=current_settings.get("CUSTOMER_SUPPORT_DOC_ID", ""),
                disabled=True,
                help="Google Doc for customer mode system instructions",
            )

        with col2:
            st.text_input(
                "Staff Support Doc",
                value=current_settings.get("STAFF_SUPPORT_DOC_ID", ""),
                disabled=True,
                help="Google Doc for staff mode system instructions",
            )

        with col3:
            st.text_input(
                "Troubleshooting Procedures Doc",
                value=current_settings.get("TROUBLESHOOTING_PROCEDURES_DOC_ID", ""),
                disabled=True,
                help="Google Doc for troubleshooting procedures (appended to both modes)",
            )

        # Row 2b: Additional docs
        col1, col2 = st.columns(2)

        with col1:
            st.text_input(
                "Expert Instructions Doc",
                value=current_settings.get("EXPERT_INSTRUCTIONS_DOC_ID", ""),
                disabled=True,
                help="Google Doc defining expert workflows, packet types, and step handlers",
            )

        with col2:
            st.text_input(
                "Verification Criteria Doc",
                value=current_settings.get("VERIFICATION_DOC_ID", ""),
                disabled=True,
                help="Google Doc for verification criteria (used for both response and broadcast verification)",
            )

        # Row 3: Telegram Settings
        st.markdown("**Telegram Settings**")
        col1, col2 = st.columns(2)

        with col1:
            st.text_input(
                "Escalation Chat ID",
                value=current_settings["ESCALATION_TELEGRAM_CHAT_ID"],
                disabled=True,
            )

        with col2:
            st.text_input(
                "Debug Chat ID",
                value=current_settings["DEBUG_TELEGRAM_CHAT_ID"],
                disabled=True,
            )

    # Auto-save any immediate (no-restart) changes silently.
    # Runs after all widgets so pending_settings reflects the latest values.
    _auto_save_immediate_changes(settings_service)

    # Show Save & Restart banner only when restart-required settings have changed.
    restart_changes = {
        k: v
        for k, v in st.session_state.pending_settings.items()
        if k in RESTART_REQUIRED_KEYS and v != st.session_state.current_settings.get(k)
    }

    if restart_changes:
        st.divider()
        labels = [_RESTART_KEY_LABELS.get(k, k) for k in restart_changes]
        st.warning(f"⚠️ Restart required to apply: **{', '.join(labels)}**")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("↩️ Reset", use_container_width=True):
                for k in RESTART_REQUIRED_KEYS:
                    if k in st.session_state.pending_settings:
                        st.session_state.pending_settings[k] = (
                            st.session_state.current_settings.get(k)
                        )
                st.rerun()
        with col2:
            if st.button("💾 Save & Restart", type="primary", use_container_width=True):
                _show_restart_confirmation_dialog()


_TEMPLATE_DRIVE_IDS = {
    "LPP_TEMPLATE_ID": "LPP Spreadsheet Template",
    "QGIS_TEMPLATE_FILE_ID": "QGIS Project Template",
    "LPP_OUTPUT_FOLDER_ID": "LPP Output Folder",
}


def _verify_drive_file(file_id: str) -> tuple[bool, str]:
    """Check if a Drive file ID is accessible. Returns (ok, name_or_error)."""
    try:
        from googleapiclient.discovery import build

        from shared.utils.google_auth import get_drive_write_credentials

        service = build("drive", "v3", credentials=get_drive_write_credentials())
        meta = (
            service.files()
            .get(fileId=file_id, fields="id, name, mimeType", supportsAllDrives=True)
            .execute()
        )
        return True, meta.get("name", file_id)
    except Exception as exc:
        return False, str(exc)


def _verify_template_access(current_settings: Dict[str, Any]):
    """Verify Drive access for all template IDs that changed."""
    pending = st.session_state.pending_settings
    any_checked = False
    for key, label in _TEMPLATE_DRIVE_IDS.items():
        file_id = pending.get(key, "").strip()
        if not file_id:
            continue
        # Only verify if the value changed or if explicitly requested
        any_checked = True
        ok, detail = _verify_drive_file(file_id)
        if ok:
            st.success(f"**{label}**: accessible — *{detail}*")
        else:
            st.error(f"**{label}**: cannot access `{file_id}` — {detail}")
    if not any_checked:
        st.info("No template IDs to verify.")


def _validate_changed_templates(current_settings: Dict[str, Any]) -> list[str]:
    """Validate Drive access for template IDs that changed. Returns list of errors."""
    pending = st.session_state.pending_settings
    errors = []
    for key, label in _TEMPLATE_DRIVE_IDS.items():
        old_val = current_settings.get(key, "")
        new_val = pending.get(key, "").strip()
        if new_val and new_val != old_val:
            ok, detail = _verify_drive_file(new_val)
            if not ok:
                errors.append(f"{label} (`{key}`): cannot access `{new_val}` — {detail}")
    return errors


_AUTOSAVE_DEBOUNCE_SECONDS = 3


def _auto_save_immediate_changes(settings_service: SettingsService) -> None:
    """Save any changed immediate (no-restart) settings to DO silently.

    Called after all widgets have rendered so pending_settings is up to date.
    Restart-required settings are intentionally excluded — they need the
    Save & Restart flow.

    Debounced: the DO API call (2-5s) only fires after a 3-second quiet period
    with no further changes, so rapid widget interactions produce a single save
    rather than one blocking call per click.
    """
    current = st.session_state.current_settings
    pending = st.session_state.pending_settings

    immediate_changes = {
        k: v
        for k, v in pending.items()
        if k not in RESTART_REQUIRED_KEYS and k not in _READONLY_KEYS and current.get(k) != v
    }

    if not immediate_changes:
        # No pending changes — clear any armed debounce timer
        st.session_state.pop("_autosave_pending_since", None)
        return

    now = time.monotonic()
    if "_autosave_pending_since" not in st.session_state:
        # First render with this change — arm the debounce timer and wait
        st.session_state._autosave_pending_since = now
        return

    if now - st.session_state._autosave_pending_since < _AUTOSAVE_DEBOUNCE_SECONDS:
        # Still within the debounce window — skip this render
        return

    # Debounce window elapsed — fire the save
    st.session_state.pop("_autosave_pending_since", None)

    # Build save dict: current saved values + the immediate changes only
    save_dict = {**current, **immediate_changes}

    success, error_msg = settings_service.update_settings(save_dict, restart_bot=False)
    if success:
        for k, v in immediate_changes.items():
            st.session_state.current_settings[k] = v
        # Grafana panel enablement also needs to propagate to Supabase for MCP hot-reload
        if "GRAFANA_ENABLED_PANELS" in immediate_changes:
            _sync_enabled_panels_to_supabase(immediate_changes["GRAFANA_ENABLED_PANELS"])
    else:
        st.toast(f"⚠️ Auto-save failed: {error_msg}", icon="⚠️")


@st.dialog("Save & Restart")
def _show_restart_confirmation_dialog():
    """Confirm before saving restart-required settings and triggering a deployment."""
    st.warning(
        "These settings require a bot restart. The bot will be unavailable for ~2-3 minutes."
    )

    # Validate changed template IDs before allowing save
    template_errors = _validate_changed_templates(st.session_state.current_settings)
    if template_errors:
        st.error("**Cannot save — template access check failed:**")
        for err in template_errors:
            st.markdown(f"- {err}")
        if st.button("Close", use_container_width=True):
            st.rerun()
        return

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Cancel", use_container_width=True):
            st.rerun()
    with col2:
        if st.button("✅ Confirm & Restart", type="primary", use_container_width=True):
            _apply_settings()


def _apply_settings() -> None:
    """Save all pending settings (including restart-required) and trigger a deployment."""
    settings_service = SettingsService()

    # Merge current saved state with all pending changes
    merged = {**st.session_state.current_settings, **st.session_state.pending_settings}

    with st.spinner("Saving and restarting..."):
        success, error_msg = settings_service.update_settings(merged, restart_bot=True)

    if success:
        st.success("✅ Settings saved. Bot is restarting (~2-3 min).")
        st.session_state.current_settings = merged.copy()
        if "pending_settings" in st.session_state:
            del st.session_state.pending_settings
        if "_last_settings_refresh" in st.session_state:
            del st.session_state._last_settings_refresh
        st.rerun()
    else:
        st.error(f"❌ Failed to save: {error_msg}")


def _sync_enabled_panels_to_supabase(enabled_panels_str: str) -> None:
    """Push enabled panel selections directly to Supabase enabled_panel_ids.

    This lets the MCP server hot-reload the new selection on the next tool
    listing without requiring a full Grafana sync.
    """
    from services.grafana_metadata_service import load_available_dashboards, update_enabled_panels

    # Group enabled panel keys by dashboard_uid
    by_dashboard: dict = {}
    for key in (k.strip() for k in enabled_panels_str.split(",") if k.strip()):
        parts = key.split(":", 1)
        if len(parts) == 2:
            uid, panel_id = parts
            by_dashboard.setdefault(uid, []).append(panel_id)

    # Update every known dashboard — including those with all panels deselected.
    # Without this, a dashboard whose last panel was removed would keep its old
    # enabled_panel_ids in Supabase and the MCP server would keep exposing them.
    all_uids = set(load_available_dashboards().keys())
    for uid in all_uids:
        update_enabled_panels(uid, by_dashboard.get(uid, []))


def _trigger_grafana_sync():
    """Trigger Grafana dashboard and panel sync immediately with live progress."""
    import os
    import subprocess
    import sys
    from datetime import datetime

    # Run the grafana indexer script from local scripts folder
    script_path = os.path.join(
        os.path.dirname(__file__),
        "../scripts/grafana_indexer_incremental.py",
    )

    # Use a status container to show live progress
    status_container = st.status("🔄 Running Grafana sync...", expanded=True)

    # Initialize log storage
    sync_logs = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stdout": [],
        "stderr": [],
        "exit_code": None,
        "exception": None,
        "has_errors": False,
    }

    try:
        # Use Popen to stream output in real-time
        process = subprocess.Popen(
            [sys.executable, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
        )

        stdout_lines = []
        stderr_lines = []

        # Stream stdout and stderr
        with status_container:
            st.write("📊 Sync in progress...")
            stdout_placeholder = st.empty()
            stderr_placeholder = st.empty()

            # Read output line by line
            while True:
                # Check if process is still running
                if process.poll() is not None:
                    # Process finished, read remaining output
                    remaining_out, remaining_err = process.communicate()
                    if remaining_out:
                        stdout_lines.extend(remaining_out.splitlines())
                    if remaining_err:
                        stderr_lines.extend(remaining_err.splitlines())
                    break

                # Read stdout
                if process.stdout:
                    line = process.stdout.readline()
                    if line:
                        stdout_lines.append(line.rstrip())
                        # Show last 10 lines
                        stdout_placeholder.code("\n".join(stdout_lines[-10:]), language="text")

                # Read stderr
                if process.stderr:
                    err_line = process.stderr.readline()
                    if err_line:
                        stderr_lines.append(err_line.rstrip())
                        # Show errors
                        if stderr_lines:
                            stderr_placeholder.error("\n".join(stderr_lines[-5:]))

                # Small delay to prevent excessive updates
                import time

                time.sleep(0.1)

        # Save logs to session state for persistence
        sync_logs["stdout"] = stdout_lines
        sync_logs["stderr"] = stderr_lines
        sync_logs["exit_code"] = process.returncode
        sync_logs["has_errors"] = process.returncode != 0 or bool(stderr_lines)
        st.session_state.grafana_sync_logs = sync_logs

        # Check result
        if process.returncode == 0:
            status_container.update(label="✅ Grafana sync completed!", state="complete")
            st.success("🔄 Refreshing settings from DigitalOcean...")

            # Force reload settings from DigitalOcean API to get updated metadata
            settings_service = SettingsService()
            fresh_settings = settings_service.get_current_settings(fetch_from_do=True)

            # Update session state with fresh settings
            st.session_state.current_settings = fresh_settings.copy()
            st.session_state.pending_settings = fresh_settings.copy()

            # Clear other caches
            if "_last_settings_refresh" in st.session_state:
                del st.session_state._last_settings_refresh
            load_available_dashboards.clear()
            load_panels_metadata.clear()

            st.rerun()
        else:
            status_container.update(label="❌ Grafana sync failed", state="error")
            if stderr_lines:
                st.error(f"Error output:\n\n{chr(10).join(stderr_lines)}")

    except Exception as e:
        sync_logs["exception"] = str(e)
        sync_logs["has_errors"] = True
        st.session_state.grafana_sync_logs = sync_logs
        status_container.update(label="❌ Grafana sync error", state="error")
        st.error(f"Failed to run Grafana sync: {e}")
