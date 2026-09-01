import json

from orchestrator.services.ticketing.alert_judgment import parse_alert_judgment

VALID = {
    "grid_impact": {
        "prior_known_status": "on",
        "current_assessed_status": "off",
        "material_status_change": True,
        "summary": "Grid is unavailable",
        "confidence": 0.94,
    },
    "notification": {
        "send_telegram": True,
        "reason": "A full outage changes grid status",
    },
    "ticket": {
        "action": "update_existing",
        "target_ticket_ref": "OPS-1234",
        "change_title": True,
        "proposed_title": "Grid outage following inverter shutdown",
        "change_description": True,
        "description_addition": "All three output phases are at 0 V.",
        "relationship": "same_root_cause",
        "root_cause_kind": "power_chain",
        "reason": "The existing BMS ticket represents the root cause",
        "confidence": 0.91,
    },
    "likely_user_action": {
        "category": "remote_investigation",
        "summary": "Check the BMS link before attempting an inverter restart",
        "confidence": 0.82,
    },
}


def test_parse_accepts_each_required_answer_individually() -> None:
    result = parse_alert_judgment(json.dumps(VALID), {"OPS-1234"}, 0.75)

    assert result.valid is True
    assert result.judgment is not None
    assert result.judgment.grid_impact.current_assessed_status == "off"
    assert result.judgment.grid_impact.material_status_change is True
    assert result.judgment.ticket.change_description is True
    assert result.judgment.likely_user_action.category == "remote_investigation"


def test_parse_rejects_material_true_with_send_false() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["notification"]["send_telegram"] = False

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is False
    assert result.error_code == "inconsistent_notification"


def test_parse_rejects_an_invented_ticket_reference() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["ticket"]["target_ticket_ref"] = "OPS-9999"

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is False
    assert result.error_code == "unknown_ticket_ref"


def test_low_confidence_existing_ticket_mutation_is_not_valid_for_suppression() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["ticket"]["confidence"] = 0.4
    payload["grid_impact"] = {
        "prior_known_status": "on",
        "current_assessed_status": "on",
        "material_status_change": False,
        "summary": "No material site-status change",
        "confidence": 0.9,
    }
    payload["notification"] = {
        "send_telegram": False,
        "reason": "Looks repetitive",
    }

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is False
    assert result.error_code == "low_ticket_confidence"


def test_low_confidence_record_occurrence_is_not_valid_for_suppression() -> None:
    """The floor covers every action that binds an alert to an existing
    ticket, not just ``update_existing``. ``record_occurrence`` is the quieter
    of the two -- it folds the alert into a ticket's history and out of
    Telegram without changing any ticket prose -- and it used to be reachable
    at any confidence at all, including 0.
    """
    payload = json.loads(json.dumps(VALID))
    payload["ticket"] = {
        "action": "record_occurrence",
        "target_ticket_ref": "OPS-1234",
        "change_title": False,
        "proposed_title": None,
        "change_description": False,
        "description_addition": None,
        "relationship": "same_issue",
        "root_cause_kind": "component",
        "reason": "Looks like the same alert",
        "confidence": 0.2,
    }
    payload["grid_impact"] = {
        "prior_known_status": "on",
        "current_assessed_status": "on",
        "material_status_change": False,
        "summary": "No material site-status change",
        "confidence": 0.9,
    }
    payload["notification"] = {"send_telegram": False, "reason": "Looks repetitive"}

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is False
    assert result.error_code == "low_ticket_confidence"


def test_confident_record_occurrence_stays_valid() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["ticket"] = {
        "action": "record_occurrence",
        "target_ticket_ref": "OPS-1234",
        "change_title": False,
        "proposed_title": None,
        "change_description": False,
        "description_addition": None,
        "relationship": "same_issue",
        "root_cause_kind": "component",
        "reason": "The same alert re-firing",
        "confidence": 0.88,
    }

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is True


def test_low_confidence_create_new_is_untouched_by_the_floor() -> None:
    """Filing a fresh ticket is the fail-open outcome -- it can never be the
    thing a low-confidence judgment gets wrong in a way that loses an alert."""
    payload = json.loads(json.dumps(VALID))
    payload["ticket"] = {
        "action": "create_new",
        "target_ticket_ref": None,
        "change_title": False,
        "proposed_title": None,
        "change_description": False,
        "description_addition": None,
        "relationship": "new_issue",
        "root_cause_kind": "component",
        "reason": "Nothing open matches",
        "confidence": 0.1,
    }

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is True


def test_parse_rejects_known_transition_marked_non_material() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["grid_impact"]["material_status_change"] = False

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is False
    assert result.error_code == "inconsistent_site_status"


def test_parse_rejects_unchanged_known_status_marked_material() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["grid_impact"]["current_assessed_status"] = "on"

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is False
    assert result.error_code == "inconsistent_site_status"


def test_parse_keeps_unknown_status_judgment_for_fail_open_delivery_policy() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["grid_impact"] = {
        "prior_known_status": "unknown",
        "current_assessed_status": "off",
        "material_status_change": True,
        "summary": "The alert establishes the grid is off",
        "confidence": 0.9,
    }

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is True
    assert result.judgment is not None
    assert result.judgment.grid_impact.prior_known_status == "unknown"


def test_parse_accepts_null_reason_and_action_summary_when_not_sending() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["grid_impact"] = {
        "prior_known_status": "on",
        "current_assessed_status": "on",
        "material_status_change": False,
        "summary": "recurrence, no status change",
        "confidence": 0.9,
    }
    payload["notification"] = {"send_telegram": False, "reason": None}
    payload["likely_user_action"] = {"category": "none", "summary": None, "confidence": 0.7}
    payload["ticket"] = {
        "action": "record_occurrence",
        "target_ticket_ref": "OPS-1234",
        "change_title": False,
        "proposed_title": None,
        "change_description": False,
        "description_addition": None,
        "relationship": "same_issue",
        "root_cause_kind": "other",
        "reason": "re-fire",
        "confidence": 0.9,
    }

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is True
    assert result.judgment is not None
    assert result.judgment.notification.reason is None
    assert result.judgment.likely_user_action.summary is None
    round_tripped = result.judgment.model_dump(mode="json")
    assert round_tripped["notification"]["reason"] is None


def test_parse_rejects_missing_reason_when_sending() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["notification"] = {"send_telegram": True, "reason": None}

    result = parse_alert_judgment(json.dumps(payload), {"OPS-1234"}, 0.75)

    assert result.valid is False
    assert result.error_code == "missing_notification_reason"
