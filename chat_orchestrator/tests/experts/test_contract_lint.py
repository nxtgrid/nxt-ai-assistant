"""Formal StepContract lint: completeness + consumes_state reachability.

Phase C Task 2 attached a `StepContract` to every `@register_step(...)` call
site under `orchestrator/experts/handlers/package_generator/` (the LPP
expert). `tests/experts/test_package_generator_contracts.py` already has a
narrow completeness + spot-check test scoped to that expert. This module adds
a more formal, standalone lint with two properties Task 2's test doesn't have:

1. It discovers a contracted expert's step names by INTROSPECTION (which
   module a registered handler function lives in) instead of a hand-maintained
   name list, so it stays correct automatically as steps are added/renamed/
   removed. Task 2's code-quality review flagged the hand-maintained-list
   approach in its own test file as a Minor maintainability concern -- this
   fixes that.
2. It checks not just "does every step have a contract" but "is every
   `consumes_state` key actually satisfiable" -- either produced by some
   other step belonging to the SAME expert, or an explicitly justified
   external input. Reachability never crosses expert boundaries: each expert
   runs its own packet/workflow, so a package_generator step's precondition
   can only ever be satisfied by another package_generator step, never by a
   grids_technical_reviewer one.

SCOPE (read before touching this file): `_CONTRACTED_EXPERTS` below is the
list of experts whose steps this lint actually checks -- Phase 8 of
docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md (Task 8.4)
extended it from package_generator-only to also cover
grids_technical_reviewer (GTR), generalizing what was originally a single
hardcoded `_PACKAGE_GENERATOR_MODULE_PREFIX`. Experts NOT in this dict
(`ingestion_expert`, `community_detector`, `signing`, `grid_analyst` as of
this phase, etc.) have registered steps with NO contracts at all -- this is
expected and correct for the current phase, not a bug. Add an expert to
`_CONTRACTED_EXPERTS` only when a phase actually annotates its steps (Phase 9:
package_generator's count may need updating if Task 9.2 changes it; Phase 10:
grid_analyst).
"""

from __future__ import annotations

import inspect

import orchestrator.experts.handlers  # noqa: F401  (registration side effect, every expert)
from orchestrator.experts.step_contracts import StepContract
from orchestrator.experts.step_registry import get_step_contract, get_step_registry

# expert label -> (module prefix used to introspect "belongs to this expert",
# expected contracted step count -- a cross-check against the introspection
# mechanism silently drifting, e.g. from a wrong prefix or a broken
# registration import). Importing orchestrator.experts.handlers above
# (mirroring orchestrator/experts/__init__.py's own pattern) triggers every
# @register_step decorator across every expert as an import-time side effect.
_CONTRACTED_EXPERTS: dict[str, tuple[str, int]] = {
    "package_generator": ("orchestrator.experts.handlers.package_generator", 17),
    "grids_technical_reviewer": (
        "orchestrator.experts.handlers.grids_technical_reviewer",
        9,
    ),
}


def _step_names_for_module_prefix(module_prefix: str) -> list[str]:
    """Every registered step name whose handler function is defined in a
    module under `module_prefix`.

    More robust against future step additions than a hardcoded name list
    (see module docstring) -- this walks the *actual* registry rather than a
    list someone has to remember to update.
    """
    registry = get_step_registry()
    names = []
    for name in registry.list_handlers():
        handler = registry.get_handler(name)
        module = inspect.getmodule(handler)
        if module is not None and module.__name__.startswith(module_prefix):
            names.append(name)
    return names


# Snapshot discovery + contracts ONCE at module-import (collection) time, right
# after the registration-triggering import above, rather than recomputing from
# the live global registry inside every test method.
#
# Why this matters: `get_step_registry()` returns a process-wide singleton.
# Each expert's handler submodules only run their `@register_step` decorators
# the FIRST time they're imported per process -- later `import` statements are
# no-ops against `sys.modules`, so they can never re-populate a registry
# that's been cleared. If some other test module (e.g. `tests/experts/
# test_parameter_confirmation.py::TestRegisterStepWithoutSchema.setup_method`,
# which calls `get_step_registry().clear()` with no teardown) runs before this
# module's tests execute, any test here that re-derives names from the live
# registry would see zero steps -- a false failure with no real code defect
# behind it, entirely dependent on test collection/execution order
# (alphabetical by default, but not guaranteed under `pytest-randomly`, xdist
# worker grouping, or a differently-scoped test run).
#
# pytest imports (collects) all test modules before running any test
# function's body, so this module-level snapshot is captured before any other
# module's `setup_method`/test body -- including the `.clear()` call above,
# which only runs when that test class's tests actually execute -- can
# interfere. Do not replace these with fresh calls inside test methods.
_STEP_NAMES: dict[str, tuple[str, ...]] = {
    expert: tuple(_step_names_for_module_prefix(prefix))
    for expert, (prefix, _count) in _CONTRACTED_EXPERTS.items()
}
_CONTRACTS: dict[str, dict[str, StepContract]] = {
    expert: {name: get_step_contract(name) for name in names}
    for expert, names in _STEP_NAMES.items()
}


