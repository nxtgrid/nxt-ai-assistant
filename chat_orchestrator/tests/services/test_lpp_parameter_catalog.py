import orchestrator.experts.handlers.package_generator  # noqa: F401

from orchestrator.services.lpp_parameter_catalog import (
    format_lpp_supported_parameters,
    get_lpp_parameter_catalog,
    get_lpp_parameter_names,
)


def test_catalog_includes_design_parameters():
    catalog = get_lpp_parameter_catalog()
    design_params = catalog["generate_powerplant_design"]

    assert "technology_family" in design_params
    assert "initial_residential_connections" in design_params
    assert "initial_business_connections" in design_params
    assert "wp_per_conn_override" in design_params


def test_catalog_includes_layout_technology_parameter():
    catalog = get_lpp_parameter_catalog()
    layout_params = catalog["generate_site_layout"]

    assert "technology_family" in layout_params
    assert "editable_site_type" in layout_params


def test_parameter_names_are_flat_unique_allowlist():
    names = get_lpp_parameter_names()

    assert "technology_family" in names
    assert "wp_per_conn_override" in names
    assert len(names) == len(set(names))


def test_format_supported_parameters_can_filter_steps():
    text = format_lpp_supported_parameters(["generate_powerplant_design"])

    assert "generate_powerplant_design" in text
    assert "technology_family" in text
    assert "wp_per_conn_override" in text
    assert "generate_site_layout" not in text


def test_design_parameter_synonyms_cover_natural_language_terms():
    catalog = get_lpp_parameter_catalog()
    design_params = catalog["generate_powerplant_design"]

    assert "residential connections" in design_params["initial_residential_connections"].synonyms
    assert "nonresidential connections" in design_params["initial_business_connections"].synonyms
    assert "non-residential connections" in design_params["initial_business_connections"].synonyms
    assert "wp per connection" in design_params["wp_per_conn_override"].synonyms
    assert "wp/conn" in design_params["wp_per_conn_override"].synonyms


def test_layout_contract_advertises_technology_family():
    catalog = get_lpp_parameter_catalog()
    param = catalog["generate_site_layout"]["technology_family"]

    assert param.param_type == "string"
    assert "deye" in param.description.lower()
    assert "victron" in param.description.lower()
