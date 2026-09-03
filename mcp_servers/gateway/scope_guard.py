"""Re-create the orchestrator's injection boundary for external MCP callers.

The tenancy surface across all 107 tools is ~12 argument names, so this is a
small table rather than a per-tool audit. Arguments fall into three classes:

  A INJECT    identity — overwritten from the session, caller value discarded
  B VALIDATE  grid references — resolved against the caller's own grid set
  C DELEGATE  meter/customer references — left to servers that filter by org

Class C is only safe for Tier 1 servers; see gateway/tiers.py.
"""

from __future__ import annotations

from typing import Any, Dict

from gateway.session import GatewaySession


class ScopeViolation(Exception):
    """The caller referenced an entity outside their permissions."""


# Class A — always injected, matching tool_executor.py's injected set.
#
# chat_id/topic_id/session_id are here because at least one Tier 1 server
# (schedule_mcp_server.py's cancel/pause/resume/list_user_schedules) scopes
# OWNERSHIP by chat_id alone, not by organization_id at all - found during
# the Class-C audit. Nothing upstream enforces additionalProperties: false,
# so a caller could otherwise supply their own chat_id/topic_id/session_id
# directly and it would reach that ownership check untouched, working
# across organizations since organization_id plays no part in that check.
# A gateway session has no Telegram identity, so these are always forced to
# None - not merely overwritten with some other value a caller could still
# try to brute-force towards, but a value no real Telegram-created row can
# ever equal.
ALWAYS_INJECTED = ("organization_id", "user_email", "user_name")
ALWAYS_INJECTED_NONE = ("chat_id", "topic_id", "session_id")

# Class A — overwritten only when the tool's schema actually uses them, so we
# do not add stray keys that a server might branch on.
INJECTED_IF_PRESENT = ("organization", "organization_name")

# Class B — caller supplies, gateway validates against their own grid set.
GRID_SCALAR_ARGS = ("grid_name", "grid")
GRID_LIST_ARGS = ("grid_names",)

# Minimum rapidfuzz score to accept a near-miss within the allowed set.
_FUZZY_CUTOFF = 85


def _resolve_grid(value: str, session: GatewaySession) -> str:
    """Resolve a caller's grid string to a canonical name they may access.

    Fuzzy matching happens HERE, against the allowed set only. AuthService's
    get_grid_portal_id fuzzy-matches downstream against ALL grids, so a raw
    caller string must never reach it — a near-miss could otherwise land on
    another organization's grid.
    """
    if not isinstance(value, str):
        raise ScopeViolation(f"Grid reference must be a string, got {type(value).__name__}")

    allowed = session.grid_names
    if not allowed:
        raise ScopeViolation("Session has no accessible grids")

    if value in allowed:
        return value

    lowered = {name.lower(): name for name in allowed}
    if value.lower() in lowered:
        return lowered[value.lower()]

    from rapidfuzz import fuzz, process

    match = process.extractOne(
        value, list(allowed), scorer=fuzz.WRatio, score_cutoff=_FUZZY_CUTOFF
    )
    if match:
        return match[0]

    raise ScopeViolation(f"Grid not accessible to this user: {value!r}")


def apply_scope_guard(arguments: Dict[str, Any], session: GatewaySession) -> Dict[str, Any]:
    """Return a copy of ``arguments`` with caller-controlled scope removed.

    Spread first, overwrite second — the injected values must win.
    """
    guarded: Dict[str, Any] = {
        **arguments,
        "organization_id": int(session.organization_id),
        "user_email": session.email,
        "user_name": session.email,
    }

    for key in ALWAYS_INJECTED_NONE:
        guarded[key] = None

    for key in INJECTED_IF_PRESENT:
        if key in arguments:
            guarded[key] = session.organization_short_name

    for key in GRID_SCALAR_ARGS:
        if key in arguments and arguments[key] is not None:
            guarded[key] = _resolve_grid(arguments[key], session)

    for key in GRID_LIST_ARGS:
        if key in arguments and arguments[key] is not None:
            values = arguments[key]
            if not isinstance(values, (list, tuple)):
                raise ScopeViolation(f"{key} must be a list")
            guarded[key] = [_resolve_grid(v, session) for v in values]

    return guarded
