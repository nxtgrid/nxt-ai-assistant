"""Tests for step_tool_schema.py (Phase 4, extended by Phase 6, of
docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md).

Four things are covered:

- `TestDeriveToolDeclaration` / `TestProducibleStateKeys`: schema shape --
  `derive_tool_declaration` builds a well-formed Gemini-format function
  declaration, `consumes_state` keys with a known producer are excluded
  (fact 2 in the module docstring), `params` and `outputs` are represented.
- `TestIsOfferable` (via `is_declared_function_step`) /
  `TestFunctionStepToolDeclarations`: the STRUCTURAL contract-bearing/
  allow_write gate, and that declaration and routing can never disagree
  about a given name on that structural question (the single-predicate
  guarantee `_is_offerable` exists for).
- `TestCallerHoldsPermission` (Phase 6, Task 6.1): the SEPARATE permission
  check, and `TestIsDeclaredFunctionStep`'s last test for why it is
  deliberately not folded into the shared structural predicate.
- `TestUnknownName`: a name with no registered contract at all is never
  offerable -- this is what lets `WorkflowExecutor._execute_skill_step_tool_call`
  fall through to the existing MCP path (and its existing never-raise
  contract) for anything this module doesn't recognize.

Tests that touch the real global step registry use `_cleanup_registry`
(mirrors `test_soft_failures.py`'s fixture of the same name exactly) to add
and then remove ONLY their own synthetic handlers -- the real ~50
production handlers (17 of them with real `package_generator` contracts)
stay registered throughout, so assertions about them are written as
membership checks (`in`), never as exact-list equality.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.experts.step_contracts import OutputSpec, ParamSpec, StepContract
from orchestrator.experts.step_registry import get_step_registry
from orchestrator.experts.step_tool_schema import (
    _is_offerable,
    _producible_state_keys,
    caller_holds_permission,
    derive_tool_declaration,
    function_step_tool_declarations,
    is_declared_function_step,
)


@pytest.fixture
def _cleanup_registry():
    """Mirrors test_soft_failures.py's fixture of the same name exactly."""
    registered: list[str] = []
    registry = get_step_registry()

    def _register(name, handler=None, contract=None):
        handler = handler or (lambda ctx: None)
        registry.register(name, handler, contract=contract)
        registered.append(name)

    yield _register

    for name in registered:
        registry.unregister(name)


