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
