#!/usr/bin/env python3
"""
Grafana MCP Server - Dashboard Panel Rendering

Provides MCP tools for rendering Grafana dashboard panels as images.
Dynamically generates tools based on panels indexed by the Grafana indexer.

Each tool accepts:
- panel_id: The specific panel to render
- variables: Dictionary of dashboard variable key-value pairs

Returns:
- Image data (base64 encoded PNG)
- Success/failure status
"""

import asyncio
import base64
import copy
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import httpx
import mcp.server.stdio
import mcp.types as types
import vl_convert as vlc
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from shared_code.utils.logger import setup_logger

from shared.charts import apply_theme

logger = setup_logger("grafana-server")

# Startup message
print("🚀 Grafana MCP Server starting...", file=sys.stderr)

# Initialize MCP server
server = Server("grafana-server")

# Configuration
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_USERNAME = os.getenv("GRAFANA_USERNAME", "")
GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD", "")

# Per-operation HTTP timeouts — tunable via env vars without a redeploy
GRAFANA_METADATA_TIMEOUT = int(os.getenv("GRAFANA_METADATA_TIMEOUT", "30"))
GRAFANA_VARIABLE_TIMEOUT = int(os.getenv("GRAFANA_VARIABLE_TIMEOUT", "60"))
GRAFANA_QUERY_TIMEOUT = int(os.getenv("GRAFANA_QUERY_TIMEOUT", "180"))


def _load_metadata_from_supabase() -> tuple[dict, dict, str]:
    """
    Load Grafana metadata from Supabase database.

    Returns:
        Tuple of (panels_metadata, dashboard_variables, enabled_panels_str)
    """
    try:
        from supabase import create_client  # type: ignore[attr-defined]
    except ImportError:
        logger.debug("supabase package not available")
        return {}, {}, ""

    url = os.getenv("CHAT_DB_URL")
    key = os.getenv("CHAT_DB_SERVICE_KEY")

    if not url or not key:
        logger.debug("CHAT_DB credentials not configured")
        return {}, {}, ""

    try:
        client = create_client(url, key)
        result = client.table("grafana_dashboard_metadata").select("*").execute()

        panels_metadata = {}
        dashboard_variables = {}
        enabled_panel_keys = []

        for row in result.data:
            dashboard_uid = row["dashboard_uid"]
            panels = row.get("panels", {})
            variables = row.get("variables", [])
            enabled_ids = row.get("enabled_panel_ids", [])

            # Store variables
            if variables:
                dashboard_variables[dashboard_uid] = variables

            # Expand panels to flat format with full keys
            for panel_id, panel_data in panels.items():
                panel_key = f"{dashboard_uid}:{panel_id}"
                panels_metadata[panel_key] = panel_data

                # Track enabled panels
                if panel_id in enabled_ids:
                    enabled_panel_keys.append(panel_key)

        logger.info(
            f"Loaded {len(panels_metadata)} panels from {len(result.data)} dashboards (database)"
        )
        return panels_metadata, dashboard_variables, ",".join(enabled_panel_keys)

    except Exception as e:
        logger.warning(f"Failed to load metadata from database: {e}")
        return {}, {}, ""


# Load metadata - prefer database, fallback to env vars
_db_panels, _db_vars, _db_enabled = _load_metadata_from_supabase()

if _db_panels:
    # Use database metadata
    GRAFANA_ENABLED_PANELS = _db_enabled
    GRAFANA_PANELS_METADATA = json.dumps(_db_panels)
    GRAFANA_DASHBOARD_VARIABLES = json.dumps(_db_vars)
else:
    # Fallback to env vars
    GRAFANA_ENABLED_PANELS = os.getenv("GRAFANA_ENABLED_PANELS", "")
    GRAFANA_PANELS_METADATA = os.getenv("GRAFANA_PANELS_METADATA", "{}")
    GRAFANA_DASHBOARD_VARIABLES = os.getenv("GRAFANA_DASHBOARD_VARIABLES", "{}")
# Server enable check: GRAFANA_ENABLED (admin UI standard) or GRAFANA_ACTIONS_ENABLED (legacy)
# Default to true to match other MCP servers
_server_enabled = os.getenv("GRAFANA_ENABLED", "").lower()
_legacy_enabled = os.getenv("GRAFANA_ACTIONS_ENABLED", "").lower()

# If either is explicitly set to false, disable. Otherwise enable by default.
if _server_enabled == "false" or _legacy_enabled == "false":
    GRAFANA_SERVER_ENABLED = False
elif _server_enabled in ["true", "1", "yes", "on"] or _legacy_enabled in ["true", "1", "yes", "on"]:
    GRAFANA_SERVER_ENABLED = True
else:
    # Default to true (enabled) like other MCP servers
    GRAFANA_SERVER_ENABLED = True

# Parse metadata and enabled panels
try:
    PANELS_METADATA = json.loads(GRAFANA_PANELS_METADATA)
    logger.info(f"Loaded {len(PANELS_METADATA)} panel definitions")
except json.JSONDecodeError:
    logger.warning("Failed to parse GRAFANA_PANELS_METADATA, using empty metadata")
    PANELS_METADATA = {}

# Parse dashboard variables (stored separately per dashboard to reduce size)
try:
    DASHBOARD_VARIABLES = json.loads(GRAFANA_DASHBOARD_VARIABLES)
    logger.info(f"Loaded variables for {len(DASHBOARD_VARIABLES)} dashboards")
except json.JSONDecodeError:
    logger.warning("Failed to parse GRAFANA_DASHBOARD_VARIABLES, using empty dict")
    DASHBOARD_VARIABLES = {}

ENABLED_PANEL_IDS = set(p.strip() for p in GRAFANA_ENABLED_PANELS.split(",") if p.strip())
logger.info(f"Enabled panels: {ENABLED_PANEL_IDS or 'None'}")


def _reload_metadata() -> bool:
    """Reload Grafana panel metadata from the database.

    Called by handle_list_tools to pick up dashboard syncs without a restart.

    Returns:
        True if metadata was refreshed, False if unchanged or unavailable.
    """
    global PANELS_METADATA, DASHBOARD_VARIABLES, ENABLED_PANEL_IDS

    db_panels, db_vars, db_enabled = _load_metadata_from_supabase()
    if not db_panels:
        return False

    new_enabled = set(p.strip() for p in db_enabled.split(",") if p.strip())

    if db_panels == PANELS_METADATA and new_enabled == ENABLED_PANEL_IDS:
        return False

    old_count = len(PANELS_METADATA)
    PANELS_METADATA = db_panels
    DASHBOARD_VARIABLES = db_vars
    ENABLED_PANEL_IDS = new_enabled

    logger.info(
        f"Hot-reloaded Grafana metadata: {old_count} -> {len(PANELS_METADATA)} panels, "
        f"{len(ENABLED_PANEL_IDS)} enabled"
    )
    return True


# Mappings to hide internal details from LLM
# These are populated when tools are listed and used by the tool handler
TOOL_NAME_TO_PANEL_KEY: Dict[str, str] = {}  # e.g., "financial_cuf" -> "df7gn304ulce8b:2"
TOOL_VAR_LABEL_TO_NAME: Dict[
    str, Dict[str, str]
] = {}  # e.g., {"financial_cuf": {"Grid Name": "gridName"}}

# Grafana color names to hex values mapping
# Based on Grafana's palette: https://grafana.com/docs/grafana/latest/panels-visualizations/configure-standard-options/#color-scheme
GRAFANA_COLORS = {
    # Classic palette colors
    "green": "#73BF69",
    "dark-green": "#37872D",
    "semi-dark-green": "#56A64B",
    "light-green": "#96D98D",
    "super-light-green": "#C8F2C2",
    "yellow": "#FADE2A",
    "dark-yellow": "#E0B400",
    "semi-dark-yellow": "#F2CC0C",
    "light-yellow": "#FFEE52",
    "super-light-yellow": "#FFF899",
    "red": "#F2495C",
    "dark-red": "#C4162A",
    "semi-dark-red": "#E02F44",
    "light-red": "#FF7383",
    "super-light-red": "#FFA6B0",
    "blue": "#5794F2",
    "dark-blue": "#1F60C4",
    "semi-dark-blue": "#3274D9",
    "light-blue": "#8AB8FF",
    "super-light-blue": "#C0D8FF",
    "orange": "#FF9830",
    "dark-orange": "#E55400",
    "semi-dark-orange": "#FA6400",
    "light-orange": "#FFAD5C",
    "super-light-orange": "#FFD185",
    "purple": "#B877D9",
    "dark-purple": "#8F3BB8",
    "semi-dark-purple": "#A352CC",
    "light-purple": "#CA95E5",
    "super-light-purple": "#DEB6F2",
    "white": "#FFFFFF",
    "black": "#000000",
    "grey": "#8E8E8E",
    "dark-grey": "#595959",
    "light-grey": "#B8B8B8",
}

# Grafana unit formats mapping to display titles
UNIT_FORMATS = {
    # Energy
    "kwatth": "kWh",
    "watth": "Wh",
    "joule": "J",
    "kwatt": "kW",
    "watt": "W",
    # Percentage
    "percent": "%",
    "percentunit": "%",
    # Currency
    "currencyUSD": "$",
    "currencyEUR": "€",
    "currencyGBP": "£",
    "currencyNGN": "₦",
    # Data
    "bytes": "bytes",
    "decbytes": "bytes",
    "bits": "bits",
    "kbytes": "KB",
    "mbytes": "MB",
    "gbytes": "GB",
    # Time
    "s": "seconds",
    "ms": "ms",
    "µs": "µs",
    "ns": "ns",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    # Count
    "short": "",
    "none": "",
    "locale": "",
}

# Grafana draw style to Vega-Lite mark type mapping
DRAW_STYLE_MAP = {
    "line": "line",
    "bars": "bar",
    "points": "point",
}

# Grafana line interpolation to Vega-Lite interpolate mapping
INTERPOLATION_MAP = {
    "linear": "linear",
    "smooth": "monotone",
    "stepBefore": "step-before",
    "stepAfter": "step-after",
}


