"""Distilled prior history for the grid or organization in scope.

Read-only. Generation is the nightly batch in shared/episodic_memory.py, driven
by anansi_app/scripts/episodic_scheduler.py -- distilling at render time would
put an LLM call on the critical path of every request that uses this module.

Two things gate this beyond the batch: scope must name a grid or an
organization (prepare_context.py does not currently pass a grid, so in practice
only the organization anchor matches), and the module must be attached to a
prompt -- ensure_singleton_modules creates it attached to none.

Grid is preferred over organization when the scope names both: it is the more
specific anchor, matching how site-scoped knowledge modules already beat
sector-scoped ones in budget_inlined.
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

    async def preview(self, ctx: ResolutionContext) -> Optional[str]:
        """What the admin Context page shows for this module.

        `resolve` is keyed on a single grid or organization taken from the
        conversation's scope. A preview has no conversation, so `resolve`
        always returns None here and the operator learns nothing about
        whether distillation is working. This instead enumerates the
        distillations the viewer is allowed to see, states that a live
        conversation gets exactly one of them, and shows the most recent in
        full as a concrete sample. Read-only, same as `resolve`.
        """
        if self._client is None:
            return "_Context storage is not configured, so no distillations can be listed._"

        try:
            result = (
                self._client.table("episodic_distillations")
                .select("anchor_type, anchor_id, anchor_name, summary, message_count, generated_at")
                .order("generated_at", desc=True)
                .execute()
            )
        except Exception as e:
            LOGGER.warning(f"Episodic distillation preview lookup failed: {e}")
            return f"_Could not read stored distillations: {e}_"

        rows = list(result.data or [])
        if not ctx.is_staff:
            allowed = set(ctx.organization_ids or ())
            rows = [
                r for r in rows
                if r.get("anchor_type") == "organization" and str(r.get("anchor_id")) in allowed
            ]
        return _render_preview(rows, is_staff=ctx.is_staff)


def _render_preview(rows: list, *, is_staff: bool) -> str:
    """Format `preview`'s row list as markdown. Pure, for testability."""
    if not rows:
        if is_staff:
            return (
                "_No distillations stored yet. The nightly batch "
                "(anansi_app/scripts/episodic_scheduler.py) writes one per site or "
                "organization that has recent chat history mentioning it by name._"
            )
        return "_No distillations are stored for your organization(s) yet._"

    # Defensive re-sort: don't rely on the query's ORDER BY surviving the
    # client/transport, and a missing generated_at must not raise.
    rows = sorted(rows, key=lambda r: r.get("generated_at") or "", reverse=True)
    grids = sum(1 for r in rows if r.get("anchor_type") == "grid")
    orgs = sum(1 for r in rows if r.get("anchor_type") == "organization")

    newest = rows[0]
    name = newest.get("anchor_name") or newest.get("anchor_id") or "(unnamed)"
    when = (newest.get("generated_at") or "")[:10]
    count = newest.get("message_count")
    header = (
        f"_{len(rows)} distillation(s) stored — {grids} site(s), {orgs} organization(s). "
        "A live conversation injects exactly one, chosen by the site or organization "
        "in scope; a preview has neither, so this shows the most recently generated "
        "one as a sample._"
    )
    meta = f"**{name}** — {newest.get('anchor_type')} · generated {when}"
    if count is not None:
        meta += f" · {count} messages"
    parts = [header, "\n---\n", meta, "", (newest.get("summary") or "").strip()]

    others = rows[1:]
    if others:
        listed = ", ".join(
            f"{r.get('anchor_name') or r.get('anchor_id')} ({(r.get('generated_at') or '')[:10]})"
            for r in others[:20]
        )
        more = "" if len(others) <= 20 else f", +{len(others) - 20} more"
        parts += ["\n---", f"_Also stored: {listed}{more}_"]
    return "\n".join(parts)


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
        LOGGER.opt(exception=True).warning(f"Grid access check failed for '{grid}'; denying")
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
        LOGGER.opt(exception=True).warning("Could not build the episodic provider client")
        return None


__all__ = ["EpisodicProvider"]
