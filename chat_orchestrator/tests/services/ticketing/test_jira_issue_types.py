"""Tests for Jira create metadata and metadata-derived issue payloads."""

from __future__ import annotations

import time

import pytest

from orchestrator.services.ticketing.jira_issue_types import (
    JiraFieldDefinition,
    JiraFieldOption,
    JiraIssueType,
    JiraIssueTypeSelector,
    normalize_issue_types,
)


def _expected_adf(text: str) -> dict[str, object]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _context(**overrides: object):
    from orchestrator.services.ticketing.jira_issue_payload import JiraCreateContext

    values: dict[str, object] = {
        "project_key": "OPS",
        "summary": "MPPT Q7II low output",
        "description": "details",
        "grid_name": "Ogheye",
        "labels": ["grid-ogheye"],
    }
    values.update(overrides)
    return JiraCreateContext(**values)


def _type_with_required_grid_option(*, grid_required: bool = True) -> JiraIssueType:
    return JiraIssueType(
        id="101",
        name="Electricity Service Disruption",
        fields=(
            JiraFieldDefinition(id="summary", name="Summary", required=True),
            JiraFieldDefinition(
                id="customfield_44",
                name="Grid",
                required=grid_required,
                allowed_values=(JiraFieldOption(id="7", value="Ogheye"),),
            ),
        ),
    )


def _type_with_required_category() -> JiraIssueType:
    return JiraIssueType(
        id="102",
        name="Categorised incident",
        fields=(
            JiraFieldDefinition(id="summary", name="Summary", required=True),
            JiraFieldDefinition(id="customfield_99", name="Category", required=True),
        ),
    )


def test_normalize_issue_types_preserves_field_names_requirements_and_options():
    types = normalize_issue_types(
        {
            "values": [
                {
                    "id": "101",
                    "name": "Electricity Service Disruption",
                    "fields": {
                        "summary": {"name": "Summary", "required": True},
                        "customfield_44": {
                            "name": "Grid",
                            "required": True,
                            "allowedValues": [{"id": "7", "value": "Ogheye"}],
                        },
                    },
                }
            ]
        }
    )

    grid = types[0].field("customfield_44")
    assert grid is not None
    assert grid.name == "Grid"
    assert grid.allowed_values[0] == JiraFieldOption(id="7", value="Ogheye")


def test_build_issue_payload_adds_a_required_grid_option_from_metadata():
    from orchestrator.services.ticketing.jira_issue_payload import build_issue_payload

    payload = build_issue_payload(_context(), _type_with_required_grid_option())

    assert payload is not None
    assert payload["fields"] == {
        "project": {"key": "OPS"},
        "summary": "MPPT Q7II low output",
        "description": _expected_adf("details"),
        "issuetype": {"id": "101"},
        "labels": ["grid-ogheye"],
        "customfield_44": {"id": "7"},
    }


def test_build_issue_payload_omits_an_optional_grid_without_a_match():
    from orchestrator.services.ticketing.jira_issue_payload import build_issue_payload

    payload = build_issue_payload(
        _context(grid_name="Unknown grid"), _type_with_required_grid_option(grid_required=False)
    )

    assert payload is not None
    assert "customfield_44" not in payload["fields"]


def test_build_issue_payload_includes_caller_supplied_assignee_and_organisation_values():
    from orchestrator.services.ticketing.jira_issue_payload import build_issue_payload

    issue_type = JiraIssueType(
        id="101",
        name="Service request",
        fields=(
            JiraFieldDefinition(id="summary", name="Summary", required=True),
            JiraFieldDefinition(id="assignee", name="Assignee"),
            JiraFieldDefinition(id="customfield_56", name="Organizations"),
        ),
    )

    payload = build_issue_payload(
        _context(assignee_account_id="account-1", organization_id="organisation-2"), issue_type
    )

    assert payload is not None
    assert payload["fields"]["assignee"] == {"accountId": "account-1"}
    assert payload["fields"]["customfield_56"] == {"id": "organisation-2"}


def test_incompatible_type_with_an_unknown_required_field_is_excluded():
    from orchestrator.services.ticketing.jira_issue_payload import compatible_issue_types

    assert compatible_issue_types(_context(), [_type_with_required_category()]) == []


@pytest.mark.asyncio
async def test_selector_uses_only_the_supplied_candidate_catalogue():
    class Gateway:
        async def generate(self, _messages, _options):
            class Result:
                text = '{"issue_type_id": "candidate", "reason": "matches alert"}'

            return Result()

    selector = JiraIssueTypeSelector(
        base_url="https://example.atlassian.net",
        headers={},
        project_key="OPS",
        model="fake-model",
        get_session=lambda: None,
        gateway=Gateway(),
    )
    selector._cached_types = [JiraIssueType(id="outside", name="Outside")]
    selector._cached_at = time.monotonic()
    candidate = JiraIssueType(id="candidate", name="Candidate")

    selected = await selector.select(
        summary="Grid down",
        description="0 kW",
        candidate_types=[candidate],
    )

    assert selected is not None
    assert selected.issue_type == candidate


@pytest.mark.asyncio
async def test_selector_rejects_a_model_id_outside_the_candidate_catalogue():
    class Gateway:
        async def generate(self, _messages, _options):
            class Result:
                text = '{"issue_type_id": "outside", "reason": "wrong type"}'

            return Result()

    selector = JiraIssueTypeSelector(
        base_url="https://example.atlassian.net",
        headers={},
        project_key="OPS",
        model="fake-model",
        get_session=lambda: None,
        gateway=Gateway(),
    )
    candidate = JiraIssueType(id="candidate", name="Candidate")

    assert await selector.select(
        summary="Grid down", description="0 kW", candidate_types=[candidate]
    ) is None
