#!/usr/bin/env python3
"""
Grafana Indexer - Incremental Wrapper

Wrapper around grafana_indexer_v2.py that integrates with the batch ingestion system
and stores metadata in Supabase database.
"""

import json
import logging
import os
import sys
from typing import Any, Dict

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grafana_indexer_v2 import index_grafana_panels
from services.grafana_metadata_service import (
    load_all_dashboards_metadata,
    save_all_dashboards_metadata,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)


def index_all_grafana_panels(since_last_run: bool = False) -> Dict[str, Any]:
    """
    Index all Grafana panels and update environment variable.

    Args:
        since_last_run: Ignored for Grafana (always does full sync)

    Returns:
        Result dictionary with status and statistics
    """
    logger.info("=" * 70)
    logger.info("GRAFANA PANELS INDEXING")
    logger.info("=" * 70)
    print("\n" + "=" * 70)
    print("GRAFANA PANELS INDEXING")
    print("=" * 70)

    try:
        # Get configuration
        grafana_url = os.getenv("GRAFANA_URL")
        grafana_username = os.getenv("GRAFANA_USERNAME")
        grafana_password = os.getenv("GRAFANA_PASSWORD")
        folder_name = os.getenv("GRAFANA_FOLDER_NAME", "")
        gemini_api_key = os.getenv("GOOGLE_API_KEY")

        logger.info(
            f"Configuration loaded: folder_name={folder_name}, has_api_key={bool(gemini_api_key)}"
        )
        system_prompt = os.getenv(
            "GRAFANA_PANEL_DESCRIPTION_PROMPT",
            "You are a system that generates tool descriptions for Grafana dashboard panels. "
            "Given a panel with title, description, query, and dashboard variables, create a concise "
            "tool description that explains what data this panel shows and what variables it requires. "
            "Format: A tool description suitable for an LLM to understand when to use this panel.",
        )

        # Validate configuration
        if not all([grafana_url, grafana_username, grafana_password, gemini_api_key]):
            missing = []
            if not grafana_url:
                missing.append("GRAFANA_URL")
            if not grafana_username:
                missing.append("GRAFANA_USERNAME")
            if not grafana_password:
                missing.append("GRAFANA_PASSWORD")
            if not gemini_api_key:
                missing.append("GOOGLE_API_KEY")

            return {
                "status": "error",
                "message": f"Missing required environment variables: {', '.join(missing)}",
                "panels_indexed": 0,
            }

        # Get enabled dashboards filter (for indexing all panels in these dashboards)
        enabled_dashboards_str = os.getenv("GRAFANA_ENABLED_DASHBOARDS", "")
        enabled_dashboard_uids = (
            [uid.strip() for uid in enabled_dashboards_str.split(",") if uid.strip()]
            if enabled_dashboards_str
            else None
        )

        # Get enabled panels filter (for Gemini description generation)
        enabled_panels_str = os.getenv("GRAFANA_ENABLED_PANELS", "")
        enabled_panel_keys = (
            [key.strip() for key in enabled_panels_str.split(",") if key.strip()]
            if enabled_panels_str
            else None
        )

        if enabled_panel_keys:
            msg = f"ℹ️  Gemini descriptions will be generated for {len(enabled_panel_keys)} enabled panels"
            logger.info(msg)
            print(msg)
        else:
            msg = "ℹ️  Gemini descriptions will be generated for ALL panels (no filter)"
            logger.info(msg)
            print(msg)

        # Load existing metadata for incremental updates - prefer Supabase, fallback to env vars
        existing_panels_metadata = {}
        try:
            existing_panels_metadata, _, _ = load_all_dashboards_metadata()
            if existing_panels_metadata:
                print(f"✓ Loaded {len(existing_panels_metadata)} existing panels from database")
            else:
                # Fallback to env vars for backwards compatibility
                existing_panels_metadata_str = os.getenv("GRAFANA_PANELS_METADATA", "{}")
                existing_panels_metadata = json.loads(existing_panels_metadata_str)
                if existing_panels_metadata:
                    print(
                        f"✓ Loaded {len(existing_panels_metadata)} existing panels from env vars (fallback)"
                    )
        except Exception as e:
            print(f"⚠️  Failed to load existing metadata: {e}, will do full reindex")
            existing_panels_metadata = {}

        # Check for force reindex flag
        force_reindex = os.getenv("GRAFANA_FORCE_FULL_REINDEX", "false").lower() == "true"
        if force_reindex:
            print("ℹ️  Force reindex enabled, will regenerate all descriptions")

        # Run indexing
        panels_metadata, available_dashboards, dashboard_variables = index_grafana_panels(
            grafana_url=grafana_url,
            grafana_username=grafana_username,
            grafana_password=grafana_password,
            folder_name=folder_name,
            gemini_api_key=gemini_api_key,
            system_prompt=system_prompt,
            enabled_dashboard_uids=enabled_dashboard_uids,
            enabled_panel_keys=enabled_panel_keys,
            existing_metadata=existing_panels_metadata,
            force_reindex=force_reindex,
        )

        # Save to Supabase (primary storage)
        enabled_panels_str = os.getenv("GRAFANA_ENABLED_PANELS", "")
        saved, failed = save_all_dashboards_metadata(
            panels_metadata=panels_metadata,
            dashboard_variables=dashboard_variables,
            enabled_panels_str=enabled_panels_str,
        )
        print(f"\n✓ Saved {saved} dashboards to database ({failed} failed)")
        print(
            f"✓ Indexed {len(panels_metadata)} panels from {len(available_dashboards)} available dashboards"
        )
        print(f"✓ Variables stored for {len(dashboard_variables)} dashboards")

        return {
            "status": "completed",
            "panels_indexed": len(panels_metadata),
            "folder_name": folder_name,
            "dashboards_processed": len(set(p["dashboard_uid"] for p in panels_metadata.values())),
        }

    except Exception as e:
        error_msg = f"\n❌ Grafana indexing failed: {e}"
        logger.error(error_msg)
        print(error_msg, file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)

        return {
            "status": "error",
            "message": str(e),
            "panels_indexed": 0,
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Index Grafana panels (incremental wrapper)")
    parser.add_argument(
        "--since-last-run",
        action="store_true",
        help="Ignored for Grafana (always full sync)",
    )

    args = parser.parse_args()

    result = index_all_grafana_panels(since_last_run=args.since_last_run)

    if result["status"] == "completed":
        print(f"\n✅ Grafana indexing completed: {result['panels_indexed']} panels indexed")
        sys.exit(0)
    else:
        print(f"\n❌ Grafana indexing failed: {result.get('message', 'Unknown error')}")
        sys.exit(1)
