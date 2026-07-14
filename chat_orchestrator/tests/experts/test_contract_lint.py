"""Formal StepContract lint: completeness + consumes_state reachability.

Phase C Task 2 attached a `StepContract` to every `@register_step(...)` call
site under `orchestrator/experts/handlers/package_generator/` (the LPP
expert). `tests/experts/test_package_generator_contracts.py` already has a
narrow completeness + spot-check test scoped to that expert. This module adds
a more formal, standalone lint with two properties Task 2's test doesn't have:

1. It discovers package_generator step names by INTROSPECTION (which module a
   registered handler function lives in) instead of a hand-maintained name
   list, so it stays correct automatically as steps are added/renamed/removed.
   Task 2's code-quality review flagged the hand-maintained-list approach in
   its own test file as a Minor maintainability concern -- this fixes that.
2. It checks not just "does every step have a contract" but "is every
   `consumes_state` key actually satisfiable" -- either produced by some
   other package_generator step, or an explicitly justified external input.

SCOPE (read before touching this file): Phase C only annotated
`package_generator` steps with `StepContract`s. Other experts
(`grids_technical_reviewer`, `ingestion_expert`, `community_detector`,
`signing`, etc.) have registered steps with NO contracts at all -- this is
expected and correct for the current phase, not a bug. Both lints below are
scoped to package_generator ONLY via `_PACKAGE_GENERATOR_MODULE_PREFIX` and
must never fail because of legitimately-uncontracted steps belonging to other
experts. Extend the prefix set (or generalize the introspection) only when a
future phase actually annotates another expert's steps.
"""

from __future__ import annotations

import inspect

import orchestrator.experts.handlers.package_generator  # noqa: F401  (registration side effect)
from orchestrator.experts.step_contracts import StepContract
from orchestrator.experts.step_registry import get_step_contract, get_step_registry

# Module path prefix used to introspect which registered steps "belong to"
# package_generator. Importing the package above (mirroring the pattern
# orchestrator/experts/handlers/__init__.py uses for all experts) triggers
# every @register_step decorator in the package as an import-time side effect.
_PACKAGE_GENERATOR_MODULE_PREFIX = "orchestrator.experts.handlers.package_generator"


def _package_generator_step_names() -> list[str]:
    """Every registered step name whose handler function is defined in a
    module under `orchestrator.experts.handlers.package_generator`.

    More robust against future step additions than a hardcoded name list
    (see module docstring) -- this walks the *actual* registry rather than a
    list someone has to remember to update.
    """
    registry = get_step_registry()
    names = []
    for name in registry.list_handlers():
        handler = registry.get_handler(name)
        module = inspect.getmodule(handler)
        if module is not None and module.__name__.startswith(_PACKAGE_GENERATOR_MODULE_PREFIX):
            names.append(name)
    return names


# Snapshot discovery + contracts ONCE at module-import (collection) time, right
# after the registration-triggering import above, rather than recomputing from
# the live global registry inside every test method.
#
# Why this matters: `get_step_registry()` returns a process-wide singleton.
# `orchestrator.experts.handlers.package_generator`'s submodules only run their
# `@register_step` decorators the FIRST time they're imported per process --
# later `import` statements are no-ops against `sys.modules`, so they can never
# re-populate a registry that's been cleared. If some other test module (e.g.
# `tests/experts/test_parameter_confirmation.py::TestRegisterStepWithoutSchema
# .setup_method`, which calls `get_step_registry().clear()` with no teardown)
# runs before this module's tests execute, any test here that re-derives names
# from the live registry would see zero steps -- a false failure with no real
# code defect behind it, entirely dependent on test collection/execution
# order (alphabetical by default, but not guaranteed under `pytest-randomly`,
# xdist worker grouping, or a differently-scoped test run).
#
# pytest imports (collects) all test modules before running any test
# function's body, so this module-level snapshot is captured before any other
# module's `setup_method`/test body -- including the `.clear()` call above,
# which only runs when that test class's tests actually execute -- can
# interfere. Do not replace these with fresh calls inside test methods.
_STEP_NAMES: tuple[str, ...] = tuple(_package_generator_step_names())
_CONTRACTS: dict[str, StepContract] = {name: get_step_contract(name) for name in _STEP_NAMES}


