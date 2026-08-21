"""Typed, fail-safe contract for the alert-correlation LLM judgment."""

from __future__ import annotations

import json
import math
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from shared.grid_status import SiteStatus


class TicketAction(str, Enum):
    CREATE_NEW = "create_new"
    UPDATE_EXISTING = "update_existing"
    RECORD_OCCURRENCE = "record_occurrence"


class TicketRelationship(str, Enum):
    SAME_ISSUE = "same_issue"
    SAME_ROOT_CAUSE = "same_root_cause"
    NEW_ISSUE = "new_issue"


class RootCauseKind(str, Enum):
    GRID_OFF = "grid_off"
    GRID_ISOLATED = "grid_isolated"
    POWER_CHAIN = "power_chain"
    COMPONENT = "component"
    OTHER = "other"


class LikelyUserActionCategory(str, Enum):
    NONE = "none"
    REMOTE_INVESTIGATION = "remote_investigation"
    EQUIPMENT_RESTART = "equipment_restart"
    SITE_VISIT = "site_visit"
    CONTACT_OPERATOR = "contact_operator"
    MONITOR = "monitor"
    OTHER = "other"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeterministicFinding(_StrictModel):
    """Factual correlation evidence supplied to, but never ordered by, the LLM."""

    candidate_ref: str | None = None
    kind: str = Field(min_length=1, max_length=100)
    facts: dict[str, Any] = Field(default_factory=dict)
    explanation: str = Field(min_length=1, max_length=500)


class GridImpact(_StrictModel):
    prior_known_status: SiteStatus
    current_assessed_status: SiteStatus
    material_status_change: bool
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)

    @field_validator("confidence")
    @classmethod
    def confidence_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("confidence must be finite")
        return value


class NotificationJudgment(_StrictModel):
    send_telegram: bool
    reason: str = Field(min_length=1, max_length=500)


class TicketJudgment(_StrictModel):
    action: TicketAction
    target_ticket_ref: str | None = Field(default=None, max_length=100)
    change_title: bool
    proposed_title: str | None = Field(default=None, max_length=240)
    change_description: bool
    description_addition: str | None = Field(default=None, max_length=1000)
    relationship: TicketRelationship
    root_cause_kind: RootCauseKind
    reason: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)

    @field_validator("confidence")
    @classmethod
    def confidence_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("confidence must be finite")
        return value


class LikelyUserAction(_StrictModel):
    category: LikelyUserActionCategory
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)

    @field_validator("confidence")
    @classmethod
    def confidence_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("confidence must be finite")
        return value


class AlertJudgment(_StrictModel):
    grid_impact: GridImpact
    notification: NotificationJudgment
    ticket: TicketJudgment
    likely_user_action: LikelyUserAction


class AlertJudgmentResult(_StrictModel):
    valid: bool
    judgment: AlertJudgment | None = None
    error_code: str = ""
    error_detail: str = ""
    raw: str | None = None


def _invalid(raw: str | None, code: str, detail: str) -> AlertJudgmentResult:
    return AlertJudgmentResult(
        valid=False,
        error_code=code,
        error_detail=detail,
        raw=raw,
    )


def _parse_json_object(raw: str | None) -> dict[str, Any] | None:
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _has_text(value: str | None) -> bool:
    return bool(value and value.strip())


def parse_alert_judgment(
    raw: str | None,
    candidate_refs: set[str],
    confidence_floor: float,
) -> AlertJudgmentResult:
    """Validate model output without raising; invalid output is fail-open input."""
    payload = _parse_json_object(raw)
    if payload is None:
        return _invalid(raw, "invalid_json", "response was not a JSON object")

    try:
        judgment = AlertJudgment.model_validate(payload)
    except ValidationError as exc:
        return _invalid(raw, "invalid_schema", str(exc))

    impact = judgment.grid_impact
    if impact.material_status_change and not judgment.notification.send_telegram:
        return _invalid(raw, "inconsistent_notification", "material change requires delivery")

    known_statuses = {SiteStatus.ON, SiteStatus.ISOLATED, SiteStatus.OFF}
    if {
        impact.prior_known_status,
        impact.current_assessed_status,
    } <= known_statuses:
        expected_materiality = impact.prior_known_status != impact.current_assessed_status
        if impact.material_status_change != expected_materiality:
            return _invalid(
                raw,
                "inconsistent_site_status",
                "known site-status transition disagrees with materiality",
            )

    ticket = judgment.ticket
    target_ref = ticket.target_ticket_ref
    if ticket.action is TicketAction.CREATE_NEW:
        if target_ref is not None:
            return _invalid(raw, "unexpected_ticket_ref", "create_new cannot target an existing ticket")
        if ticket.change_title or ticket.change_description:
            return _invalid(raw, "invalid_ticket_action", "create_new cannot amend an existing ticket")
    else:
        if not target_ref or target_ref not in candidate_refs:
            return _invalid(raw, "unknown_ticket_ref", "ticket target is not an offered candidate")

    if ticket.action is TicketAction.RECORD_OCCURRENCE and (
        ticket.change_title or ticket.change_description
    ):
        return _invalid(raw, "invalid_ticket_action", "record_occurrence cannot amend a ticket")

    if ticket.change_title and not _has_text(ticket.proposed_title):
        return _invalid(raw, "invalid_ticket_title", "title change requires a proposed title")
    if ticket.change_description and not _has_text(ticket.description_addition):
        return _invalid(raw, "invalid_ticket_description", "description change requires an addition")

    if ticket.action is TicketAction.UPDATE_EXISTING and ticket.confidence < confidence_floor:
        return _invalid(raw, "low_ticket_confidence", "existing-ticket mutation confidence is too low")

    return AlertJudgmentResult(valid=True, judgment=judgment, raw=raw)
