"""USD -> NGN exchange rate (port of API_Helpers.USDtoNGN).

Source: open.er-api.com (no auth). Cached with a TTL: the engine now runs
inside long-lived server processes (anansi-bot), so an unbounded cache would
price BOMs off a stale rate for the whole process lifetime.
"""

from __future__ import annotations

import time

import requests

from shared.grid_design import settings

_cache: dict[str, float] = {}
_TTL_SECONDS = 3600.0


def get_usd_to_ngn(force: bool = False) -> float | None:
    now = time.monotonic()
    if not force and "ngn" in _cache and now - _cache.get("at", 0.0) < _TTL_SECONDS:
        return _cache["ngn"]
    try:
        resp = requests.get(settings.EXCHANGE_RATE_API_URL, timeout=15)
        resp.raise_for_status()
        rate = resp.json().get("rates", {}).get("NGN")
        if rate:
            _cache["ngn"] = float(rate)
            _cache["at"] = now
            return _cache["ngn"]
    except Exception:
        # Fetch failed after TTL expiry: a stale rate beats None (callers fall
        # back to the design's stored rate or 0).
        return _cache.get("ngn")
    return _cache.get("ngn")
