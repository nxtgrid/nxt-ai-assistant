"""Distilled prior history for the grid or organization in scope.

Read-only. Generation is scripts/distill_episodic_memory.py, run nightly --
distilling at render time would put an LLM call on the critical path of every
request that pins this module.

Grid is preferred over organization when the scope names both: it is the more
specific anchor, matching how site-scoped knowledge modules already beat
sector-scoped ones in budget_pinned.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class EpisodicProvider:
    """Resolves the `episodic` source."""

    source = "episodic"

    def __init__(
        self,
        client: Any = None,
        grid_access: Optional[Callable[[str, ResolutionContext], Awaitable[bool]]] = None,
    ) -> None:
        self._client = client if client is not None else _default_client()
        self._grid_access = grid_access or _default_grid_access

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]:
        if self._client is None:
            return None

        if ctx.scope.grid:
            anchor_type, anchor_id = "grid", ctx.scope.grid
            if not await self._grid_access(ctx.scope.grid, ctx):
                LOGGER.info(f"Episodic history for grid '{ctx.scope.grid}' withheld: no access")
                return None
        elif ctx.scope.organization_id:
            anchor_type, anchor_id = "organization", ctx.scope.organization_id
            if not (ctx.is_staff or anchor_id in ctx.organization_ids):
                return None
        else:
            return None

        try:
            result = (
                self._client.table("episodic_distillations")
                .select("anchor_name, summary")
                .eq("anchor_type", anchor_type)
                .eq("anchor_id", anchor_id)
                .limit(1)
                .execute()
            )
        except Exception as e:
            LOGGER.warning(f"Episodic distillation lookup failed: {e}")
            return None

        rows = result.data or []
        if not rows:
            return None
        summary = (rows[0].get("summary") or "").strip()
        return summary or None


async def _default_grid_access(grid: str, ctx: ResolutionContext) -> bool:
    """Staff see every grid; everyone else needs it in their permitted set.

    Deliberately conservative: an unresolvable grid is treated as denied.
    Async, and directly awaited -- not wrapped in asyncio.run_until_complete,
    which raises when called from inside a loop that's already running
    (resolve() itself is a coroutine on the request's event loop).
    """
    if ctx.is_staff:
        return True
    try:
        from shared.auth import get_auth_service

        names = await get_auth_service().get_grid_names_for_organization(
            organization_id=ctx.organization_ids[0] if ctx.organization_ids else None
        )
        return grid in (names or [])
    except Exception:
        LOGGER.warning(f"Grid access check failed for '{grid}'; denying", exc_info=True)
        return False


def _default_client() -> Any:
    from shared.config.db_credentials import chat_db_service_key, chat_db_url

    url, key = chat_db_url(), chat_db_service_key()
    if not (url and key):
        return None
    try:
        from supabase import create_client

        return create_client(url, key)
    except Exception:
        LOGGER.warning("Could not build the episodic provider client", exc_info=True)
        return None


__all__ = ["EpisodicProvider"]
