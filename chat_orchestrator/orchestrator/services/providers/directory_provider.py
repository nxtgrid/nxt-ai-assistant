"""The grids / organizations / users directory, as a context module.

Replaces ContextEnrichmentProvider's hardcoded injection at
prepare_context.py's _fetch_enrichment. Same data, same staff gate, but
reachable as a module an operator can attach to specific prompts or detach
entirely -- which the hardcoded path never allowed.

Note on Jira data: ContextEnrichmentProvider.get_enrichment_context only
fetches Jira assignees/organizations when given a real tool_executor, and
prepare_context.py's _fetch_enrichment has only ever called it with
tool_executor=None. So in current production, the Jira half of enrichment is
already dead -- this provider's jira_fetcher defaults to None too, which is
parity with reality, not a capability regression.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

GRID_CACHE_TTL = 300
JIRA_CACHE_TTL = 600

# Keyed on the permission set, never on the module -- caching a staff-wide
# grid list under a key a customer request can hit is exactly the bug this
# provider exists to make impossible.
_CACHE: Dict[Tuple, Tuple[float, Any]] = {}


def _cached(key: Tuple, ttl: float):
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _store(key: Tuple, ttl: float, value: Any) -> Any:
    _CACHE[key] = (time.time() + ttl, value)
    return value


def render_directory(
    grids: List[str], organizations: List[str], users: List[str]
) -> Optional[str]:
    """Format the directory. None when there is nothing to say."""
    parts: List[str] = []
    if grids:
        parts.append(f"Available grids: {', '.join(grids)}")
    if organizations:
        parts.append(f"Available organizations: {', '.join(organizations)}")
    if users:
        parts.append(f"Team members: {', '.join(users)}")
    if not parts:
        return None
    parts.append(
        "When a user mentions a name, check if it matches a grid, team member, "
        "or organization above."
    )
    return "\n".join(parts)


class DirectoryProvider:
    """Resolves the `directory` source."""

    source = "directory"

    def __init__(self, auth_service: Any = None, jira_fetcher: Any = None) -> None:
        if auth_service is None:
            from shared.auth import get_auth_service

            auth_service = get_auth_service()
        self._auth = auth_service
        self._jira = jira_fetcher

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]:
        grids = await self._grids(ctx)
        users: List[str] = []
        organizations: List[str] = []
        if ctx.is_staff and self._jira is not None:
            users = await self._jira_users()
            organizations = await self._jira_organizations()
        return render_directory(grids, organizations, users)

    async def _grids(self, ctx: ResolutionContext) -> List[str]:
        key = ("grids", "all" if ctx.is_staff else ctx.organization_ids)
        hit = _cached(key, GRID_CACHE_TTL)
        if hit is not None:
            return list(hit)
        try:
            if ctx.is_staff:
                names = await self._auth.get_grid_names_for_organization(include_all=True)
            elif ctx.organization_ids:
                names = await self._auth.get_grid_names_for_organization(
                    organization_id=ctx.organization_ids[0]
                )
            else:
                names = []
        except Exception as e:
            LOGGER.warning(f"Directory grid lookup failed: {e}")
            return []
        return list(_store(key, GRID_CACHE_TTL, list(names)))

    async def _jira_users(self) -> List[str]:
        hit = _cached(("jira_users",), JIRA_CACHE_TTL)
        if hit is not None:
            return list(hit)
        try:
            names = await self._jira.participants()
        except Exception as e:
            LOGGER.warning(f"Directory user lookup failed: {e}")
            return []
        return list(_store(("jira_users",), JIRA_CACHE_TTL, list(names)))

    async def _jira_organizations(self) -> List[str]:
        hit = _cached(("jira_orgs",), JIRA_CACHE_TTL)
        if hit is not None:
            return list(hit)
        try:
            names = await self._jira.organizations()
        except Exception as e:
            LOGGER.warning(f"Directory organization lookup failed: {e}")
            return []
        return list(_store(("jira_orgs",), JIRA_CACHE_TTL, list(names)))


__all__ = ["DirectoryProvider", "render_directory"]
