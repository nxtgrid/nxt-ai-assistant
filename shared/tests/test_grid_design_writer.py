"""Tests for shared/grid_design/design_writer.py — full AppSheet form parameter set.

The internal engine must accept every parameter the AppSheet design form offered
and apply the same initial values the form applied when a field was left blank,
so API-created designs match UI-created ones.
"""

from unittest.mock import MagicMock, patch

from shared.grid_design import design_writer


def _create(payload: dict, grid_id: str = "grid1") -> dict:
    """Run create_design with a mocked Repository; return the inserted row."""
    inserted: dict = {}

    def capture(row):
        inserted.update(row)
        return row

    repo = MagicMock()
    repo.insert.side_effect = capture
    with patch.object(design_writer, "Repository", return_value=repo):
        design_writer.create_design(payload, grid_id)
    return inserted


BASE = {"design_name": "Test design", "max_connections": 100}


def test_form_defaults_applied_when_omitted():
    row = _create(dict(BASE))
    assert row["constrain_design_to_known_regulation"] == "Nigeria - DARES"
    assert row["spd_type"] == ("Keep default T1+T2 Type SPD (Any lightning probability)")
    assert row["average_service_drop_length_m"] == 25
    assert row["avg_distance_to_pv_combiner"] == 40
    assert row["distance_to_feeder_pillar"] == 7
    assert row["pue_hours_per_day"] == 3
    assert row["target_tariff_usd"] == 0.45
    assert row["anchor_load_kw"] == 0
    assert row["force_3_phase"] is False
    assert row["auto_design"] is True


def test_optional_fields_without_defaults_are_omitted():
    row = _create(dict(BASE))
    assert "target_kwp" not in row
    assert "target_kwh" not in row
    assert "wp_per_conn_override" not in row
    assert "daily_generation_potential_kwh_kwp" not in row
    assert "max_distance_to_center_of_consumption" not in row
    assert "created_by" not in row


def test_explicit_values_override_defaults_and_map_to_gd_columns():
    row = _create(
        dict(
            BASE,
            wp_per_conn_override=850,
            regulation_constraint="None",
            spd_type="Use T2 type as T1+T2 Type due to Low (<=16 strikes per sq km per yr) lightning probability",
            pue_hours_per_day=5,
            daily_generation_potential_kwh_kwp=4.2,
            target_tariff_usd=0.5,
            max_distance_to_center_of_consumption_m=300,
            avg_distance_to_pv_combiner_m=15.5,
            distance_to_feeder_pillar_m=12,
            avg_service_drop_length_m=30,
            target_kwp=75.0,
            target_kwh=150.0,
            force_3phase=True,
            created_by="engineer@example.com",
        )
    )
    assert row["wp_per_conn_override"] == 850
    assert row["constrain_design_to_known_regulation"] == "None"
    assert row["spd_type"].startswith("Use T2 type")
    assert row["pue_hours_per_day"] == 5
    assert row["daily_generation_potential_kwh_kwp"] == 4.2
    assert row["target_tariff_usd"] == 0.5
    assert row["max_distance_to_center_of_consumption"] == 300
    assert row["avg_distance_to_pv_combiner"] == 15.5
    assert row["distance_to_feeder_pillar"] == 12
    assert row["average_service_drop_length_m"] == 30
    assert row["target_kwp"] == 75.0
    assert row["target_kwh"] == 150.0
    assert row["force_3_phase"] is True
    assert row["created_by"] == "engineer@example.com"


def test_connection_split_defaults_match_appsheet_flow():
    row = _create(dict(BASE))
    assert row["initial_residential_connections"] == 90
    assert row["initial_business_connections"] == 10
    assert row["initial_3_phase_connections"] == 0


def test_business_count_clamped_to_zero():
    """Oversized residential/3-phase inputs must not yield a negative business count."""
    row = _create(
        dict(
            BASE,
            max_connections=4,
            initial_residential_connections=5,
            initial_3phase_connections=1,
        )
    )
    assert row["initial_business_connections"] == 0


