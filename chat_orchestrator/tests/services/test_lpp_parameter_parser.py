from orchestrator.services.lpp_parameter_parser import (
    merge_lpp_parameter_inputs,
    parse_lpp_request_parameters,
)


def test_extracts_deye_not_victron():
    params = parse_lpp_request_parameters(
        "Can you create an LPP for 9.3947551,9.3176320 using Deye technology not Victron?"
    )
    assert params["technology_family"] == "deye"


def test_extracts_connection_counts_and_wp_per_conn():
    params = parse_lpp_request_parameters(
        "Use 120 residential connections, 35 non-residential connections, and 850 Wp/conn."
    )
    assert params["initial_residential_connections"] == 120
    assert params["initial_business_connections"] == 35
    assert params["wp_per_conn_override"] == 850


def test_extracts_targets_and_design_options():
    params = parse_lpp_request_parameters(
        "Target 45.5 kWp and 148.8 kWh, force 3 phase, no DARES, tariff 0.55 USD."
    )
    assert params["editable_total_kwp"] == 45.5
    assert params["editable_total_kwh"] == 148.8
    assert params["force_3phase"] is True
    assert params["regulation_constraint"] == "None"
    assert params["target_tariff_usd"] == 0.55


def test_merge_keeps_parameters_when_synthetic_command_loses_text():
    params = merge_lpp_parameter_inputs(
        "create LPP using Deye with 850 Wp/conn",
        "/lpp 9.3947551,9.3176320",
    )
    assert params["technology_family"] == "deye"
    assert params["wp_per_conn_override"] == 850


def test_victron_negated_by_deye_request():
    params = parse_lpp_request_parameters("Deye, not Victron")
    assert params["technology_family"] == "deye"
    assert params.get("editable_site_type") != "victron"


def test_does_not_extract_unknown_parameters():
    params = parse_lpp_request_parameters("make it blue and add a mascot")
    assert params == {}