class TestDeriveToolDeclaration:
    """Schema shape for one contract, in isolation from the registry."""

    def test_has_name_description_parameters(self):
        decl = derive_tool_declaration("fetch_grid_status", StepContract(), set())
        assert decl["name"] == "fetch_grid_status"
        assert isinstance(decl["description"], str) and decl["description"]
        assert decl["parameters"] == {"type": "OBJECT", "properties": {}, "required": []}

    def test_uses_contract_description_verbatim_when_present(self):
        decl = derive_tool_declaration(
            "copy_lpp_template", StepContract(description="Copies the LPP template."), set()
        )
        assert decl["description"].startswith("Copies the LPP template.")

    def test_falls_back_to_a_generated_description_when_blank(self):
        decl = derive_tool_declaration("some_step", StepContract(), set())
        assert "some_step" in decl["description"]

    def test_consumes_state_key_with_no_producer_becomes_an_argument(self):
        contract = StepContract(consumes_state=("site_name",))
        decl = derive_tool_declaration("copy_lpp_template", contract, producible_state_keys=set())
        assert "site_name" in decl["parameters"]["properties"]
        assert decl["parameters"]["properties"]["site_name"]["type"] == "STRING"

    def test_consumes_state_key_with_a_known_producer_is_excluded(self):
        """Fact 2: document_id has a producer (copy_lpp_template), so
        populate_lpp_cells's declaration must not offer it as an argument --
        it's a precondition, checked by _soft_failure_before_running_step,
        not something the model should invent a value for."""
        contract = StepContract(consumes_state=("document_id",))
        decl = derive_tool_declaration(
            "populate_lpp_cells", contract, producible_state_keys={"document_id"}
        )
        assert "document_id" not in decl["parameters"]["properties"]

    def test_consumes_state_arguments_are_never_required(self):
        """Every consumes_state key that reaches the schema at all has no
        known producer (fact 2) -- some resolve via an env-var/default
        fallback inside the handler, so omitting the argument must remain
        valid, matching what a top-level recipe run gets today."""
        contract = StepContract(consumes_state=("template_id",))
        decl = derive_tool_declaration("copy_lpp_template", contract, set())
        assert "template_id" not in decl["parameters"]["required"]

    def test_params_become_properties_with_mapped_gemini_types(self):
        contract = StepContract(
            params=(
                ParamSpec(name="editable_total_kwp", param_type="number", description="Override."),
            )
        )
        decl = derive_tool_declaration("populate_lpp_cells", contract, set())
        prop = decl["parameters"]["properties"]["editable_total_kwp"]
        assert prop == {"type": "NUMBER", "description": "Override."}

    def test_required_param_with_no_default_is_required(self):
        contract = StepContract(params=(ParamSpec(name="site_id", required=True),))
        decl = derive_tool_declaration("some_step", contract, set())
        assert decl["parameters"]["required"] == ["site_id"]

    def test_required_param_with_a_default_is_not_required(self):
        """A required param with a default can always be resolved without
        the caller supplying it (see PrereqReport's own missing_params
        logic, which applies the identical rule) -- required=True alone
        isn't enough to force the model's hand."""
        contract = StepContract(
            params=(ParamSpec(name="site_id", required=True, default="unknown"),)
        )
        decl = derive_tool_declaration("some_step", contract, set())
        assert decl["parameters"]["required"] == []

    def test_unrecognized_param_type_falls_back_to_string(self):
        contract = StepContract(params=(ParamSpec(name="weird", param_type="frobnicate"),))
        decl = derive_tool_declaration("some_step", contract, set())
        assert decl["parameters"]["properties"]["weird"]["type"] == "STRING"

    def test_mutates_true_is_noted_in_the_description(self):
        decl = derive_tool_declaration(
            "write_review_section", StepContract(mutates=True), set()
        )
        assert "side effect" in decl["description"].lower()

    def test_mutates_false_says_nothing_about_side_effects(self):
        decl = derive_tool_declaration("fetch_grid_status", StepContract(mutates=False), set())
        assert "side effect" not in decl["description"].lower()

    def test_outputs_are_folded_into_the_description(self):
        """Task 4.1: 'outputs come from outputs'. Gemini's function-
        declaration format has no separate response-schema slot, so this is
        the only place a caller can learn what a call hands back."""
        contract = StepContract(
            outputs=(
                OutputSpec(name="document_id", value_type="string", description="The doc's ID."),
            )
        )
        decl = derive_tool_declaration("copy_lpp_template", contract, set())
        assert "document_id" in decl["description"]
        assert "The doc's ID." in decl["description"]

    def test_no_outputs_leaves_description_unchanged(self):
        decl = derive_tool_declaration(
            "some_step", StepContract(description="Does a thing."), set()
        )
        assert decl["description"] == "Does a thing."


class TestProducibleStateKeys:
    def test_collects_produces_state_across_every_contract(self):
        contracts = {
            "copy_lpp_template": StepContract(produces_state=("document_id", "document_url")),
            "populate_lpp_cells": StepContract(produces_state=("cells_populated",)),
        }
        assert _producible_state_keys(contracts) == {
            "document_id",
            "document_url",
            "cells_populated",
        }

    def test_empty_when_nothing_produces_anything(self):
        contracts = {"fetch_thing": StepContract(consumes_state=("site_name",))}
        assert _producible_state_keys(contracts) == set()

    def test_empty_registry_is_empty_set(self):
        assert _producible_state_keys({}) == set()


class TestIsOfferable:
    """The STRUCTURAL gate shared by declaration and routing (Task 4.3) --
    contract existence and the mutation/allow_write bar only. Permission
    (Phase 6) is a separate, later concern -- see TestCallerHoldsPermission
    and step_tool_schema.py's module docstring addendum."""

    def test_no_contract_is_never_offerable(self):
        assert _is_offerable(None, allow_write=True) is False

    def test_plain_non_mutating_contract_is_offerable(self):
        assert _is_offerable(StepContract(), allow_write=False) is True

    def test_required_permission_alone_does_not_affect_offerability(self):
        """Phase 6: _is_offerable no longer knows about required_permission
        at all -- that moved to caller_holds_permission, checked separately
        at declare-time (function_step_tool_declarations) and call-time
        (_execute_declared_function_step_call)."""
        contract = StepContract(required_permission="staff_only")
        assert _is_offerable(contract, allow_write=True) is True
        assert _is_offerable(contract, allow_write=False) is True

    def test_mutating_contract_needs_allow_write(self):
        contract = StepContract(mutates=True)
        assert _is_offerable(contract, allow_write=False) is False
        assert _is_offerable(contract, allow_write=True) is True


