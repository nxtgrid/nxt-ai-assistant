"""Phase 9 (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md)
additions to package_generator's (LPP) 17 pre-existing StepContracts:
`mutates`/`mutation_kind`/`mock`/`expected_latency_seconds`, audited and
backfilled since they predate Phase 1.

Reuses `test_package_generator_contracts.py`'s own `PACKAGE_GENERATOR_STEP_NAMES`
list (and its module-import-time contract snapshot pattern, for the identical
process-wide-registry-singleton reason documented there and in
test_contract_lint.py) rather than re-declaring a second copy of the same 17
names.

Four things are covered:

- `TestEveryMutatingStepHasAWorkingMock`: Task 9.3's actual acceptance bar --
  every `mutates=True` step's `MockSpec` covers 100% of its own
  `produces_state` keys (`validate_mock_covers_outputs` returns no findings).
  This is a regression guard for the exact failure mode Task 1.5/9.3 warn
  about by name: mock `copy_lpp_template` into an empty result and
  `populate_lpp_cells` fails its `document_id` precondition, collapsing a
  mocked run at the first mutation.
- `TestCopyLppTemplateMockPopulatesDocumentId`: that same scenario, named
  explicitly in the plan, spot-checked directly rather than only implied by
  the general sweep above.
- `TestLongRunningSteps` (Task 9.1 / R5): the three steps whose handler body
  itself declares an explicit wait (`generate_distribution_layout` up to
  180s, `generate_site_layout` up to 120s, `update_design_distances` up to
  60s on the legacy AppSheet backend) declare `expected_latency_seconds`, and
  the derived tool declaration folds a latency warning into the description
  above `step_tool_schema.LONG_RUNNING_THRESHOLD_SECONDS` -- the honest,
  built subset of "steps above a threshold get different handling" (see
  update_design_distances.py's contract for why a real poll/resume execution
  path was NOT built).
- `TestGeneratePowerplantDesignSchema` (Task 9.4): the 22-param derived
  schema is coherent -- every declared param has a real type/description and
  only the genuinely required one is marked required.
"""

import orchestrator.experts.handlers.package_generator  # noqa: F401  (registration side effect)
from orchestrator.experts.step_contracts import validate_mock_covers_outputs
from orchestrator.experts.step_tool_schema import (
    LONG_RUNNING_THRESHOLD_SECONDS,
    function_step_tool_declarations,
)
from tests.experts.test_package_generator_contracts import (
    _CONTRACTS,
    PACKAGE_GENERATOR_STEP_NAMES,
)

NON_MUTATING_STEPS = {"fetch_geo_hazard", "fetch_solar_potential", "resolve_sites"}
MUTATING_STEPS = set(PACKAGE_GENERATOR_STEP_NAMES) - NON_MUTATING_STEPS

LONG_RUNNING_STEPS = {
    "generate_distribution_layout": 180.0,
    "generate_site_layout": 120.0,
    "update_design_distances": 60.0,
}


class TestMutatesIsSetForEveryStep:
    """Every one of the 17 contracts explicitly declares mutates now (Task
    9.2) -- not just relying on the dataclass default, which would be
    indistinguishable from "never audited"."""

    def test_known_mutating_steps_are_flagged(self):
        not_flagged = [name for name in MUTATING_STEPS if not _CONTRACTS[name].mutates]
        assert not not_flagged, f"Expected mutates=True: {not_flagged}"

    def test_known_read_only_steps_are_not_flagged(self):
        still_flagged = [name for name in NON_MUTATING_STEPS if _CONTRACTS[name].mutates]
        assert not still_flagged, f"Expected mutates=False: {still_flagged}"

    def test_every_mutating_step_names_a_mutation_kind(self):
        missing_kind = [
            name for name in MUTATING_STEPS if not _CONTRACTS[name].mutation_kind
        ]
        assert not missing_kind, f"mutates=True with no mutation_kind: {missing_kind}"


class TestEveryMutatingStepHasAWorkingMock:
    """Task 9.3's actual acceptance bar."""

    def test_every_mutating_step_has_a_mockspec(self):
        missing_mock = [name for name in MUTATING_STEPS if _CONTRACTS[name].mock is None]
        assert not missing_mock, f"mutates=True with no MockSpec: {missing_mock}"

    def test_every_mockspec_covers_100_percent_of_produces_state(self):
        # The regression guard: a mock that doesn't populate every
        # produces_state key is the exact failure mode that makes a mocked
        # run collapse at the first mutation (see this module's docstring).
        all_findings = {
            name: validate_mock_covers_outputs(_CONTRACTS[name])
            for name in PACKAGE_GENERATOR_STEP_NAMES
        }
        failing = {name: f for name, f in all_findings.items() if f}
        assert not failing, f"MockSpec gaps found: {failing}"