def test_explicit_connection_split_preserved():
    row = _create(
        dict(
            BASE,
            initial_residential_connections=60,
            initial_business_connections=30,
            initial_3phase_connections=10,
        )
    )
    assert row["initial_residential_connections"] == 60
    assert row["initial_business_connections"] == 30
    assert row["initial_3_phase_connections"] == 10


def _get(design_id: str, row: dict | None):
    """Run get_design with a mocked Repository returning `row`; return the result."""
    repo = MagicMock()
    repo.get.return_value = row
    with patch.object(design_writer, "Repository", return_value=repo):
        return design_writer.get_design(design_id)


def test_get_design_returns_row_as_is():
    row = {"id": "design1", "name": "Test design", "max_connections": 100}
    result = _get("design1", row)
    assert result == row


def test_get_design_returns_none_when_not_found():
    result = _get("missing-id", None)
    assert result is None


def test_design_row_to_payload_maps_columns_to_api_fields():
    design_row = {
        "id": "design1",
        "name": "Test design",
        "max_connections": 100,
        "avg_distance_to_pv_combiner": 15.5,
        "constrain_design_to_known_regulation": "None",
        "wp_per_conn_override": 850,
        "initial_3_phase_connections": 5,
        "created_by": "engineer@example.com",
    }
    payload = design_writer.design_row_to_payload(design_row)
    assert payload["design_name"] == "Test design"
    assert payload["max_connections"] == 100
    assert payload["avg_distance_to_pv_combiner_m"] == 15.5
    assert payload["regulation_constraint"] == "None"
    assert payload["wp_per_conn_override"] == 850
    assert payload["initial_3phase_connections"] == 5
    assert payload["created_by"] == "engineer@example.com"
    # "id" has no reverse-map entry (it isn't a value in _DESIGN_FIELD_MAP), so it's dropped.
    assert "id" not in payload


def test_design_row_to_payload_omits_none_valued_columns():
    design_row = {
        "name": "Test design",
        "max_connections": 100,
        "target_kwp": None,
        "target_kwh": None,
        "wp_per_conn_override": None,
    }
    payload = design_writer.design_row_to_payload(design_row)
    assert "target_kwp" not in payload
    assert "target_kwh" not in payload
    assert "wp_per_conn_override" not in payload
    assert payload["design_name"] == "Test design"
    assert payload["max_connections"] == 100


def test_design_row_to_payload_preserves_falsy_values():
    """False and 0 are real values, not "absent" -- must survive, unlike None."""
    design_row = {
        "name": "Test design",
        "force_3_phase": False,
        "anchor_load_kw": 0,
        "initial_3_phase_connections": 0,
    }
    payload = design_writer.design_row_to_payload(design_row)
    assert payload["force_3phase"] is False
    assert payload["anchor_load_kw"] == 0
    assert payload["initial_3phase_connections"] == 0


def test_get_design_and_design_row_to_payload_compose_with_create_design():
    """The future duplicate_design flow: get_design -> design_row_to_payload -> create_design."""
    source_row = {
        "id": "design1",
        "grid": "grid1",
        "active": True,
        "name": "Original design",
        "max_connections": 100,
        "avg_distance_to_pv_combiner": 15.5,
        "wp_per_conn_override": 850,
        "constrain_design_to_known_regulation": "None",
    }
    fetched = _get("design1", source_row)
    payload = design_writer.design_row_to_payload(fetched)
    # Simulate the duplicate_design caller's policy decision to rename.
    payload["design_name"] = "Duplicated design"

    row = _create(payload, grid_id="grid2")
    assert row["name"] == "Duplicated design"
    assert row["max_connections"] == 100
    assert row["avg_distance_to_pv_combiner"] == 15.5
    assert row["wp_per_conn_override"] == 850
    assert row["constrain_design_to_known_regulation"] == "None"
