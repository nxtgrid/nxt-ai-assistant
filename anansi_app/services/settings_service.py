"""
Settings management service for Anansi App.

Manages bot configuration settings stored in DigitalOcean app environment variables.

IMPORTANT: DigitalOcean has a limit of ~65KB per environment variable value.
Large JSON values (like GRAFANA_PANELS_METADATA) should NOT be saved to DO env vars.
These internal metadata fields are managed by sync scripts, not the settings UI.
"""

import os
from typing import Any, Dict, List, Optional

import requests

# Maximum size for env var values (conservative limit, DO allows ~65KB)
MAX_ENV_VAR_SIZE = 32000  # 32KB

# Settings that should NOT be saved to DigitalOcean via the settings UI
# These are either read-only system configs or internal metadata managed by sync scripts
DO_NOT_SAVE_TO_DO = {
    # Read-only system configuration (managed via code/deployment)
    # Note: LOG_LEVEL is intentionally NOT here - it can be changed via settings UI
    "ESCALATION_TELEGRAM_CHAT_ID",
    "DEBUG_TELEGRAM_CHAT_ID",
    "GEMINI_MODEL",
    "GEMINI_FALLBACK_MODEL",
    "VERIFICATION_MODEL",
    "EMBEDDING_MODEL",
    "GEMINI_MAX_OUTPUT_TOKENS",
    "GEMINI_LITE_MAX_OUTPUT_TOKENS",
    "CUSTOMER_SUPPORT_DOC_ID",
    "STAFF_SUPPORT_DOC_ID",
    "TROUBLESHOOTING_PROCEDURES_DOC_ID",
    # Internal metadata managed by sync scripts, not settings UI
    # These can be very large JSON strings that exceed env var limits
    "GRAFANA_PANELS_METADATA",
    "GRAFANA_AVAILABLE_DASHBOARDS",
}


