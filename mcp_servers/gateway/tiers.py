"""Which servers the gateway will expose, and why.

Tier 1  consume organization_id internally, so Class A injection genuinely
        scopes their queries.
Tier 2  carry no internal org filtering, but every scope-bearing argument they
        take is grid-shaped, so Class B validation covers them.
Tier 3  side-effecting AND unscoped. Denied in v1; exposing them needs real
        per-server tenant isolation, which does not exist yet.

Deny-by-default: a server absent from every tier is not exposed, so a newly
added server cannot leak in without an explicit decision here.
"""

from __future__ import annotations

from typing import FrozenSet

TIER_1: FrozenSet[str] = frozenset(
    {
        "customer",
        "equipment_diagnostics",
        "grid_design",
        "jira",
        "knowledge",
        "meta",
        "meters",
        "schedule",
    }
)

TIER_2: FrozenSet[str] = frozenset(
    {
        "grafana",
        "reference",
        "solar",
    }
)

TIER_3_DENIED: FrozenSet[str] = frozenset(
    {
        "equipment_control",
        "payment_processor",
        "messaging",
    }
)

ALLOWED_SERVERS: FrozenSet[str] = TIER_1 | TIER_2


def is_server_allowed(server_name: str) -> bool:
    """Whether the gateway exposes this server at all."""
    return server_name in ALLOWED_SERVERS