class GrafanaDataClient:
    """Client for Grafana data API"""

    def __init__(self, base_url: str, username: str, password: str):
        """
        Initialize Grafana data client.

        Args:
            base_url: Grafana instance URL
            username: Grafana username
            password: Grafana password
        """
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password)

    def get_dashboard(
        self, dashboard_uid: str, timeout: int = GRAFANA_METADATA_TIMEOUT
    ) -> Dict[str, Any]:
        """
        Fetch dashboard JSON by UID.

        Args:
            dashboard_uid: Dashboard UID
            timeout: Request timeout in seconds

        Returns:
            Dashboard JSON including panel definitions
        """
        url = f"{self.base_url}/api/dashboards/uid/{dashboard_uid}"

        logger.info(f"Fetching dashboard {dashboard_uid}")

        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, auth=self.auth)
            response.raise_for_status()
            result: Dict[str, Any] = response.json()
            return result

    def query_panel_data(
        self,
        queries: List[Dict[str, Any]],
        time_range: Optional[Dict[str, str]] = None,
        timeout: int = GRAFANA_QUERY_TIMEOUT,
    ) -> Dict[str, Any]:
        """
        Query panel data via /api/ds/query.

        Args:
            queries: List of query objects (each with its own datasource)
            time_range: Time range dict with 'from' and 'to' keys
            timeout: Request timeout in seconds

        Returns:
            Query results
        """
        url = f"{self.base_url}/api/ds/query"

        if time_range is None:
            time_range = {"from": "now-6h", "to": "now"}

        # Build request payload
        payload = {
            "queries": queries,
            "from": time_range.get("from", "now-6h"),
            "to": time_range.get("to", "now"),
        }

        # Log datasources being queried
        datasource_uids = set(q.get("datasource", {}).get("uid", "unknown") for q in queries)
        logger.info(f"Querying {len(queries)} queries across {len(datasource_uids)} datasource(s)")
        logger.debug(f"Query payload: {json.dumps(payload, indent=2)}")

        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload, auth=self.auth)

            # Log detailed error info before raising
            if response.status_code >= 400:
                error_body = response.text[:500] if response.text else "(empty)"
                logger.error(
                    f"Grafana API error {response.status_code}: {error_body}\n"
                    f"Query payload: {json.dumps(payload, default=str)[:500]}"
                )

            response.raise_for_status()
            result: Dict[str, Any] = response.json()

            # Log raw result for debugging Infinity datasource issues
            results_section = result.get("results", {})
            for ref_id, ref_result in results_section.items():
                frames = ref_result.get("frames", [])
                if not frames or (frames and not frames[0].get("schema", {}).get("fields")):
                    # Log the full result when frame is empty (helps debug Infinity issues)
                    logger.warning(
                        f"Empty frame returned for refId={ref_id}. "
                        f"Raw result: {json.dumps(ref_result, default=str)[:1000]}"
                    )

            return result

    def resolve_query_variable(
        self,
        variable_def: Dict[str, Any],
        provided_vars: Dict[str, str],
        time_range: Optional[Dict[str, str]] = None,
        timeout: int = GRAFANA_VARIABLE_TIMEOUT,
    ) -> Optional[str]:
        """
        Resolve a query-type variable by executing its defining query.

        Query variables are dashboard variables that get their values from a
        data source query (e.g., "SELECT DISTINCT grid_name FROM grids").
        These need to be resolved at render time if not provided by the caller.

        Args:
            variable_def: Variable definition dict with 'query', 'datasource', 'type' fields
            provided_vars: Already-provided variables (for cascading dependencies)
            time_range: Time range for the query
            timeout: Request timeout in seconds

        Returns:
            Resolved value (first option) or None if query fails or not a query variable
        """
        # Only resolve query-type variables
        if variable_def.get("type") != "query":
            return None

        query = variable_def.get("query", "")
        if not query:
            logger.debug(f"No query defined for variable {variable_def.get('name')}")
            return None

        # Get datasource info
        datasource = variable_def.get("datasource", {})
        ds_uid = None
        if isinstance(datasource, dict):
            ds_uid = datasource.get("uid")
        elif isinstance(datasource, str):
            ds_uid = datasource

        if not ds_uid:
            # Try to find datasource from same variable in another dashboard
            var_name = variable_def.get("name", "")
            logger.debug(
                f"No datasource UID for variable {var_name}, searching other dashboards..."
            )
            for other_dashboard_uid, other_vars in DASHBOARD_VARIABLES.items():
                for other_var in other_vars:
                    if other_var.get("name") == var_name and other_var.get("type") == "query":
                        other_ds = other_var.get("datasource", {})
                        if isinstance(other_ds, dict):
                            ds_uid = other_ds.get("uid")
                        elif isinstance(other_ds, str):
                            ds_uid = other_ds
                        if ds_uid:
                            logger.info(
                                f"Found datasource {ds_uid} for {var_name} from dashboard {other_dashboard_uid}"
                            )
                            break
                if ds_uid:
                    break

            if not ds_uid:
                # Use default datasource as last resort
                # Auth DB (d9ac763b-79bd-4fac-8ef5-97d2b441afef) works for:
                # - Simple math queries (no FROM clause)
                # - Queries on grids table
                default_ds = "d9ac763b-79bd-4fac-8ef5-97d2b441afef"
                logger.info(f"Using default datasource {default_ds} for variable {var_name}")
                ds_uid = default_ds

        # Check cache first
        var_name = variable_def.get("name", "")
        cache_key = f"{var_name}:{json.dumps(sorted(provided_vars.items()))}"
        cached = _get_cached_variable(cache_key)
        if cached is not None:
            logger.debug(f"Using cached value for variable {var_name}: {cached}")
            return cached

        # Substitute any already-provided variables into the query
        # (handles cascading dependencies, e.g., kwhSold depends on Grid)
        # For SQL queries, string values need to be properly quoted
        resolved_query = query
        for dep_name, dep_value in provided_vars.items():
            # Quote string values for SQL (escape single quotes in the value)
            escaped_value = dep_value.replace("'", "''")
            quoted_value = f"'{escaped_value}'"
            # Handle basic syntax: $gridName
            resolved_query = resolved_query.replace(f"${dep_name}", quoted_value)
            # Handle braced syntax: ${gridName}
            resolved_query = resolved_query.replace(f"${{{dep_name}}}", quoted_value)
            # Handle Grafana formatting syntax: ${gridName:raw}, ${gridName:sqlstring}, etc.
            # These formats tell Grafana how to render the value, but we substitute directly
            resolved_query = re.sub(
                rf"\$\{{{dep_name}:[^}}]+\}}",  # Matches ${gridName:anything}
                quoted_value,
                resolved_query,
            )

        # Check if there are still unresolved variables (excluding Grafana time macros)
        # Grafana built-in macros start with __ (e.g., $__to, $__from, $__timeFilter)
        all_vars = re.findall(r"\$\{?(\w+)\}?", resolved_query)
        # Filter out Grafana built-in macros (start with __)
        unresolved = [v for v in all_vars if not v.startswith("__")]
        if unresolved:
            logger.warning(
                f"Cannot resolve variable {var_name}: query has unresolved dependencies {unresolved}"
            )
            return None

        logger.info(f"Resolving query variable {var_name} with query: {resolved_query[:100]}...")

        # Execute the query via Grafana's /api/ds/query endpoint
        url = f"{self.base_url}/api/ds/query"

        if time_range is None:
            time_range = {"from": "now-1M", "to": "now"}

        # Substitute Grafana time macros ($__to, $__from, $__timeFilter, etc.)
        # These are not auto-substituted by the /api/ds/query endpoint
        final_query = self._substitute_variables(
            resolved_query,
            {},  # No additional variables to substitute
            is_sql=True,
            time_range=time_range,
        )

        payload = {
            "queries": [
                {
                    "refId": "variable",
                    "datasource": {"uid": ds_uid},
                    "rawSql": final_query,
                    "format": "table",
                }
            ],
            "from": time_range.get("from", "now-1M"),
            "to": time_range.get("to", "now"),
        }

        logger.debug(f"Variable resolution query for {var_name}: {final_query[:200]}")

        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload, auth=self.auth)
                if response.status_code >= 400:
                    logger.error(
                        f"Grafana variable query failed: {response.status_code} - {response.text[:300]}"
                    )
                response.raise_for_status()
                result = response.json()

            # Extract the first value from the response
            results = result.get("results", {})
            for ref_id, data in results.items():
                frames = data.get("frames", [])
                for frame in frames:
                    frame_data = frame.get("data", {})
                    values = frame_data.get("values", [])
                    if values and len(values) > 0 and len(values[0]) > 0:
                        # Get first value from first column
                        resolved_value = str(values[0][0])
                        logger.info(f"Resolved query variable {var_name} = {resolved_value}")

                        # Cache the resolved value
                        _set_cached_variable(cache_key, resolved_value)

                        return resolved_value

            logger.warning(f"Query for variable {var_name} returned no data")
            return None

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error resolving variable {var_name}: {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"Error resolving variable {var_name}: {e}")
            return None

    def _get_reduce_options(self, panel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract reduce options from panel configuration.

        Grafana panels can be configured to show calculated values (like percentile,
        mean, last) instead of raw time series. This is configured in:
        panel.options.reduceOptions

        Args:
            panel: Panel definition from Grafana

        Returns:
            Dict with 'calcs' and 'values' if reductions are configured, None otherwise
        """
        options = panel.get("options", {})
        reduce_opts = options.get("reduceOptions", {})

        # Check if reductions are configured
        calcs = reduce_opts.get("calcs", [])
        values = reduce_opts.get("values", True)  # True = show all values (default)

        # Only return if calcs are specified AND we're NOT showing all values
        # (values=false means show reduced/calculated value)
        if calcs and not values:
            return {
                "calcs": calcs,
                "values": values,
                "fields": reduce_opts.get("fields", ""),
            }

        return None

    def _apply_calculation(self, values: List[float], calc: str) -> Optional[float]:
        """
        Apply a Grafana calculation to a list of values.

        Args:
            values: List of numeric values from the time series
            calc: Grafana calculation type (e.g., 'last', 'mean', 'percentile_80')

        Returns:
            Calculated value, or None if calculation cannot be performed
        """
        if not values:
            return None

        # Filter out None/NaN values
        clean_values = [v for v in values if v is not None]
        try:
            # Filter NaN values (floats)
            clean_values = [v for v in clean_values if not (isinstance(v, float) and v != v)]
        except (TypeError, ValueError):
            pass

        if not clean_values:
            return None

        # Handle percentiles first (before the lookup dict)
        if calc.startswith("percentile_"):
            try:
                pct = int(calc.split("_")[1])
                # Simple percentile calculation without numpy
                sorted_vals = sorted(clean_values)
                idx = (len(sorted_vals) - 1) * pct / 100
                lower = int(idx)
                upper = min(lower + 1, len(sorted_vals) - 1)
                weight = idx - lower
                return sorted_vals[lower] * (1 - weight) + sorted_vals[upper] * weight
            except (ValueError, IndexError):
                return clean_values[-1]  # Fallback to last

        # Define calculation functions
        calc_funcs = {
            "last": lambda v: v[-1],
            "lastNotNull": lambda v: v[-1],  # Already filtered nulls
            "first": lambda v: v[0],
            "firstNotNull": lambda v: v[0],  # Already filtered nulls
            "mean": lambda v: sum(v) / len(v),
            "max": lambda v: max(v),
            "min": lambda v: min(v),
            "sum": lambda v: sum(v),
            "total": lambda v: sum(v),
            "count": lambda v: float(len(v)),
            "range": lambda v: max(v) - min(v),
            "delta": lambda v: v[-1] - v[0] if len(v) >= 2 else 0.0,
            "diff": lambda v: v[-1] - v[0] if len(v) >= 2 else 0.0,
            "allIsZero": lambda v: 1.0 if all(x == 0 for x in v) else 0.0,
            "allIsNull": lambda v: 0.0,  # We filtered nulls, so if we get here, not all are null
        }

        func = calc_funcs.get(calc, calc_funcs["last"])
        try:
            return float(func(clean_values))
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def _is_single_metric_result(self, panel: Dict[str, Any], query_result: Dict[str, Any]) -> bool:
        """
        Determine if the query result represents a single metric value.

        Returns True for:
        - Stat panels (designed to show single values)
        - Gauge/bargauge panels
        - Query results with only one data point across all series

        Args:
            panel: Panel definition
            query_result: Query results from /api/ds/query

        Returns:
            True if this should be returned as JSON metric, False for chart
        """
        panel_type = panel.get("type", "graph")

        # Stat and gauge panels are always single metrics
        if panel_type in ["stat", "gauge", "bargauge"]:
            return True

        # Check if panel has reduceOptions configured (shows calculated values instead of time series)
        # This catches timeseries panels configured to show percentile, mean, last, etc.
        reduce_opts = self._get_reduce_options(panel)
        if reduce_opts:
            logger.info(
                f"Panel has reduceOptions configured with calcs={reduce_opts.get('calcs')}, "
                f"treating as single metric"
            )
            return True

        # For other panels, check if the result is effectively a single value
        results = query_result.get("results", {})
        total_data_points = 0
        total_series = 0

        for ref_id, result in results.items():
            frames = result.get("frames", [])
            for frame in frames:
                data = frame.get("data", {})
                values = data.get("values", [])
                if len(values) >= 2:  # Has time + at least one value column
                    # Count non-time data points
                    value_column = values[1] if len(values) > 1 else []
                    total_data_points += len(value_column)
                    total_series += 1

        # Consider it a single metric if there's only one data point total
        # or one series with very few points (likely an aggregation result)
        if total_data_points == 1:
            return True
        if total_series == 1 and total_data_points <= 2:
            return True

        return False

    def _extract_single_metric(
        self, panel: Dict[str, Any], query_result: Dict[str, Any], styling: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Extract metric data as JSON for single-value results.

        Args:
            panel: Panel definition
            query_result: Query results from /api/ds/query
            styling: Panel styling configuration

        Returns:
            Dictionary with metric data suitable for JSON response
        """
        # Get configured calculation type from panel's reduceOptions
        reduce_opts = self._get_reduce_options(panel)
        calc_type = "last"  # Default calculation
        if reduce_opts and reduce_opts.get("calcs"):
            calc_type = reduce_opts["calcs"][0]  # Use the first configured calculation

        results = query_result.get("results", {})
        metrics = []

        for ref_id, result in results.items():
            frames = result.get("frames", [])
            logger.debug(f"_extract_single_metric: ref_id={ref_id}, num_frames={len(frames)}")

            for frame_idx, frame in enumerate(frames):
                schema = frame.get("schema", {})
                fields = schema.get("fields", [])
                data = frame.get("data", {})
                values = data.get("values", [])

                # Log frame structure for debugging (especially Infinity datasource)
                field_info = [{"name": f.get("name"), "type": f.get("type")} for f in fields]
                logger.debug(
                    f"  Frame {frame_idx}: fields={field_info}, "
                    f"num_values={len(values)}, "
                    f"value_lengths={[len(v) if isinstance(v, list) else 'N/A' for v in values]}"
                )

                # Find value fields (skip time field)
                for i, field in enumerate(fields):
                    field_type = field.get("type")
                    field_name = field.get("name", f"value_{i}")

                    # Handle numeric field types
                    if field_type in ["number", "float64", "int64"]:
                        field_values = values[i] if i < len(values) else []

                        if field_values:
                            # Apply the configured calculation instead of just taking last value
                            value = self._apply_calculation(field_values, calc_type)

                            # Get display name from overrides if available
                            series_overrides = styling.get("series_overrides", {})
                            override = series_overrides.get(field_name, {})
                            display_name = override.get("displayName", field_name)

                            # Get unit
                            unit = styling.get("unit", "")
                            unit_label = UNIT_FORMATS.get(unit, unit) if unit else ""

                            # Handle percentunit (0-1 scale) vs percent (0-100 scale)
                            # Grafana's percentunit means value is 0-1, needs *100 for display
                            display_value = value
                            if unit == "percentunit" and isinstance(value, (int, float)):
                                display_value = value * 100

                            metric = {
                                "name": display_name,
                                "value": value,  # Keep raw value
                                "display_value": display_value,  # Scaled for display
                                "unit": unit_label,
                                "calculation": calc_type,
                                "ref_id": ref_id,
                            }

                            # Format value for display (use display_value for percentunit)
                            if isinstance(display_value, float):
                                metric["formatted_value"] = f"{display_value:.2f}"
                            else:
                                metric["formatted_value"] = (
                                    str(display_value) if display_value is not None else "N/A"
                                )

                            if unit_label:
                                metric["formatted_value"] += f" {unit_label}"

                            metrics.append(metric)

                    # Handle string fields that might contain numeric values
                    # (Infinity datasource often returns counts as strings)
                    elif field_type == "string":
                        field_values = values[i] if i < len(values) else []
                        if field_values:
                            # Try to convert the first string value to a number
                            try:
                                str_val = str(field_values[0]).strip()
                                value = float(str_val)

                                series_overrides = styling.get("series_overrides", {})
                                override = series_overrides.get(field_name, {})
                                display_name = override.get("displayName", field_name)

                                metric = {
                                    "name": display_name,
                                    "value": value,
                                    "display_value": value,
                                    "unit": "",
                                    "calculation": calc_type,
                                    "ref_id": ref_id,
                                    "formatted_value": f"{value:.2f}",
                                }
                                metrics.append(metric)
                                logger.debug(
                                    f"  Extracted numeric value from string field '{field_name}': {value}"
                                )
                            except (ValueError, TypeError, IndexError):
                                # Not a numeric string, skip
                                pass

                # Handle Infinity datasource: data is in meta.custom.data, not in values
                # This happens when Infinity parses JSON (e.g., JIRA API) with root_selector
                # The data array is in metadata, not the standard frame values
                if not metrics:
                    meta = schema.get("meta", {})
                    custom = meta.get("custom", {})
                    custom_data = custom.get("data", {})

                    # Look for arrays in custom_data (e.g., "issues" from JIRA)
                    for key, arr in custom_data.items():
                        if isinstance(arr, list):
                            count = len(arr)
                            logger.info(
                                f"  Infinity datasource: counted {count} items in "
                                f"meta.custom.data.{key} (calc_type={calc_type})"
                            )
                            # For Infinity datasource with no columns, return the count
                            # This matches what Grafana's stat panel does with "Count" calc
                            metric = {
                                "name": schema.get("name", ref_id),
                                "value": float(count),
                                "display_value": float(count),
                                "unit": "",
                                "calculation": "count",
                                "ref_id": ref_id,
                                "formatted_value": str(count),
                            }
                            metrics.append(metric)
                            break  # Only count the first array found

        return {
            "type": "single_metric",
            "panel_title": panel.get("title", ""),
            "panel_type": panel.get("type", ""),
            "calculation": calc_type,
            "metrics": metrics,
            "summary": self._format_metric_summary(metrics, calc_type),
        }

    def _format_metric_summary(self, metrics: List[Dict[str, Any]], calc_type: str = "last") -> str:
        """
        Format metrics with calculation type into human-readable summary.

        Args:
            metrics: List of metric dictionaries
            calc_type: The calculation type applied (e.g., 'last', 'mean', 'percentile_80')

        Returns:
            Human-readable summary string
        """
        if not metrics:
            return "No data available"

        # Map calculation types to human-readable labels
        calc_labels = {
            "last": "latest",
            "lastNotNull": "latest",
            "first": "first",
            "firstNotNull": "first",
            "mean": "average",
            "max": "maximum",
            "min": "minimum",
            "sum": "total",
            "total": "total",
            "count": "count",
            "range": "range",
            "delta": "change",
            "diff": "difference",
            "percentile_80": "80th percentile",
            "percentile_90": "90th percentile",
            "percentile_95": "95th percentile",
            "percentile_99": "99th percentile",
        }
        calc_label = calc_labels.get(calc_type, calc_type)

        if len(metrics) == 1:
            m = metrics[0]
            return f"{m['name']} ({calc_label}): {m['formatted_value']}"

        parts = [f"{m['name']}: {m['formatted_value']}" for m in metrics]
        return f"{calc_label} values - " + "; ".join(parts)

    @staticmethod
    def _find_panel(
        panels: List[Dict[str, Any]],
        panel_id: int,
        panel_title: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Search for a panel by ID then title, including nested row panels.

        Grafana dashboards can nest panels inside collapsed row panels.
        This searches both top-level and nested panels.
        Title comparison is whitespace-trimmed and case-insensitive.
        """

        def _all_panels(
            top_panels: List[Dict[str, Any]],
        ) -> Iterator[Dict[str, Any]]:
            """Yield all panels including those nested inside rows."""
            for p in top_panels:
                yield p
                if p.get("type") == "row" and isinstance(p.get("panels"), list):
                    yield from p["panels"]

        # First pass: search by ID (exact)
        for p in _all_panels(panels):
            if p.get("id") == panel_id:
                return p  # type: ignore[no-any-return]

        # Second pass: search by title (fallback for stale IDs)
        if panel_title:
            needle = panel_title.strip().lower()
            for p in _all_panels(panels):
                if p.get("title", "").strip().lower() == needle:
                    logger.warning(
                        f"Panel ID {panel_id} not found, resolved by title '{panel_title}' "
                        f"to panel ID {p.get('id')}. Re-index Grafana metadata to fix."
                    )
                    return p  # type: ignore[no-any-return]

        return None

    def generate_panel_visualization(
        self,
        dashboard_uid: str,
        panel_id: int,
        variables: Dict[str, str],
        time_range: Optional[Dict[str, str]] = None,
        width: int = 1000,
        height: int = 500,
        panel_title: Optional[str] = None,
    ) -> Union[Tuple[bytes, Dict[str, Any]], Dict[str, Any]]:
        """
        Generate panel visualization using data-driven approach.

        Args:
            dashboard_uid: Dashboard UID
            panel_id: Panel ID
            variables: Dashboard variables as key-value pairs
            time_range: Optional time range dict with 'from' and 'to' keys
                       (e.g., {"from": "now-24h", "to": "now"})
            width: Image width in pixels
            height: Image height in pixels
            panel_title: Optional panel title for fallback lookup when panel ID is stale

        Returns:
            Either PNG image bytes (for time series/charts) or a Dict with metric
            data (for single-value panels like stat, gauge, or single data points).
            The Dict format includes: type, panel_title, metrics[], summary.
        """
        # Fetch dashboard definition
        dashboard_response = self.get_dashboard(dashboard_uid)
        dashboard = dashboard_response.get("dashboard", {})

        # Find the panel by ID, then by title — searching nested row panels too
        panel = self._find_panel(dashboard.get("panels", []), panel_id, panel_title)

        if not panel:
            raise ValueError(f"Panel {panel_id} not found in dashboard {dashboard_uid}")

        # Extract dashboard variable definitions
        templating = dashboard.get("templating", {})
        template_list = templating.get("list", [])

        # Check which variables are used in this panel
        targets = panel.get("targets", [])
        if not targets:
            raise ValueError(f"Panel {panel_id} has no queries")

        # Log target details for debugging (especially for expression panels)
        target_summary = [
            {
                "refId": t.get("refId"),
                "datasource": (
                    t.get("datasource", {}).get("uid")
                    if isinstance(t.get("datasource"), dict)
                    else t.get("datasource")
                ),
                "hide": t.get("hide", False),
            }
            for t in targets
        ]
        logger.info(f"Panel {panel_id} has {len(targets)} targets: {json.dumps(target_summary)}")

        # Log template variables from dashboard for debugging
        template_summary = [
            {"name": v.get("name"), "type": v.get("type"), "label": v.get("label")}
            for v in template_list
        ]
        logger.info(
            f"Dashboard has {len(template_list)} template variables: {json.dumps(template_summary)}"
        )

        # Normalize provided variables to match LIVE dashboard variable names
        # Variables are now keyed by LABEL (from handle_call_tool), which is stable.
        # Map labels to the live dashboard's internal variable names.
        live_label_to_name = {}
        for var in template_list:
            var_name = var.get("name")
            var_label = var.get("label") or var_name
            if var_name:
                live_label_to_name[var_label] = var_name
                # Also map name→name for direct matches (backwards compat)
                live_label_to_name[var_name] = var_name

        logger.info(f"Live dashboard label->name mapping: {live_label_to_name}")

        # Remap variables from label→value to live_name→value
        normalized_variables = {}
        for key, value in variables.items():
            if key in live_label_to_name:
                # Key matches a label or name in live dashboard
                live_name = live_label_to_name[key]
                normalized_variables[live_name] = value
                if live_name != key:
                    logger.info(f"Mapped label '{key}' to live variable name '{live_name}'")
            else:
                # Key doesn't match - try case-insensitive match
                key_lower = key.lower()
                matched = False
                for label, name in live_label_to_name.items():
                    if label.lower() == key_lower:
                        normalized_variables[name] = value
                        logger.info(f"Case-insensitive match: '{key}' -> '{name}'")
                        matched = True
                        break
                if not matched:
                    # Keep as-is (might be a custom variable or direct name)
                    normalized_variables[key] = value
                    logger.warning(f"No match found for variable key '{key}', keeping as-is")

        variables = normalized_variables
        logger.info(f"Normalized variables: {variables}")

        # Collect all query text to detect variable usage
        # We need to scan ALL text in targets, including nested expressions
        # because variables might be in referenced queries or nested structures
        all_query_text = ""
        for target in targets:
            # Get explicit query fields
            query_text = target.get("expr") or target.get("rawSql") or target.get("query") or ""
            all_query_text += " " + query_text
            # Also serialize the entire target to catch variables in nested structures
            # (e.g., expression queries that reference other queries with variables)
            all_query_text += " " + json.dumps(target, default=str)

        # Auto-resolve query variables that are used but not provided
        # Process in order (Grafana variables can have cascading dependencies)
        for var in template_list:
            var_name = var.get("name")
            if not var_name:
                continue

            # Check if variable is used in queries
            is_used = f"${var_name}" in all_query_text or f"${{{var_name}}}" in all_query_text

            # If used but not provided, try to auto-resolve if it's a query variable
            if is_used and var_name not in variables:
                var_type = var.get("type", "")

                if var_type == "query":
                    # Try to auto-resolve this query variable
                    resolved_value = self.resolve_query_variable(
                        variable_def=var,
                        provided_vars=variables,
                        time_range=time_range,
                    )
                    if resolved_value:
                        variables[var_name] = resolved_value
                        logger.info(f"Auto-resolved query variable {var_name}={resolved_value}")
                elif var_type == "constant":
                    # Constant variables have a fixed value in 'query' field
                    const_value = var.get("query", "")
                    if const_value:
                        variables[var_name] = const_value
                        logger.info(f"Auto-resolved constant variable {var_name}={const_value}")
                elif var_type == "interval":
                    # Interval variables - use the current/default value
                    current = var.get("current", {})
                    if isinstance(current, dict):
                        interval_value = current.get("value") or current.get("text")
                        if interval_value:
                            variables[var_name] = str(interval_value)
                            logger.info(
                                f"Auto-resolved interval variable {var_name}={interval_value}"
                            )

        # After auto-resolution, validate that all required variables are provided
        missing_vars = []
        for var in template_list:
            var_name = var.get("name")
            if not var_name:
                continue

            # Check if variable is used in queries
            is_used = f"${var_name}" in all_query_text or f"${{{var_name}}}" in all_query_text

            # If used but not provided (after auto-resolution), it's missing
            if is_used and var_name not in variables:
                var_type = var.get("type", "")
                var_label = var.get("label") or var_name

                # Only report as missing if it's a user-provided type (custom, textbox)
                # Query variables that couldn't be resolved should show a more specific error
                if var_type == "query":
                    # This means we tried to resolve but failed
                    missing_vars.append(f"{var_label} (could not auto-resolve)")
                else:
                    missing_vars.append(var_label)

        if missing_vars:
            raise ValueError(
                f"Missing required variables for this panel: {', '.join(missing_vars)}. "
                f"Please provide values for these variables."
            )

        # Extract panel queries and datasource (already validated above)

        # Determine effective time range early (needed for SQL macro substitution)
        # Priority: 1. User-provided time_range, 2. Panel's time setting, 3. Default 6h
        effective_time_range = time_range
        if effective_time_range is None and "time" in panel:
            effective_time_range = panel["time"]
        if effective_time_range is None:
            effective_time_range = {"from": "now-6h", "to": "now"}

        # Prepare queries for /api/ds/query
        queries = []

        # Track hidden query refIds — these are sent to the API (expressions may
        # reference them) but their results are excluded from the chart.
        hidden_ref_ids = set()

        for target in targets:
            if target.get("hide"):
                hidden_ref_ids.add(target.get("refId"))
                logger.info(f"Query refId={target.get('refId')} is hidden, will exclude from chart")

            # Extract datasource for THIS target (each target can have different datasource)
            target_datasource = target.get("datasource", {})
            if isinstance(target_datasource, dict):
                datasource_uid = target_datasource.get("uid")
            elif isinstance(target_datasource, str):
                datasource_uid = target_datasource
            else:
                datasource_uid = None

            # Build query object with this target's datasource
            query = {
                "refId": target.get("refId", "A"),
                "datasource": {"uid": datasource_uid} if datasource_uid else target_datasource,
                "maxDataPoints": 1000,
                "intervalMs": 1000,
            }

            # Check if this is a Grafana expression query (__expr__ datasource)
            is_expression = datasource_uid == "__expr__" or (
                isinstance(target_datasource, dict) and target_datasource.get("type") == "__expr__"
            )

            if is_expression:
                # Expression queries have special fields: type, expression, conditions, etc.
                # Copy all expression-specific fields, substituting variables in the expression
                for key in [
                    "type",
                    "expression",
                    "conditions",
                    "reducer",
                    "settings",
                    "window",
                    "downsampler",
                    "upsampler",
                ]:
                    if key in target:
                        original_value = target[key]
                        # Substitute dashboard variables in the expression field
                        # e.g., "$kwhSold / $estimatedActuals" -> "42500 / 45000"
                        if key == "expression" and isinstance(original_value, str):
                            logger.info(f"Variables for expression substitution: {variables}")
                            substituted_value = self._substitute_variables(
                                original_value,
                                variables,
                                is_sql=False,
                                time_range=effective_time_range,
                            )
                            query[key] = substituted_value
                            logger.info(
                                f"Expression substitution: '{original_value}' -> '{substituted_value}'"
                            )
                        else:
                            query[key] = original_value
                logger.debug(
                    f"Building expression query refId={target.get('refId')}, type={target.get('type')}"
                )
            else:
                # Regular data queries - copy ALL target fields with variable substitution
                # This ensures datasource-specific fields (like Infinity's url, type, columns, etc.)
                # are properly passed through to the Grafana query API
                skip_keys = {"refId", "datasource"}  # Already handled above
                for key, original_value in target.items():
                    if key in skip_keys:
                        continue

                    # Substitute variables in string fields (except format)
                    if isinstance(original_value, str) and key != "format":
                        # Use SQL-aware substitution for rawSql queries
                        is_sql = key == "rawSql"
                        substituted_value = self._substitute_variables(
                            original_value,
                            variables,
                            is_sql=is_sql,
                            time_range=effective_time_range,
                        )
                        query[key] = substituted_value
                        if substituted_value != original_value:
                            logger.info(
                                f"Substituted {key}: {original_value} -> {substituted_value}"
                            )
                    else:
                        query[key] = original_value

                # Log all target fields for debugging Infinity and other datasources
                logger.debug(f"Target fields copied: {list(target.keys())}")

                # Ensure format is set for SQL queries (required by PostgreSQL datasource)
                if "rawSql" in query and "format" not in query:
                    query["format"] = "time_series"

            queries.append(query)

        # Log query structure for debugging
        logger.debug(f"Prepared {len(queries)} queries: {[q.get('refId') for q in queries]}")

        query_result = self.query_panel_data(queries, effective_time_range)

        # Filter out results from hidden queries before any downstream processing
        if hidden_ref_ids and "results" in query_result:
            for ref_id in hidden_ref_ids:
                if ref_id in query_result["results"]:
                    del query_result["results"][ref_id]
                    logger.info(f"Removed hidden query results for refId={ref_id}")

        # Extract styling configuration (needed for both single metric and chart)
        styling = self._extract_panel_styling(panel)

        # Check if this is a single metric result - return JSON instead of chart
        if self._is_single_metric_result(panel, query_result):
            logger.info(f"Panel {panel_id} detected as single metric - returning JSON")
            return self._extract_single_metric(panel, query_result, styling)

        # Transform to Vega-Lite format for time series/charts
        vl_spec, data_values = self._transform_to_vegalite(
            panel, query_result, width, height, variables
        )

        # Apply shared theme and generate PNG
        themed_spec = apply_theme(vl_spec)
        png_bytes: bytes = vlc.vegalite_to_png(themed_spec, scale=2)

        logger.info(f"Generated visualization for panel {panel_id}")

        # Build per-series structured data with ISO timestamps so the LLM can
        # reason over the values (e.g. "average between 11am and 1pm").
        unit = styling.get("unit", "")
        unit_label = UNIT_FORMATS.get(unit, unit) if unit else "value"
        series_map: Dict[str, List[Dict[str, Any]]] = {}
        for dv in data_values:
            series_name = dv.get("series", "value")
            val = dv.get("value")
            ts = dv.get("time")
            try:
                iso_time = (
                    datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
                    if ts is not None
                    else None
                )
            except (TypeError, OSError, OverflowError):
                iso_time = str(ts)
            series_map.setdefault(series_name, []).append({"time": iso_time, "value": val})

        series_data = {
            "unit": unit_label,
            "series": [{"name": name, "points": pts} for name, pts in series_map.items()],
        }

        return png_bytes, series_data

    def _transform_to_vegalite(
        self,
        panel: Dict[str, Any],
        query_result: Dict[str, Any],
        width: int,
        height: int,
        variables: Optional[Dict[str, str]] = None,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Transform Grafana query results to Vega-Lite specification.

        Returns:
            Tuple of (vl_spec, data_values) where data_values is the list of
            {time_iso, value, series} dicts used to build the chart — included
            so callers can surface structured data alongside the rendered image.

        Args:
            panel: Panel definition
            query_result: Query results from /api/ds/query
            width: Chart width
            height: Chart height

        Returns:
            Vega-Lite specification
        """
        # Extract data from query result
        data_values = []

        # Handle Grafana's data frames format
        results = query_result.get("results", {})

        for ref_id, result in results.items():
            frames = result.get("frames", [])

            for frame in frames:
                schema = frame.get("schema", {})
                fields = schema.get("fields", [])
                data = frame.get("data", {})

                # Find time and value fields
                time_field = None
                value_fields = []

                for field in fields:
                    field_name = field.get("name")
                    field_type = field.get("type")

                    if field_type == "time":
                        time_field = field_name
                    elif field_type in ["number", "float64", "int64"]:
                        value_fields.append(field_name)

                # Extract values
                if time_field and value_fields:
                    time_values = data.get("values", [])[0] if data.get("values") else []

                    for i, value_field in enumerate(value_fields):
                        value_values = (
                            data.get("values", [])[i + 1]
                            if len(data.get("values", [])) > i + 1
                            else []
                        )

                        for time_val, data_val in zip(time_values, value_values):
                            data_values.append(
                                {
                                    "time": time_val,
                                    "value": data_val,
                                    "series": value_field,
                                }
                            )

        # Extract panel styling configuration
        styling = self._extract_panel_styling(panel)

        # Filter out series hidden via custom.hideFrom.viz overrides
        hidden_series = {
            name for name, cfg in styling.get("series_overrides", {}).items() if cfg.get("hidden")
        }

        # Apply byRegexp overrides to resolve hidden series by pattern
        regex_overrides = styling.get("_regex_overrides", [])
        if regex_overrides:
            all_series_names = {dv.get("series") for dv in data_values if dv.get("series")}
            for ro in regex_overrides:
                try:
                    pattern = re.compile(ro["pattern"])
                    for prop in ro["properties"]:
                        if prop.get("id") == "custom.hideFrom":
                            hide_val = prop.get("value", {})
                            if isinstance(hide_val, dict) and hide_val.get("viz"):
                                for sn in all_series_names:
                                    if pattern.search(sn):
                                        hidden_series.add(sn)
                except re.error:
                    logger.warning(f"Invalid regex override pattern: {ro.get('pattern')}")

        # If defaults hide all series, only show those explicitly un-hidden via overrides
        if styling.get("default_hidden"):
            all_series_names = {dv.get("series") for dv in data_values if dv.get("series")}
            shown_series = {
                name
                for name, cfg in styling.get("series_overrides", {}).items()
                if cfg.get("hidden") is False
            }
            hidden_series |= all_series_names - shown_series

        if hidden_series:
            data_values = [dv for dv in data_values if dv.get("series") not in hidden_series]
            logger.info(f"Filtered hidden series from chart: {hidden_series}")

        # Determine chart type from panel type and build appropriate chart
        panel_type = panel.get("type", "graph")
        unit = styling.get("unit", "")
        unit_label = UNIT_FORMATS.get(unit, unit) if unit else "Value"

        # Handle percentunit (0-1 scale) - multiply values by 100 for display
        # Grafana's percentunit means value is 0-1, needs *100 for display
        if unit == "percentunit":
            for dv in data_values:
                if isinstance(dv.get("value"), (int, float)):
                    dv["value"] = dv["value"] * 100

        if panel_type in ["stat"]:
            # Stat panels show a single big number - use the last/most recent value
            main_chart = self._build_stat_chart(data_values, panel, unit_label)
        elif panel_type in ["gauge", "bargauge"]:
            # Gauge panels show progress bars
            main_chart = self._build_gauge_chart(data_values, panel, unit_label)
        elif panel_type in ["piechart"]:
            # Pie chart for distribution data
            main_chart = self._build_pie_chart(data_values, panel, unit_label)
        else:
            # Default: timeseries/graph - line chart with full styling support
            main_chart = self._build_timeseries_chart(data_values, panel, unit_label, styling)

        # Build watermark text layer
        org_name = os.getenv("ORGANIZATION_NAME", "Anansi")
        watermark_text = (
            f"© {org_name}.\n"
            f"Confidential to {org_name} and authorized partners.\n"
            "Do not share or use externally.\n"
            "Bots such as this one can make mistakes."
        )

        watermark_layer = {
            "data": {"values": [{}]},  # Empty data for static text
            "mark": {
                "type": "text",
                "align": "center",
                "baseline": "middle",
                "angle": -30,
                "fontSize": 16,
                "color": "gray",
                "opacity": 0.15,
                "lineBreak": "\n",
            },
            "encoding": {
                "text": {"value": watermark_text},
                "x": {"value": width / 2},
                "y": {"value": height / 2},
            },
        }

        # Substitute Grafana variables in panel title (e.g., ${gridName} → "ExampleGrid")
        panel_title = panel.get("title", "")
        if variables and ("${" in panel_title or "$" in panel_title):
            panel_title = self._substitute_variables(panel_title, variables)

        # Combine layers with watermark
        vl_spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": panel_title,
            "width": width,
            "height": height,
            "layer": [main_chart, watermark_layer],
        }

        return vl_spec, data_values

    def _extract_panel_styling(self, panel: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract styling configuration from Grafana panel.

        Args:
            panel: Panel definition from Grafana

        Returns:
            Dictionary with styling configuration including:
            - unit: Display unit for values
            - draw_style: line, bars, points
            - line_interpolation: linear, smooth, step
            - line_width: Line thickness
            - fill_opacity: Area fill opacity
            - default_axis: Default Y-axis placement (left/right)
            - series_overrides: Per-series styling overrides
        """
        field_config = panel.get("fieldConfig", {})
        defaults = field_config.get("defaults", {})
        custom = defaults.get("custom", {})

        styling = {
            "unit": defaults.get("unit", ""),
            "draw_style": custom.get("drawStyle", "line"),
            "line_interpolation": custom.get("lineInterpolation", "linear"),
            "line_width": custom.get("lineWidth", 1),
            "fill_opacity": custom.get("fillOpacity", 0),
            "default_axis": custom.get("axisPlacement", "left"),
            "series_overrides": {},
        }

        # Check defaults-level hideFrom (hides ALL series unless overridden)
        default_hide_from = custom.get("hideFrom", {})
        if isinstance(default_hide_from, dict) and default_hide_from.get("viz"):
            styling["default_hidden"] = True

        # Extract per-series overrides from fieldConfig.overrides
        for override in field_config.get("overrides", []):
            matcher = override.get("matcher", {})
            matcher_id = matcher.get("id")

            # Resolve series names this override applies to
            target_names: list = []
            if matcher_id == "byName":
                target_names = [matcher.get("options", "")]
            elif matcher_id == "byRegexp":
                # Store regex pattern — will be matched against actual series names later
                styling.setdefault("_regex_overrides", []).append(
                    {
                        "pattern": matcher.get("options", ""),
                        "properties": override.get("properties", []),
                    }
                )
                continue

            for series_name in target_names:
                series_config = {}

                for prop in override.get("properties", []):
                    prop_id = prop.get("id")
                    prop_value = prop.get("value")

                    if prop_id == "custom.axisPlacement":
                        series_config["axis"] = prop_value
                    elif prop_id == "color":
                        if isinstance(prop_value, dict):
                            color_name = prop_value.get("fixedColor", "")
                            # Convert Grafana color name to hex
                            series_config["color"] = GRAFANA_COLORS.get(color_name, color_name)
                    elif prop_id == "displayName":
                        series_config["displayName"] = prop_value
                    elif prop_id == "custom.lineWidth":
                        series_config["lineWidth"] = prop_value
                    elif prop_id == "custom.fillOpacity":
                        series_config["fillOpacity"] = prop_value
                    elif prop_id == "custom.drawStyle":
                        series_config["drawStyle"] = prop_value
                    elif prop_id == "custom.lineInterpolation":
                        series_config["lineInterpolation"] = prop_value
                    elif prop_id == "custom.lineStyle":
                        # Grafana lineStyle: {"fill": "solid"|"dash"|"dot", "dash": [n, n]}
                        if isinstance(prop_value, dict):
                            series_config["lineStyle"] = prop_value
                    elif prop_id == "unit":
                        series_config["unit"] = prop_value
                    elif prop_id == "custom.hideFrom":
                        # Grafana hideFrom: {legend: bool, tooltip: bool, viz: bool}
                        if isinstance(prop_value, dict) and prop_value.get("viz"):
                            series_config["hidden"] = True

                if series_config:
                    styling["series_overrides"][series_name] = series_config

        # Override: FS Off should be yellow, not gray (improves readability on Telegram)
        for name, cfg in styling["series_overrides"].items():
            if "fs" in name.lower() and "off" in name.lower():
                if cfg.get("color") in (
                    GRAFANA_COLORS.get("grey"),
                    GRAFANA_COLORS.get("light-grey"),
                    GRAFANA_COLORS.get("dark-grey"),
                    "gray",
                    "grey",
                ):
                    cfg["color"] = GRAFANA_COLORS["yellow"]

        logger.debug(f"Extracted panel styling: {styling}")
        return styling

    def _substitute_variables(
        self,
        text: str,
        variables: Dict[str, str],
        is_sql: bool = False,
        time_range: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Substitute Grafana variable placeholders with actual values.

        Handles:
        - ${varName} syntax
        - $varName syntax
        - Multi-value variables (comma-separated)
        - SQL IN clause quoting (when is_sql=True)
        - Grafana time macros ($__timeFilter, $__timeFrom, $__timeTo)

        Args:
            text: Text containing variable placeholders
            variables: Dict of variable names to values
            is_sql: Whether this is a SQL query (enables proper quoting)
            time_range: Time range dict with 'from' and 'to' keys

        Returns:
            Text with variables substituted
        """
        result = text

        # Handle Grafana time macros for SQL queries
        if is_sql and time_range:
            time_from = time_range.get("from", "now-6h")
            time_to = time_range.get("to", "now")

            # Convert Grafana relative time to PostgreSQL interval
            # $__timeFilter(column) -> column >= NOW() - INTERVAL '6 hours' AND column <= NOW()
            def convert_grafana_time(time_str: str) -> str:
                """Convert Grafana time string to PostgreSQL expression."""
                if time_str == "now":
                    return "NOW()"

                # Unit mapping for both interval and truncation
                unit_map = {
                    "h": "hours",
                    "d": "days",
                    "m": "minutes",
                    "M": "months",
                    "y": "years",
                    "w": "weeks",
                }
                trunc_map = {
                    "h": "hour",
                    "d": "day",
                    "m": "minute",
                    "M": "month",
                    "y": "year",
                    "w": "week",
                }

                # Handle now/X (start of current period) - e.g., now/M = start of month
                match = re.match(r"now/([hdmMyw])", time_str)
                if match:
                    trunc_unit = match.group(1)
                    pg_trunc = trunc_map.get(trunc_unit, "day")
                    return f"date_trunc('{pg_trunc}', NOW())"

                # Handle now-X/Y (relative time with rounding) - e.g., now-1M/M
                match = re.match(r"now-(\d+)([hdmMyw])/([hdmMyw])", time_str)
                if match:
                    value, unit, trunc_unit = match.groups()
                    pg_unit = unit_map.get(unit, "hours")
                    pg_trunc = trunc_map.get(trunc_unit, "day")
                    return f"date_trunc('{pg_trunc}', NOW() - INTERVAL '{value} {pg_unit}')"

                # Handle now-Xh, now-Xd, now-Xm patterns (without rounding)
                match = re.match(r"now-(\d+)([hdmMyw])", time_str)
                if match:
                    value, unit = match.groups()
                    pg_unit = unit_map.get(unit, "hours")
                    return f"NOW() - INTERVAL '{value} {pg_unit}'"

                # Fallback: try to use as-is (might be ISO timestamp)
                return f"'{time_str}'::timestamp"

            pg_from = convert_grafana_time(time_from)
            pg_to = convert_grafana_time(time_to)

            # Replace $__timeFilter(column) with proper SQL
            def replace_time_filter(match: re.Match) -> str:
                column = match.group(1)
                return f"{column} >= {pg_from} AND {column} <= {pg_to}"

            result = re.sub(r"\$__timeFilter\(([^)]+)\)", replace_time_filter, result)

            # Replace $__timeFrom() and $__timeTo() (with parentheses)
            result = result.replace("$__timeFrom()", pg_from)
            result = result.replace("$__timeTo()", pg_to)

            # Replace $__from and $__to (without parentheses) - used as raw timestamp values
            # Use word boundary to avoid partial matches
            result = re.sub(r"\$__from\b", pg_from, result)
            result = re.sub(r"\$__to\b", pg_to, result)

            # Replace ${__from} and ${__to} (with braces) - used in expressions like to_timestamp(${__to}/1000)
            # These need to return epoch milliseconds for division by 1000
            # Convert to epoch ms: EXTRACT(EPOCH FROM NOW()) * 1000
            epoch_ms_from = f"(EXTRACT(EPOCH FROM {pg_from}) * 1000)"
            epoch_ms_to = f"(EXTRACT(EPOCH FROM {pg_to}) * 1000)"
            result = result.replace("${__from}", epoch_ms_from)
            result = result.replace("${__to}", epoch_ms_to)

            # Replace $__timeGroupAlias(column, interval) - time bucketing for SQL
            # Converts to: date_trunc(interval, column) AS time
            def replace_time_group_alias(match: re.Match) -> str:
                column = match.group(1).strip()
                interval = match.group(2).strip().strip("'\"")
                # Convert Grafana interval to PostgreSQL date_trunc unit
                interval_map = {
                    "1s": "second",
                    "1m": "minute",
                    "1h": "hour",
                    "1d": "day",
                    "1w": "week",
                    "1M": "month",
                    "1y": "year",
                }
                pg_interval = interval_map.get(interval, "day")
                return f"date_trunc('{pg_interval}', {column}) AS time"

            result = re.sub(
                r"\$__timeGroupAlias\(([^,]+),\s*([^)]+)\)",
                replace_time_group_alias,
                result,
            )

            # Replace $__interval and $__interval_ms with reasonable defaults
            # These are typically used for aggregation - use 1 hour as default
            result = re.sub(r"\$__interval\b", "'1 hour'", result)
            result = re.sub(r"\$__interval_ms\b", "3600000", result)

            # Replace $__unixEpochFilter(column) for unix timestamp columns
            def replace_unix_epoch_filter(match: re.Match) -> str:
                column = match.group(1)
                # Convert Grafana time to unix epoch
                return f"{column} >= EXTRACT(EPOCH FROM {pg_from}) AND {column} <= EXTRACT(EPOCH FROM {pg_to})"

            result = re.sub(r"\$__unixEpochFilter\(([^)]+)\)", replace_unix_epoch_filter, result)

            # Replace $__unixEpochFrom() and $__unixEpochTo()
            result = result.replace("$__unixEpochFrom()", f"EXTRACT(EPOCH FROM {pg_from})::bigint")
            result = result.replace("$__unixEpochTo()", f"EXTRACT(EPOCH FROM {pg_to})::bigint")

            # Replace $__timeGroup(column, interval) - similar to timeGroupAlias but without AS
            def replace_time_group(match: re.Match) -> str:
                column = match.group(1).strip()
                interval = match.group(2).strip().strip("'\"")
                interval_map = {
                    "1s": "second",
                    "1m": "minute",
                    "1h": "hour",
                    "1d": "day",
                    "1w": "week",
                    "1M": "month",
                    "1y": "year",
                }
                pg_interval = interval_map.get(interval, "day")
                return f"date_trunc('{pg_interval}', {column})"

            result = re.sub(r"\$__timeGroup\(([^,]+),\s*([^)]+)\)", replace_time_group, result)

        for var_name, var_value in variables.items():
            # For SQL queries, check if variable is used in IN clause and quote appropriately
            if is_sql:
                # Handle IN ($varName) or IN (${varName}) - need single quotes around values
                # Pattern: IN ( followed by variable reference
                in_pattern_curly = rf"IN\s*\(\s*\$\{{{var_name}\}}\s*\)"
                in_pattern_simple = rf"IN\s*\(\s*\${var_name}\b\s*\)"

                # Format value for SQL IN clause: 'value' or 'val1', 'val2' for multi-value
                if "," in var_value:
                    # Multi-value: quote each value
                    quoted_values = ", ".join(f"'{v.strip()}'" for v in var_value.split(","))
                else:
                    quoted_values = f"'{var_value}'"

                # Replace IN clause patterns with quoted values
                result = re.sub(
                    in_pattern_curly, f"IN ({quoted_values})", result, flags=re.IGNORECASE
                )
                result = re.sub(
                    in_pattern_simple, f"IN ({quoted_values})", result, flags=re.IGNORECASE
                )

            # Handle ${varName} syntax (non-IN clause contexts)
            result = result.replace(f"${{{var_name}}}", var_value)
            # Handle ${varName:raw} syntax (Grafana raw formatting - no escaping)
            result = result.replace(f"${{{var_name}:raw}}", var_value)
            # Handle ${varName:csv} syntax (comma-separated values)
            if "," in var_value:
                result = result.replace(f"${{{var_name}:csv}}", var_value)
            else:
                result = result.replace(f"${{{var_name}:csv}}", var_value)
            # Handle ${varName:singlequote} syntax (single-quoted for SQL)
            if "," in var_value:
                quoted = ", ".join(f"'{v.strip()}'" for v in var_value.split(","))
            else:
                quoted = f"'{var_value}'"
            result = result.replace(f"${{{var_name}:singlequote}}", quoted)
            # Handle any other ${varName:format} syntax (catch-all for unhandled formats)
            # Replaces with the value as-is (same as :raw)
            result = re.sub(rf"\${{{var_name}:[^}}]+\}}", var_value, result)
            # Handle $varName syntax (word boundary to avoid partial matches)
            result = re.sub(rf"\${var_name}\b", var_value, result)

        return result

    def _resolve_stroke_dash(
        self,
        line_style: Optional[Dict[str, Any]],
        line_width: float,
    ) -> Optional[List[float]]:
        """
        Translate a Grafana lineStyle into a Vega-Lite strokeDash array.

        Grafana lineStyle: {"fill": "solid"|"dash"|"dot", "dash": [dash, gap]}.
        Returns None for solid lines (Vega-Lite default). For dot/dash without an
        explicit dash array, scales the pattern by line width so it stays visible.
        """
        if not isinstance(line_style, dict):
            return None

        fill = line_style.get("fill", "solid")
        if fill == "solid":
            return None

        explicit = line_style.get("dash")
        if isinstance(explicit, list) and explicit:
            return [float(x) for x in explicit]

        width = line_width or 1
        if fill == "dot":
            return [width, 2 * width]
        # dash
        return [4 * width, 2 * width]

    def _get_effective_mark_config(
        self,
        series_name: str,
        styling: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Resolve the Vega-Lite mark config for a given series.

        Uses per-series overrides first, then panel defaults.
        When drawStyle is "line" and fillOpacity > 0, returns an "area" mark
        so Vega-Lite renders a filled region instead of a bare line.
        """
        series_overrides = styling.get("series_overrides", {})
        override = series_overrides.get(series_name, {})

        # Resolve draw style: per-series override → panel default
        draw_style = override.get("drawStyle", styling.get("draw_style", "line"))
        fill_opacity = override.get("fillOpacity", styling.get("fill_opacity", 0))
        interpolation = override.get(
            "lineInterpolation", styling.get("line_interpolation", "linear")
        )
        vl_interpolation = INTERPOLATION_MAP.get(interpolation, "linear")
        line_width = override.get("lineWidth", styling.get("line_width", 3))
        line_style = override.get("lineStyle", styling.get("line_style"))

        # Line with fillOpacity > 0 → area mark (Grafana renders these as shaded regions)
        if draw_style == "line" and fill_opacity and fill_opacity > 0:
            stroke_dash = self._resolve_stroke_dash(line_style, line_width)
            # Vega-Lite area: a "line" object styles the border (dash carries onto it).
            area_line: Any = (
                {"strokeDash": stroke_dash, "strokeWidth": line_width} if stroke_dash else True
            )
            return {
                "type": "area",
                "line": area_line,
                "opacity": fill_opacity / 100,
                "interpolate": vl_interpolation,
                "tooltip": True,
            }

        mark_type = DRAW_STYLE_MAP.get(draw_style, "line")

        if mark_type == "line":
            line_mark = {
                "type": "line",
                "interpolate": vl_interpolation,
                "strokeWidth": line_width,
                "point": False,
                "tooltip": True,
            }
            stroke_dash = self._resolve_stroke_dash(line_style, line_width)
            if stroke_dash:
                line_mark["strokeDash"] = stroke_dash
            return line_mark

        # bars, points, etc.
        return {
            "type": mark_type,
            "tooltip": True,
        }

    def _build_timeseries_chart(
        self,
        data_values: List[Dict],
        panel: Dict[str, Any],
        unit: str,
        styling: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build a timeseries/line chart specification with full styling support.

        Supports:
        - Dual Y-axis (left/right) for series with different scales
        - Custom colors per series
        - Draw style (line, bars, points)
        - Line interpolation (linear, smooth, step)
        - Line width
        """
        if styling is None:
            styling = {}

        # Get unique series names preserving query result order (deterministic colors)
        series_names = list(dict.fromkeys(d["series"] for d in data_values))

        # Categorize series by axis placement
        left_series = []
        right_series = []
        series_overrides = styling.get("series_overrides", {})

        for series_name in series_names:
            override = series_overrides.get(series_name, {})
            axis = override.get("axis", styling.get("default_axis", "left"))
            if axis == "right":
                right_series.append(series_name)
            else:
                left_series.append(series_name)

        # Build color scale from overrides
        color_domain = []
        color_range = []
        default_palette = [
            "#5794F2",
            "#73BF69",
            "#F2495C",
            "#FF9830",
            "#B877D9",
            "#FADE2A",
            "#3274D9",
            "#56A64B",
            "#E02F44",
            "#FA6400",
        ]

        for i, series_name in enumerate(series_names):
            override = series_overrides.get(series_name, {})
            color_domain.append(series_name)

            if "color" in override:
                color_range.append(override["color"])
            else:
                # Use default palette color
                color_range.append(default_palette[i % len(default_palette)])

        # Resolve per-series mark configs
        mark_configs: Dict[str, Dict[str, Any]] = {}
        for sn in series_names:
            mark_configs[sn] = self._get_effective_mark_config(sn, styling)

        # Check if we need dual Y-axis
        if right_series and left_series:
            return self._build_dual_axis_chart(
                data_values,
                left_series,
                right_series,
                unit,
                styling,
                color_domain,
                color_range,
                mark_configs,
            )

        # Single Y-axis chart — check if all series share the same mark type
        return self._build_single_axis_chart(
            data_values,
            series_names,
            unit,
            color_domain,
            color_range,
            mark_configs,
        )

    @staticmethod
    def _build_layers_for_series_group(
        data_values: List[Dict],
        series_list: List[str],
        mark_configs: Dict[str, Dict[str, Any]],
        encoding: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Group series by their effective mark config and return one Vega-Lite
        layer per distinct mark type.  If all series share the same mark,
        returns a single layer (no unnecessary nesting).
        """
        # Group series by mark config (use JSON repr as grouping key)
        groups: Dict[str, list] = {}
        for sn in series_list:
            key = json.dumps(mark_configs[sn], sort_keys=True)
            groups.setdefault(key, []).append(sn)

        layers = []
        for mark_json, group_series in groups.items():
            mark = json.loads(mark_json)
            layer: Dict[str, Any] = {"mark": mark, "encoding": encoding}
            # Filter to this group's series if not the only group
            if len(groups) > 1:
                layer["transform"] = [{"filter": {"field": "series", "oneOf": group_series}}]
            layers.append(layer)
        return layers

    def _build_single_axis_chart(
        self,
        data_values: List[Dict],
        series_names: List[str],
        unit: str,
        color_domain: List[str],
        color_range: List[str],
        mark_configs: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build a single Y-axis chart, using layers when mark types are mixed."""
        encoding = {
            "x": {"field": "time", "type": "temporal", "title": "Time"},
            "y": {"field": "value", "type": "quantitative", "title": unit},
            "color": {
                "field": "series",
                "type": "nominal",
                "title": "Series",
                "scale": {"domain": color_domain, "range": color_range},
            },
        }

        layers = self._build_layers_for_series_group(
            data_values, series_names, mark_configs, encoding
        )

        if len(layers) == 1:
            # All series share same mark — flat spec (no layer nesting)
            return {
                "data": {"values": data_values},
                "mark": layers[0]["mark"],
                "encoding": encoding,
            }

        # Mixed mark types — layered spec
        return {
            "data": {"values": data_values},
            "layer": layers,
        }

    def _build_dual_axis_chart(
        self,
        data_values: List[Dict],
        left_series: List[str],
        right_series: List[str],
        unit: str,
        styling: Dict[str, Any],
        color_domain: List[str],
        color_range: List[str],
        mark_configs: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Build a dual Y-axis chart with independent scales.

        Left Y-axis: Primary series
        Right Y-axis: Secondary series (different scale)

        Each axis group may contain mixed mark types (e.g. line + area),
        which are rendered as sub-layers.
        """
        series_overrides = styling.get("series_overrides", {})

        # Get right axis unit (may be different from left)
        right_unit = "Count"  # Default for secondary axis
        for series_name in right_series:
            override = series_overrides.get(series_name, {})
            if "unit" in override:
                right_unit = UNIT_FORMATS.get(override["unit"], override["unit"])
                break

        # Filter data for each axis
        left_data = [d for d in data_values if d["series"] in left_series]
        right_data = [d for d in data_values if d["series"] in right_series]

        color_encoding = {
            "field": "series",
            "type": "nominal",
            "scale": {"domain": color_domain, "range": color_range},
        }

        # --- Left axis layers ---
        left_encoding = {
            "x": {"field": "time", "type": "temporal", "title": "Time"},
            "y": {
                "field": "value",
                "type": "quantitative",
                "title": unit,
                "axis": {"titleColor": color_range[0] if color_range else "#5794F2"},
            },
            "color": color_encoding,
        }
        left_layers = self._build_layers_for_series_group(
            left_data, left_series, mark_configs, left_encoding
        )
        # Attach data to each left sub-layer
        for layer in left_layers:
            layer["data"] = {"values": left_data}

        # --- Right axis layers ---
        right_encoding = {
            "x": {"field": "time", "type": "temporal"},
            "y": {
                "field": "value",
                "type": "quantitative",
                "title": right_unit,
                "axis": {
                    "orient": "right",
                    "titleColor": (
                        color_range[color_domain.index(right_series[0])]
                        if right_series and right_series[0] in color_domain
                        else "#FF9830"
                    ),
                },
            },
            "color": color_encoding,
        }
        right_layers = self._build_layers_for_series_group(
            right_data, right_series, mark_configs, right_encoding
        )
        for layer in right_layers:
            layer["data"] = {"values": right_data}

        # Suppress duplicate axes — only the first layer per side renders an axis.
        # Each layer shares the same encoding dict reference from
        # _build_layers_for_series_group, so deepcopy before mutating.
        for layer in left_layers[1:]:
            layer["encoding"] = copy.deepcopy(layer["encoding"])
            layer["encoding"]["y"]["axis"] = None

        for layer in right_layers[1:]:
            layer["encoding"] = copy.deepcopy(layer["encoding"])
            layer["encoding"]["y"]["axis"] = None

        return {
            "layer": left_layers + right_layers,
            "resolve": {
                "scale": {"y": "independent"},
                "axis": {"y": "independent"},
            },
        }

    def _build_stat_chart(
        self, data_values: List[Dict], panel: Dict[str, Any], unit: str
    ) -> Dict[str, Any]:
        """
        Build a stat/big number visualization.
        Shows the most recent value prominently.
        """
        # Get the last value for each series, then take the most recent
        if data_values:
            # Sort by time and get the last value
            sorted_values = sorted(data_values, key=lambda x: x.get("time", 0))
            last_value = sorted_values[-1].get("value", 0) if sorted_values else 0

            # Format the value
            if isinstance(last_value, float):
                formatted_value = f"{last_value:.2f}"
            else:
                formatted_value = str(last_value)
        else:
            formatted_value = "N/A"

        return {
            "data": {"values": [{"label": panel.get("title", "Value"), "value": formatted_value}]},
            "mark": {
                "type": "text",
                "fontSize": 72,
                "fontWeight": "bold",
                "align": "center",
                "baseline": "middle",
            },
            "encoding": {
                "text": {"field": "value", "type": "nominal"},
            },
        }

    def _build_gauge_chart(
        self, data_values: List[Dict], panel: Dict[str, Any], unit: str
    ) -> Dict[str, Any]:
        """
        Build a gauge/bar gauge visualization.
        Shows horizontal bars for each series value.
        """
        # Aggregate to get the last value per series
        series_values = {}
        for dv in data_values:
            series = dv.get("series", "Value")
            series_values[series] = dv.get("value", 0)

        bar_data = [{"series": k, "value": v} for k, v in series_values.items()]

        return {
            "data": {"values": bar_data if bar_data else [{"series": "No data", "value": 0}]},
            "mark": {"type": "bar", "tooltip": True},
            "encoding": {
                "y": {"field": "series", "type": "nominal", "title": "Series"},
                "x": {"field": "value", "type": "quantitative", "title": unit},
                "color": {"field": "series", "type": "nominal", "legend": None},
            },
        }

    def _build_pie_chart(
        self, data_values: List[Dict], panel: Dict[str, Any], unit: str
    ) -> Dict[str, Any]:
        """
        Build a pie chart visualization.
        Shows distribution of values across series.
        """
        # Aggregate to get the last value per series
        series_values = {}
        for dv in data_values:
            series = dv.get("series", "Value")
            series_values[series] = dv.get("value", 0)

        pie_data = [{"series": k, "value": v} for k, v in series_values.items()]

        return {
            "data": {"values": pie_data if pie_data else [{"series": "No data", "value": 1}]},
            "mark": {"type": "arc", "tooltip": True, "innerRadius": 50},
            "encoding": {
                "theta": {"field": "value", "type": "quantitative"},
                "color": {"field": "series", "type": "nominal", "title": "Series"},
            },
        }


# Global data client
data_client = GrafanaDataClient(GRAFANA_URL, GRAFANA_USERNAME, GRAFANA_PASSWORD)

# Cache for resolved query variables: key = (var_name, dependent_vars_tuple) -> (value, timestamp)
_variable_cache: Dict[str, Tuple[str, datetime]] = {}
CACHE_TTL = timedelta(minutes=5)


def _get_cached_variable(cache_key: str) -> Optional[str]:
    """Get a cached variable value if not expired."""
    if cache_key in _variable_cache:
        value, timestamp = _variable_cache[cache_key]
        if datetime.now() - timestamp < CACHE_TTL:
            return value
        # Expired, remove from cache
        del _variable_cache[cache_key]
    return None


def _set_cached_variable(cache_key: str, value: str) -> None:
    """Cache a resolved variable value."""
    _variable_cache[cache_key] = (value, datetime.now())


def _sanitize_tool_name(title: str) -> str:
    """
    Convert panel title to a safe tool name.

    Examples:
        "Financial Capacity Utilization Factor (CUF)" -> "financial_capacity_utilization_factor_cuf"
        "Trend: kWh sold / connection / month" -> "trend_kwh_sold_connection_month"
    """
    # Convert to lowercase
    name = title.lower()
    # Replace special characters with spaces
    name = re.sub(r"[:/\-()]+", " ", name)
    # Replace multiple spaces with single underscore
    name = re.sub(r"\s+", "_", name.strip())
    # Remove any remaining non-alphanumeric characters (except underscore)
    name = re.sub(r"[^a-z0-9_]", "", name)
    # Remove leading/trailing underscores
    name = name.strip("_")
    # Ensure it starts with a letter (prepend 'panel_' if not)
    if name and not name[0].isalpha():
        name = f"panel_{name}"
    return name or "unnamed_panel"


def _build_tool_mappings() -> None:
    """
    Build tool name to panel key mappings and variable label to name mappings.
    Called at startup and when tools are listed to ensure mappings are available.
    """
    global TOOL_NAME_TO_PANEL_KEY, TOOL_VAR_LABEL_TO_NAME

    # Skip if mappings already built
    if TOOL_NAME_TO_PANEL_KEY:
        return

    used_tool_names: Dict[str, int] = {}

    for panel_key, panel_info in PANELS_METADATA.items():
        if panel_key not in ENABLED_PANEL_IDS:
            continue

        panel_title = panel_info.get("title", "Untitled")

        # Build tool name from panel TITLE
        base_tool_name = _sanitize_tool_name(panel_title)
        tool_name = base_tool_name
        if base_tool_name in used_tool_names:
            used_tool_names[base_tool_name] += 1
            tool_name = f"{base_tool_name}_{used_tool_names[base_tool_name]}"
        else:
            used_tool_names[base_tool_name] = 1

        # Store mapping from tool name to panel key
        TOOL_NAME_TO_PANEL_KEY[tool_name] = panel_key

        # Build variable label to name mapping
        dashboard_uid = panel_info.get("dashboard_uid", "")
        variables = DASHBOARD_VARIABLES.get(dashboard_uid, [])
        if not variables:
            variables = panel_info.get("variables", [])

        var_label_to_name: Dict[str, str] = {}
        if variables and isinstance(variables[0], dict):
            for var in variables:
                var_name = var.get("name")
                if var_name:
                    var_label = var.get("label", var_name)
                    var_label_to_name[var_label] = var_name
        else:
            for var_name in variables:
                var_label_to_name[var_name] = var_name

        TOOL_VAR_LABEL_TO_NAME[tool_name] = var_label_to_name

    logger.info(f"Built tool mappings for {len(TOOL_NAME_TO_PANEL_KEY)} panels")


# Build mappings at startup
_build_tool_mappings()


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List available Grafana panel rendering tools"""
    global TOOL_NAME_TO_PANEL_KEY, TOOL_VAR_LABEL_TO_NAME

    # Don't list any tools if actions are disabled - prevents confusing UX
    # where bot describes capabilities but can't execute them
    if not GRAFANA_SERVER_ENABLED:
        logger.info("Grafana actions disabled - no tools listed")
        return []

    # Hot-reload metadata from DB so dashboard syncs take effect without restart
    _reload_metadata()

    # Rebuild mappings from (possibly refreshed) metadata
    TOOL_NAME_TO_PANEL_KEY = {}
    TOOL_VAR_LABEL_TO_NAME = {}
    _build_tool_mappings()

    tools = []
    used_tool_names: Dict[str, int] = {}

    # Only include enabled panels
    for panel_key, panel_info in PANELS_METADATA.items():
        if panel_key not in ENABLED_PANEL_IDS:
            continue

        # Extract panel metadata
        panel_title = panel_info.get("title", "Untitled")
        panel_type = panel_info.get("panel_type", "timeseries")
        base_description = panel_info.get("tool_description", f"Render {panel_title} panel")

        # Build tool description with guidance for the LLM
        # Instruct the LLM to ask for required variables rather than just listing them
        if panel_type in ["stat", "gauge", "bargauge"]:
            tool_description = (
                f"{base_description}. "
                f"Returns a calculated metric value. "
                f"IMPORTANT: Before calling this tool, ask the user which grid and time period "
                f"they want if not already specified."
            )
        else:
            tool_description = (
                f"{base_description}. "
                f"Returns a chart image or metric data. "
                f"IMPORTANT: Before calling this tool, ask the user which grid and time period "
                f"they want if not already specified."
            )

        # Look up variables from dashboard-level storage (not per-panel)
        dashboard_uid = panel_info.get("dashboard_uid", "")
        variables = DASHBOARD_VARIABLES.get(dashboard_uid, [])
        # Fallback to panel-level variables for backwards compatibility
        if not variables:
            variables = panel_info.get("variables", [])

        # Build tool name from panel TITLE (not internal ID)
        base_tool_name = _sanitize_tool_name(panel_title)
        tool_name = base_tool_name
        # Handle duplicate names by appending a number
        if base_tool_name in used_tool_names:
            used_tool_names[base_tool_name] += 1
            tool_name = f"{base_tool_name}_{used_tool_names[base_tool_name]}"
        else:
            used_tool_names[base_tool_name] = 1

        # Store mapping from tool name to panel key
        TOOL_NAME_TO_PANEL_KEY[tool_name] = panel_key

        # Build input schema with human-friendly variable LABELS (not internal names)
        var_properties = {}
        required_vars = []
        var_label_to_name: Dict[str, str] = {}

        # Check if variables is a list of dicts (new format) or list of strings (old format)
        if variables and isinstance(variables[0], dict):
            # New format with full variable metadata
            for var in variables:
                var_name = var.get("name")
                if not var_name:
                    continue

                # Distinguish between user-selectable and computed query variables:
                # - User-selectable: top-level dropdowns (e.g., gridName) with no dependencies
                # - Computed: variables that depend on other variables (e.g., estimatedActuals
                #   depends on gridName and time). These should be auto-resolved.
                #
                # Heuristic: If a query variable's query contains references to other
                # variables ($varname or ${varname}), it's computed and shouldn't be exposed.
                # Exclude Grafana time macros ($__from, $__to, $__timeFilter, etc.)
                var_type = var.get("type", "")
                if var_type == "query":
                    var_query = var.get("query", "")
                    # Find all variable references in the query
                    var_refs = re.findall(r"\$\{?(\w+)\}?", var_query)
                    # Filter out Grafana built-in macros (start with __)
                    dependent_vars = [v for v in var_refs if not v.startswith("__")]
                    if dependent_vars:
                        logger.debug(
                            f"Skipping computed query variable {var_name} - depends on: {dependent_vars}"
                        )
                        continue
                    # User-selectable query variables (no dependencies) are kept and exposed

                # Use label as the property key (human-friendly)
                var_label = var.get("label", var_name)
                # Store mapping from label to internal name
                var_label_to_name[var_label] = var_name

                var_schema: Dict[str, Any] = {
                    "type": "string",
                }

                # Build description without exposing internal names
                description_parts = []
                if var.get("description"):
                    description_parts.append(var["description"])

                # Add enum if options available
                if var.get("options"):
                    var_schema["enum"] = var["options"]
                    options_preview = ", ".join(var["options"][:5])
                    if len(var["options"]) > 5:
                        options_preview += f" (and {len(var['options']) - 5} more)"
                    description_parts.append(f"Options: {options_preview}")

                # Add free-text hint
                if var.get("free_text"):
                    description_parts.append("Free text input accepted")

                var_schema["description"] = (
                    ". ".join(description_parts) if description_parts else var_label
                )

                # Add default value
                if var.get("default"):
                    var_schema["default"] = var["default"]

                # Add to required list if marked as required (use label as key)
                if var.get("required", False):
                    required_vars.append(var_label)

                var_properties[var_label] = var_schema
        else:
            # Old format - backwards compatibility (list of variable names as labels)
            for var_name in variables:
                var_label_to_name[var_name] = var_name
                var_properties[var_name] = {
                    "type": "string",
                    "description": f"Variable: {var_name}",
                }

        # Store variable label->name mapping for this tool
        TOOL_VAR_LABEL_TO_NAME[tool_name] = var_label_to_name

        # Build final input schema (hide technical params like width/height from LLM)
        input_schema = {
            "type": "object",
            "properties": {
                **var_properties,  # Spread individual variable properties with label keys
                "time_from": {
                    "type": "string",
                    "description": "Start of time period (e.g., now-7d, now-30d). Default: last 6 hours",
                    "default": "now-6h",
                },
                "time_to": {
                    "type": "string",
                    "description": "End of time period. Usually 'now' for current time",
                    "default": "now",
                },
            },
            "required": required_vars,  # Only required variables (using labels)
        }

        tools.append(
            types.Tool(
                name=tool_name,
                description=tool_description,
                inputSchema=input_schema,
                visible_to_customer=False,
            )
        )

    logger.info(f"Grafana server: {len(tools)} tools available")
    logger.debug(f"Tool name mappings: {TOOL_NAME_TO_PANEL_KEY}")
    return tools


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: Dict[str, Any]
) -> List[types.TextContent | types.ImageContent]:
    """Handle tool calls"""

    try:
        # Check if actions are enabled
        if not GRAFANA_SERVER_ENABLED:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": False,
                            "error": "Grafana actions are disabled. Enable them in settings.",
                        }
                    ),
                )
            ]

        # Look up panel key from tool name mapping
        panel_key = TOOL_NAME_TO_PANEL_KEY.get(name)
        if not panel_key:
            raise ValueError(f"Unknown tool: {name}")

        panel_info = PANELS_METADATA.get(panel_key)
        if not panel_info:
            raise ValueError(f"Panel not found: {panel_key}")

        # Extract arguments
        width = arguments.get("width", 1000)
        height = arguments.get("height", 500)
        time_from = arguments.get("time_from", "now-6h")
        time_to = arguments.get("time_to", "now")
        time_range = {"from": time_from, "to": time_to}

        # Build variables dict using LABELS as keys (not internal names)
        # This allows generate_panel_visualization to match with the live dashboard
        # by label, avoiding issues where indexed metadata has stale variable names
        var_label_to_name = TOOL_VAR_LABEL_TO_NAME.get(name, {})
        logger.info(f"Tool {name} arguments: {arguments}")
        logger.info(f"Tool {name} var_label_to_name mapping: {var_label_to_name}")

        # Build variables dict with labels as keys for reliable matching
        variables = {}
        for label, internal_name in var_label_to_name.items():
            if label in arguments:
                # Use label as key, not internal_name
                variables[label] = arguments[label]
            elif internal_name in arguments:
                # Fallback: also accept internal name directly (backwards compat)
                # Map it to the label for consistent handling
                variables[label] = arguments[internal_name]

        logger.info(f"Variables (keyed by label): {variables}")

        # Extract panel details
        dashboard_uid: str = str(panel_info["dashboard_uid"])
        # Panel key format: {dashboard_uid}:{panel_id} (e.g., "df7gn304ulce8b:2")
        panel_id_str = panel_key.split(":")[-1]
        panel_id: int = int(panel_id_str)
        panel_title = panel_info.get("title")

        # Build direct panel link with variables pre-filled
        dashboard_slug = panel_info.get("dashboard_name", "")
        panel_url_parts = [
            f"{GRAFANA_URL}/d/{dashboard_uid}/{dashboard_slug}",
            f"?orgId=1&viewPanel={panel_id_str}",
            f"&from={time_from}&to={time_to}",
        ]
        for label, value in variables.items():
            internal_name = var_label_to_name.get(label, label)
            panel_url_parts.append(f"&var-{internal_name}={value}")
        panel_url = "".join(panel_url_parts)

        # Generate panel visualization (returns bytes for charts, dict for single metrics).
        # Offload to a thread so the sync httpx calls don't block the async event loop.
        # wait_for cap (150s) is below GRAFANA_QUERY_TIMEOUT (180s) so Gemini gets an error
        # response before its own retry threshold, preventing doubled worst-case wait times.
        result = await asyncio.wait_for(
            asyncio.to_thread(
                data_client.generate_panel_visualization,
                dashboard_uid=dashboard_uid,
                panel_id=panel_id,
                variables=variables,
                time_range=time_range,
                width=width,
                height=height,
                panel_title=panel_title,
            ),
            timeout=150,
        )

        # Check if result is a single metric (dict) or chart image+data (tuple)
        if isinstance(result, dict):
            # Single metric - return JSON data only (no image)
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": True,
                            "result_type": "single_metric",
                            "panel_title": panel_info.get("title"),
                            "dashboard_title": panel_info.get("dashboard_title"),
                            "variables": variables,
                            "time_range": time_range,
                            "panel_url": panel_url,
                            "data": result,
                        }
                    ),
                ),
            ]
        else:
            # Chart image + structured series data
            assert isinstance(result, tuple)
            png_bytes, series_data = result
            image_base64 = base64.b64encode(png_bytes).decode("utf-8")

            return [
                types.ImageContent(
                    type="image",
                    data=image_base64,
                    mimeType="image/png",
                ),
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": True,
                            "result_type": "chart_image",
                            "panel_title": panel_info.get("title"),
                            "dashboard_title": panel_info.get("dashboard_title"),
                            "variables": variables,
                            "time_range": time_range,
                            "panel_url": panel_url,
                            "series_data": series_data,
                        }
                    ),
                ),
            ]

    except ValueError as e:
        error_msg = str(e)
        logger.error(f"Validation error rendering panel: {error_msg}")

        # Determine error type for better LLM handling
        error_response = {
            "success": False,
            "error": error_msg,
        }

        if "Missing required variables" in error_msg:
            error_response["error_type"] = "missing_variables"
            error_response["help"] = (
                "Please provide all required variables as parameters to this tool."
            )
        elif "Panel" in error_msg and "not found" in error_msg:
            error_response["error_type"] = "panel_not_found"
            error_response["help"] = (
                "This panel may have been removed or the panel ID is incorrect."
            )
        elif "no queries" in error_msg:
            error_response["error_type"] = "no_data"
            error_response["help"] = "This panel has no query configured."
        else:
            error_response["error_type"] = "validation_error"

        return [types.TextContent(type="text", text=json.dumps(error_response))]

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error rendering panel: {e.response.status_code} - {str(e)}")
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "error": f"Grafana API error: {e.response.status_code}",
                        "error_type": "api_error",
                        "help": "There was an error communicating with Grafana. The dashboard or panel may not be accessible.",
                    }
                ),
            )
        ]

    except Exception as e:
        logger.error(f"Unexpected error rendering panel: {str(e)}")
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {"success": False, "error": str(e), "error_type": "unexpected_error"}
                ),
            )
        ]


@server.list_resources()
async def handle_list_resources() -> List[types.Resource]:
    """List available resources"""
    return [
        types.Resource(
            uri="grafana://config",
            name="Grafana Server Configuration",
            description="Current Grafana server configuration and enabled panels",
            mimeType="application/json",
        )
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content"""
    if uri == "grafana://config":
        config = {
            "grafana_url": GRAFANA_URL,
            "actions_enabled": GRAFANA_SERVER_ENABLED,
            "total_panels": len(PANELS_METADATA),
            "enabled_panels": len(ENABLED_PANEL_IDS),
            "enabled_panel_ids": list(ENABLED_PANEL_IDS),
        }
        return json.dumps(config, indent=2)
    else:
        raise ValueError(f"Unknown resource: {uri}")


async def main():
    """Main entry point"""
    try:
        logger.info("Starting Grafana MCP Server...")
        print("✅ Grafana server initialized successfully", file=sys.stderr)

        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="grafana-server",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(), experimental_capabilities={}
                    ),
                ),
            )
    except Exception as e:
        print(f"❌ Fatal error in Grafana server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Grafana server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Grafana server crashed: {e}", file=sys.stderr)
        sys.exit(1)