class TestCallerHoldsPermission:
    """Phase 6 (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-
    tools.md), Task 6.1."""

    def test_empty_requirement_always_clears(self):
        assert caller_holds_permission("", None) is True
        assert caller_holds_permission("", MagicMock(is_staff=False, roles=[])) is True

    def test_no_user_context_never_clears_a_real_requirement(self):
        assert caller_holds_permission("staff_only", None) is False

    def test_staff_clears_any_requirement_regardless_of_roles(self):
        user_context = MagicMock(is_staff=True, roles=[])
        assert caller_holds_permission("staff_only", user_context) is True

    def test_non_staff_with_the_named_role_clears(self):
        user_context = MagicMock(is_staff=False, roles=["telegram_send"])
        assert caller_holds_permission("telegram_send", user_context) is True

    def test_non_staff_without_the_named_role_does_not_clear(self):
        user_context = MagicMock(is_staff=False, roles=["some_other_role"])
        assert caller_holds_permission("telegram_send", user_context) is False

    def test_non_staff_with_no_roles_at_all_does_not_clear(self):
        user_context = MagicMock(is_staff=False, roles=[])
        assert caller_holds_permission("telegram_send", user_context) is False

    def test_missing_roles_attribute_does_not_crash(self):
        """A duck-typed user_context (e.g. a test double) with no `roles`
        attribute at all must not raise -- treated as no roles held."""
        user_context = MagicMock(spec=["is_staff"], is_staff=False)
        assert caller_holds_permission("telegram_send", user_context) is False


class TestFunctionStepToolDeclarations:
    """Integration over the real global step registry plus synthetic additions."""

    def test_includes_a_freshly_registered_contract_bearing_step(self, _cleanup_registry):
        _cleanup_registry("zzz_test_fetch_thing", contract=StepContract(description="Test step."))
        names = {d["name"] for d in function_step_tool_declarations()}
        assert "zzz_test_fetch_thing" in names

    def test_excludes_a_registered_step_with_no_contract(self, _cleanup_registry):
        _cleanup_registry("zzz_test_no_contract", contract=None)
        names = {d["name"] for d in function_step_tool_declarations()}
        assert "zzz_test_no_contract" not in names

    def test_excludes_a_permission_gated_step(self, _cleanup_registry):
        _cleanup_registry(
            "zzz_test_gated_step", contract=StepContract(required_permission="staff_only")
        )
        names = {d["name"] for d in function_step_tool_declarations(allow_write=True)}
        assert "zzz_test_gated_step" not in names

    def test_excludes_a_mutating_step_when_allow_write_is_false(self, _cleanup_registry):
        _cleanup_registry("zzz_test_mutator", contract=StepContract(mutates=True))
        names = {d["name"] for d in function_step_tool_declarations(allow_write=False)}
        assert "zzz_test_mutator" not in names

    def test_includes_a_mutating_step_when_allow_write_is_true(self, _cleanup_registry):
        _cleanup_registry("zzz_test_mutator", contract=StepContract(mutates=True))
        names = {d["name"] for d in function_step_tool_declarations(allow_write=True)}
        assert "zzz_test_mutator" in names

    def test_result_is_sorted_by_name(self, _cleanup_registry):
        _cleanup_registry("zzz_test_last_step", contract=StepContract())
        _cleanup_registry("aaa_test_first_step", contract=StepContract())
        names = [d["name"] for d in function_step_tool_declarations()]
        assert names == sorted(names)

    def test_a_key_produced_within_this_synthetic_pair_is_not_declared_as_an_argument(
        self, _cleanup_registry
    ):
        """End-to-end version of the fact-2 rule, through the real
        registry-scanning path (not a hand-built producible_state_keys
        set)."""
        _cleanup_registry(
            "zzz_test_producer", contract=StepContract(produces_state=("zzz_test_key",))
        )
        _cleanup_registry(
            "zzz_test_consumer", contract=StepContract(consumes_state=("zzz_test_key",))
        )
        by_name = {d["name"]: d for d in function_step_tool_declarations()}
        assert "zzz_test_key" not in by_name["zzz_test_consumer"]["parameters"]["properties"]

    # Phase 6: permission-gated declarations are hidden from a caller who
    # doesn't hold the required permission, declared to one who does.

    def test_excludes_a_permission_gated_step_with_no_user_context(self, _cleanup_registry):
        _cleanup_registry(
            "zzz_test_gated", contract=StepContract(required_permission="staff_only")
        )
        names = {d["name"] for d in function_step_tool_declarations()}
        assert "zzz_test_gated" not in names

    def test_excludes_a_permission_gated_step_for_a_non_qualifying_caller(
        self, _cleanup_registry
    ):
        _cleanup_registry(
            "zzz_test_gated", contract=StepContract(required_permission="staff_only")
        )
        user_context = MagicMock(is_staff=False, roles=[])
        names = {d["name"] for d in function_step_tool_declarations(user_context=user_context)}
        assert "zzz_test_gated" not in names

    def test_includes_a_permission_gated_step_for_a_qualifying_staff_caller(
        self, _cleanup_registry
    ):
        _cleanup_registry(
            "zzz_test_gated", contract=StepContract(required_permission="staff_only")
        )
        user_context = MagicMock(is_staff=True, roles=[])
        names = {d["name"] for d in function_step_tool_declarations(user_context=user_context)}
        assert "zzz_test_gated" in names

    def test_includes_a_permission_gated_step_for_a_caller_with_the_named_role(
        self, _cleanup_registry
    ):
        _cleanup_registry(
            "zzz_test_gated", contract=StepContract(required_permission="telegram_send")
        )
        user_context = MagicMock(is_staff=False, roles=["telegram_send"])
        names = {d["name"] for d in function_step_tool_declarations(user_context=user_context)}
        assert "zzz_test_gated" in names

    # No test here asserts a specific REAL production contract name (e.g.
    # "copy_lpp_template") is present in function_step_tool_declarations()'s
    # output. get_step_registry() is a process-wide singleton, and
    # `test_parameter_confirmation.py::TestRegisterStepWithoutSchema
    # .setup_method` calls `get_step_registry().clear()` with no teardown --
    # if that runs first in a full-suite run, real registrations are gone
    # for the rest of the process (see the identical issue documented in
    # test_contract_lint.py/test_workflow_executor.py/
    # test_package_generator_contracts.py, each of which works around it by
    # snapshotting real (name, contract) pairs at module-import time). Every
    # test above already covers this module's actual logic with synthetic,
    # locally-owned contracts, which is unaffected by that global-state
    # hazard -- not worth importing that workaround here for a smoke test
    # that would add no unique coverage.