class TestPackageGeneratorContractCompleteness:
    """Part 1: every package_generator step has a non-None StepContract."""

    def test_discovery_finds_package_generator_steps(self):
        # Guards against a silent introspection failure (e.g. wrong module
        # prefix, or the registration import not actually firing) quietly
        # passing the completeness check below with zero names discovered.
        # Uses the module-level snapshot (see comment above _STEP_NAMES) so
        # this can't be poisoned by another test file clearing the live
        # registry after collection.
        assert _STEP_NAMES, (
            "No package_generator step names discovered via introspection -- "
            "check that importing orchestrator.experts.handlers.package_generator "
            "still triggers @register_step registration, and that "
            "_PACKAGE_GENERATOR_MODULE_PREFIX still matches the handler module path."
        )

    def test_discovered_step_count_matches_known_baseline(self):
        # Cross-check against Task 2's own count (test_package_generator_contracts.py
        # asserts 17 via a hardcoded list). If this ever drifts from that file,
        # one of the two discovery mechanisms is wrong and needs a look.
        assert len(_STEP_NAMES) == 17

    def test_every_discovered_step_has_a_contract(self):
        missing = [name for name, contract in _CONTRACTS.items() if contract is None]
        assert not missing, f"package_generator steps missing a StepContract: {missing}"


# --- Part 2: consumes_state reachability ------------------------------------
#
# Built empirically: the check below collects every consumes_state key across
# all package_generator contracts that is NOT produced by any package_generator
# step's produces_state. Each key in that raw output is investigated by reading
# the actual handler source and classified as either:
#
#   (a) a legitimate external input -- something that genuinely comes from
#       packet_inputs, a user-confirmation override, or other system-level
#       plumbing no step "produces" per se (allowlist it here with a citation), or
#   (b) a mis-scoped key that actually belongs in `optional_consumes_state`
#       (see step_contracts.py) -- i.e. the handler reads it via
#       context.get_state(...) with genuine in-body fallback logic and
#       functions correctly without it, so it isn't a hard requirement at all
#       (fix the contract, don't allowlist it), or
#   (c) a genuine gap needing a real fix (add a producer, or supply the value
#       another way).
#
# A prior audit (Phase D) found that EVERY key formerly listed below fell into
# bucket (b): each was read via context.get_state(...) with a genuine
# fallback/default in the handler body (idempotency guards, "or <default>"
# reads, get_previous_result()-then-state fallback chains, or dead/vestigial
# keys nothing ever writes) and has been moved to that step's
# `optional_consumes_state` instead. `optional_consumes_state` is deliberately
# NOT reachability-checked here (see class docstring below) -- "optional and
# possibly never produced" is the entire point of that field, not a gap to
# flag. As a result this allowlist is currently empty. It is kept (rather than
# deleted) as a landing spot for any future bucket-(a) key, and
# `test_allowlist_has_no_stale_entries` below keeps it honest if one is added
# and later becomes producible.
_EXTERNAL_INPUT_ALLOWLIST: dict[str, str] = {}


class TestConsumesStateReachability:
    """Part 2: every consumes_state key is either producible or justified.

    Deliberately scoped to `consumes_state` only -- `optional_consumes_state`
    (see step_contracts.py) is NOT reachability-checked, and that's by design,
    not an oversight. `optional_consumes_state` documents keys a step reads
    opportunistically with its own in-body fallback logic; "this key might
    never be produced by any step" is exactly the expected, healthy case for
    that field, not a gap worth flagging. Reachability only matters for
    `consumes_state`, where an unproducible key means `validate_step_prerequisites`
    can never report the step as satisfied -- a real bug (see the module
    docstring above and the Phase D fix that emptied `_EXTERNAL_INPUT_ALLOWLIST`).
    """

    def test_every_consumes_state_key_is_reachable_or_allowlisted(self):
        # Uses the module-level _CONTRACTS snapshot (see comment above
        # _STEP_NAMES) rather than recomputing from the live registry, so a
        # `.clear()` call by another test file after collection can't produce
        # a false "0 steps discovered" failure here.
        produced: set[str] = set()
        for contract in _CONTRACTS.values():
            produced.update(contract.produces_state)

        unexplained = []
        for step_name, contract in _CONTRACTS.items():
            for key in contract.consumes_state:
                if key in produced:
                    continue
                if key in _EXTERNAL_INPUT_ALLOWLIST:
                    continue
                unexplained.append((step_name, key))

        assert not unexplained, (
            "consumes_state keys with no producing package_generator step AND no "
            "allowlist justification (add a citation to _EXTERNAL_INPUT_ALLOWLIST "
            f"in this file, or fix the underlying contract): {unexplained}"
        )

    def test_allowlist_has_no_stale_entries(self):
        # Keeps the empirical investigation honest as contracts evolve: if a
        # later change makes an allowlisted key producible by some step (or
        # removes the consuming step entirely), the allowlist entry is no
        # longer doing anything and should be deleted rather than silently
        # kept around.
        produced: set[str] = set()
        all_consumed: set[str] = set()
        for contract in _CONTRACTS.values():
            produced.update(contract.produces_state)
            all_consumed.update(contract.consumes_state)

        stale = [
            key for key in _EXTERNAL_INPUT_ALLOWLIST if key in produced or key not in all_consumed
        ]
        assert not stale, f"Allowlist entries no longer needed (remove them): {stale}"
