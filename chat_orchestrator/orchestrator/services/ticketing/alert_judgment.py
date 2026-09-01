"""Typed, fail-safe contract for the alert-correlation LLM judgment."""

from __future__ import annotations

import json
import math
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from shared.grid_status import SiteStatus
from shared.llm import Usage


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
    # Nullable: the prompt tells the model to omit this when send_telegram is
    # false (nobody reads it). A sent alert must still carry one -- enforced by
    # the ``missing_notification_reason`` guardrail in ``parse_alert_judgment``.
    reason: str | None = Field(default=None, max_length=500)


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
    # Nullable for the same reason as ``NotificationJudgment.reason`` -- the
    # prompt asks for null (with ``category: "none"``) when not sending.
    summary: str | None = Field(default=None, max_length=500)
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
    usage: Usage | None = None


def _invalid(
    raw: str | None, code: str, detail: str, judgment: AlertJudgment | None = None
) -> AlertJudgmentResult:
    """Build a failed result. ``judgment`` carries the already-parsed
    ``AlertJudgment`` through a guardrail rejection, when the caller has one --
    every guardrail below `AlertJudgment.model_validate` succeeding checks
    internal consistency of the `ticket`/`notification` sections only, never
    the content of `grid_impact`/`likely_user_action`. Discarding a
    well-formed judgment wholesale on a ticket-only guardrail failure used to
    also throw away a perfectly good root-cause/action summary that has
    nothing to do with the failure -- callers that only need those two
    fields (e.g. rendering an operator-facing line) should still get them
    even when `valid` is False and the ticket decision itself is untrusted.
    """
    return AlertJudgmentResult(
        valid=False,
        judgment=judgment,
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

    # Every guardrail from here down checks internal consistency of the
    # `ticket`/`notification` sections only -- none of them re-examine
    # `grid_impact`/`likely_user_action` content, so a rejection here still
    # passes the already-parsed `judgment` through (see `_invalid`).
    if judgment.notification.send_telegram and not _has_text(judgment.notification.reason):
        return _invalid(
            raw,
            "missing_notification_reason",
            "a sent alert must carry a notification reason",
            judgment,
        )

    impact = judgment.grid_impact
    if impact.material_status_change and not judgment.notification.send_telegram:
        return _invalid(
            raw, "inconsistent_notification", "material change requires delivery", judgment
        )

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
                judgment,
            )

    ticket = judgment.ticket
    target_ref = ticket.target_ticket_ref
    if ticket.action is TicketAction.CREATE_NEW:
        if target_ref is not None:
            return _invalid(
                raw, "unexpected_ticket_ref", "create_new cannot target an existing ticket", judgment
            )
        if ticket.change_title or ticket.change_description:
            return _invalid(
                raw, "invalid_ticket_action", "create_new cannot amend an existing ticket", judgment
            )
    else:
        if not target_ref or target_ref not in candidate_refs:
            return _invalid(
                raw, "unknown_ticket_ref", "ticket target is not an offered candidate", judgment
            )

    if ticket.action is TicketAction.RECORD_OCCURRENCE and (
        ticket.change_title or ticket.change_description
    ):
        return _invalid(
            raw, "invalid_ticket_action", "record_occurrence cannot amend a ticket", judgment
        )

    if ticket.change_title and not _has_text(ticket.proposed_title):
        return _invalid(
            raw, "invalid_ticket_title", "title change requires a proposed title", judgment
        )
    if ticket.change_description and not _has_text(ticket.description_addition):
        return _invalid(
            raw, "invalid_ticket_description", "description change requires an addition", judgment
        )

    # Every action but create_new binds this alert to an existing ticket, and
    # binding is the decision that can go wrong silently -- a record_occurrence
    # is what folds an alert into a ticket's history and out of Telegram. The
    # floor used to cover update_existing only, which left the quieter of the
    # two mutations reachable at any confidence at all.
    if ticket.action is not TicketAction.CREATE_NEW and ticket.confidence < confidence_floor:
        return _invalid(
            raw,
            "low_ticket_confidence",
            "existing-ticket decision confidence is too low",
            judgment,
        )

    return AlertJudgmentResult(valid=True, judgment=judgment, raw=raw)
