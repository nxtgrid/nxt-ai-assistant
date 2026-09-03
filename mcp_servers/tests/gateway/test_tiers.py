"""Server tiering.

Tier 3 is denied because those servers are side-effecting AND carry no
organization_id handling of their own, so Class A injection enforces nothing
for them.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
# The repo root too, unlike the other gateway tests: these import the REAL
# server_registry, which pulls in shared.* transitively.
_REPO_ROOT = _MCP_ROOT.parent
for _path in (_MCP_ROOT, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from gateway.tiers import TIER_1, TIER_2, TIER_3_DENIED, is_server_allowed


def test_org_aware_servers_are_tier_1():
    assert "customer" in TIER_1
    assert "meters" in TIER_1
    assert is_server_allowed("customer") is True


def test_grid_shaped_servers_are_tier_2():
    # "vrm" used to be asserted here. It is not a registered server - the name
    # came from VRM_ENABLED, an env var that gates VRM access *inside* other
    # servers rather than naming one of its own. Asserting it kept the phantom
    # alive through four merged PRs; the registry-pinning tests at the bottom
    # of this file are what actually prevent that now.
    assert "grafana" in TIER_2
    assert "solar" in TIER_2
    assert is_server_allowed("grafana") is True


def test_side_effecting_unscoped_servers_are_denied():
    assert "equipment_control" in TIER_3_DENIED
    assert "payment_processor" in TIER_3_DENIED
    assert "messaging" in TIER_3_DENIED
    assert is_server_allowed("equipment_control") is False


def test_unknown_server_is_denied():
    assert is_server_allowed("some_new_server") is False


def test_tiers_do_not_overlap():
    assert not (TIER_1 & TIER_2)
    assert not (TIER_1 & TIER_3_DENIED)
    assert not (TIER_2 & TIER_3_DENIED)


# --- the tier names must match the REAL registry ----------------------------
#
# These exist because every other test in this suite uses a fake registry, so
# nothing ever compared the tier names against the servers that actually
# exist. "codebase", "logs" and "vrm" sat in TIER_2 for four merged PRs -
# names taken from the *_ENABLED env vars (LOGS_ENABLED and CODEBASE_ENABLED
# are real flags) rather than from server_registry. In production the gateway
# tried to list "logs", server_registry raised ValueError("Unknown server:
# logs"), and the client got ZERO tools while still reporting a healthy
# connection. Importing the real registry here is the whole point: a fake
# cannot catch a name that does not exist.

import server_registry  # noqa: E402


def test_every_allowed_server_actually_exists_in_the_registry():
    # The strict direction. An allow-listed name that isn't real gets called
    # at runtime and raises; a deny-listed one never does.
    missing = sorted((TIER_1 | TIER_2) - set(server_registry.SERVER_METADATA))
    assert not missing, (
        f"tiers.py allows servers that server_registry does not know: {missing}. "
        "Listing one of these raises and returns an empty tool list to every client."
    )


def test_every_real_server_is_classified_somewhere():
    # The completeness direction: a new server added to the registry must be
    # deliberately placed in a tier, not silently unreachable (or, worse,
    # silently exposed if the default ever changes).
    unclassified = sorted(set(server_registry.SERVER_METADATA) - (TIER_1 | TIER_2 | TIER_3_DENIED))
    assert not unclassified, (
        f"server_registry has servers no tier classifies: {unclassified}. "
        "Add each to TIER_1/TIER_2 (exposed) or TIER_3_DENIED (never exposed)."
    )


def test_denied_tier_may_name_a_server_that_is_not_registered_yet():
    # Deliberately NOT symmetric with the allow-list check above. Denying a
    # server that doesn't exist yet is safe and useful - mcp_servers/servers/
    # contains messaging_server, which is not in SERVER_METADATA today, and
    # deny-listing it now means it starts closed if it is ever registered.
    assert "messaging" in TIER_3_DENIED
