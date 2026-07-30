"""build_issue_payload's ``reporter`` field handling.

Jira Service Management project configs commonly mark ``reporter`` required
on every customer-facing issue type (the ticket log showed exactly this:
"Email request", "Equipment or Comms Issue", "Electricity Service
Disruption", "Payment Systems Issue" and "Task" all rejected for missing
required field 'Reporter', so *no* issue type was ever compatible and every
ticket silently fell back to the internal backend). Customers aren't Jira
users, so the only reporter identity this integration can always supply is
its own (bot) account -- callers resolve that account id and pass it through
``JiraCreateContext.reporter_account_id``.
"""

from __future__ import annotations

from orchestrator.services.ticketing.jira_issue_payload import (
    JiraCreateContext,
    build_issue_payload,
    compatible_issue_types,
    incompatible_issue_type_reason,
)
from orchestrator.services.ticketing.jira_issue_types import JiraFieldDefinition, JiraIssueType


def _context(**overrides) -> JiraCreateContext:
    defaults = dict(project_key="OPS", summary="Grid down", description="0 kW")
    defaults.update(overrides)
    return JiraCreateContext(**defaults)


def _issue_type_requiring_reporter() -> JiraIssueType:
    return JiraIssueType(
        id="email-request-id",
        name="Email request",
        fields=(JiraFieldDefinition(id="reporter", name="Reporter", required=True),),
    )


def test_build_issue_payload_sets_reporter_when_account_id_provided():
    issue_type = _issue_type_requiring_reporter()
    context = _context(reporter_account_id="bot-account-1")

    payload = build_issue_payload(context, issue_type)

    assert payload is not None
    assert payload["fields"]["reporter"] == {"accountId": "bot-account-1"}


def test_build_issue_payload_rejects_type_when_reporter_required_but_unresolved():
    issue_type = _issue_type_requiring_reporter()
    context = _context()  # reporter_account_id defaults to None

    assert build_issue_payload(context, issue_type) is None


def test_compatible_issue_types_includes_reporter_requiring_type_once_resolved():
    issue_type = _issue_type_requiring_reporter()

    assert compatible_issue_types(_context(), [issue_type]) == []
    assert compatible_issue_types(_context(reporter_account_id="acc-1"), [issue_type]) == [
        issue_type
    ]


def test_incompatible_issue_type_reason_names_reporter_when_unresolved():
    issue_type = _issue_type_requiring_reporter()

    assert incompatible_issue_type_reason(_context(), issue_type) == "Reporter"
    assert (
        incompatible_issue_type_reason(_context(reporter_account_id="acc-1"), issue_type) is None
    )