class TestContractCompleteness:
    """Part 1: every contracted expert's steps all have a non-None StepContract."""

    def test_discovery_finds_steps_for_every_contracted_expert(self):
        # Guards against a silent introspection failure (e.g. wrong module
        # prefix, or the registration import not actually firing) quietly
        # passing the completeness check below with zero names discovered.
        # Uses the module-level snapshot (see comment above _STEP_NAMES) so
        # this can't be poisoned by another test file clearing the live
        # registry after collection.
        for expert, names in _STEP_NAMES.items():
            assert names, (
                f"No {expert} step names discovered via introspection -- check that "
                f"importing its handlers package still triggers @register_step "
                f"registration, and that its module prefix in _CONTRACTED_EXPERTS "
                f"still matches the handler module path."
            )

    def test_discovered_step_count_matches_known_baseline(self):
        # Cross-check against each expert's own hand-counted baseline (Task
        # 2's test_package_generator_contracts.py asserts 17 via a hardcoded
        # list; GTR's 9 comes from orchestrator/experts/handlers/
        # grids_technical_reviewer/__init__.py's __all__). If this ever drifts
        # from those, one of the discovery mechanisms is wrong and needs a look.
        for expert, (_prefix, expected_count) in _CONTRACTED_EXPERTS.items():
            actual = len(_STEP_NAMES[expert])
            assert actual == expected_count, (
                f"{expert}: discovered {actual} steps via introspection, expected "
                f"{expected_count} -- update _CONTRACTED_EXPERTS's count if this is "
                f"a real, reviewed step addition/removal, not a discovery bug."
            )

    def test_every_discovered_step_has_a_contract(self):
        for expert, contracts in _CONTRACTS.items():
            missing = [name for name, contract in contracts.items() if contract is None]
            assert not missing, f"{expert} steps missing a StepContract: {missing}"


# --- Part 2: consumes_state reachability ------------------------------------
#
# Built empirically: the check below collects, PER EXPERT, every consumes_state
# key across that expert's contracts that is NOT produced by any of that SAME
# expert's steps' produces_state (never checked across expert boundaries --
# see module docstring). Each key in that raw output is investigated by reading
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
# A prior audit (Phase D, package_generator only) found that EVERY key formerly
# listed below fell into bucket (b) and has been moved to
# `optional_consumes_state` instead. Phase 8's GTR contracts were written with
# this lesson already applied -- every genuinely-optional key went straight
# into `optional_consumes_state` at authoring time, and every GTR
# `consumes_state` key is producible by another GTR step (see this file's own
# reachability test) -- so the allowlist below is still empty after adding GTR.
# `optional_consumes_state` is deliberately NOT reachability-checked here (see
# class docstring below) -- "optional and possibly never produced" is the
# entire point of that field, not a gap to flag. This allowlist is kept
# (rather than deleted) as a landing spot for any future bucket-(a) key, and
# `test_allowlist_has_no_stale_entries` below keeps it honest if one is added
# and later becomes producible.
_EXTERNAL_INPUT_ALLOWLIST: dict[str, str] = {}


class TestConsumesStateReachability:
    """Part 2: every consumes_state key is either producible (within its own
    expert) or justified.

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
        unexplained = []
        for expert, contracts in _CONTRACTS.items():
            produced: set[str] = set()
            for contract in contracts.values():
                produced.update(contract.produces_state)

            for step_name, contract in contracts.items():
                for key in contract.consumes_state:
                    if key in produced:
                        continue
                    if key in _EXTERNAL_INPUT_ALLOWLIST:
                        continue
                    unexplained.append((expert, step_name, key))

        assert not unexplained, (
            "consumes_state keys with no producing step within their own expert AND "
            "no allowlist justification (add a citation to _EXTERNAL_INPUT_ALLOWLIST "
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
        for contracts in _CONTRACTS.values():
            for contract in contracts.values():
                produced.update(contract.produces_state)
                all_consumed.update(contract.consumes_state)

        stale = [
            key for key in _EXTERNAL_INPUT_ALLOWLIST if key in produced or key not in all_consumed
        ]
        assert not stale, f"Allowlist entries no longer needed (remove them): {stale}"
