#!/usr/bin/env python3
"""
Grafana Dashboard Indexer V2 - Panel Metadata Extraction

Fetches dashboards from a specified Grafana folder, extracts panel information,
and generates tool descriptions using Gemini LLM.

The metadata is stored as a JSON string in the GRAFANA_PANELS_METADATA environment
variable for consumption by the Grafana MCP server.

Features:
- Grafana API integration for folder/dashboard/panel extraction
- Gemini LLM-based tool description generation
- Environment variable-based metadata storage
- DigitalOcean app spec updates via API

Usage:
    # Full sync
    python grafana_indexer_v2.py --folder-name ""

    # Use environment variable for folder name
    python grafana_indexer_v2.py
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

from shared.llm import GeminiGateway, GenerationOptions, LLMMessage


def compute_variables_hash(variables: List[Dict[str, Any]]) -> str:
    """
    Compute hash of variable definitions for change detection.

    This allows detecting when variable options change (e.g., new grid added)
    even if the dashboard version hasn't changed.

    Args:
        variables: List of variable definition dictionaries

    Returns:
        16-character hex hash of the variables
    """
    # Sort keys for consistent hashing
    var_str = json.dumps(variables, sort_keys=True)
    return hashlib.md5(var_str.encode()).hexdigest()[:16]


# Configure logging to ensure errors appear in application logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


class GrafanaClient:
    """Client for interacting with Grafana API"""

    def __init__(self, base_url: str, username: str, password: str):
        """
        Initialize Grafana client.

        Args:
            base_url: Grafana instance URL (e.g., https://grafana.example.com)
            username: Grafana username
            password: Grafana password
        """
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password)
        self.client = httpx.Client(timeout=30.0)

    def search_folders(self, query: str) -> List[Dict[str, Any]]:
        """
        Search for folders by name.

        Args:
            query: Folder name to search for

        Returns:
            List of matching folders
        """
        url = f"{self.base_url}/api/search"
        params = {"type": "dash-folder", "query": query}

        response = self.client.get(url, auth=self.auth, params=params)
        response.raise_for_status()

        result: List[Dict[str, Any]] = response.json()
        return result

    def get_dashboards_in_folder(self, folder_id: int) -> List[Dict[str, Any]]:
        """
        Get all dashboards in a folder.

        Args:
            folder_id: Folder ID

        Returns:
            List of dashboards
        """
        url = f"{self.base_url}/api/search"
        params = {"type": "dash-db", "folderIds": folder_id}

        response = self.client.get(url, auth=self.auth, params=params)
        response.raise_for_status()

        result: List[Dict[str, Any]] = response.json()
        return result

    def get_dashboard_by_uid(self, dashboard_uid: str) -> Dict[str, Any]:
        """
        Get full dashboard definition by UID.

        Args:
            dashboard_uid: Dashboard UID

        Returns:
            Dashboard definition
        """
        url = f"{self.base_url}/api/dashboards/uid/{dashboard_uid}"

        response = self.client.get(url, auth=self.auth)
        response.raise_for_status()

        result: Dict[str, Any] = response.json()
        return result

    def resolve_query_variable(
        self, datasource_uid: str, query: str, max_options: int = 50
    ) -> List[str]:
        """
        Execute a variable query against Grafana to get dynamic options.

        This is used for query-type variables that populate their options
        dynamically from a data source (e.g., list of grid names from database).

        Args:
            datasource_uid: Data source UID to query against
            query: The variable query (e.g., SQL or PromQL)
            max_options: Maximum number of options to return

        Returns:
            List of variable options (up to max_options)
        """
        if not datasource_uid or not query:
            return []

        url = f"{self.base_url}/api/ds/query"

        # Build request payload for the variable query
        payload = {
            "queries": [
                {
                    "refId": "variable",
                    "datasource": {"uid": datasource_uid},
                    "rawSql": query,
                    "format": "table",
                }
            ],
            "from": "now-1h",
            "to": "now",
        }

        try:
            response = self.client.post(url, auth=self.auth, json=payload)
            response.raise_for_status()
            result = response.json()

            # Extract values from the response
            options = []
            results = result.get("results", {})

            for ref_id, data in results.items():
                frames = data.get("frames", [])
                for frame in frames:
                    frame_data = frame.get("data", {})
                    values = frame_data.get("values", [])
                    if values and len(values) > 0:
                        # Get first column values (variable values)
                        for val in values[0]:
                            if val is not None:
                                str_val = str(val)
                                if str_val and str_val not in options:
                                    options.append(str_val)
                                    if len(options) >= max_options:
                                        break
                    if len(options) >= max_options:
                        break
                if len(options) >= max_options:
                    break

            return options[:max_options]

        except Exception as e:
            logger.warning(f"Failed to resolve query variable: {e}")
            return []

    def extract_panels(self, dashboard_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract panels from dashboard definition with full variable metadata.

        Args:
            dashboard_data: Dashboard definition from API

        Returns:
            List of panel metadata with rich variable definitions
        """
        dashboard = dashboard_data.get("dashboard", {})
        panels = []

        # Extract dashboard-level variables with full metadata
        templating = dashboard.get("templating", {})
        template_list = templating.get("list", [])

        variable_definitions = []
        for var in template_list:
            var_name = var.get("name")
            if not var_name:
                continue

            var_def = {
                "name": var_name,
                "type": var.get("type", "query"),
                "label": var.get("label") or var_name,
                "description": var.get("description", ""),
            }

            # Extract options/values based on variable type
            var_type = var.get("type", "query")

            if var_type == "custom":
                # Custom variables have predefined options
                query = var.get("query", "")
                if query:
                    # Parse comma-separated values
                    var_def["options"] = [opt.strip() for opt in query.split(",") if opt.strip()]
            elif var_type == "query":
                # Query variables - store the query definition and datasource for runtime resolution
                var_query = var.get("query", "")
                var_def["query"] = var_query
                # Always store datasource reference for runtime resolution of query variables
                datasource = var.get("datasource", {})
                if datasource:
                    var_def["datasource"] = datasource
                # Current options if available from dashboard
                options = var.get("options", [])
                if options:
                    var_def["options"] = [
                        opt.get("text") or opt.get("value") for opt in options if opt
                    ]
                else:
                    # Try to resolve options dynamically from the datasource
                    ds_uid = None
                    if isinstance(datasource, dict):
                        ds_uid = datasource.get("uid")
                    elif isinstance(datasource, str):
                        ds_uid = datasource
                    if ds_uid and var_query:
                        resolved_options = self.resolve_query_variable(ds_uid, var_query)
                        if resolved_options:
                            var_def["options"] = resolved_options
                            logger.info(
                                f"Resolved {len(resolved_options)} options for variable '{var_name}'"
                            )
            elif var_type == "interval":
                # Interval variables
                query = var.get("query", "")
                if query:
                    var_def["options"] = [opt.strip() for opt in query.split(",") if opt.strip()]
            elif var_type == "constant":
                # Constant has a single value
                var_def["options"] = [var.get("query", "")]
            elif var_type == "textbox":
                # Textbox is free-form
                var_def["free_text"] = True

            # Extract current/default value
            current = var.get("current", {})
            if isinstance(current, dict):
                default_value = current.get("value") or current.get("text")
                if default_value:
                    var_def["default"] = str(default_value)

            # Check if variable is required (will be updated based on query usage)
            var_def["required"] = False

            variable_definitions.append(var_def)

        # Extract panels (including those inside collapsed rows)
        def collect_all_panels(panels_list):
            """Recursively collect panels, including those nested in collapsed rows."""
            collected = []
            for panel in panels_list:
                panel_type = panel.get("type")
                if panel_type == "row":
                    # Row panels may contain nested panels when collapsed
                    nested_panels = panel.get("panels", [])
                    if nested_panels:
                        collected.extend(collect_all_panels(nested_panels))
                else:
                    collected.append(panel)
            return collected

        all_panels = collect_all_panels(dashboard.get("panels", []))

        for panel in all_panels:
            panel_id = panel.get("id")
            panel_type = panel.get("type")

            # Extract basic info
            panel_info = {
                "id": panel_id,
                "title": panel.get("title", "Untitled"),
                "description": panel.get("description", ""),
                "type": panel_type,
                "variables": variable_definitions,
            }

            # Extract query/target information
            targets = panel.get("targets", [])
            if targets:
                # Take the first target as representative
                target = targets[0]
                query_text = target.get("expr") or target.get("rawSql") or str(target)
                panel_info["query"] = query_text

                # Detect which variables are actually used in this panel's queries
                used_vars = set()
                for var_def in variable_definitions:
                    var_name = var_def["name"]
                    # Check for $varName or ${varName} in query
                    if f"${var_name}" in query_text or f"${{{var_name}}}" in query_text:
                        used_vars.add(var_name)

                # Mark used variables as required for this panel
                panel_vars = []
                for var_def in variable_definitions:
                    var_copy = var_def.copy()
                    if var_def["name"] in used_vars:
                        var_copy["required"] = True
                    panel_vars.append(var_copy)

                panel_info["variables"] = panel_vars
            else:
                panel_info["query"] = ""

            panels.append(panel_info)

        return panels


class GeminiDescriptionGenerator:
    """Generate tool descriptions using Gemini LLM"""

    def __init__(self, api_key: str, system_prompt: str):
        """
        Initialize Gemini client.

        Args:
            api_key: Google API key
            system_prompt: System instructions for description generation
        """
        self.api_key = api_key
        self.system_prompt = system_prompt
        # Use main GEMINI_MODEL env var for consistency across the app
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.gateway = GeminiGateway(api_key=api_key, default_model=self.model)

    def generate_description(
        self,
        panel_title: str,
        panel_description: str,
        panel_query: str,
        dashboard_variables: List[Dict[str, Any]],
    ) -> str:
        """
        Generate tool description for a panel.

        Args:
            panel_title: Panel title
            panel_description: Panel description
            panel_query: Panel query
            dashboard_variables: List of dashboard variable definitions with metadata

        Returns:
            Generated tool description
        """
        # Format variables for prompt with rich metadata
        var_descriptions = []
        for var in dashboard_variables:
            var_name = var.get("name", "")
            var_type = var.get("type", "")
            var_label = var.get("label", var_name)
            required = var.get("required", False)
            options = var.get("options", [])
            free_text = var.get("free_text", False)
            default = var.get("default", "")

            var_desc = f"- {var_name} ({var_type})"
            if required:
                var_desc += " [REQUIRED]"
            if var_label != var_name:
                var_desc += f": {var_label}"
            if options:
                # Show first 10 options
                shown_options = options[:10]
                var_desc += f" - Options: {', '.join(str(o) for o in shown_options)}"
                if len(options) > 10:
                    var_desc += f" (and {len(options) - 10} more)"
            elif free_text:
                var_desc += " - Free text input"
            if default:
                var_desc += f" - Default: {default}"

            var_descriptions.append(var_desc)

        variables_text = "\n".join(var_descriptions) if var_descriptions else "None"

        # Build user prompt with query context and time range info
        user_prompt = f"""Generate a tool description for this Grafana dashboard panel:

Title: {panel_title}
Description: {panel_description}

DATA QUERY:
{panel_query}

Dashboard Variables:
{variables_text}

TIME RANGE: This tool accepts time_from and time_to parameters for custom time ranges.
- Examples: "now-1h", "now-24h", "now-7d", "now-30d" for relative times
- User might ask for "last 24 hours" (use time_from="now-24h", time_to="now")

The tool description should:
1. Explain what data this panel visualizes (based on the query)
2. List required variables with their valid options
3. Mention that time range can be customized
4. Be concise (2-3 sentences max)"""

        try:
            result = self.gateway.generate_sync(
                [
                    LLMMessage(role="system", text=self.system_prompt),
                    LLMMessage(role="user", text=user_prompt),
                ],
                GenerationOptions(
                    model=self.model,
                    temperature=0.2,
                    max_output_tokens=500,
                ),
            )
            text = result.text.strip()
            if text:
                return text
            warning = f"⚠️ Warning: Gemini returned empty text for '{panel_title}'"
            logger.warning(warning)
            print(warning, file=sys.stderr)
            print(warning)
        except Exception as e:
            error_line = (
                f"❌ Unexpected error generating description for '{panel_title}': "
                f"{type(e).__name__}: {e}"
            )
            logger.error(error_line)
            print(error_line, file=sys.stderr)
            print(error_line)

        return f"Tool for viewing {panel_title} panel"


def index_grafana_panels(
    grafana_url: str,
    grafana_username: str,
    grafana_password: str,
    folder_name: str,
    gemini_api_key: str,
    system_prompt: str,
    enabled_dashboard_uids: Optional[List[str]] = None,
    enabled_panel_keys: Optional[List[str]] = None,
    existing_metadata: Optional[Dict[str, Any]] = None,
    force_reindex: bool = False,
) -> tuple[Dict[str, Any], Dict[str, str], Dict[str, List[Dict[str, Any]]]]:
    """
    Index Grafana panels and generate metadata.

    Args:
        grafana_url: Grafana instance URL
        grafana_username: Grafana username
        grafana_password: Grafana password
        folder_name: Folder name to search for
        gemini_api_key: Google API key for Gemini
        system_prompt: System instructions for description generation
        enabled_dashboard_uids: Optional list of dashboard UIDs to index. If None, indexes all.
        enabled_panel_keys: Optional list of panel keys (uid:id) to generate descriptions for. If None, generates for all.
        existing_metadata: Optional existing panel metadata for incremental updates
        force_reindex: If True, regenerate all descriptions even if unchanged

    Returns:
        Tuple of (panel_metadata_dict, available_dashboards_dict, dashboard_variables_dict)
        - panel_metadata_dict: Dictionary of panel metadata keyed by panel key (dashboard_uid:panel_id)
        - available_dashboards_dict: Dictionary of {dashboard_uid: dashboard_title} for all dashboards
        - dashboard_variables_dict: Dictionary of {dashboard_uid: [variable_definitions]} - variables stored once per dashboard
    """
    print("=" * 70)
    print("GRAFANA PANEL INDEXING START")
    print("=" * 70)
    print(f"Grafana URL: {grafana_url}")
    print(f"Folder Name: {folder_name}")
    print(f"Incremental Mode: {not force_reindex}")
    print(f"Existing Panels: {len(existing_metadata) if existing_metadata else 0}")
    print("=" * 70)

    # Initialize clients
    grafana = GrafanaClient(grafana_url, grafana_username, grafana_password)
    gemini = GeminiDescriptionGenerator(gemini_api_key, system_prompt)

    # Compute system prompt hash for change detection
    import hashlib

    system_prompt_hash = hashlib.md5(system_prompt.encode()).hexdigest()[:16]
    print(f"System prompt hash: {system_prompt_hash}")

    # Initialize existing metadata dict
    if existing_metadata is None:
        existing_metadata = {}

    # Tracking stats
    stats = {
        "skipped": 0,
        "regenerated": 0,
        "new": 0,
        "dashboard_version_changed": 0,
        "prompt_changed": 0,
        "variables_changed": 0,
    }

    # Search for folder
    print(f"\n🔍 Searching for folder: {folder_name}")
    folders = grafana.search_folders(folder_name)

    if not folders:
        print(f"❌ No folder found with name: {folder_name}")
        return {}, {}, {}

    folder = folders[0]
    folder_id = folder["id"]
    print(f"✓ Found folder: {folder['title']} (ID: {folder_id})")

    # Get dashboards in folder
    print("\n📊 Fetching dashboards in folder...")
    dashboards = grafana.get_dashboards_in_folder(folder_id)
    print(f"✓ Found {len(dashboards)} dashboards")

    # Build available dashboards dictionary
    available_dashboards = {d["uid"]: d["title"] for d in dashboards}

    # Filter dashboards if enabled list provided
    if enabled_dashboard_uids:
        dashboards_to_process = [d for d in dashboards if d["uid"] in enabled_dashboard_uids]
        print(
            f"ℹ️  Filtering to {len(dashboards_to_process)} enabled dashboards (out of {len(dashboards)} total)"
        )
    else:
        dashboards_to_process = dashboards
        print(f"ℹ️  Processing all {len(dashboards)} dashboards")

    # Process each dashboard
    all_panels_metadata = {}
    dashboard_variables = {}  # Store variables once per dashboard (not per panel)
    total_panels = 0

    for dashboard_info in dashboards_to_process:
        dashboard_uid = dashboard_info["uid"]
        dashboard_title = dashboard_info["title"]
        dashboard_url = dashboard_info["url"]

        print(f"\n📈 Processing dashboard: {dashboard_title}")

        # Get full dashboard definition
        dashboard_data = grafana.get_dashboard_by_uid(dashboard_uid)

        # Extract dashboard version for change detection
        dashboard_meta = dashboard_data.get("meta", {})
        dashboard_version = dashboard_meta.get("version", 0)
        dashboard_updated = dashboard_meta.get("updated", "")

        # Extract panels
        panels = grafana.extract_panels(dashboard_data)
        print(f"  Found {len(panels)} panels (version: {dashboard_version})")

        # Store dashboard variables once (shared by all panels in this dashboard)
        if panels and panels[0].get("variables"):
            dashboard_variables[dashboard_uid] = panels[0]["variables"]

        # Generate descriptions for each panel
        for panel in panels:
            panel_id = panel["id"]
            panel_title = panel["title"]
            panel_key = f"{dashboard_uid}:{panel_id}"

            # Check if this panel should get a Gemini description
            panel_is_enabled = enabled_panel_keys is None or panel_key in enabled_panel_keys

            # Determine if we should call Gemini
            should_call_gemini = False
            reason = None

            # Compute variables hash for this panel
            panel_variables_hash = compute_variables_hash(panel["variables"])

            if panel_is_enabled:
                # Panel is enabled, check if we need to regenerate
                if force_reindex:
                    should_call_gemini = True
                    reason = "force reindex"
                elif panel_key not in existing_metadata:
                    should_call_gemini = True
                    reason = "new panel"
                    stats["new"] += 1
                elif existing_metadata[panel_key].get("dashboard_version") != dashboard_version:
                    should_call_gemini = True
                    reason = "dashboard version changed"
                    stats["dashboard_version_changed"] += 1
                elif existing_metadata[panel_key].get("system_prompt_hash") != system_prompt_hash:
                    should_call_gemini = True
                    reason = "system prompt changed"
                    stats["prompt_changed"] += 1
                elif existing_metadata[panel_key].get("variables_hash") != panel_variables_hash:
                    should_call_gemini = True
                    reason = "variables changed"
                    stats["variables_changed"] += 1

            # Generate description based on panel status
            tool_description = None  # Will be set only for enabled panels

            if panel_is_enabled and should_call_gemini:
                msg = f"    Panel {panel_id}: {panel_title} [CALLING GEMINI: {reason}]"
                logger.info(msg)
                print(msg)
                tool_description = gemini.generate_description(
                    panel_title=panel_title,
                    panel_description=panel["description"],
                    panel_query=panel["query"],
                    dashboard_variables=panel["variables"],
                )
                stats["regenerated"] += 1
            elif panel_is_enabled and panel_key in existing_metadata:
                # Reuse existing tool_description if it exists
                if "tool_description" in existing_metadata[panel_key]:
                    print(
                        f"    Panel {panel_id}: {panel_title} [REUSING EXISTING: enabled, unchanged]"
                    )
                    tool_description = existing_metadata[panel_key]["tool_description"]
                    stats["skipped"] += 1
                else:
                    # Panel was previously indexed but never had Gemini called - call it now
                    msg = f"    Panel {panel_id}: {panel_title} [CALLING GEMINI: newly enabled]"
                    logger.info(msg)
                    print(msg)
                    tool_description = gemini.generate_description(
                        panel_title=panel_title,
                        panel_description=panel["description"],
                        panel_query=panel["query"],
                        dashboard_variables=panel["variables"],
                    )
                    stats["regenerated"] += 1
            else:
                # Panel not enabled - don't store tool_description at all
                print(f"    Panel {panel_id}: {panel_title} [NOT ENABLED: metadata only]")
                stats["skipped"] += 1

            # Store metadata with version tracking
            import datetime

            panel_metadata = {
                "title": panel_title,
                "description": panel["description"],
                "dashboard_id": dashboard_info["id"],
                "dashboard_uid": dashboard_uid,
                "dashboard_name": dashboard_url.strip("/").split("/")[-1],  # Extract slug
                "dashboard_title": dashboard_title,
                "dashboard_version": dashboard_version,
                "dashboard_updated": dashboard_updated,
                "system_prompt_hash": system_prompt_hash,
                "variables_hash": panel_variables_hash,
                "last_indexed_at": (
                    datetime.datetime.utcnow().isoformat() + "Z"
                    if should_call_gemini
                    else existing_metadata.get(panel_key, {}).get("last_indexed_at", "")
                ),
                # Note: variables stored at dashboard level (GRAFANA_DASHBOARD_VARIABLES)
                # Use dashboard_uid to look up variables
                "panel_type": panel["type"],
            }

            # Only add tool_description if it exists (i.e., panel is enabled and Gemini was called)
            if tool_description is not None:
                panel_metadata["tool_description"] = tool_description

            all_panels_metadata[panel_key] = panel_metadata

            total_panels += 1

    print(f"\n{'=' * 70}")
    print("GRAFANA INDEXING COMPLETE")
    print(f"{'=' * 70}")
    print(f"Total dashboards available: {len(dashboards)}")
    print(f"Total dashboards processed: {len(dashboards_to_process)}")
    print(f"Total panels indexed: {total_panels}")
    print("\n📊 Incremental Update Stats:")
    print(f"  New panels: {stats['new']}")
    print(f"  Regenerated (dashboard version changed): {stats['dashboard_version_changed']}")
    print(f"  Regenerated (prompt changed): {stats['prompt_changed']}")
    print(f"  Regenerated (variables changed): {stats['variables_changed']}")
    force_reindex_count = (
        stats["regenerated"]
        - stats["new"]
        - stats["dashboard_version_changed"]
        - stats["prompt_changed"]
        - stats["variables_changed"]
    )
    print(f"  Regenerated (force reindex): {force_reindex_count}")
    print(f"  Skipped (unchanged): {stats['skipped']}")
    print(f"  Total Gemini API calls: {stats['regenerated']}")
    print(f"  API calls saved: {stats['skipped']}")
    print(f"  Dashboard variables stored: {len(dashboard_variables)}")
    print(f"{'=' * 70}")

    return all_panels_metadata, available_dashboards, dashboard_variables


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Index Grafana dashboard panels and generate tool descriptions"
    )
    parser.add_argument(
        "--folder-name",
        type=str,
        help="Folder name to search for (default: from GRAFANA_FOLDER_NAME env var)",
    )
    parser.add_argument(
        "--output-file", type=str, help="Output file path for metadata JSON (for testing)"
    )

    args = parser.parse_args()

    # Get configuration from environment
    grafana_url = os.getenv("GRAFANA_URL")
    grafana_username = os.getenv("GRAFANA_USERNAME")
    grafana_password = os.getenv("GRAFANA_PASSWORD")
    folder_name = args.folder_name or os.getenv("GRAFANA_FOLDER_NAME", "")
    gemini_api_key = os.getenv("GOOGLE_API_KEY")
    system_prompt = os.getenv(
        "GRAFANA_PANEL_DESCRIPTION_PROMPT",
        "You are a system that generates tool descriptions for Grafana dashboard panels. "
        "Given a panel with title, description, query, and dashboard variables, create a concise "
        "tool description that explains what data this panel shows and what variables it requires. "
        "Format: A tool description suitable for an LLM to understand when to use this panel.",
    )

    # Validate configuration
    if not all([grafana_url, grafana_username, grafana_password, gemini_api_key]):
        print("❌ Missing required environment variables:", file=sys.stderr)
        if not grafana_url:
            print("  - GRAFANA_URL", file=sys.stderr)
        if not grafana_username:
            print("  - GRAFANA_USERNAME", file=sys.stderr)
        if not grafana_password:
            print("  - GRAFANA_PASSWORD", file=sys.stderr)
        if not gemini_api_key:
            print("  - GOOGLE_API_KEY", file=sys.stderr)
        sys.exit(1)

    # Run indexing
    try:
        panels_metadata, available_dashboards, dashboard_variables = index_grafana_panels(
            grafana_url=grafana_url,
            grafana_username=grafana_username,
            grafana_password=grafana_password,
            folder_name=folder_name,
            gemini_api_key=gemini_api_key,
            system_prompt=system_prompt,
        )

        # Convert to JSON string
        metadata_json = json.dumps(panels_metadata, indent=2)
        variables_json = json.dumps(dashboard_variables, indent=2)

        # Output or save
        if args.output_file:
            with open(args.output_file, "w") as f:
                f.write(metadata_json)
            print(f"\n✓ Metadata saved to: {args.output_file}")
            # Also save variables to a separate file
            vars_file = args.output_file.replace(".json", "_variables.json")
            with open(vars_file, "w") as f:
                f.write(variables_json)
            print(f"✓ Variables saved to: {vars_file}")
        else:
            print("\n📝 Metadata JSON (set GRAFANA_PANELS_METADATA):")
            print(metadata_json)
            print("\n📝 Dashboard Variables JSON (set GRAFANA_DASHBOARD_VARIABLES):")
            print(variables_json)

        print(f"\n📝 Available Dashboards ({len(available_dashboards)}):")
        for uid, title in available_dashboards.items():
            print(f"  {uid}: {title}")

        print("\n✅ Indexing completed successfully")
        sys.exit(0)

    except Exception as e:
        print(f"\n❌ Indexing failed: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
