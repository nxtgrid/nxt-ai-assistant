"""Comment-driven filling: resolution, failure paths, and the audit reply."""

from orchestrator.experts.output_catalogue import CatalogueEntry
from orchestrator.experts.step_registry import get_step_contract


def test_contract_marks_it_mutating_with_a_mock():
    contract = get_step_contract("fill_annotations")
    assert contract is not None
    assert contract.mutates is True
    assert contract.mock is not None


def test_contract_exposes_file_id_and_dry_run():
    contract = get_step_contract("fill_annotations")
    names = {p.name for p in contract.params}
    assert {"file_id", "dry_run"} <= names


CATALOGUE = [
    CatalogueEntry(path="energy.total_kwp", value=42.5, value_type="number",
                   description="Total installed solar peak capacity in kWp.",
                   produced_by="generate_site_bom"),
    CatalogueEntry(path="site.site_name", value="ExampleGrid", value_type="string",
                   description="Canonical site name.", produced_by="resolve_sites"),
]


def test_plan_writes_pairs_each_match_to_its_cell():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes
    from shared.utils.sheet_editing import CellMatch

    plan = plan_writes(
        resolutions=[{"request": 1, "path": "energy.total_kwp", "confidence": 0.95}],
        matches_by_request={1: [CellMatch(tab="Main Input", a1="B1", row=1, column=2)]},
        catalogue=CATALOGUE,
    )
    assert plan.writes == [("Main Input", "B1", 42.5)]
    assert plan.replies[0].startswith("Done: energy.total_kwp = 42.5")


def test_a_null_path_produces_a_question_not_a_write():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes
    from shared.utils.sheet_editing import CellMatch

    plan = plan_writes(
        resolutions=[{"request": 1, "path": None, "confidence": 0.0}],
        matches_by_request={1: [CellMatch(tab="Main Input", a1="B1", row=1, column=2)]},
        catalogue=CATALOGUE,
    )
    assert plan.writes == []
    assert plan.unresolved and "could not find a value" in plan.unresolved[0][1].lower()


def test_a_token_matching_many_cells_fills_all_of_them():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes
    from shared.utils.sheet_editing import CellMatch

    plan = plan_writes(
        resolutions=[{"request": 1, "path": "energy.total_kwp", "confidence": 0.9}],
        matches_by_request={1: [
            CellMatch(tab="Main Input", a1="B1", row=1, column=2),
            CellMatch(tab="Second", a1="A1", row=1, column=1),
        ]},
        catalogue=CATALOGUE,
    )
    assert len(plan.writes) == 2


def test_no_cell_match_leaves_the_thread_open_with_an_explanation():
    from orchestrator.experts.handlers.templates.fill_annotations import plan_writes

    plan = plan_writes(
        resolutions=[{"request": 1, "path": "energy.total_kwp", "confidence": 0.9}],
        matches_by_request={1: []},
        catalogue=CATALOGUE,
    )
    assert plan.writes == []
    assert "no longer appears" in plan.unresolved[0][1]