class SettingsService:
    """Manage bot settings via DigitalOcean API."""

    def __init__(self):
        """Initialize settings service."""
        self.app_id = os.getenv("DIGITALOCEAN_APP_ID", "")
        # Strip whitespace from token to avoid issues with copy-paste
        raw_token = os.getenv("DIGITALOCEAN_API_TOKEN")
        self.api_token = raw_token.strip() if raw_token else None
        self.api_base = "https://api.digitalocean.com/v2"

    def _fetch_all_envs_from_do(self) -> Dict[str, str]:
        """
        Fetch ALL environment variables from DigitalOcean app spec in ONE API call.
        Returns a flat dict of key -> value for both global and service-specific envs.
        """
        # Settings that belong to specific services (not global)
        SERVICE_SPECIFIC_SETTINGS = {
            "EQUIPMENT_CONTROL_ALLOWED_USERS": "anansi-bot",
            "VERIFICATION_ENABLED": "anansi-bot",
            "VERIFICATION_DOC_ID": "anansi-bot",
            "VERIFICATION_MODEL": "anansi-bot",
            "WORKFLOW_PARAMETER_CONFIRMATION": "anansi-bot",
            "INLINE_BUTTONS_ENABLED": "anansi-bot",
            "PROCEDURE_BUTTONS_ENABLED": "anansi-bot",
            "MINI_APP_FORMS_ENABLED": "anansi-bot",
            "CONTEXT_FILTER_ENABLED": "anansi-bot",
            "THREAD_DISENTANGLEMENT_ENABLED": "anansi-bot",
            "ACTIVE_THREAD_WINDOW_MINUTES": "anansi-bot",
            "CONVERSATION_SUMMARY_ENABLED": "anansi-bot",
            "PERSISTENT_AGENTS_ENABLED": "anansi-bot",
            "LANGFUSE_ENABLED": "anansi-bot",
            "AWAITING_INPUT_TIMEOUT_MINUTES": "anansi-bot",
            "EXPERT_INSTRUCTIONS_DOC_ID": "anansi-bot",
            "GEMINI_DEEP_THINKING_MODEL": "anansi-bot",
            "LPP_TEMPLATE_ID": "anansi-bot",
            "QGIS_TEMPLATE_FILE_ID": "anansi-bot",
            "LPP_OUTPUT_FOLDER_ID": "anansi-bot",
            "LAYOUT_POLE_SPACING_M": "anansi-bot",
            "LAYOUT_MAX_DROP_DISTANCE_M": "anansi-bot",
            "LAYOUT_TARGET_COVERAGE_PCT": "anansi-bot",
            "LAYOUT_SQM_PER_KWP": "anansi-bot",
            "LAYOUT_KWP_PER_BUILDING": "anansi-bot",
            "LAYOUT_MIN_ESTIMATED_KWP": "anansi-bot",
            "LAYOUT_BUILDING_BUFFER_M": "anansi-bot",
            "LAYOUT_SITE_SETBACK_M": "anansi-bot",
            "LAYOUT_ROAD_SETBACK_M": "anansi-bot",
            "LAYOUT_CORRIDOR_CLEARANCE_M": "anansi-bot",
            "LAYOUT_CANOPY_THRESHOLD_M": "anansi-bot",
            "LAYOUT_MIN_CANDIDATE_SEPARATION_M": "anansi-bot",
            "LAYOUT_MAX_CANDIDATES": "anansi-bot",
            "LAYOUT_POLE_DEDUP_DISTANCE_M": "anansi-bot",
            "LAYOUT_SNAP_NODE_TOLERANCE_M": "anansi-bot",
            "LAYOUT_MERGE_GAP_THRESHOLD_M": "anansi-bot",
            "LAYOUT_REDISTRIBUTE_GAP_MAX_M": "anansi-bot",
            "LAYOUT_LIGHTNING_RADIUS_M": "anansi-bot",
            "NIGERIA_IMPORT_TARIFF_SHEET_ID": "anansi-bot",
            "NIGERIA_IMPORT_STANDARDS_PDF_ID": "anansi-bot",
        }

        result: Dict[str, str] = {}

        if not self.api_token:
            return result

        try:
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            }
            response = requests.get(
                f"{self.api_base}/apps/{self.app_id}", headers=headers, timeout=10
            )
            response.raise_for_status()

            app_data = response.json()
            spec = app_data.get("app", {}).get("spec", {})

            # Collect global envs
            for env in spec.get("envs", []):
                key = env.get("key")
                value = env.get("value")
                if key and value is not None:
                    result[key] = str(value)

            # Collect service-specific envs (these override globals if present)
            for service in spec.get("services", []):
                service_name = service.get("name")
                for env in service.get("envs", []):
                    key = env.get("key")
                    value = env.get("value")
                    # Only include if this key belongs to this service
                    if key and value is not None:
                        if key in SERVICE_SPECIFIC_SETTINGS:
                            if SERVICE_SPECIFIC_SETTINGS[key] == service_name:
                                result[key] = str(value)

            return result

        except Exception as e:
            print(f"Failed to fetch envs from DO: {e}")
            return result

    def get_current_settings(self, fetch_from_do: bool = False) -> Dict[str, Any]:
        """
        Get current settings from environment variables.

        Args:
            fetch_from_do: If True, fetch fresh values from DigitalOcean API instead of local env

        Returns:
            Dict of setting names to current values
        """
        # Fetch all envs from DO in ONE API call if requested
        do_envs: Dict[str, str] = {}
        if fetch_from_do:
            do_envs = self._fetch_all_envs_from_do()

        # Helper to get env value - uses DO cache if available, else local env
        def getenv(key: str, default: str = "") -> str:
            if fetch_from_do and key in do_envs:
                return do_envs[key]
            return os.getenv(key, default)

        return {
            # Bot Behavior & Core Settings
            "ALLOW_PARALLEL_CALLS": getenv("ALLOW_PARALLEL_CALLS", "true").lower() == "true",
            "MAX_TOOL_ROUNDS": int(getenv("MAX_TOOL_ROUNDS", "5")),
            # AI Model Settings
            "GEMINI_MODEL": getenv("GEMINI_MODEL", "gemini-flash-latest"),
            "GEMINI_FALLBACK_MODEL": getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash"),
            "GEMINI_TEMPERATURE": float(getenv("GEMINI_TEMPERATURE", "0.2")),
            "VERIFICATION_MODEL": getenv("VERIFICATION_MODEL", "gemini-2.5-flash-lite"),
            "GEMINI_MAX_OUTPUT_TOKENS": int(getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192")),
            "GEMINI_LITE_MAX_OUTPUT_TOKENS": int(getenv("GEMINI_LITE_MAX_OUTPUT_TOKENS", "1024")),
            # MCP Server Enable/Disable - each server can be toggled independently
            # Format: {SERVER_NAME}_ENABLED, default: true
            "EQUIPMENT_DIAGNOSTICS_ENABLED": getenv("EQUIPMENT_DIAGNOSTICS_ENABLED", "true").lower()
            == "true",
            "JIRA_ENABLED": getenv("JIRA_ENABLED", "true").lower() == "true",
            "METERS_ENABLED": getenv("METERS_ENABLED", "true").lower() == "true",
            "EQUIPMENT_CONTROL_ENABLED": getenv("EQUIPMENT_CONTROL_ENABLED", "true").lower()
            == "true",
            "CUSTOMER_ENABLED": getenv("CUSTOMER_ENABLED", "true").lower() == "true",
            "GRAFANA_ENABLED": getenv("GRAFANA_ENABLED", "true").lower() == "true",
            "SCHEDULE_ENABLED": getenv("SCHEDULE_ENABLED", "true").lower() == "true",
            "META_ENABLED": getenv("META_ENABLED", "true").lower() == "true",
            "GRID_DESIGN_ENABLED": getenv("GRID_DESIGN_ENABLED", "true").lower() == "true",
            "SOLAR_ENABLED": getenv("SOLAR_ENABLED", "true").lower() == "true",
            "KNOWLEDGE_ENABLED": getenv("KNOWLEDGE_ENABLED", "true").lower() == "true",
            "MESSAGING_ENABLED": getenv("MESSAGING_ENABLED", "true").lower() == "true",
            "REFERENCE_ENABLED": getenv("REFERENCE_ENABLED", "true").lower() == "true",
            "LOGS_ENABLED": getenv("LOGS_ENABLED", "true").lower() == "true",
            "CODEBASE_ENABLED": getenv("CODEBASE_ENABLED", "true").lower() == "true",
            "PAYMENT_PROCESSOR_ENABLED": getenv("PAYMENT_PROCESSOR_ENABLED", "true").lower()
            == "true",
            # MCP Disabled Tools - JSON array of "server:tool" strings
            "MCP_DISABLED_TOOLS": getenv("MCP_DISABLED_TOOLS", "[]"),
            # RAG Settings
            "rag__enabled": getenv("rag__enabled", "false").lower() == "true",
            "rag__top_k": int(getenv("rag__top_k", "5")),
            # Metrics & Monitoring
            "METRICS_ENABLED": getenv("METRICS_ENABLED", "true").lower() == "true",
            "METRICS_SCHEDULE_HOUR": int(getenv("METRICS_SCHEDULE_HOUR", "9")),
            "BOT_ENABLED": getenv("BOT_ENABLED", "true").lower() == "true",
            # Response Verification (LLM-as-judge for customer mode)
            "VERIFICATION_ENABLED": getenv("VERIFICATION_ENABLED", "false").lower() == "true",
            "VERIFICATION_DOC_ID": getenv("VERIFICATION_DOC_ID", ""),
            # Expert Workflow Settings
            "WORKFLOW_PARAMETER_CONFIRMATION": getenv(
                "WORKFLOW_PARAMETER_CONFIRMATION", "true"
            ).lower()
            == "true",
            # Telegram Inline Buttons for Decision Prompts
            "INLINE_BUTTONS_ENABLED": getenv("INLINE_BUTTONS_ENABLED", "false").lower() == "true",
            # Procedure Buttons for Customer Support Conversations
            "PROCEDURE_BUTTONS_ENABLED": getenv("PROCEDURE_BUTTONS_ENABLED", "false").lower()
            == "true",
            # Mini App Forms (Telegram WebApp popups for expert workflow parameters)
            "MINI_APP_FORMS_ENABLED": getenv("MINI_APP_FORMS_ENABLED", "false").lower() == "true",
            # Conversation Intelligence (context filter + summarization)
            "CONTEXT_FILTER_ENABLED": getenv("CONTEXT_FILTER_ENABLED", "false").lower() == "true",
            "THREAD_DISENTANGLEMENT_ENABLED": getenv(
                "THREAD_DISENTANGLEMENT_ENABLED", "false"
            ).lower()
            == "true",
            "ACTIVE_THREAD_WINDOW_MINUTES": int(getenv("ACTIVE_THREAD_WINDOW_MINUTES", "60")),
            "CONVERSATION_SUMMARY_ENABLED": getenv("CONVERSATION_SUMMARY_ENABLED", "false").lower()
            == "true",
            # Persistent Agents
            "PERSISTENT_AGENTS_ENABLED": getenv("PERSISTENT_AGENTS_ENABLED", "false").lower()
            == "true",
            # Observability
            "LANGFUSE_ENABLED": getenv("LANGFUSE_ENABLED", "false").lower() == "true",
            # Grafana Settings
            "GRAFANA_URL": getenv("GRAFANA_URL", "http://localhost:3000"),
            "GRAFANA_USERNAME": getenv("GRAFANA_USERNAME", ""),
            "GRAFANA_PASSWORD": getenv("GRAFANA_PASSWORD", ""),
            "GRAFANA_FOLDER_NAME": getenv("GRAFANA_FOLDER_NAME", ""),
            "GRAFANA_PANEL_DESCRIPTION_PROMPT": getenv(
                "GRAFANA_PANEL_DESCRIPTION_PROMPT",
                "You are a system that generates tool descriptions for Grafana dashboard panels. Given a panel with title, description, query, and dashboard variables, create a concise tool description that explains what data this panel shows and what variables it requires. Format: A tool description suitable for an LLM to understand when to use this panel.",
            ),
            "GRAFANA_ENABLED_PANELS": getenv("GRAFANA_ENABLED_PANELS", ""),
            "GRAFANA_PANELS_METADATA": getenv("GRAFANA_PANELS_METADATA", "{}"),
            "GRAFANA_AVAILABLE_DASHBOARDS": getenv("GRAFANA_AVAILABLE_DASHBOARDS", "{}"),
            "GRAFANA_ENABLED_DASHBOARDS": getenv("GRAFANA_ENABLED_DASHBOARDS", ""),
            "GRAFANA_SYNC_HOUR": int(getenv("GRAFANA_SYNC_HOUR", "2")),
            "GRAFANA_FORCE_FULL_REINDEX": getenv("GRAFANA_FORCE_FULL_REINDEX", "false").lower()
            == "true",
            # Access Control
            "ALLOWED_VIEWER_EMAILS": getenv("ALLOWED_VIEWER_EMAILS", ""),
            # Equipment Control Access (anansi-bot service-specific env var)
            "EQUIPMENT_CONTROL_ALLOWED_USERS": getenv("EQUIPMENT_CONTROL_ALLOWED_USERS", ""),
            # Expert (Subagent) Settings
            "AWAITING_INPUT_TIMEOUT_MINUTES": int(getenv("AWAITING_INPUT_TIMEOUT_MINUTES", "180")),
            # Logging (editable via settings UI)
            "LOG_LEVEL": getenv("LOG_LEVEL", "INFO"),
            # System Config (read-only)
            "ESCALATION_TELEGRAM_CHAT_ID": getenv("ESCALATION_TELEGRAM_CHAT_ID", ""),
            "DEBUG_TELEGRAM_CHAT_ID": getenv("DEBUG_TELEGRAM_CHAT_ID", ""),
            # System Instruction Documents (read-only)
            "CUSTOMER_SUPPORT_DOC_ID": getenv("CUSTOMER_SUPPORT_DOC_ID", ""),
            "STAFF_SUPPORT_DOC_ID": getenv("STAFF_SUPPORT_DOC_ID", ""),
            "TROUBLESHOOTING_PROCEDURES_DOC_ID": getenv("TROUBLESHOOTING_PROCEDURES_DOC_ID", ""),
            "EXPERT_INSTRUCTIONS_DOC_ID": getenv("EXPERT_INSTRUCTIONS_DOC_ID", ""),
            # AI Model Settings (service-specific, read-only in config section)
            "GEMINI_DEEP_THINKING_MODEL": getenv("GEMINI_DEEP_THINKING_MODEL", ""),
            # Note: VERIFICATION_DOC_ID is already included above (line 130)
            # and is used for both response and broadcast verification
            # RAG Embedding Model (read-only)
            # Note: text-embedding-005 is NOT supported by genai v1beta API
            "EMBEDDING_MODEL": getenv("EMBEDDING_MODEL", "gemini-embedding-001"),
            # Layout & Site Selection
            "LAYOUT_POLE_SPACING_M": float(getenv("LAYOUT_POLE_SPACING_M", "45.0")),
            "LAYOUT_MAX_DROP_DISTANCE_M": float(getenv("LAYOUT_MAX_DROP_DISTANCE_M", "40.0")),
            "LAYOUT_TARGET_COVERAGE_PCT": float(getenv("LAYOUT_TARGET_COVERAGE_PCT", "90.0")),
            "LAYOUT_SQM_PER_KWP": float(getenv("LAYOUT_SQM_PER_KWP", "15.5")),
            "LAYOUT_KWP_PER_BUILDING": float(getenv("LAYOUT_KWP_PER_BUILDING", "0.25")),
            "LAYOUT_MIN_ESTIMATED_KWP": float(getenv("LAYOUT_MIN_ESTIMATED_KWP", "30.0")),
            "LAYOUT_BUILDING_BUFFER_M": float(getenv("LAYOUT_BUILDING_BUFFER_M", "15.0")),
            "LAYOUT_SITE_SETBACK_M": float(getenv("LAYOUT_SITE_SETBACK_M", "5.0")),
            "LAYOUT_ROAD_SETBACK_M": float(getenv("LAYOUT_ROAD_SETBACK_M", "5.0")),
            "LAYOUT_CORRIDOR_CLEARANCE_M": float(getenv("LAYOUT_CORRIDOR_CLEARANCE_M", "10.0")),
            "LAYOUT_CANOPY_THRESHOLD_M": float(getenv("LAYOUT_CANOPY_THRESHOLD_M", "5.0")),
            "LAYOUT_MIN_CANDIDATE_SEPARATION_M": float(
                getenv("LAYOUT_MIN_CANDIDATE_SEPARATION_M", "100.0")
            ),
            "LAYOUT_MAX_CANDIDATES": int(getenv("LAYOUT_MAX_CANDIDATES", "3")),
            "LAYOUT_POLE_DEDUP_DISTANCE_M": float(getenv("LAYOUT_POLE_DEDUP_DISTANCE_M", "5.0")),
            "LAYOUT_SNAP_NODE_TOLERANCE_M": float(getenv("LAYOUT_SNAP_NODE_TOLERANCE_M", "1.0")),
            "LAYOUT_MERGE_GAP_THRESHOLD_M": float(getenv("LAYOUT_MERGE_GAP_THRESHOLD_M", "5.0")),
            "LAYOUT_REDISTRIBUTE_GAP_MAX_M": float(getenv("LAYOUT_REDISTRIBUTE_GAP_MAX_M", "10.0")),
            "LAYOUT_LIGHTNING_RADIUS_M": float(getenv("LAYOUT_LIGHTNING_RADIUS_M", "13.5")),
            # Reference Server (Nigerian import data — staff only)
            "NIGERIA_IMPORT_TARIFF_SHEET_ID": getenv("NIGERIA_IMPORT_TARIFF_SHEET_ID", ""),
            "NIGERIA_IMPORT_STANDARDS_PDF_ID": getenv("NIGERIA_IMPORT_STANDARDS_PDF_ID", ""),
        }

    def update_settings(
        self, settings: Dict[str, Any], restart_bot: bool = True
    ) -> tuple[bool, Optional[str]]:
        """
        Update settings in DigitalOcean app spec.

        Args:
            settings: Dict of setting names to new values
            restart_bot: Whether to trigger a new deployment

        Returns:
            Tuple of (success: bool, error_message: Optional[str])
        """
        if not self.api_token:
            return False, "No DigitalOcean API token configured"

        try:
            # Get current app spec
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            }

            # Get current app (which includes spec)
            url = f"{self.api_base}/apps/{self.app_id}"
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 401:
                return False, "Authentication failed - invalid API token"
            elif response.status_code == 403:
                return (
                    False,
                    "Permission denied - API token needs write access to apps",
                )
            elif response.status_code == 404:
                return (
                    False,
                    f"App not found - token may not have access to app {self.app_id} or app doesn't exist",
                )
            elif response.status_code != 200:
                try:
                    error_detail = response.json()
                    return (
                        False,
                        f"Failed to fetch app spec: HTTP {response.status_code} - {error_detail}",
                    )
                except Exception:
                    return (
                        False,
                        f"Failed to fetch app: HTTP {response.status_code} - {response.text[:200]}",
                    )

            # Get the spec from the app object
            app_data = response.json()
            app_spec = app_data.get("app", {}).get("spec")

            # Update environment variables in the spec
            self._update_env_vars(app_spec, settings)

            # Push updated spec
            update_response = requests.put(
                f"{self.api_base}/apps/{self.app_id}",
                headers=headers,
                json={"spec": app_spec},
                timeout=30,
            )

            if update_response.status_code == 401:
                return False, "Authentication failed - invalid API token"
            elif update_response.status_code == 403:
                return (
                    False,
                    "Permission denied - API token needs write access to apps",
                )
            elif update_response.status_code != 200:
                error_msg = update_response.json().get("message", "Unknown error")
                return False, f"Failed to update app: {error_msg}"

            return True, None

        except Exception as e:
            print(f"Error updating settings: {e}")
            return False, f"Exception: {str(e)}"

    def _update_env_vars(self, app_spec: Dict[str, Any], settings: Dict[str, Any]) -> None:
        """
        Update environment variables in app spec.

        Args:
            app_spec: DigitalOcean app specification
            settings: Settings to update

        Note:
            - Settings in DO_NOT_SAVE_TO_DO are filtered out
            - Values exceeding MAX_ENV_VAR_SIZE are skipped with a warning
        """
        # Settings that belong to specific services (not global)
        SERVICE_SPECIFIC_SETTINGS = {
            "EQUIPMENT_CONTROL_ALLOWED_USERS": "anansi-bot",
            "VERIFICATION_ENABLED": "anansi-bot",
            "VERIFICATION_DOC_ID": "anansi-bot",
            "VERIFICATION_MODEL": "anansi-bot",
            "WORKFLOW_PARAMETER_CONFIRMATION": "anansi-bot",
            "INLINE_BUTTONS_ENABLED": "anansi-bot",
            "PROCEDURE_BUTTONS_ENABLED": "anansi-bot",
            "MINI_APP_FORMS_ENABLED": "anansi-bot",
            "CONTEXT_FILTER_ENABLED": "anansi-bot",
            "THREAD_DISENTANGLEMENT_ENABLED": "anansi-bot",
            "ACTIVE_THREAD_WINDOW_MINUTES": "anansi-bot",
            "CONVERSATION_SUMMARY_ENABLED": "anansi-bot",
            "PERSISTENT_AGENTS_ENABLED": "anansi-bot",
            "LANGFUSE_ENABLED": "anansi-bot",
            "AWAITING_INPUT_TIMEOUT_MINUTES": "anansi-bot",
            "EXPERT_INSTRUCTIONS_DOC_ID": "anansi-bot",
            "GEMINI_DEEP_THINKING_MODEL": "anansi-bot",
            "LPP_TEMPLATE_ID": "anansi-bot",
            "QGIS_TEMPLATE_FILE_ID": "anansi-bot",
            "LPP_OUTPUT_FOLDER_ID": "anansi-bot",
            "LAYOUT_POLE_SPACING_M": "anansi-bot",
            "LAYOUT_MAX_DROP_DISTANCE_M": "anansi-bot",
            "LAYOUT_TARGET_COVERAGE_PCT": "anansi-bot",
            "LAYOUT_SQM_PER_KWP": "anansi-bot",
            "LAYOUT_KWP_PER_BUILDING": "anansi-bot",
            "LAYOUT_MIN_ESTIMATED_KWP": "anansi-bot",
            "LAYOUT_BUILDING_BUFFER_M": "anansi-bot",
            "LAYOUT_SITE_SETBACK_M": "anansi-bot",
            "LAYOUT_ROAD_SETBACK_M": "anansi-bot",
            "LAYOUT_CORRIDOR_CLEARANCE_M": "anansi-bot",
            "LAYOUT_CANOPY_THRESHOLD_M": "anansi-bot",
            "LAYOUT_MIN_CANDIDATE_SEPARATION_M": "anansi-bot",
            "LAYOUT_MAX_CANDIDATES": "anansi-bot",
            "LAYOUT_POLE_DEDUP_DISTANCE_M": "anansi-bot",
            "LAYOUT_SNAP_NODE_TOLERANCE_M": "anansi-bot",
            "LAYOUT_MERGE_GAP_THRESHOLD_M": "anansi-bot",
            "LAYOUT_REDISTRIBUTE_GAP_MAX_M": "anansi-bot",
            "LAYOUT_LIGHTNING_RADIUS_M": "anansi-bot",
            "NIGERIA_IMPORT_TARIFF_SHEET_ID": "anansi-bot",
            "NIGERIA_IMPORT_STANDARDS_PDF_ID": "anansi-bot",
        }

        # Separate global and service-specific settings
        global_settings = {}
        service_settings: Dict[str, Dict[str, Any]] = {}

        for key, value in settings.items():
            # Skip read-only and internal metadata fields
            if key in DO_NOT_SAVE_TO_DO:
                continue

            # Check value size to prevent DO API failures
            str_value = (
                str(value) if not isinstance(value, bool) else ("true" if value else "false")
            )
            if len(str_value) > MAX_ENV_VAR_SIZE:
                print(
                    f"WARNING: Skipping {key} - value exceeds {MAX_ENV_VAR_SIZE} chars ({len(str_value)} chars)"
                )
                continue

            if key in SERVICE_SPECIFIC_SETTINGS:
                service_name = SERVICE_SPECIFIC_SETTINGS[key]
                if service_name not in service_settings:
                    service_settings[service_name] = {}
                service_settings[service_name][key] = value
            else:
                global_settings[key] = value

        # Update global envs (all services inherit these)
        if "envs" in app_spec:
            app_spec["envs"] = self._merge_env_vars(app_spec["envs"], global_settings)

        # Update service-specific envs
        for service in app_spec.get("services", []):
            service_name = service.get("name")
            if service_name in service_settings:
                if "envs" not in service:
                    service["envs"] = []
                service["envs"] = self._merge_env_vars(
                    service["envs"], service_settings[service_name]
                )

    def _merge_env_vars(
        self, existing_envs: List[Dict[str, str]], settings: Dict[str, Any]
    ) -> List[Dict[str, str]]:
        """
        Merge new settings into existing environment variables.
        Adds new env vars if they don't already exist.

        Args:
            existing_envs: List of existing env var dicts
            settings: New settings to apply

        Returns:
            Updated list of env vars
        """
        # Create a map of existing env vars
        env_map = {env["key"]: env for env in existing_envs}

        # Update with new values or add new env vars
        for key, value in settings.items():
            # Convert value to string for environment variable
            if isinstance(value, bool):
                str_value = "true" if value else "false"
            else:
                str_value = str(value)

            if key in env_map:
                # Update existing env var
                env_map[key]["value"] = str_value
            else:
                # Add new env var if it doesn't exist
                env_map[key] = {
                    "key": key,
                    "value": str_value,
                    "scope": "RUN_TIME",
                }

        return list(env_map.values())

    def get_available_models(self) -> List[str]:
        """Get list of available Gemini models."""
        return [
            "gemini-flash-latest",
            "gemini-pro-latest",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]

    def get_log_levels(self) -> List[str]:
        """Get list of available log levels."""
        return ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