class TestIsDeclaredFunctionStep:
    """No-drift guarantee on the STRUCTURAL half (contract exists,
    mutation/allow_write gate) -- declared and routable must always agree
    on that. Permission (Phase 6) is DELIBERATELY NOT part of this
    guarantee any more; see the last test below and step_tool_schema.py's
    module docstring addendum."""

    def test_agrees_with_function_step_tool_declarations_for_a_plain_step(
        self, _cleanup_registry
    ):
        _cleanup_registry("zzz_test_plain", contract=StepContract())
        declared_names = {d["name"] for d in function_step_tool_declarations()}
        assert ("zzz_test_plain" in declared_names) == is_declared_function_step(
            "zzz_test_plain"
        )

    def test_agrees_for_a_mutating_step_across_both_allow_write_values(self, _cleanup_registry):
        _cleanup_registry("zzz_test_mutator", contract=StepContract(mutates=True))
        for allow_write in (False, True):
            declared_names = {
                d["name"] for d in function_step_tool_declarations(allow_write=allow_write)
            }
            assert ("zzz_test_mutator" in declared_names) == is_declared_function_step(
                "zzz_test_mutator", allow_write=allow_write
            )

    def test_a_permission_gated_step_is_routable_even_when_not_declared_to_this_caller(
        self, _cleanup_registry
    ):
        """The one deliberate divergence (Phase 6): a permission-gated step
        is hidden from a non-qualifying caller at declare-time, but STILL
        structurally routable -- WorkflowExecutor._execute_declared_function_step_call
        is what actually rejects the call, with an explicit not_permitted
        soft failure, not a fallthrough to a confusing generic MCP error."""
        _cleanup_registry(
            "zzz_test_gated", contract=StepContract(required_permission="staff_only")
        )
        user_context = MagicMock(is_staff=False, roles=[])
        declared_names = {
            d["name"] for d in function_step_tool_declarations(user_context=user_context)
        }

        assert "zzz_test_gated" not in declared_names  # hidden from this caller
        assert is_declared_function_step("zzz_test_gated") is True  # still routable


class TestUnknownName:
    """A name with no contract at all is never offerable -- this is what
    lets the executor's routing fall through to the MCP path (and its
    existing never-raise contract) for anything unrecognized."""

    def test_unregistered_name_is_never_declared(self):
        names = {d["name"] for d in function_step_tool_declarations(allow_write=True)}
        assert "totally_made_up_tool_name" not in names

    def test_unregistered_name_is_not_routable(self):
        assert is_declared_function_step("totally_made_up_tool_name", allow_write=True) is False
