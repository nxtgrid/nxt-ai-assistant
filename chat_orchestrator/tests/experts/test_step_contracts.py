"""Tests for step contracts (ParamSpec, StepContract) and registry support.

Covers the plain dataclasses in step_contracts.py plus the contract-related
additions to StepHandlerRegistry / register_step in step_registry.py.
"""

import dataclasses

import pytest

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import (
    MUTATION_KINDS,
    MockSpec,
    OutputSpec,
    ParamSpec,
    StepContract,
    validate_mock_covers_outputs,
)
from orchestrator.experts.step_registry import (
    StepHandlerRegistry,
    get_step_contract,
    get_step_registry,
    register_step,
)


class TestParamSpecConstruction:
    """ParamSpec construction and immutability."""

    def test_bare_construction_defaults(self):
        """ParamSpec requires only `name`; everything else defaults."""
        spec = ParamSpec(name="site_name")
        assert spec.name == "site_name"
        assert spec.param_type == "string"
        assert spec.description == ""
        assert spec.synonyms == ()
        assert spec.required is False
        assert spec.default is None

    def test_full_construction(self):
        spec = ParamSpec(
            name="max_connections",
            param_type="integer",
            description="Max connections for the design",
            synonyms=("connections", "conn_count"),
            required=True,
            default=0,
        )
        assert spec.param_type == "integer"
        assert spec.synonyms == ("connections", "conn_count")
        assert spec.required is True
        assert spec.default == 0

    def test_is_frozen(self):
        spec = ParamSpec(name="site_name")
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "other_name"  # type: ignore[misc]


