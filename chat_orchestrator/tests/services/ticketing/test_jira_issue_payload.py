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


def _issue_type_requiring_organization(field_name: str = "Organizations") -> JiraIssueType:
    return JiraIssueType(
        id="disruption-id",
        name="Electricity Service Disruption",
        fields=(
            JiraFieldDefinition(id="customfield_10002", name=field_name, required=True),
        ),
    )


def test_build_issue_payload_wraps_organization_id_in_array_when_org_id_provided():
    # Jira's Organizations field (customfield_10002 in production) is a
    # multi-value field even for a single org: the create API 400s with
    # "Specify the value for Organizations in an array" for a bare object.
    # Reproduces the 2026-08-11 Hardrock incident (both escalations for
    # meter 47003337616 failed with exactly this error).
    issue_type = _issue_type_requiring_organization()
    context = _context(organization_id="org-14")

    payload = build_issue_payload(context, issue_type)

    assert payload is not None
    assert payload["fields"]["customfield_10002"] == [{"id": "org-14"}]


def test_build_issue_payload_wraps_organization_id_in_array_for_organisation_variant():
    # _ORGANIZATION_FIELD_NAMES matches "organization"/"organisation" (both
    # spellings, singular and plural) case-insensitively -- confirm the
    # array-wrap applies via that shared set, not a one-off literal check
    # bolted onto just the "Organizations" spelling.
    issue_type = _issue_type_requiring_organization(field_name="Organisation")
    context = _context(organization_id="org-14")

    payload = build_issue_payload(context, issue_type)

    assert payload is not None
    assert payload["fields"]["customfield_10002"] == [{"id": "org-14"}]


def test_build_issue_payload_rejects_type_when_organizations_required_but_unresolved():
    issue_type = _issue_type_requiring_organization()

    assert build_issue_payload(context=_context(), issue_type=issue_type) is None
