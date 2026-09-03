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
ALWAYS_INJECTED = ("organization_id", "user_email", "user_name")

# Class A — overwritten only when the tool's schema actually uses them, so we
# do not add stray keys that a server might branch on.
INJECTED_IF_PRESENT = ("organization", "organization_name")


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

    for key in INJECTED_IF_PRESENT:
        if key in arguments:
            guarded[key] = session.organization_short_name

    return guarded
