"""Thin generic data-access layer over supabase-py.

Mirrors anansi's connection approach (``CHAT_DB_SERVICE_KEY`` / ``CHAT_DB_URL``,
see ``mcp_servers/shared_code/database/connections.py``) but synchronous, since
Streamlit runs synchronously. A single client is cached for the Streamlit session
via ``@st.cache_resource``.

Tables are addressed by their *bare* name (e.g. ``"grids"``); the ``gd_`` prefix
is applied here so callers and EntitySpecs stay prefix-agnostic.
"""

from __future__ import annotations

from typing import Any, List

from supabase import Client, create_client

from shared.grid_design import settings

try:  # Streamlit is always present at runtime; guard so the module imports in plain scripts.
    import streamlit as st

    _cache_resource = st.cache_resource
except Exception:  # pragma: no cover - used by CLI import scripts

    def _cache_resource(func):
        _holder: dict[str, Any] = {}

        def wrapper(*a, **k):
            if "v" not in _holder:
                _holder["v"] = func(*a, **k)
            return _holder["v"]

        return wrapper


@_cache_resource
def get_client() -> Client:
    """Return a cached Supabase client built from the shared chat-DB creds."""
    if not settings.is_db_configured():
        raise RuntimeError(
            "Chat DB not configured. Set CHAT_DB_URL and CHAT_DB_SERVICE_KEY (see .env.example)."
        )
    return create_client(settings.CHAT_DB_URL, settings.CHAT_DB_SERVICE_KEY)


def _t(table: str) -> str:
    return table if table.startswith(settings.TABLE_PREFIX) else settings.TABLE_PREFIX + table


class Repository:
    """Generic CRUD over a single gd_* table.

    AppSheet kept an ``active`` flag rather than hard-deleting; we honour that with
    soft deletes and an ``active``-only default filter on lists.
    """

    def __init__(self, table: str, pk: str = "id"):
        self.table = _t(table)
        self.pk = pk

    # ── reads ────────────────────────────────────────────────────────────────
    # PostgREST caps a single response (Supabase default ~1000 rows), so when no
    # explicit limit is given we page through in chunks to return the WHOLE table.
    _PAGE = 1000

    def _query(self, active_only, order_by, desc, filters):
        q = get_client().table(self.table).select("*")
        if active_only:
            q = q.eq("active", True)
        for col, val in (filters or {}).items():
            q = q.eq(col, val)
        if order_by:
            q = q.order(order_by, desc=desc)
        return q

    def list(
        self,
        *,
        active_only: bool = True,
        order_by: str | None = None,
        desc: bool = False,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> List[dict[str, Any]]:
        if limit is not None:
            start = offset or 0
            q = self._query(active_only, order_by, desc, filters)
            page: List[dict[str, Any]] = q.range(start, start + limit - 1).execute().data or []
            return page
        # No limit → fetch all rows, paginating past the PostgREST cap.
        out: List[dict[str, Any]] = []
        start = 0
        while True:
            q = self._query(active_only, order_by, desc, filters)
            batch = q.range(start, start + self._PAGE - 1).execute().data or []
            out.extend(batch)
            if len(batch) < self._PAGE:
                return out
            start += self._PAGE

    def get(self, pk_value: str) -> dict[str, Any] | None:
        rows: List[dict[str, Any]] = (
            get_client().table(self.table).select("*").eq(self.pk, pk_value).limit(1).execute().data
        ) or []
        return rows[0] if rows else None

    def get_many(self, pk_values: List[str]) -> dict[str, Any]:
        """Fetch multiple rows by PK in one query. Returns {pk: row}."""
        unique = list({str(v) for v in pk_values if v})
        if not unique:
            return {}
        rows: List[dict[str, Any]] = (
            get_client().table(self.table).select("*").in_(self.pk, unique).execute().data or []
        )
        return {r[self.pk]: r for r in rows}

    def count(self, *, active_only: bool = True) -> int:
        q = get_client().table(self.table).select(self.pk, count="exact")
        if active_only:
            q = q.eq("active", True)
        return q.execute().count or 0

    # ── writes ─────────────────────────────────────────────────────────────────
    def insert(self, row: dict[str, Any]) -> dict[str, Any]:
        data: List[dict[str, Any]] = get_client().table(self.table).insert(row).execute().data or []
        return data[0] if data else row

    def upsert(self, rows: List[dict[str, Any]]) -> int:
        if not rows:
            return 0
        data = get_client().table(self.table).upsert(rows).execute().data
        return len(data or [])

    def update(self, pk_value: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        data: List[dict[str, Any]] = (
            get_client().table(self.table).update(changes).eq(self.pk, pk_value).execute().data
        ) or []
        return data[0] if data else None

    def soft_delete(self, pk_value: str) -> None:
        get_client().table(self.table).update({"active": False}).eq(self.pk, pk_value).execute()
