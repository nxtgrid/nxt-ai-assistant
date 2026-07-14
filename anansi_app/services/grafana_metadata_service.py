"""
Grafana Metadata Storage Service

Stores and retrieves Grafana dashboard metadata from Supabase instead of
environment variables. This allows for larger metadata storage and
updates without redeploying.

Schema:
    CREATE TABLE grafana_dashboard_metadata (
      dashboard_uid TEXT PRIMARY KEY,
      dashboard_name TEXT NOT NULL,
      dashboard_title TEXT,
      panels JSONB NOT NULL DEFAULT '{}',
      variables JSONB NOT NULL DEFAULT '[]',
      enabled_panel_ids TEXT[] DEFAULT '{}',
      indexed_at TIMESTAMPTZ DEFAULT NOW(),
      created_at TIMESTAMPTZ DEFAULT NOW()
    );
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from services._cache_compat import cache_data

logger = logging.getLogger(__name__)


def get_supabase_client():
    """Get Supabase client for Chat DB."""
    try:
        from supabase import create_client
    except ImportError:
        logger.error("supabase package not installed")
        return None

    url = os.getenv("CHAT_DB_URL")
    key = os.getenv("CHAT_DB_SERVICE_KEY")

    if not url or not key:
        logger.error("CHAT_DB_URL or CHAT_DB_SERVICE_KEY not configured")
        return None

    return create_client(url, key)


def save_dashboard_metadata(
    dashboard_uid: str,
    dashboard_name: str,
    dashboard_title: str,
    panels: Dict[str, Any],
    variables: List[Dict[str, Any]],
    enabled_panel_ids: List[str],
) -> bool:
    """
    Save or update metadata for a single dashboard.

    Args:
        dashboard_uid: Dashboard UID
        dashboard_name: Dashboard name (slug)
        dashboard_title: Dashboard display title
        panels: Dict of panel metadata keyed by panel_id (not full key)
        variables: List of dashboard variable definitions
        enabled_panel_ids: List of enabled panel IDs for this dashboard

    Returns:
        True if successful, False otherwise
    """
    client = get_supabase_client()
    if not client:
        return False

    try:
        data = {
            "dashboard_uid": dashboard_uid,
            "dashboard_name": dashboard_name,
            "dashboard_title": dashboard_title,
            "panels": panels,
            "variables": variables,
            "enabled_panel_ids": enabled_panel_ids,
            "indexed_at": datetime.utcnow().isoformat(),
        }

        # Upsert (insert or update on conflict)
        (
            client.table("grafana_dashboard_metadata")
            .upsert(data, on_conflict="dashboard_uid")
            .execute()
        )

        logger.info(f"Saved metadata for dashboard {dashboard_uid}: {len(panels)} panels")
        return True

    except Exception as e:
        logger.error(f"Failed to save dashboard metadata for {dashboard_uid}: {e}")
        return False


def save_all_dashboards_metadata(
    panels_metadata: Dict[str, Any],
    dashboard_variables: Dict[str, List[Dict[str, Any]]],
    enabled_panels_str: str,
) -> Tuple[int, int]:
    """
    Save metadata for all dashboards from indexer output.

    This converts the flat panel metadata dict (keyed by dashboard_uid:panel_id)
    into per-dashboard rows for efficient storage and retrieval.

    Args:
        panels_metadata: Dict of panel metadata keyed by "dashboard_uid:panel_id"
        dashboard_variables: Dict of variables keyed by dashboard_uid
        enabled_panels_str: Comma-separated string of enabled panel keys

    Returns:
        Tuple of (dashboards_saved, dashboards_failed)
    """
    # Parse enabled panels into a set for fast lookup
    enabled_panel_keys = set(key.strip() for key in enabled_panels_str.split(",") if key.strip())

    # Group panels by dashboard
    dashboards: Dict[str, Dict[str, Any]] = {}

    for panel_key, panel_data in panels_metadata.items():
        parts = panel_key.split(":", 1)
        if len(parts) != 2:
            logger.warning(f"Invalid panel key format: {panel_key}")
            continue

        dashboard_uid, panel_id = parts

        if dashboard_uid not in dashboards:
            dashboards[dashboard_uid] = {
                "dashboard_name": panel_data.get("dashboard_name", ""),
                "dashboard_title": panel_data.get("dashboard_title", ""),
                "panels": {},
                "enabled_panel_ids": [],
            }

        # Store panel data keyed by panel_id only (not full key)
        dashboards[dashboard_uid]["panels"][panel_id] = panel_data

        # Track if this panel is enabled
        if panel_key in enabled_panel_keys:
            dashboards[dashboard_uid]["enabled_panel_ids"].append(panel_id)

    # Save each dashboard
    saved = 0
    failed = 0

    for dashboard_uid, dash_data in dashboards.items():
        variables = dashboard_variables.get(dashboard_uid, [])

        success = save_dashboard_metadata(
            dashboard_uid=dashboard_uid,
            dashboard_name=dash_data["dashboard_name"],
            dashboard_title=dash_data["dashboard_title"],
            panels=dash_data["panels"],
            variables=variables,
            enabled_panel_ids=dash_data["enabled_panel_ids"],
        )

        if success:
            saved += 1
        else:
            failed += 1

    logger.info(f"Saved {saved} dashboards, {failed} failed")
    return saved, failed


def load_all_dashboards_metadata() -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]], str]:
    """
    Load all dashboard metadata from Supabase.

    Returns:
        Tuple of (panels_metadata, dashboard_variables, enabled_panels_str)
        - panels_metadata: Dict keyed by "dashboard_uid:panel_id"
        - dashboard_variables: Dict keyed by dashboard_uid
        - enabled_panels_str: Comma-separated string of enabled panel keys
    """
    client = get_supabase_client()
    if not client:
        logger.warning("Supabase client not available, returning empty metadata")
        return {}, {}, ""

    try:
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

        enabled_panels_str = ",".join(enabled_panel_keys)

        logger.info(f"Loaded {len(panels_metadata)} panels from {len(result.data)} dashboards")
        return panels_metadata, dashboard_variables, enabled_panels_str

    except Exception as e:
        logger.error(f"Failed to load dashboard metadata: {e}")
        return {}, {}, ""


@cache_data(ttl=300, show_spinner=False)
def load_panels_metadata() -> Dict[str, Dict[str, Any]]:
    """
    Return a {panel_key: {title, dashboard_uid, dashboard_title}} mapping from Supabase.

    Used by the settings page for the Enabled Panels dropdown.
    Cached for 300s — call load_panels_metadata.clear() after a sync.
    """
    client = get_supabase_client()
    if not client:
        return {}

    try:
        result = (
            client.table("grafana_dashboard_metadata")
            .select("dashboard_uid, dashboard_title, panels")
            .execute()
        )
        panels: Dict[str, Dict[str, Any]] = {}
        for row in result.data:
            dashboard_uid = row.get("dashboard_uid", "")
            dashboard_title = row.get("dashboard_title", "")
            for panel_id, panel_data in (row.get("panels") or {}).items():
                panel_key = f"{dashboard_uid}:{panel_id}"
                panels[panel_key] = {
                    "title": panel_data.get("title", "Untitled"),
                    "dashboard_uid": dashboard_uid,
                    "dashboard_title": dashboard_title,
                }
        return panels
    except Exception as e:
        logger.error(f"Failed to load panels metadata: {e}")
        return {}


@cache_data(ttl=300, show_spinner=False)
def load_available_dashboards() -> Dict[str, str]:
    """
    Return a {dashboard_uid: dashboard_title} mapping from Supabase.

    Used by the settings page for the Enabled Dashboards dropdown.
    Falls back to an empty dict if the DB is unavailable.
    Cached for 300s — call load_available_dashboards.clear() after a sync.
    """
    client = get_supabase_client()
    if not client:
        return {}

    try:
        result = (
            client.table("grafana_dashboard_metadata")
            .select("dashboard_uid, dashboard_title")
            .execute()
        )
        return {
            row["dashboard_uid"]: row["dashboard_title"] or row["dashboard_uid"]
            for row in result.data
            if row.get("dashboard_uid")
        }
    except Exception as e:
        logger.error(f"Failed to load available dashboards: {e}")
        return {}


def load_dashboard_metadata(dashboard_uid: str) -> Optional[Dict[str, Any]]:
    """
    Load metadata for a single dashboard.

    Args:
        dashboard_uid: Dashboard UID

    Returns:
        Dashboard metadata dict or None if not found
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        result = (
            client.table("grafana_dashboard_metadata")
            .select("*")
            .eq("dashboard_uid", dashboard_uid)
            .single()
            .execute()
        )

        data: Optional[Dict[str, Any]] = result.data
        return data

    except Exception as e:
        logger.error(f"Failed to load dashboard metadata for {dashboard_uid}: {e}")
        return None


