"""Tests for Jira create metadata and metadata-derived issue payloads."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from orchestrator.services.ticketing.jira_issue_types import (
    JiraFieldDefinition,
    JiraFieldOption,
    JiraIssueType,
    JiraIssueTypeSelector,
    normalize_issue_types,
)


class _Response:
    def __init__(self, status: int, payload: Any):
        self.status = status
        self._payload = payload

    async def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _HangingResponse:
    async def __aenter__(self) -> "_HangingResponse":
        await asyncio.Event().wait()
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


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
        "grid_name": "GridW",
        "labels": ["grid-gridw"],
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
                allowed_values=(JiraFieldOption(id="7", value="GridW"),),
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
                            "allowedValues": [{"id": "7", "value": "GridW"}],
                        },
                    },
                }
            ]
        }
    )

    grid = types[0].field("customfield_44")
    assert grid is not None
    assert grid.name == "Grid"
    assert grid.allowed_values[0] == JiraFieldOption(id="7", value="GridW")


def test_build_issue_payload_adds_a_required_grid_option_from_metadata():
    from orchestrator.services.ticketing.jira_issue_payload import build_issue_payload

    payload = build_issue_payload(_context(), _type_with_required_grid_option())

    assert payload is not None
    assert payload["fields"] == {
        "project": {"key": "OPS"},
        "summary": "MPPT Q7II low output",
        "description": _expected_adf("details"),
        "issuetype": {"id": "101"},
        "labels": ["grid-gridw"],
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
        _context(assignee_account_id="account-1", organization_id="2"), issue_type
    )

    assert payload is not None
    assert payload["fields"]["assignee"] == {"accountId": "account-1"}
    # Organizations takes an array of numeric org ids against the real API:
    # a bare object 400s with "Specify the value ... in an array"
    # (2026-08-11 Hardrock) and an array of objects 400s with "Operation
    # value must be a number" (2026-08-24 Hardrock).
    assert payload["fields"]["customfield_56"] == [2]


def test_incompatible_type_with_an_unknown_required_field_is_excluded():
    from orchestrator.services.ticketing.jira_issue_payload import compatible_issue_types

    assert compatible_issue_types(_context(), [_type_with_required_category()]) == []


def test_incompatible_issue_type_reason_names_the_missing_required_field():
    from orchestrator.services.ticketing.jira_issue_payload import incompatible_issue_type_reason

    reason = incompatible_issue_type_reason(_context(), _type_with_required_category())

    assert reason == "Category"


def test_incompatible_issue_type_reason_names_an_unmatched_required_grid_field():
    from orchestrator.services.ticketing.jira_issue_payload import incompatible_issue_type_reason

    reason = incompatible_issue_type_reason(
        _context(grid_name="Unknown grid"), _type_with_required_grid_option()
    )

    assert reason == "Grid"


def test_incompatible_issue_type_reason_is_none_for_a_compatible_type():
    from orchestrator.services.ticketing.jira_issue_payload import incompatible_issue_type_reason

    reason = incompatible_issue_type_reason(_context(), _type_with_required_grid_option())

    assert reason is None


@pytest.mark.asyncio
async def test_available_types_merges_paged_jira_field_metadata_into_the_known_type():
    base_url = "https://example.atlassian.net/rest/api/3/issue/createmeta/OPS/issuetypes"

    class Session:
        def get(self, url: str, *, params=None, **_kwargs):
            if url == base_url:
                return _Response(
                    200,
                    {
                        "values": [
                            {
                                "id": "101",
                                "name": "Electricity Service Disruption",
                                "description": "Grid supply incident",
                            }
                        ]
                    },
                )
            assert url == f"{base_url}/101"
            start_at = int((params or {}).get("startAt", 0))
            if start_at == 0:
                return _Response(
                    200,
                    {
                        "startAt": 0,
                        "maxResults": 2,
                        "total": 3,
                        "fields": [
                            {
                                "fieldId": "summary",
                                "name": "Summary",
                                "required": True,
                            },
                            {
                                "fieldId": "customfield_44",
                                "name": "Grid",
                                "required": True,
                                "allowedValues": [{"id": "7", "value": "GridW"}],
                            },
                        ],
                    },
                )
            assert start_at == 2
            return _Response(
                200,
                {
                    "startAt": 2,
                    "maxResults": 2,
                    "total": 3,
                    "fields": [
                        {
                            "fieldId": "priority",
                            "name": "Priority",
                            "required": False,
                        }
                    ],
                },
            )

    selector = JiraIssueTypeSelector(
        base_url="https://example.atlassian.net",
        headers={},
        project_key="OPS",
        model="fake-model",
        get_session=Session,
    )

    available = await selector.available_types()

    assert [(item.id, item.name, item.description) for item in available] == [
        ("101", "Electricity Service Disruption", "Grid supply incident")
    ]
    assert available[0].required_fields == ("summary", "customfield_44")
    assert available[0].field("customfield_44").allowed_values == (
        JiraFieldOption(id="7", value="GridW"),
    )
    assert available[0].field("priority") == JiraFieldDefinition(
        id="priority", name="Priority", required=False
    )


@pytest.mark.asyncio
async def test_available_types_fetches_every_paged_catalogue_entry_before_detail_filtering():
    base_url = "https://example.atlassian.net/rest/api/3/issue/createmeta/OPS/issuetypes"

    class Session:
        def get(self, url: str, *, params=None, **_kwargs):
            start_at = int((params or {}).get("startAt", 0))
            if url == base_url:
                issue_type = (
                    {"id": "task", "name": "Task"}
                    if start_at == 0
                    else {"id": "comms", "name": "Comms Failure"}
                )
                return _Response(
                    200,
                    {
                        "startAt": start_at,
                        "maxResults": 1,
                        "total": 2,
                        "values": [issue_type],
                    },
                )
            assert url in {f"{base_url}/task", f"{base_url}/comms"}
            return _Response(
                200,
                {
                    "startAt": 0,
                    "maxResults": 50,
                    "total": 1,
                    "fields": [
                        {"fieldId": "summary", "name": "Summary", "required": True}
                    ],
                },
            )

    selector = JiraIssueTypeSelector(
        base_url="https://example.atlassian.net",
        headers={},
        project_key="OPS",
        model="fake-model",
        get_session=Session,
    )

    available = await selector.available_types()

    assert [item.id for item in available] == ["task", "comms"]


@pytest.mark.asyncio
async def test_available_types_excludes_a_type_when_its_detail_metadata_stalls():
    base_url = "https://example.atlassian.net/rest/api/3/issue/createmeta/OPS/issuetypes"

    class Session:
        def get(self, url: str, **_kwargs):
            if url == base_url:
                return _Response(
                    200,
                    {
                        "values": [
                            {"id": "stalled", "name": "Stalled"},
                            {"id": "ready", "name": "Task"},
                        ]
                    },
                )
            if url == f"{base_url}/stalled":
                return _HangingResponse()
            assert url == f"{base_url}/ready"
            return _Response(
                200,
                {
                    "startAt": 0,
                    "maxResults": 50,
                    "total": 1,
                    "fields": [
                        {"fieldId": "summary", "name": "Summary", "required": True}
                    ],
                },
            )

    selector = JiraIssueTypeSelector(
        base_url="https://example.atlassian.net",
        headers={},
        project_key="OPS",
        model="fake-model",
        get_session=Session,
        metadata_timeout_seconds=0.05,
    )

    started = time.monotonic()
    available = await selector.available_types()

    assert time.monotonic() - started < 0.5
    assert [item.id for item in available] == ["ready"]


@pytest.mark.asyncio
async def test_selector_times_out_a_stalled_llm_call():
    class Gateway:
        async def generate(self, _messages, _options):
            await asyncio.Event().wait()

    selector = JiraIssueTypeSelector(
        base_url="https://example.atlassian.net",
        headers={},
        project_key="OPS",
        model="fake-model",
        get_session=lambda: None,
        gateway=Gateway(),
        llm_timeout_seconds=0.05,
    )

    started = time.monotonic()
    selected = await selector.select(
        summary="Grid down",
        description="0 kW",
        candidate_types=[JiraIssueType(id="task", name="Task")],
    )

    assert time.monotonic() - started < 0.5
    assert selected is None


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
