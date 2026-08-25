"""The grids / organizations / users directory, as a context module.

Replaces ContextEnrichmentProvider's hardcoded injection at
prepare_context.py's _fetch_enrichment. Same data, same staff gate, but
reachable as a module an operator can attach to specific prompts or detach
entirely -- which the hardcoded path never allowed.

Organizations and people come from the Auth DB, the same place grids do.
They used to come from a `jira_fetcher` that nothing ever supplied:
build_default_registry constructed DirectoryProvider() with no arguments, so
`self._jira` was always None and both lines were permanently empty. The
legacy path it inherited from was no better -- ContextEnrichmentProvider
called `jira_get_schedule_participants` and `jira_get_organization_options`
with a tool_executor prepare_context.py has only ever passed as None, and
neither tool name exists in mcp_servers at all. So the interface
(`participants()` / `organizations()`) had no implementation anywhere in the
repo, and could not have had one.

Sourcing all three lines from the Auth DB makes the module coherent -- it is
"Known Grids, Organizations and People", and those are the system's own
directory, permission-filtered the same way -- and removes a dependency on a
Jira capability that was never built.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

GRID_CACHE_TTL = 300
ORG_CACHE_TTL = 600
STAFF_CACHE_TTL = 600

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

    def __init__(self, auth_service: Any = None) -> None:
        if auth_service is None:
            from shared.auth import get_auth_service

            auth_service = get_auth_service()
        self._auth = auth_service

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]:
        grids = await self._grids(ctx)
        organizations = await self._organizations(ctx)
        # Staff-only, unchanged from the original gate: the internal team
        # roster is not something a customer's prompt should carry.
        users = await self._staff_members() if ctx.is_staff else []
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

    async def _organizations(self, ctx: ResolutionContext) -> List[str]:
        """Staff see every organization; everyone else sees their own.

        Keyed on the permission set like _grids, for the same reason: a
        staff-wide list must never be served from a cache entry a customer
        request can hit.
        """
        key = ("orgs", "all" if ctx.is_staff else ctx.organization_ids)
        hit = _cached(key, ORG_CACHE_TTL)
        if hit is not None:
            return list(hit)
        try:
            if ctx.is_staff:
                names = await self._auth.get_organization_names(include_all=True)
            elif ctx.organization_ids:
                # Every org the caller belongs to, not organization_ids[0] --
                # the grid lookup's one-org shortcut is a limitation, not a
                # pattern worth copying.
                names = await self._auth.get_organization_names(
                    organization_ids=list(ctx.organization_ids)
                )
            else:
                names = []
        except Exception as e:
            LOGGER.warning(f"Directory organization lookup failed: {e}")
            return []
        return list(_store(key, ORG_CACHE_TTL, list(names)))

    async def _staff_members(self) -> List[str]:
        """The internal team roster. Caller must already have checked is_staff.

        Unkeyed by permission because there is exactly one staff org and only
        staff ever reach this -- see resolve().
        """
        hit = _cached(("staff_members",), STAFF_CACHE_TTL)
        if hit is not None:
            return list(hit)
        try:
            names = await self._auth.get_staff_member_names()
        except Exception as e:
            LOGGER.warning(f"Directory staff lookup failed: {e}")
            return []
        return list(_store(("staff_members",), STAFF_CACHE_TTL, list(names)))


__all__ = ["DirectoryProvider", "render_directory"]