def get_enabled_panels() -> List[str]:
    """
    Get list of all enabled panel keys across all dashboards.

    Returns:
        List of panel keys in format "dashboard_uid:panel_id"
    """
    client = get_supabase_client()
    if not client:
        return []

    try:
        result = (
            client.table("grafana_dashboard_metadata")
            .select("dashboard_uid, enabled_panel_ids")
            .execute()
        )

        enabled_keys = []
        for row in result.data:
            dashboard_uid = row["dashboard_uid"]
            enabled_ids = row.get("enabled_panel_ids", [])
            for panel_id in enabled_ids:
                enabled_keys.append(f"{dashboard_uid}:{panel_id}")

        return enabled_keys

    except Exception as e:
        logger.error(f"Failed to get enabled panels: {e}")
        return []


def update_enabled_panels(dashboard_uid: str, enabled_panel_ids: List[str]) -> bool:
    """
    Update the list of enabled panels for a dashboard.

    Args:
        dashboard_uid: Dashboard UID
        enabled_panel_ids: List of panel IDs (not full keys) to enable

    Returns:
        True if successful, False otherwise
    """
    client = get_supabase_client()
    if not client:
        return False

    try:
        (
            client.table("grafana_dashboard_metadata")
            .update({"enabled_panel_ids": enabled_panel_ids})
            .eq("dashboard_uid", dashboard_uid)
            .execute()
        )

        logger.info(f"Updated enabled panels for {dashboard_uid}: {len(enabled_panel_ids)} panels")
        return True

    except Exception as e:
        logger.error(f"Failed to update enabled panels for {dashboard_uid}: {e}")
        return False
