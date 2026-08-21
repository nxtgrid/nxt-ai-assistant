"""OutputSpec coverage for the steps the value catalogue reads from."""

from orchestrator.experts.step_registry import get_step_contract


def _output_names(step_name: str) -> set[str]:
    contract = get_step_contract(step_name)
    assert contract is not None, f"{step_name} has no StepContract"
    return {spec.name for spec in contract.outputs}


def test_generate_distribution_map_declares_its_statistics():
    names = _output_names("generate_distribution_map")
    assert {
        "meta.pole_count",
        "meta.served_building_count",
        "meta.unserved_building_count",
        "meta.coverage_percentage",
        "meta.backbone_cable_length_m",
        "meta.drop_cable_length_m",
        "computed.total_buildings",
        "location.lat",
        "location.lon",
        "location.gps",
    } <= names


def test_every_catalogue_output_has_a_description():
    """A bare key name is not matchable — the LLM matches on description."""
    for step in (
        "generate_distribution_map",
        "generate_powerplant_design",
        "generate_site_bom",
        "fetch_solar_potential",
        "resolve_sites",
    ):
        contract = get_step_contract(step)
        assert contract is not None, f"{step} has no StepContract"
        for spec in contract.outputs:
            assert spec.description.strip(), f"{step}.{spec.name} has no description"


def test_design_and_bom_declare_their_energy_and_cost_values():
    design = _output_names("generate_powerplant_design")
    assert {"design.design_id", "design.design_name"} <= design

    bom = _output_names("generate_site_bom")
    assert {
        "bom.total_cost",
        "bom.main_energy_asset_cost",
        "bom.metering_cost",
        "bom.bos_cost",
        "energy.total_kwp",
        "energy.total_kwh",
        "energy.total_kva",
        "energy.Wp_per_conn",
        "energy.num_subsystems",
        "energy.num_inverters",
        "energy.num_batteries",
        "energy.num_panels",
    } <= bom
