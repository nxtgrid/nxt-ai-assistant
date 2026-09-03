"""Class A: identity arguments are injected, never accepted from the caller.

Mirrors tool_executor.py's spread-then-overwrite ordering, which is what makes
injection authoritative over anything the caller supplied.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.scope_guard import ScopeViolation, apply_scope_guard
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


def test_exact_grid_name_passes_through():
    guarded = apply_scope_guard({"grid_name": "Alpha Site"}, SESSION)
    assert guarded["grid_name"] == "Alpha Site"


def test_case_insensitive_grid_name_is_canonicalised():
    guarded = apply_scope_guard({"grid_name": "alpha site"}, SESSION)
    assert guarded["grid_name"] == "Alpha Site"


def test_near_miss_resolves_within_allowed_set_only():
    # A typo resolves to the caller's own grid, and the CANONICAL name is
    # forwarded — downstream fuzzy matching must never see the raw string.
    guarded = apply_scope_guard({"grid_name": "Alpha Sight"}, SESSION)
    assert guarded["grid_name"] == "Alpha Site"


def test_grid_outside_permissions_is_rejected():
    with pytest.raises(ScopeViolation):
        apply_scope_guard({"grid_name": "Gamma Site"}, SESSION)


def test_grid_list_argument_is_validated_elementwise():
    guarded = apply_scope_guard({"grid_names": ["Alpha Site", "beta site"]}, SESSION)
    assert guarded["grid_names"] == ["Alpha Site", "Beta Site"]

    with pytest.raises(ScopeViolation):
        apply_scope_guard({"grid_names": ["Alpha Site", "Gamma Site"]}, SESSION)


def test_short_alias_grid_argument_is_validated():
    with pytest.raises(ScopeViolation):
        apply_scope_guard({"grid": "Gamma Site"}, SESSION)


def test_session_with_no_grids_rejects_any_grid_reference():
    empty = GatewaySession(
        email="user@example.com",
        user_id="u1",
        organization_id="4",
        organization_short_name="testorg",
        grid_names=frozenset(),
        is_staff=False,
    )
    with pytest.raises(ScopeViolation):
        apply_scope_guard({"grid_name": "Alpha Site"}, empty)


# Class A gap found during the Class-C audit: tool_executor.py injects
# chat_id/topic_id/session_id alongside organization_id/user_email/user_name
# (schedule_mcp_server.py's cancel/pause/resume/list_user_schedules handlers
# scope OWNERSHIP by chat_id, not organization_id at all), but the original
# scope guard never touched these three keys. Nothing enforces
# additionalProperties: false before arguments reach apply_scope_guard, so a
# caller could supply their own chat_id directly - complete exploit chain:
# list_user_schedules with a guessed/leaked chat_id enumerates another
# party's (even another ORG's) schedules, cancel_user_schedule with that
# chat_id + the returned 8-char id prefix then cancels it. Zero
# organization_id involvement, so Tier 1's "organization_id scoping makes
# this safe" argument never even applied here.


def test_caller_supplied_chat_id_is_overwritten():
    guarded = apply_scope_guard({"chat_id": "-1009999999999"}, SESSION)
    assert guarded["chat_id"] != "-1009999999999"


def test_caller_supplied_topic_id_is_overwritten():
    guarded = apply_scope_guard({"topic_id": "12345"}, SESSION)
    assert guarded["topic_id"] != "12345"


def test_caller_supplied_session_id_is_overwritten():
    guarded = apply_scope_guard({"session_id": "someone-elses-session"}, SESSION)
    assert guarded["session_id"] != "someone-elses-session"


def test_mcp_session_has_no_telegram_identity_so_chat_scoped_tools_find_nothing():
    # The gateway session has no chat_id/topic_id concept at all, so the
    # injected value must be one no real Telegram-created row can ever equal
    # - not merely "different from the caller's guess", which a cleverer
    # attacker could still brute-force towards.
    guarded = apply_scope_guard({}, SESSION)
    assert guarded["chat_id"] is None
    assert guarded["topic_id"] is None
    assert guarded["session_id"] is None
