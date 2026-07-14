"""Tests for step contracts (ParamSpec, StepContract) and registry support.

Covers the plain dataclasses in step_contracts.py plus the contract-related
additions to StepHandlerRegistry / register_step in step_registry.py.
"""

import dataclasses

import pytest

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import ParamSpec, StepContract
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
        """A bare StepContract() constructs fine with all-empty defaults."""
        contract = StepContract()
        assert contract.description == ""
        assert contract.consumes_state == ()
        assert contract.optional_consumes_state == ()
        assert contract.produces_state == ()
        assert contract.consumes_results == ()
        assert contract.params == ()
        assert contract.guard_keys == ()
        assert contract.side_effects == ""

    def test_full_construction(self):
        param = ParamSpec(name="site_name", required=True)
        contract = StepContract(
            description="Generates a powerplant design",
            consumes_state=("site_name", "site_id"),
            optional_consumes_state=("layout_result",),
            produces_state=("design_id", "design_generated"),
            consumes_results=("generate_distribution_map",),
            params=(param,),
            guard_keys=("design_generated",),
            side_effects="Calls grid_design MCP server",
        )
        assert contract.consumes_state == ("site_name", "site_id")
        assert contract.optional_consumes_state == ("layout_result",)
        assert contract.produces_state == ("design_id", "design_generated")
        assert contract.consumes_results == ("generate_distribution_map",)
        assert contract.params == (param,)
        assert contract.guard_keys == ("design_generated",)
        assert contract.side_effects == "Calls grid_design MCP server"

    def test_is_frozen(self):
        contract = StepContract(description="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            contract.description = "y"  # type: ignore[misc]


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
