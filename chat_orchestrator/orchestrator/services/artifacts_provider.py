"""
Artifacts Provider - Unified bot artifacts management from Supabase.

This service retrieves bot artifacts (prompts, templates, training data, etc.)
from the unified bot_artifacts table in Supabase, replacing file-based loading.

Supports both customer support and staff modes with consistent interface.
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Module-level cache for Google Docs (survives across requests)
import time
from typing import Tuple

_GDOC_CACHE: Dict[str, Tuple[Any, float]] = {}
_GDOC_CACHE_TTL = 3600  # 1 hour


def _get_cached_gdoc(cache_key: str) -> Optional[Any]:
    """Get cached Google Doc data if not expired."""
    if cache_key in _GDOC_CACHE:
        data, expires_at = _GDOC_CACHE[cache_key]
        if time.time() < expires_at:
            LOGGER.debug(f"Module cache hit for: {cache_key}")
            return data
        del _GDOC_CACHE[cache_key]  # Expired, clean up
    return None


def _set_cached_gdoc(cache_key: str, data: Any) -> None:
    """Cache Google Doc data with TTL."""
    _GDOC_CACHE[cache_key] = (data, time.time() + _GDOC_CACHE_TTL)
    LOGGER.info(f"Cached Google Doc: {cache_key} (TTL: {_GDOC_CACHE_TTL}s)")


def clear_gdoc_cache(doc_id: Optional[str] = None) -> None:
    """Clear Google Doc cache to force reload.

    Args:
        doc_id: If provided, clear only this doc's cache.
                If None, clear all cached docs.
    """
    global _GDOC_CACHE
    if doc_id:
        keys_to_clear = [k for k in _GDOC_CACHE if doc_id in k]
        for key in keys_to_clear:
            del _GDOC_CACHE[key]
        LOGGER.info(f"Cleared cache for Google Doc: {doc_id} ({len(keys_to_clear)} entries)")
    else:
        count = len(_GDOC_CACHE)
        _GDOC_CACHE.clear()
        LOGGER.info(f"Cleared all Google Doc cache ({count} entries)")


# Add parent directory (anansi) to path for shared module imports
_ANANSI_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ANANSI_ROOT) not in sys.path:
    sys.path.insert(0, str(_ANANSI_ROOT))

# Add rag_pipeline to path for google_auth import
_RAG_PIPELINE_PATH = _ANANSI_ROOT / "rag_pipeline" / "ingestion"
if str(_RAG_PIPELINE_PATH) not in sys.path:
    sys.path.insert(0, str(_RAG_PIPELINE_PATH))


class Artifact(BaseModel):
    """Represents a bot artifact."""

    id: str
    artifact_type: str
    bot_mode: str
    name: str
    category: Optional[str] = None
    tags: List[str] = []
    content: Dict[str, Any]
    version: int = 1
    priority: int = 0
    metadata: Dict[str, Any] = {}
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ArtifactsProvider:
    """
    Provides unified access to bot artifacts from Supabase.

    Replaces file-based loading of customer support JSONs and provides
    consistent interface for staff mode artifacts.
    """

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
    ):
        """
        Initialize artifacts provider.

        Args:
            supabase_url: URL to main Supabase instance
            supabase_key: Service key for Supabase
        """
        self._supabase_url = (
            supabase_url or os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
        )
        self._supabase_key = (
            supabase_key or os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
        )
        self._client = None
        self._cache: Dict[str, Any] = {}

    def _get_client(self):
        """Get or create Supabase client."""
        if self._client is None and self._supabase_url and self._supabase_key:
            try:
                from supabase import create_client

                self._client = create_client(self._supabase_url, self._supabase_key)
                LOGGER.info("Artifacts Supabase client initialized")
            except ImportError:
                LOGGER.error("supabase-py not installed. Install with: pip install supabase")
                return None
        return self._client

    async def get_customer_support_artifacts(self, use_cache: bool = True) -> Dict[str, Any]:
        """
        Get all customer support artifacts in structured format.

        Returns dictionary with:
        - system_instructions: str
        - qa_knowledge_base: dict with metadata and qa_pairs list
        - response_templates: dict with metadata and templates by category
        - decision_rules: dict with metadata and rules list
        - entity_training: dict with metadata and training_data list

        Args:
            use_cache: Whether to use cached data if available

        Returns:
            Dict containing all customer support artifacts
        """
        cache_key = "customer_support_artifacts"

        if use_cache and cache_key in self._cache:
            LOGGER.info("Returning cached customer support artifacts")
            return dict(self._cache[cache_key])

        client = self._get_client()
        if not client:
            LOGGER.error("No Supabase client available")
            return self._get_empty_customer_artifacts()

        try:
            # Use the RPC function to get all artifacts in legacy format
            result = client.rpc("get_customer_support_artifacts").execute()

            if not result.data:
                LOGGER.warning("No customer support artifacts found")
                return self._get_empty_customer_artifacts()

            artifacts = result.data

            # Cache the result
            self._cache[cache_key] = artifacts

            LOGGER.info(
                f"Loaded customer support artifacts: "
                f"{len(artifacts.get('qa_knowledge_base', {}).get('qa_pairs', []))} QA pairs, "
                f"{len(artifacts.get('decision_rules', {}).get('rules', []))} rules, "
                f"{len(artifacts.get('entity_training', {}).get('training_data', []))} entity examples"
            )

            return dict(artifacts)

        except Exception as e:
            LOGGER.exception(f"Error loading customer support artifacts: {e}")
            return self._get_empty_customer_artifacts()

    async def get_system_instructions(self, bot_mode: str = "customer_support") -> str:
        """
        Get system instructions for a specific bot mode.

        Args:
            bot_mode: 'customer_support' or 'staff'

        Returns:
            System instructions text
        """
        client = self._get_client()
        if not client:
            return ""

        try:
            result = (
                client.table("bot_artifacts")
                .select("content")
                .eq("artifact_type", "system_instruction")
                .in_("bot_mode", [bot_mode, "shared"])
                .eq("is_active", True)
                .is_("deleted_at", None)
                .order("priority", desc=True)
                .order("updated_at", desc=True)
                .limit(1)
                .execute()
            )

            if result.data and len(result.data) > 0:
                content = result.data[0]["content"]
                return str(content.get("text", ""))

            return ""

        except Exception as e:
            LOGGER.exception(f"Error loading system instructions: {e}")
            return ""

    async def get_artifacts(
        self,
        bot_mode: str,
        artifact_types: Optional[List[str]] = None,
        category: Optional[str] = None,
        include_inactive: bool = False,
    ) -> List[Artifact]:
        """
        Get artifacts with filtering.

        Args:
            bot_mode: Bot mode to filter by
            artifact_types: List of artifact types to include
            category: Optional category filter
            include_inactive: Include inactive artifacts

        Returns:
            List of Artifact objects
        """
        client = self._get_client()
        if not client:
            return []

        try:
            result = client.rpc(
                "get_bot_artifacts",
                {
                    "p_bot_mode": bot_mode,
                    "p_artifact_types": artifact_types,
                    "p_category": category,
                    "p_include_inactive": include_inactive,
                },
            ).execute()

            if not result.data:
                return []

            # Convert to Artifact objects
            artifacts = [Artifact(**item) for item in result.data]

            LOGGER.info(
                f"Loaded {len(artifacts)} artifacts for bot_mode={bot_mode}, "
                f"types={artifact_types}, category={category}"
            )

            return artifacts

        except Exception as e:
            LOGGER.exception(f"Error loading artifacts: {e}")
            return []

    async def get_staff_instructions(
        self,
        roles: Optional[List[str]] = None,
        entity_types: Optional[List[str]] = None,
        context_type: Optional[str] = None,
    ) -> List[Artifact]:
        """
        Get staff mode instructions with filtering.

        Args:
            roles: Filter by roles (e.g., ['admin', 'developer'])
            entity_types: Filter by entity types (e.g., ['grid', 'meter'])
            context_type: Filter by context type (e.g., 'general', 'role', 'entity')

        Returns:
            List of instruction artifacts
        """
        client = self._get_client()
        if not client:
            return []

        try:
            result = client.rpc(
                "get_staff_instructions",
                {
                    "p_roles": roles,
                    "p_entity_types": entity_types,
                    "p_context_type": context_type,
                },
            ).execute()

            if not result.data:
                return []

            # Convert to Artifact objects
            artifacts = [Artifact(**item) for item in result.data]

            LOGGER.info(
                f"Loaded {len(artifacts)} staff instructions for "
                f"roles={roles}, entity_types={entity_types}, context={context_type}"
            )

            return artifacts

        except Exception as e:
            LOGGER.exception(f"Error loading staff instructions: {e}")
            return []

    def _fetch_google_doc(self, doc_id: str) -> Optional[str]:
        """
        Fetch Google Drive document content as plain text.

        Uses shared GoogleDriveDocFetcher for consistency across Anansi.
        Supports Google Docs, Sheets, PDFs, DOCX, and other formats.

        Args:
            doc_id: Google Drive document ID

        Returns:
            Document content as plain text, or None if fetch fails
        """
        if not doc_id:
            return None

        # Check module-level cache first (survives across requests)
        cache_key = f"gdoc_{doc_id}"
        cached = _get_cached_gdoc(cache_key)
        if cached is not None:
            LOGGER.info(f"Using module-cached Google Doc: {doc_id}")
            return str(cached)

        try:
            # Import shared fetcher
            import time

            from shared.utils.gdrive_doc_fetcher import GoogleDriveDocFetcher

            fetcher = GoogleDriveDocFetcher()

            # Retry on transient Google API failures
            content = None
            for attempt in range(3):
                content = fetcher.fetch_document(doc_id, auto_detect_type=True)
                if content:
                    break
                if attempt < 2:
                    LOGGER.warning(
                        f"Google Doc fetch attempt {attempt + 1}/3 failed for {doc_id}, retrying..."
                    )
                    time.sleep(1 + random.random())  # 1-2s with jitter

            if content:
                # Cache in module-level cache
                _set_cached_gdoc(cache_key, content)
                LOGGER.info(f"Fetched Google Doc {doc_id}: {len(content)} chars")
                return content
            else:
                LOGGER.error(f"Failed to fetch Google Doc {doc_id} after 3 attempts")
                return None

        except Exception as e:
            LOGGER.exception(f"Error fetching Google Doc {doc_id}: {e}")
            return None

    def _fetch_google_doc_sections(
        self, doc_id: str, start_section: str = "system instructions"
    ) -> Optional[Dict[str, str]]:
        """
        Fetch Google Drive document with markdown conversion and parse into sections.

        Uses Google Docs API to preserve formatting (headings, bold, etc.) and converts
        to markdown, then parses by heading sections.

        Args:
            doc_id: Google Drive document ID
            start_section: Section name to start from (default: 'system instructions').
                          Everything before this section is ignored.

        Returns:
            Dictionary mapping section names to content, or None if fetch fails

        Example doc structure (using Google Docs heading styles):
            Heading 1: System Instructions
            Be helpful and professional...

            Heading 1: QnA Knowledge Base
            Q: What is X?
            A: X is Y...

            Heading 1: Example Conversations
            User: Hello
            Bot: Hi! How can I help?
        """
        if not doc_id:
            return None

        # Check module-level cache first (survives across requests)
        cache_key = f"gdoc_sections_{doc_id}_{start_section}"
        cached = _get_cached_gdoc(cache_key)
        if cached is not None:
            LOGGER.info(f"Using module-cached Google Doc sections: {doc_id}")
            return dict(cached) if cached else None

        try:
            # Import shared fetcher with markdown conversion
            # Fetch with retry (Google Docs API has transient failures)
            import time

            from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown_sections

            sections = None
            for attempt in range(3):
                sections = fetch_google_doc_markdown_sections(doc_id, start_section=start_section)
                if sections:
                    break
                if attempt < 2:
                    LOGGER.warning(
                        f"Google Doc fetch attempt {attempt + 1}/3 failed for {doc_id}, retrying..."
                    )
                    time.sleep(1 + random.random())  # 1-2s with jitter

            if sections:
                # Cache in module-level cache
                _set_cached_gdoc(cache_key, sections)
                LOGGER.info(
                    f"Fetched Google Doc {doc_id} with {len(sections)} sections: "
                    f"{list(sections.keys())}"
                )
                return sections
            else:
                LOGGER.error(f"Failed to fetch Google Doc sections {doc_id} after 3 attempts")
                return None

        except Exception as e:
            LOGGER.exception(f"Error fetching Google Doc sections {doc_id}: {e}")
            return None

    def clear_cache(self):
        """Clear the artifacts cache."""
        self._cache.clear()
        LOGGER.info("Artifacts cache cleared")

    def _get_empty_customer_artifacts(self) -> Dict[str, Any]:
        """Return empty structure for customer artifacts."""
        return {
            "system_instructions": "",
            "qa_knowledge_base": {
                "metadata": {"total_pairs": 0, "source": "supabase"},
                "qa_pairs": [],
            },
            "response_templates": {
                "metadata": {"total_categories": 0, "description": ""},
                "templates": {},
            },
            "decision_rules": {
                "metadata": {"total_rules": 0, "description": ""},
                "rules": [],
            },
            "entity_training": {
                "metadata": {"total_examples": 0, "entity_types": [], "description": ""},
                "training_data": [],
            },
        }


__all__ = ["ArtifactsProvider", "Artifact", "clear_gdoc_cache"]
