"""Class A: identity arguments are injected, never accepted from the caller.

Mirrors tool_executor.py's spread-then-overwrite ordering, which is what makes
injection authoritative over anything the caller supplied.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from gateway.scope_guard import apply_scope_guard
from gateway.session import GatewaySession

SESSION = GatewaySession(
    email="user@example.com",
    user_id="u1",
    organization_id="4",
    organization_short_name="testorg",
    grid_names=frozenset({"Alpha Site", "Beta Site"}),
    is_staff=False,
)


def test_injects_identity_arguments():
    guarded = apply_scope_guard({"limit": 10}, SESSION)

    assert guarded["organization_id"] == 4
    assert guarded["user_email"] == "user@example.com"
    assert guarded["limit"] == 10


def test_caller_supplied_organization_id_is_overwritten():
    guarded = apply_scope_guard({"organization_id": 99}, SESSION)

    assert guarded["organization_id"] == 4


def test_caller_supplied_email_is_overwritten():
    guarded = apply_scope_guard({"user_email": "attacker@example.com"}, SESSION)

    assert guarded["user_email"] == "user@example.com"


def test_org_name_overwritten_only_when_tool_asked_for_it():
    assert "organization" not in apply_scope_guard({"limit": 1}, SESSION)

    guarded = apply_scope_guard({"organization": "someone else"}, SESSION)
    assert guarded["organization"] == "testorg"


def test_original_arguments_are_not_mutated():
    original = {"organization_id": 99}
    apply_scope_guard(original, SESSION)
    assert original == {"organization_id": 99}