class TestCopyLppTemplateMockPopulatesDocumentId:
    """The exact scenario named in Task 1.5/9.3: mock copy_lpp_template into
    an empty result and populate_lpp_cells fails its document_id
    precondition, collapsing a mocked run at the very first mutation."""

    def test_document_id_is_populated(self):
        mock = _CONTRACTS["copy_lpp_template"].mock
        assert mock is not None
        assert mock.state_updates.get("document_id")

    def test_document_id_looks_synthetic(self):
        # MockSpec's own docstring convention: self-evidently synthetic
        # values so a mocked artefact is never mistaken for a real one.
        document_id = _CONTRACTS["copy_lpp_template"].mock.state_updates["document_id"]
        assert "MOCK" in document_id

    def test_every_key_populate_lpp_cells_needs_is_covered(self):
        # populate_lpp_cells's own hard precondition (consumes_state) is just
        # document_id, but this proves the full mocked chain doesn't stop
        # there either.
        populate_cells_contract = _CONTRACTS["populate_lpp_cells"]
        mock_state = _CONTRACTS["copy_lpp_template"].mock.state_updates
        for key in populate_cells_contract.consumes_state:
            assert key in mock_state, f"populate_lpp_cells needs {key!r} from copy_lpp_template's mock"


class TestLongRunningSteps:
    """Task 9.1 / R5."""

    def test_expected_latency_seconds_set_for_known_long_running_steps(self):
        for name, expected in LONG_RUNNING_STEPS.items():
            assert _CONTRACTS[name].expected_latency_seconds == expected, name

    def test_every_other_step_defaults_to_zero(self):
        # Confirms the three above are a deliberate, reviewed exception, not
        # a copy-paste that silently spread to steps with no stated wait.
        others = set(PACKAGE_GENERATOR_STEP_NAMES) - set(LONG_RUNNING_STEPS)
        nonzero = [name for name in others if _CONTRACTS[name].expected_latency_seconds]
        assert not nonzero, f"Unexpected non-zero expected_latency_seconds: {nonzero}"

    def test_derived_tool_declaration_warns_about_latency(self):
        declarations = {
            d["name"]: d for d in function_step_tool_declarations(allow_write=True)
        }
        for name, expected_seconds in LONG_RUNNING_STEPS.items():
            assert expected_seconds >= LONG_RUNNING_THRESHOLD_SECONDS  # sanity on the fixture itself
            description = declarations[name]["description"]
            assert "can take up to" in description, (
                f"{name}: expected a latency warning in the derived tool description"
            )


class TestGeneratePowerplantDesignSchema:
    """Task 9.4: the 22-param schema is coherent at that size."""

    def test_declares_all_22_params(self):
        contract = _CONTRACTS["generate_powerplant_design"]
        assert len(contract.params) == 22

    def test_only_site_name_is_required(self):
        declarations = {
            d["name"]: d for d in function_step_tool_declarations(allow_write=True)
        }
        schema = declarations["generate_powerplant_design"]["parameters"]
        assert schema["required"] == ["site_name"]

    def test_every_param_has_a_real_gemini_type_and_description(self):
        declarations = {
            d["name"]: d for d in function_step_tool_declarations(allow_write=True)
        }
        properties = declarations["generate_powerplant_design"]["parameters"]["properties"]
        contract = _CONTRACTS["generate_powerplant_design"]
        for param in contract.params:
            prop = properties[param.name]
            assert prop["type"] in {"STRING", "INTEGER", "NUMBER", "BOOLEAN", "OBJECT", "ARRAY"}
            assert prop["description"], f"{param.name} has no description"

    def test_declaration_is_a_reasonable_size(self):
        import json

        declarations = {
            d["name"]: d for d in function_step_tool_declarations(allow_write=True)
        }
        size = len(json.dumps(declarations["generate_powerplant_design"]))
        # Generous ceiling -- this is a sanity check against something
        # pathological (e.g. a runaway description), not a tight budget.
        assert size < 10_000, f"generate_powerplant_design's declaration is {size} bytes"
