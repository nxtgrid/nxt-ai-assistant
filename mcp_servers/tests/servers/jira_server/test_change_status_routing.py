"""_tool_change_status routing: closing transitions must go through
TicketService (close_ticket_via_orchestrator) for BOTH internal and
Jira-backed tickets, so the update notifier fires immediately rather than
waiting on Jira's webhook to call back. Non-closing transitions (e.g. "In
Progress") have no notifier concept in this design and must still go
straight to Jira, unchanged.
"""

import json
import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.jira_server import jira_mcp_server as jira_module  # noqa: E402

pytestmark = pytest.mark.asyncio


def _parse(result):
    assert len(result) == 1
    return json.loads(result[0].text)


@pytest.fixture(autouse=True)
def _actions_enabled(monkeypatch):
    monkeypatch.setattr(jira_module.ActionFlags, "is_actions_enabled", lambda _s: True)


@pytest.fixture(autouse=True)
def _no_internal_ticket_by_default(monkeypatch):
    """Default every ref to "not an internal ticket" (no Supabase configured
    in this test process, so get_internal_ticket's own DB lookup naturally
    returns None). Individual tests override with a real fake_tables setup
    when they need the internal-ticket branch specifically."""
    monkeypatch.setattr(jira_module.client, "_get_chat_supabase", lambda: None)


async def test_jira_closing_transition_routes_through_the_orchestrator(monkeypatch):
    calls: list[str] = []

    async def fake_close(ref):
        calls.append(ref)
        return True

    async def fail_if_called(*_a, **_k):
        raise AssertionError("must not call the raw Jira transitions API for a closing transition")

    monkeypatch.setattr(jira_module.client, "close_ticket_via_orchestrator", fake_close)
    monkeypatch.setattr(jira_module.client, "transition_issue", fail_if_called)

    result = await jira_module._tool_change_status(
        {"issue_key": "OPS-3424", "transition": "Done"}
    )

    data = _parse(result)
    assert data == {
        "success": True,
        "issue_key": "OPS-3424",
        "backend": "jira",
        "status": "done",
        "message": "Closed OPS-3424",
    }
    assert calls == ["OPS-3424"]


@pytest.mark.parametrize("transition_name", ["close", "closed", "resolve", "Resolved"])
async def test_every_closing_transition_alias_routes_through_the_orchestrator(
    monkeypatch, transition_name
):
    async def fake_close(ref):
        return True

    monkeypatch.setattr(jira_module.client, "close_ticket_via_orchestrator", fake_close)

    result = await jira_module._tool_change_status(
        {"issue_key": "OPS-1", "transition": transition_name}
    )

    assert _parse(result)["status"] == "done"


async def test_jira_closing_transition_raises_when_the_close_fails(monkeypatch):
    async def fake_close(ref):
        return False

    monkeypatch.setattr(jira_module.client, "close_ticket_via_orchestrator", fake_close)

    with pytest.raises(ValueError, match="Unable to close OPS-3424"):
        await jira_module._tool_change_status({"issue_key": "OPS-3424", "transition": "Done"})


async def test_non_closing_jira_transition_still_calls_transition_issue_directly(monkeypatch):
    """"In Progress" (and any other non-closing transition) has no notifier
    concept in this design -- must be unaffected by the closing-transition
    routing change."""
    calls: list[tuple] = []

    async def fake_transition_issue(issue_key, transition, user_email, user_name):
        calls.append((issue_key, transition, user_email, user_name))
        return {"success": True, "issue_key": issue_key, "status": transition}

    async def fail_if_called(ref):
        raise AssertionError("must not route a non-closing transition through the orchestrator")

    monkeypatch.setattr(jira_module.client, "transition_issue", fake_transition_issue)
    monkeypatch.setattr(jira_module.client, "close_ticket_via_orchestrator", fail_if_called)

    result = await jira_module._tool_change_status(
        {
            "issue_key": "OPS-3424",
            "transition": "In Progress",
            "user_email": "ada@example.com",
            "user_name": "Ada",
        }
    )

    assert calls == [("OPS-3424", "In Progress", "ada@example.com", "Ada")]
    assert _parse(result)["status"] == "In Progress"


async def test_internal_ticket_closing_transition_still_routes_through_the_orchestrator(
    monkeypatch,
):
    """Regression guard for the pre-existing internal-ticket branch, now
    sharing the same close_ticket_via_orchestrator call as the new Jira
    branch."""

    async def fake_get_internal_ticket(issue_key):
        return {"ticket_ref": issue_key, "backend": "internal"}

    calls: list[str] = []

    async def fake_close(ref):
        calls.append(ref)
        return True

    monkeypatch.setattr(jira_module.client, "get_internal_ticket", fake_get_internal_ticket)
    monkeypatch.setattr(jira_module.client, "close_ticket_via_orchestrator", fake_close)

    result = await jira_module._tool_change_status(
        {"issue_key": "TKT-000001", "transition": "Done"}
    )

    data = _parse(result)
    assert data["backend"] == "internal"
    assert calls == ["TKT-000001"]


async def test_internal_ticket_rejects_a_non_closing_transition(monkeypatch):
    async def fake_get_internal_ticket(issue_key):
        return {"ticket_ref": issue_key, "backend": "internal"}

    monkeypatch.setattr(jira_module.client, "get_internal_ticket", fake_get_internal_ticket)

    with pytest.raises(ValueError, match="Internal tickets support only closing"):
        await jira_module._tool_change_status(
            {"issue_key": "TKT-000001", "transition": "In Progress"}
        )


async def test_actions_disabled_refuses_before_any_routing_decision(monkeypatch):
    monkeypatch.setattr(jira_module.ActionFlags, "is_actions_enabled", lambda _s: False)

    with pytest.raises(ValueError, match="JIRA_ACTIONS_ENABLED"):
        await jira_module._tool_change_status({"issue_key": "OPS-1", "transition": "Done"})
