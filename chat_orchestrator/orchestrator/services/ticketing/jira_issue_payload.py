"""Build safe Jira create payloads from the project's live create metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from shared.utils.grid_matcher import find_best_grid_match

from .jira_issue_types import JiraFieldDefinition, JiraIssueType


@dataclass(frozen=True)
class JiraCreateContext:
    """Caller-provided values that are safe to use in a Jira create request."""

    project_key: str
    summary: str
    description: str
    labels: Sequence[str] = ()
    grid_name: str | None = None
    assignee_account_id: str | None = None
    organization_id: str | None = None
    priority_id: str | None = None
    reporter_account_id: str | None = None


_STANDARD_FIELDS = {"project", "summary", "description", "issuetype", "labels"}
_ORGANIZATION_FIELD_NAMES = {"organization", "organizations", "organisation", "organisations"}


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _field_name(field: JiraFieldDefinition) -> str:
    return field.name.strip().casefold()


def _grid_option(field: JiraFieldDefinition, grid_name: str | None) -> dict[str, str] | None:
    if not grid_name or not field.allowed_values:
        return None
    matched_value, _was_fuzzy, _score = find_best_grid_match(
        grid_name, [option.value for option in field.allowed_values]
    )
    if matched_value is None:
        return None
    option = next((item for item in field.allowed_values if item.value == matched_value), None)
    return {"id": option.id} if option is not None else None


def _resolve_field_value(field: JiraFieldDefinition, context: JiraCreateContext) -> Any:
    """Value ``build_issue_payload`` would set for a non-standard field, or ``None``
    when this context has nothing to offer it. Shared with
    ``incompatible_issue_type_reason`` so the two never drift out of sync."""
    if field.id == "assignee" and context.assignee_account_id:
        return {"accountId": context.assignee_account_id}
    if field.id == "reporter" and context.reporter_account_id:
        return {"accountId": context.reporter_account_id}
    if field.id == "priority" and context.priority_id:
        return {"id": context.priority_id}
    if _field_name(field) in _ORGANIZATION_FIELD_NAMES and context.organization_id:
        return {"id": context.organization_id}
    if _field_name(field) == "grid":
        return _grid_option(field, context.grid_name)
    return None


def build_issue_payload(
    context: JiraCreateContext, issue_type: JiraIssueType
) -> dict[str, Any] | None:
    """Create a payload only when every required metadata field is satisfiable.

    Metadata is the sole source of custom field IDs and option values. Unknown
    required fields therefore make an issue type incompatible rather than
    inviting a project-specific guess.
    """
    fields: dict[str, Any] = {
        "project": {"key": context.project_key},
        "summary": context.summary,
        "description": _adf(context.description),
        "issuetype": {"id": issue_type.id},
        "labels": list(context.labels),
    }
    for field in issue_type.fields:
        if field.id in _STANDARD_FIELDS:
            continue
        value = _resolve_field_value(field, context)
        if value is None:
            if field.required:
                return None
            continue
        fields[field.id] = value
    return {"fields": fields}


def compatible_issue_types(
    context: JiraCreateContext, issue_types: Sequence[JiraIssueType]
) -> list[JiraIssueType]:
    """Return just the types whose required metadata fields are satisfiable."""
    return [issue_type for issue_type in issue_types if build_issue_payload(context, issue_type)]


def incompatible_issue_type_reason(
    context: JiraCreateContext, issue_type: JiraIssueType
) -> str | None:
    """Name of the first required field this context can't satisfy for
    ``issue_type``, or ``None`` when the type is compatible.

    For diagnostics only -- mirrors ``build_issue_payload``'s field-resolution
    logic via the shared ``_resolve_field_value`` helper rather than
    re-deriving it, so the two can't silently disagree on what's compatible.
    """
    for field in issue_type.fields:
        if field.id in _STANDARD_FIELDS or not field.required:
            continue
        if _resolve_field_value(field, context) is None:
            return field.name
    return None
