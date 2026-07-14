from orchestrator.services.lpp_parameter_help import (
    detect_lpp_parameter_help_request,
    format_lpp_parameter_help,
)


def test_detects_lpp_parameter_help_request():
    assert detect_lpp_parameter_help_request("what parameters can I set for LPP?")
    assert detect_lpp_parameter_help_request("what can I configure for generate_powerplant_design?")
    assert not detect_lpp_parameter_help_request("create an LPP for Site Alpha")


def test_formats_step_specific_help():
    text = format_lpp_parameter_help("what can I configure for generate_powerplant_design?")

    assert "generate_powerplant_design" in text
    assert "technology_family" in text
    assert "wp_per_conn_override" in text
    assert "generate_site_layout" not in text
