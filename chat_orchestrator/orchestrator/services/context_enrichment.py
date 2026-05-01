"""
Context Enrichment Provider

Fetches dynamic context (grid names, Jira assignees, Jira organizations) to enrich LLM context.
This helps the LLM disambiguate when users mention names that could refer to
grids, people, organizations, or other entities.

Performance: Cache is module-level so it persists across requests within the
same process, avoiding redundant DB/API calls on every request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.auth import get_auth_service
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass
class CacheEntry:
    """Cache entry with expiration time."""

    data: Any
    expires_at: float


# Module-level cache shared across all instances within the process.
# This ensures the TTL-based cache actually survives across requests
# instead of being discarded with each new ContextEnrichmentProvider instance.
_MODULE_CACHE: Dict[str, CacheEntry] = {}


class ContextEnrichmentProvider:
    """
    Provides dynamic context enrichment for LLM system instructions.

    Fetches and caches:
    - Grid names accessible to the user's organization
    - Jira Ops assignees (team members who can be assigned tickets)
    - Jira organizations (available organization options for tickets)
    """

    GRID_CACHE_TTL = 300  # 5 minutes for grid names
    JIRA_CACHE_TTL = 600  # 10 minutes for Jira assignees and organizations

    def __init__(self) -> None:
        self._auth_service = get_auth_service()

    async def get_enrichment_context(
        self,
        organization_ids: List[str],
        is_staff: bool,
        tool_executor: Optional[Any] = None,
    ) -> str:
        """
        Get dynamic context enrichment text.

        Args:
            organization_ids: User's organization IDs
            is_staff: Whether user is staff (set via STAFF_ORG_ID env var)
            tool_executor: Optional ToolExecutor for MCP tool calls (for Jira)

        Returns:
            Formatted enrichment text to append to context_message
        """
        # Fetch grid names
        grid_names = await self._get_grid_names(organization_ids, is_staff)

        # Fetch Jira data ONLY for staff (not customer mode)
        jira_assignees: List[str] = []
        jira_organizations: List[str] = []
        if is_staff and tool_executor:
            jira_assignees = await self._get_jira_assignees(tool_executor)
            jira_organizations = await self._get_jira_organizations(tool_executor)

        return self._format_enrichment_text(grid_names, jira_assignees, jira_organizations)

    async def _get_grid_names(
        self,
        organization_ids: List[str],
        is_staff: bool,
    ) -> List[str]:
        """Fetch grid names with caching."""
        cache_key = "grids_all" if is_staff else f"grids_{','.join(sorted(organization_ids))}"

        # Check module-level cache
        if cache_key in _MODULE_CACHE:
            entry = _MODULE_CACHE[cache_key]
            if entry.expires_at > time.time():
                LOGGER.debug(f"Cache hit for grid names: {cache_key}")
                return list(entry.data)

        try:
            if is_staff:
                names = await self._auth_service.get_grid_names_for_organization(include_all=True)
            else:
                # Get grids for first org (typically users have one org)
                org_id = organization_ids[0] if organization_ids else None
                if org_id:
                    names = await self._auth_service.get_grid_names_for_organization(
                        organization_id=org_id
                    )
                else:
                    names = []

            # Cache result at module level
            _MODULE_CACHE[cache_key] = CacheEntry(
                data=names,
                expires_at=time.time() + self.GRID_CACHE_TTL,
            )
            LOGGER.info(f"Fetched {len(names)} grid names for {cache_key}")
            return names

        except Exception as e:
            LOGGER.warning(f"Failed to fetch grid names: {e}")
            return []

    async def _get_jira_assignees(self, executor: Any) -> List[str]:
        """Fetch Jira assignees via MCP tool call."""
        cache_key = "jira_assignees"

        # Check module-level cache
        if cache_key in _MODULE_CACHE:
            entry = _MODULE_CACHE[cache_key]
            if entry.expires_at > time.time():
                LOGGER.debug("Cache hit for Jira assignees")
                return list(entry.data)

        try:
            # Get all schedule participants (team members in rotations)
            result = await executor.execute_tool(
                tool_name="jira_get_schedule_participants",
                arguments={},
                metadata={},
            )

            # Extract display names from participants
            names = []
            if result and isinstance(result, dict) and "participants" in result:
                for participant in result["participants"]:
                    display_name = participant.get("display_name")
                    if display_name:
                        names.append(display_name)

            # Cache result at module level
            _MODULE_CACHE[cache_key] = CacheEntry(
                data=names,
                expires_at=time.time() + self.JIRA_CACHE_TTL,
            )
            LOGGER.info(f"Fetched {len(names)} Jira schedule participants")
            return names

        except Exception as e:
            LOGGER.warning(f"Failed to fetch Jira assignees: {e}")
            return []

    async def _get_jira_organizations(self, executor: Any) -> List[str]:
        """Fetch JIRA organization options via MCP tool call."""
        cache_key = "jira_organizations"

        # Check module-level cache
        if cache_key in _MODULE_CACHE:
            entry = _MODULE_CACHE[cache_key]
            if entry.expires_at > time.time():
                LOGGER.debug("Cache hit for JIRA organizations")
                return list(entry.data)

        try:
            result = await executor.execute_tool(
                tool_name="jira_get_organization_options",
                arguments={},
                metadata={},
            )

            # Extract options from result
            options: List[str] = []
            if result and isinstance(result, dict) and "options" in result:
                options = result["options"]

            # Cache result at module level
            _MODULE_CACHE[cache_key] = CacheEntry(
                data=options,
                expires_at=time.time() + self.JIRA_CACHE_TTL,
            )
            LOGGER.info(f"Fetched {len(options)} JIRA organization options")
            return options

        except Exception as e:
            LOGGER.warning(f"Failed to fetch JIRA organizations: {e}")
            return []

    def _format_enrichment_text(
        self,
        grid_names: List[str],
        jira_assignees: List[str],
        jira_organizations: Optional[List[str]] = None,
    ) -> str:
        """Format enrichment data as text for LLM context."""
        parts = []

        if grid_names:
            grid_list = ", ".join(grid_names)
            parts.append(f"Available grids: {grid_list}")

        if jira_assignees:
            assignee_list = ", ".join(jira_assignees)
            parts.append(f"Jira Ops team members: {assignee_list}")

        if jira_organizations:
            org_list = ", ".join(jira_organizations)
            parts.append(f"Available JIRA organizations: {org_list}")

        if parts:
            parts.append(
                "When a user mentions a name, check if it matches a grid, team member, "
                "or organization above."
            )

        return "\n".join(parts) if parts else ""


__all__ = ["ContextEnrichmentProvider"]
