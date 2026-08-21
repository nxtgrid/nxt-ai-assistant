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


def test_solar_potential_declares_its_irradiation_values():
    names = _output_names("fetch_solar_potential")
    assert {
        "energy.gsa_daily_potential_kwhperkwp",
        "energy.gsa_yearly_potential_kwhperkwp",
        "solar.optimal_tilt_deg",
        "solar.ghi_kwh_m2",
        "solar.gti_kwh_m2",
        "solar.dni_kwh_m2",
        "solar.avg_temp_c",
        "solar.elevation_m",
    } <= names


def test_resolve_sites_declares_the_site_identity_values():
    names = _output_names("resolve_sites")
    assert {"site.site_name", "site.site_id"} <= names


def test_site_state_is_declared_on_the_map_step_not_resolve_sites():
    """site_state is only ever populated by generate_distribution_map --
    declaring it on resolve_sites would make it permanently unmatchable,
    since that step never produces a value for it."""
    assert "site.state" in _output_names("generate_distribution_map")
    assert "site.state" not in _output_names("resolve_sites")


def test_every_declared_output_name_is_a_literal_top_level_data_or_state_key():
    """Guards the actual bug this file was written to catch.

    OutputSpec.name is looked up as a *literal* key -- accumulated_results
    [step][spec.name] for where="data", packet_state[spec.name] for
    where="state" (see output_catalogue.build_catalogue_from, once it
    exists in Phase 4). A step whose real StepResult only nests that value
    inside a sub-dict (e.g. "statistics") satisfies this test's sibling
    name-only tests above while silently returning nothing at runtime --
    this test instead inspects each handler's actual source for a
    string literal matching the declared name, which is a cheap proxy for
    "this key is really published flat somewhere in this function".
    """
    import inspect

    from orchestrator.experts.handlers.package_generator import (
        fetch_solar_potential,
        generate_bom,
        generate_design,
        generate_map,
        resolve_sites,
    )

    module_by_step = {
        "generate_distribution_map": generate_map,
        "generate_powerplant_design": generate_design,
        "generate_site_bom": generate_bom,
        "fetch_solar_potential": fetch_solar_potential,
        "resolve_sites": resolve_sites,
    }
    for step_name, module in module_by_step.items():
        source = inspect.getsource(module)
        for spec in get_step_contract(step_name).outputs:
            needle = f'"{spec.name}"'
            assert needle in source, (
                f"{step_name}'s module has no literal {needle} -- "
                f"{spec.name} is declared but never actually published flat"
            )


from orchestrator.experts.output_catalogue import CatalogueEntry, build_catalogue_from
from orchestrator.experts.step_contracts import OutputSpec, StepContract


def test_builds_entries_from_contracts_of_steps_that_ran():
    contracts = {
        "step_a": StepContract(
            description="a",
            outputs=(
                OutputSpec(name="energy.total_kwp", value_type="number", where="data",
                           description="Total installed solar peak capacity in kWp."),
            ),
        ),
        "step_b": StepContract(
            description="b",
            outputs=(
                OutputSpec(name="site.site_name", value_type="string", where="state",
                           description="Canonical site name."),
            ),
        ),
    }
    entries = build_catalogue_from(
        contracts=contracts,
        accumulated_results={"step_a": {"energy.total_kwp": 42.5}},
        packet_state={"site.site_name": "ExampleGrid"},
    )
    by_path = {e.path: e for e in entries}
    assert by_path["energy.total_kwp"].value == 42.5
    assert by_path["energy.total_kwp"].produced_by == "step_a"
    assert by_path["site.site_name"].value == "ExampleGrid"


def test_skips_steps_that_have_not_run():
    contracts = {
        "step_a": StepContract(
            description="a",
            outputs=(OutputSpec(name="x", where="data", description="An x."),),
        ),
    }
    assert build_catalogue_from(contracts, accumulated_results={}, packet_state={}) == []


def test_skips_declared_outputs_with_no_value_yet():
    contracts = {
        "step_a": StepContract(
            description="a",
            outputs=(
                OutputSpec(name="present", where="data", description="Here."),
                OutputSpec(name="absent", where="data", description="Not here."),
            ),
        ),
    }
    entries = build_catalogue_from(
        contracts, accumulated_results={"step_a": {"present": 1}}, packet_state={}
    )
    assert [e.path for e in entries] == ["present"]


def test_renders_a_prompt_block_with_descriptions():
    entries = [
        CatalogueEntry(path="energy.total_kwp", value=42.5, value_type="number",
                       description="Total installed solar peak capacity in kWp.",
                       produced_by="generate_site_bom"),
    ]
    from orchestrator.experts.output_catalogue import render_catalogue

    block = render_catalogue(entries)
    assert "energy.total_kwp" in block
    assert "Total installed solar peak capacity in kWp." in block
    assert "42.5" in block