class TestStepContractConstruction:
    """StepContract construction and immutability."""

    def test_bare_construction_defaults(self):
        """A bare StepContract() constructs fine with all-empty defaults.

        Regression per Phase 1 Task 1.6: adding mutates/mutation_kind/outputs/
        mock/required_permission/expected_latency_seconds must not break any
        of the 17 existing contracts or 35 uncontracted handlers that predate
        these fields -- a bare StepContract() must keep constructing.
        """
        contract = StepContract()
        assert contract.description == ""
        assert contract.consumes_state == ()
        assert contract.optional_consumes_state == ()
        assert contract.produces_state == ()
        assert contract.consumes_results == ()
        assert contract.params == ()
        assert contract.guard_keys == ()
        assert contract.side_effects == ""
        assert contract.mutates is False
        assert contract.mutation_kind == ""
        assert contract.outputs == ()
        assert contract.mock is None
        assert contract.required_permission == ""
        assert contract.expected_latency_seconds == 0.0

    def test_full_construction(self):
        param = ParamSpec(name="site_name", required=True)
        output = OutputSpec(name="design_id", value_type="string", where="state")
        mock = MockSpec(state_updates={"design_id": "MOCK-design-1", "design_generated": True})
        contract = StepContract(
            description="Generates a powerplant design",
            consumes_state=("site_name", "site_id"),
            optional_consumes_state=("layout_result",),
            produces_state=("design_id", "design_generated"),
            consumes_results=("generate_distribution_map",),
            params=(param,),
            guard_keys=("design_generated",),
            side_effects="Calls grid_design MCP server",
            mutates=True,
            mutation_kind="external_write",
            outputs=(output,),
            mock=mock,
            required_permission="package_generator.write",
            expected_latency_seconds=45.0,
        )
        assert contract.consumes_state == ("site_name", "site_id")
        assert contract.optional_consumes_state == ("layout_result",)
        assert contract.produces_state == ("design_id", "design_generated")
        assert contract.consumes_results == ("generate_distribution_map",)
        assert contract.params == (param,)
        assert contract.guard_keys == ("design_generated",)
        assert contract.side_effects == "Calls grid_design MCP server"
        assert contract.mutates is True
        assert contract.mutation_kind == "external_write"
        assert contract.outputs == (output,)
        assert contract.mock is mock
        assert contract.required_permission == "package_generator.write"
        assert contract.expected_latency_seconds == 45.0

    def test_is_frozen(self):
        contract = StepContract(description="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            contract.description = "y"  # type: ignore[misc]


class TestOutputSpecConstruction:
    """OutputSpec construction, defaults, and immutability."""

    def test_bare_construction_defaults(self):
        spec = OutputSpec(name="document_id")
        assert spec.name == "document_id"
        assert spec.value_type == "string"
        assert spec.description == ""
        assert spec.where == "state"

    def test_full_construction(self):
        spec = OutputSpec(
            name="analysis_summary",
            value_type="object",
            description="Structured summary of the failure analysis",
            where="data",
        )
        assert spec.value_type == "object"
        assert spec.description == "Structured summary of the failure analysis"
        assert spec.where == "data"

    def test_is_frozen(self):
        spec = OutputSpec(name="document_id")
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "other_id"  # type: ignore[misc]


class TestMutationKinds:
    """MUTATION_KINDS is the closed vocabulary mutation_kind draws from."""

    def test_contains_expected_kinds(self):
        assert MUTATION_KINDS == (
            "external_write",
            "db_write",
            "notification",
            "control_action",
        )

    def test_is_a_tuple(self):
        """Immutable by construction -- nothing should be able to append to it."""
        assert isinstance(MUTATION_KINDS, tuple)


class TestMockSpecConstruction:
    """MockSpec construction, defaults, and (deliberate) mutability."""

    def test_bare_construction_defaults(self):
        mock = MockSpec()
        assert mock.state_updates == {}
        assert mock.data == {}
        assert mock.message == ""

    def test_full_construction(self):
        mock = MockSpec(
            state_updates={"document_id": "MOCK-doc-1"},
            data={"template_name": "MOCK-template"},
            message="Would have copied the LPP template.",
        )
        assert mock.state_updates == {"document_id": "MOCK-doc-1"}
        assert mock.data == {"template_name": "MOCK-template"}
        assert mock.message == "Would have copied the LPP template."

    def test_is_not_frozen(self):
        """MockSpec is deliberately mutable (see its docstring): it holds
        dict fields, and a dataclass-generated __hash__ over mutable fields
        would raise at runtime. Assigning to it must NOT raise."""
        mock = MockSpec()
        mock.message = "updated"
        assert mock.message == "updated"

    def test_default_dicts_are_independent_instances(self):
        """default_factory=dict, not a shared mutable default."""
        mock_a = MockSpec()
        mock_b = MockSpec()
        mock_a.state_updates["key"] = "value"
        assert mock_b.state_updates == {}


class TestValidateMockCoversOutputs:
    """validate_mock_covers_outputs: findings-based, never raises."""

    def test_non_mutating_contract_needs_no_mock(self):
        contract = StepContract(mutates=False)
        assert validate_mock_covers_outputs(contract) == []

    def test_mutating_contract_with_no_mock_is_flagged(self):
        contract = StepContract(mutates=True, produces_state=("document_id",))
        findings = validate_mock_covers_outputs(contract)
        assert len(findings) == 1
        assert "mock is None" in findings[0]

    def test_mock_missing_a_produces_state_key_is_flagged(self):
        """The exact failure mode from the docstring: copy_lpp_template's mock
        must populate document_id or populate_lpp_cells's precondition fails
        and a mocked run collapses at the first mutation."""
        contract = StepContract(
            mutates=True,
            produces_state=("document_id", "template_copied"),
            mock=MockSpec(state_updates={"template_copied": True}),
        )
        findings = validate_mock_covers_outputs(contract)
        assert len(findings) == 1
        assert "document_id" in findings[0]
        assert "template_copied" not in findings[0].split("--")[0]

    def test_mock_covering_all_keys_passes(self):
        contract = StepContract(
            mutates=True,
            produces_state=("document_id", "template_copied"),
            mock=MockSpec(
                state_updates={"document_id": "MOCK-doc-1", "template_copied": True}
            ),
        )
        assert validate_mock_covers_outputs(contract) == []

    def test_mutating_contract_with_no_produces_state_and_empty_mock_passes(self):
        """A mutating step that produces nothing (e.g. a pure notification)
        is satisfied by an empty-but-present MockSpec."""
        contract = StepContract(mutates=True, mock=MockSpec())
        assert validate_mock_covers_outputs(contract) == []

    def test_never_raises_on_a_bare_mutating_contract(self):
        """Belt-and-braces: calling this on adversarial-ish input returns
        findings, never an exception."""
        contract = StepContract(mutates=True)
        findings = validate_mock_covers_outputs(contract)
        assert isinstance(findings, list)
        assert len(findings) >= 1


class TestRegistryContractSupport:
    """StepHandlerRegistry contract registration, retrieval, and clearing."""

    def test_register_without_contract(self):
        """register_step-equivalent call with no contract behaves as before."""
        registry = StepHandlerRegistry()

        async def my_handler(ctx: StepContext) -> StepResult:
            return StepResult.success()

        registry.register("plain_step", my_handler)

        assert registry.get_handler("plain_step") is my_handler
        assert registry.get_contract("plain_step") is None
        assert registry.has_contract("plain_step") is False

    def test_register_with_contract(self):
        """Registering with a contract stores and retrieves the exact object."""
        registry = StepHandlerRegistry()
        contract = StepContract(description="does a thing")

        async def my_handler(ctx: StepContext) -> StepResult:
            return StepResult.success()

        registry.register("contract_step", my_handler, contract=contract)

        assert registry.get_contract("contract_step") is contract
        assert registry.has_contract("contract_step") is True

    def test_clear_clears_contracts(self):
        """clear() removes contracts along with handlers/schemas."""
        registry = StepHandlerRegistry()
        contract = StepContract(description="does a thing")

        async def my_handler(ctx: StepContext) -> StepResult:
            return StepResult.success()

        registry.register("contract_step", my_handler, contract=contract)
        assert registry.has_contract("contract_step") is True

        registry.clear()

        assert registry.get_contract("contract_step") is None
        assert registry.has_contract("contract_step") is False
        assert registry.has_handler("contract_step") is False

    def test_reregister_overwrites_contract(self):
        """Registering the same name twice overwrites the contract too."""
        registry = StepHandlerRegistry()
        contract_v1 = StepContract(description="v1")
        contract_v2 = StepContract(description="v2")

        async def handler_v1(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"version": 1})

        async def handler_v2(ctx: StepContext) -> StepResult:
            return StepResult.success(data={"version": 2})

        registry.register("dup_step", handler_v1, contract=contract_v1)
        assert registry.get_contract("dup_step") is contract_v1

        registry.register("dup_step", handler_v2, contract=contract_v2)

        assert registry.get_handler("dup_step") is handler_v2
        assert registry.get_contract("dup_step") is contract_v2


class TestRegisterStepDecoratorBackwardsCompat:
    """@register_step decorator: old single-arg call sites keep working."""

    def test_register_step_no_contract_still_works(self):
        """@register_step("name") with no contract kwarg works exactly as before."""

        @register_step("test_contract_module_no_contract_step")
        async def my_handler(ctx: StepContext) -> StepResult:
            return StepResult.success()

        registry = get_step_registry()
        assert registry.has_handler("test_contract_module_no_contract_step")
        assert registry.get_handler("test_contract_module_no_contract_step") is my_handler
        assert get_step_contract("test_contract_module_no_contract_step") is None
        assert registry.has_contract("test_contract_module_no_contract_step") is False

    def test_register_step_with_contract(self):
        """@register_step("name", contract=...) attaches the contract."""
        contract = StepContract(
            description="Test step with a contract",
            consumes_state=("some_key",),
        )

        @register_step("test_contract_module_with_contract_step", contract=contract)
        async def my_handler(ctx: StepContext) -> StepResult:
            return StepResult.success()

        registry = get_step_registry()
        assert get_step_contract("test_contract_module_with_contract_step") is contract
        assert registry.has_contract("test_contract_module_with_contract_step") is True
