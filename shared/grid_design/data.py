"""Shared helpers for the compute engines.

AppSheet's Apps Script read whole sheets into arrays of dicts keyed by header.
We mirror that by loading whole gd_* tables via the Repository, then build the
same lookup maps. Numeric coercion is explicit because, although our schema types
many columns as numeric, refs/specs remain text and may be blank.
"""

from __future__ import annotations

from typing import Any

from shared.grid_design.db import Repository


def num(x: Any, default: float = 0.0) -> float:
    """Coerce a possibly-text/None value to float (AppSheet stored everything as text)."""
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def truthy(x: Any) -> bool:
    return x in (True, "TRUE", "true", "1", 1, "Yes", "yes")


def load(bare: str, *, active_only: bool = False) -> list[dict]:
    """Load a whole table (Repository.list paginates past the PostgREST row cap)."""
    rows: list[dict] = Repository(bare).list(active_only=active_only)
    return rows


def index_by(rows: list[dict], key: str) -> dict[str, dict]:
    return {r.get(key): r for r in rows if r.get(key) is not None}


def group_by(rows: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        k = r.get(key)
        if k:
            out.setdefault(k, []).append(r)
    return out
