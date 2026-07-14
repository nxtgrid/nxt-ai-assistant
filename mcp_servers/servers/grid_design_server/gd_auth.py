"""Grid-level authorization gate for the grid_design MCP server.

The internal grid-design engine (`shared/grid_design/db.py`'s `Repository`) talks
directly to the Chat DB with zero access control — any caller can currently
read/write any grid's design rows. As the grid_design server grows fine-grained,
LLM-callable tools that mutate `gd_*` rows, each grid-anchored write must first
confirm the caller's organization actually owns that grid.

`assert_grid_access` is that gate. It is a *security check*, not a name
resolution helper: it only accepts an exact (case-insensitive) grid name match
against the Auth DB, scoped to the caller's organization_id. Callers are
responsible for resolving/canonicalizing a user-typo'd grid name (e.g. via
`shared.utils.grid_matcher.find_best_grid_match` against the Chat DB) before
calling this function.

`resolve_grid_name_for_design` is a separate, small Chat DB helper that maps a
`gd_designs` row to its grid's name. Its two current callers, in
`grid_design_mcp_server.py`, wrap its resolved grid name (and any
`GridAccessDenied`/not-found outcome) in their OWN generic, identically-worded
denial message rather than propagating this module's `grid_name`-bearing one —
because for a design_id/row_id-anchored tool, the caller supplied an opaque ID,
not a grid name, so echoing the real grid name back (or letting "not found"
and "found but denied" read differently) would leak another organization's
grid name and create an existence oracle. That per-call-site wrapping is
intentional and lives in `grid_design_mcp_server.py`, not here.

The denial message raised directly by `assert_grid_access` below is
hand-scrubbed (no organization_id, no other org's data) and is appropriate
as-is for its 3 direct callers (`design_and_bom`, `find_grid`, `create_design`)
where the caller already supplied the grid name themselves. If a caller ever
needs to further sanitize error text before showing it to a user, see
`shared.utils.error_messages.sanitize_error_for_user`.
"""

from __future__ import annotations

import os

import asyncpg

from shared.grid_design.db import Repository


class GridAccessDenied(PermissionError):
    """Raised when the caller's organization does not have access to a grid.

    Subclasses PermissionError so it is picked up by
    shared.utils.error_messages.categorize_error's isinstance(error, PermissionError)
    branch on any caller that routes through that helper. On the grid_design
    MCP server's actual call path, `handle_call_tool`'s outer `except Exception`
    stringifies this (like any exception) into `{"success": False, "error": str(e)}`
    without going through `categorize_error` — so the PermissionError subclass
    matters for callers that DO use that helper, not for this server's own
    dispatch. The message is deliberately scrubbed of organization_id and any
    other org's data — see assert_grid_access.
    """


async def assert_grid_access(grid_name: str, organization_id: int | None) -> None:
    """Raise GridAccessDenied unless `organization_id` owns `grid_name`.

    Staff bypass: if organization_id matches STAFF_ORG_ID, access is granted
    immediately with no DB lookup (staff can access every grid).

    Otherwise, this queries the Auth DB (read-only, via asyncpg — never the
    Supabase/PostgREST client, which is for the Chat DB only) for an exact,
    case-insensitive match of grid_name scoped to organization_id. No fuzzy
    matching is performed here; that is the caller's responsibility.

    organization_id=None is treated as fail-closed (denied), without touching
    the database.
    """
    staff_org_id = int(os.getenv("STAFF_ORG_ID", "2"))
    if organization_id == staff_org_id:
        return

    if organization_id is None:
        raise GridAccessDenied(f"You don't have access to grid '{grid_name}'")

    conn = await asyncpg.connect(
        host=os.getenv("AUTH_DB_HOST"),
        port=int(os.getenv("AUTH_DB_PORT", "5432")),
        user=os.getenv("AUTH_DB_USER"),
        password=os.getenv("AUTH_DB_PASSWORD"),
        database=os.getenv("AUTH_DB_NAME", "postgres"),
        ssl="require",
        statement_cache_size=0,
    )
    try:
        row = await conn.fetchrow(
            "SELECT 1 FROM grids WHERE lower(name) = lower($1) "
            "AND organization_id = $2 AND deleted_at IS NULL LIMIT 1",
            grid_name,
            organization_id,
        )
    finally:
        await conn.close()

    if row is None:
        raise GridAccessDenied(f"You don't have access to grid '{grid_name}'")


def resolve_grid_name_for_design(design_id: str) -> str | None:
    """Resolve a gd_designs row to its grid's name via the Chat DB.

    This is a plain synchronous function (Repository itself is synchronous,
    wrapping supabase-py) — callers running inside an async context must wrap
    it with `asyncio.to_thread`. Returns None if the design or its grid can't
    be found; callers decide how to handle a missing design.
    """
    design = Repository("designs").get(design_id)
    if not design:
        return None

    grid_id = design.get("grid")
    if not grid_id:
        return None

    grid = Repository("grids").get(grid_id)
    if not grid:
        return None

    name = grid.get("name")
    return name if isinstance(name, str) else None
