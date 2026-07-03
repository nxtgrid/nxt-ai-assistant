"""Convert messy AppSheet/Google-Sheet column headers into the snake_case
convention used by the anansi chat DB.

The generic normaliser handles the common cases (spaces, punctuation, embedded
units like ``(m)``, simple camelCase). Genuinely ambiguous headers — acronym runs
like ``USDtoNGN`` — are resolved by explicit per-table overrides in
``db/column_map.py`` and should be passed via ``overrides``.
"""

from __future__ import annotations

import re

# Split lowercase/digit -> uppercase  (e.g. "Spec1Name" -> "Spec1 Name")
_CAMEL_TAIL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# Split acronym -> Titlecase  (e.g. "PVType" -> "PV Type")
_CAMEL_ACRONYM = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Engineering unit / acronym tokens that should stay glued (and not get
# camel-split into nonsense like "k_wp"). Applied as case-sensitive substring
# replacements BEFORE camel splitting; longest first so "kW" doesn't pre-empt
# "kWh"/"kWp". Extend as new oddities surface.
_TOKEN_MAP: list[tuple[str, str]] = [
    ("USDtoNGN", "usd_to_ngn"),
    ("kWh", "kwh"),
    ("kWp", "kwp"),
    ("kVA", "kva"),
    ("kW", "kw"),
    ("sq.m", "sqm"),
    ("Sq.m", "sqm"),
]


def to_snake_case(header: str) -> str:
    """Normalise a single header to a safe snake_case identifier."""
    s = (header or "").strip()
    if not s:
        return ""
    for tok, repl in _TOKEN_MAP:
        s = s.replace(tok, repl)
    s = _CAMEL_ACRONYM.sub(" ", s)
    s = _CAMEL_TAIL.sub(" ", s)
    s = s.lower()
    s = _NON_ALNUM.sub("_", s)
    s = s.strip("_")
    # Postgres identifiers may not start with a digit (when unquoted).
    if s and s[0].isdigit():
        s = "n_" + s
    return s


def normalize_headers(
    headers: list[str], overrides: dict[str, str] | None = None
) -> dict[str, str]:
    """Map each original header -> snake_case column name.

    ``overrides`` (keyed by the *exact* original header) wins over the generic
    rule. Blank/unnamed trailing columns are skipped. Collisions are made unique
    with a numeric suffix so a row dict never silently drops a value.
    """
    overrides = overrides or {}
    seen: dict[str, int] = {}
    out: dict[str, str] = {}
    for h in headers:
        if h is None or str(h).strip() == "":
            continue
        col = overrides.get(h) or to_snake_case(str(h))
        if not col:
            continue
        if col in seen:
            seen[col] += 1
            col = f"{col}_{seen[col]}"
        else:
            seen[col] = 0
        out[h] = col
    return out
