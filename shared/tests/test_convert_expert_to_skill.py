"""Converting a prompt-only expert into a skill.

See scripts/convert_expert_to_skill.py's module docstring for why there is
no fixed CONVERTIBLE_EXPERTS allowlist: the plan this implements named five
experts as "prompt-only" (grid_analyst, grid_monitor, site_visit_tracker,
signing, community_sizing); direct inspection of the live experts.definitions
doc found that premise wrong for all five, for two different reasons (real
[function:...] workflow steps for three of them, dead wake/persistent-agent
scaffolding for the other two -- see that module's docstring for the full
finding). Eligibility here is a mechanical, per-handler check instead (Phase
7 of docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md).

Phase 11 fix (2026-08-21): `build_steps_from_workflow_lines`/`expert_to_skill`
now take an already-split `List[str]` of workflow step lines (what
`ExpertInstructionsProvider.get_expert_config(...).workflows[packet_type]`
produces for real expert execution) instead of one raw, un-split doc-section
string. The OLD signature's own numbered-list splitter silently collapsed
every currently-live expert's doc (none use numbered list syntax -- see
`main()`'s docstring note) into a single opaque step; these tests exercise
the NEW, narrower unit (per-line classification only -- `_step_dict_for_body`
via `build_steps_from_workflow_lines`) directly with already-split lines,
matching what `main()` actually feeds it now. Numbered-vs-unnumbered line
splitting and `## Packet: name` boundary detection are
ExpertInstructionsProvider's job and are tested there, not duplicated here.

Every handler these tests register (via `_cleanup_registry`, mirroring the
same-named fixture chat_orchestrator/tests/experts/test_soft_failures.py and
friends use) is synthetic, `zzz_test_*`-prefixed -- never a real production
handler. This matters here even though the converter never CALLS a handler
(only inspects its `StepContract`): a test asserting "no contract" or
"mutates" about a REAL handler name would go stale the moment a later phase
in this same plan gives that handler a contract (Phases 8-10 do exactly
that for several).
"""

import pytest

from orchestrator.experts.step_contracts import MockSpec, StepContract
from orchestrator.experts.step_registry import get_step_registry
from scripts.convert_expert_to_skill import (
    build_steps_from_workflow_lines,
    expert_to_skill,
    has_function_steps,
)


@pytest.fixture
def _cleanup_registry():
    """Mirrors chat_orchestrator/tests/experts/test_soft_failures.py's
    fixture of the same name exactly -- adds and then removes ONLY its own
    synthetic handlers, leaving the real global registry (17 real
    package_generator contracts and counting) untouched."""
    registered: list = []
    registry = get_step_registry()

    def _register(name, handler=None, contract=None):
        handler = handler or (lambda ctx: None)
        registry.register(name, handler, contract=contract)
        registered.append(name)

    yield _register

    for name in registered:
        registry.unregister(name)


def test_plain_prose_has_no_function_steps():
    assert has_function_steps("You are the Grid Analyst. Do the thing.") is False


def test_a_function_marker_is_detected():
    assert has_function_steps("Step one.\n\n[function:fetch_month_metrics]\n\nStep two.") is True


def test_a_single_line_becomes_a_one_step_skill():
    steps = build_steps_from_workflow_lines(["You are the Grid Analyst. Do the thing."])
    assert len(steps) == 1
    assert "Grid Analyst" in steps[0]["instruction"]


def test_unnumbered_lines_each_become_their_own_step():
    """The real, currently-live shape: ExpertInstructionsProvider._extract_
    workflow strips numbering before these lines ever reach this module (see
    main()'s docstring note) -- neither grids_technical_reviewer's nor
    package_generator's live doc uses numbered list syntax at all. One
    already-split line in, one step out, numbering never re-examined here."""
    lines = [
        "Analyze performance data",
        "Identify anomalies",
        "Generate recommendations",
    ]
    steps = build_steps_from_workflow_lines(lines)
    assert len(steps) == 3
    assert "Analyze performance data" in steps[0]["instruction"]
    assert "Generate recommendations" in steps[2]["instruction"]


def test_the_preamble_is_prepended_to_the_first_step():
    """Dropping 'You are the Grid Analyst' would lose the whole persona."""
    steps = build_steps_from_workflow_lines(
        ["Analyze data", "Report"], preamble="You are the Grid Analyst."
    )
    assert "Grid Analyst" in steps[0]["instruction"]
    assert "Grid Analyst" not in steps[1]["instruction"]  # only step one


def test_steps_are_indexed_from_zero():
    steps = build_steps_from_workflow_lines(["A", "B"])
    assert [s["index"] for s in steps] == [0, 1]


def test_every_llm_step_defaults_to_read_only():
    steps = build_steps_from_workflow_lines(["A", "B"])
    assert all(s["allow_write"] is False for s in steps)


def test_a_leading_llm_marker_is_stripped():
    """A small addition beyond Task 7.2's literal text: the real recipe
    parser (WorkflowExecutor._parse_step_line) strips [llm] too; this
    converter never did, leaving the literal marker text in a converted
    step's instruction."""
    steps = build_steps_from_workflow_lines(["[llm] understand_request - Parse intent"])
    assert steps[0]["instruction"] == "understand_request - Parse intent"


def test_blank_lines_are_ignored():
    steps = build_steps_from_workflow_lines(["A", "", "  ", "B"])
    assert len(steps) == 2


def test_converted_skill_starts_as_a_draft():
    skill = expert_to_skill("grid_analyst", "You are the Grid Analyst.", ["Do the thing."])
    assert skill["status"] == "draft"
    assert skill["staff_only"] is True


def test_a_skill_with_no_workflow_lines_is_refused():
    with pytest.raises(ValueError, match="no workflow steps"):
        expert_to_skill("empty_expert", "", [])


# Task 7.1: refuse only per-handler, on a real contract check -- not the old
# blanket "any [function:...] marker at all" refusal.


def test_a_contract_bearing_function_marker_converts(_cleanup_registry):
    _cleanup_registry("zzz_test_step", contract=StepContract(description="Does a thing."))
    steps = build_steps_from_workflow_lines(
        ["[function:zzz_test_step] - Do the thing"], preamble="You are a test expert."
    )
    assert len(steps) == 1
    assert steps[0]["kind"] == "function"
    assert steps[0]["handler"] == "zzz_test_step"
    assert "Do the thing" in steps[0]["instruction"]
    assert "test expert" in steps[0]["instruction"]  # preamble preserved even on step 1


def test_expert_to_skill_converts_a_contract_bearing_function_step(_cleanup_registry):
    """Realistic recipe shape: a [function:name] marker leads a workflow
    line, matching WorkflowExecutor._parse_step_line's own convention."""
    _cleanup_registry("zzz_test_step", contract=StepContract())
    skill = expert_to_skill(
        "zzz_test_expert", "", ["[function:zzz_test_step] - Get approval", "Confirm sent"]
    )
    assert skill["steps"][0]["kind"] == "function"
    assert skill["steps"][0]["handler"] == "zzz_test_step"


def test_expert_to_skill_refuses_a_handler_with_no_contract():
    """This is the real shape of grid_analyst/signing/community_sizing's
    actual doc sections at the time this plan started, not a hypothetical
    -- see this module's docstring. zzz_test_unregistered is never
    registered at all, standing in for a handler with no StepContract."""
    with pytest.raises(ValueError, match="zzz_test_unregistered"):
        expert_to_skill(
            "signing",
            "Get approval.",
            ["[function:zzz_test_unregistered]", "Confirm sent."],
        )


def test_refusal_message_names_every_unconvertible_handler(_cleanup_registry):
    _cleanup_registry("zzz_test_has_contract", contract=StepContract())
    with pytest.raises(ValueError) as exc_info:
        expert_to_skill(
            "zzz_test_expert",
            "",
            ["[function:zzz_test_has_contract]", "[function:zzz_test_missing]"],
        )
    message = str(exc_info.value)
    assert "zzz_test_missing" in message
    assert "zzz_test_has_contract" not in message  # only the bad one is named


def test_a_commented_out_step_never_reaches_the_contract_check(_cleanup_registry):
    """ExpertInstructionsProvider._extract_workflow drops `//`/`--`-prefixed
    lines before workflows is populated -- a disabled step naming a
    contractless handler must NOT block conversion of the steps that
    actually run. This module has no `//`-stripping of its own (deliberately
    -- see _unconvertible_function_handlers's docstring); this test pins
    that the CALLER is expected to have already excluded such lines, by
    simply never passing one."""
    _cleanup_registry("zzz_test_has_contract", contract=StepContract())
    skill = expert_to_skill("zzz_test_expert", "", ["[function:zzz_test_has_contract] - Real step"])
    assert len(skill["steps"]) == 1


def test_registered_but_contractless_handler_is_also_refused(_cleanup_registry):
    """A handler that exists (registered) but predates StepContract adoption
    is exactly as unconvertible as one that was never registered -- Phase
    4's tool machinery needs the contract either way."""
    _cleanup_registry("zzz_test_no_contract", contract=None)
    with pytest.raises(ValueError, match="zzz_test_no_contract"):
        expert_to_skill("zzz_test_expert", "", ["[function:zzz_test_no_contract]"])


def test_required_permission_alone_does_not_refuse_conversion(_cleanup_registry):
    """Task 6's permission gate is a runtime, per-caller check -- this
    design-time script must not pre-judge it."""
    _cleanup_registry("zzz_test_gated", contract=StepContract(required_permission="staff_only"))
    skill = expert_to_skill("zzz_test_expert", "", ["[function:zzz_test_gated]"])
    assert skill["steps"][0]["handler"] == "zzz_test_gated"


# mutates/mock stamping (closes the loop with Phase 5's skill_builder.py
# switch, which reads exactly these two keys off a converted step).


def test_a_mutating_handler_is_stamped_mutates_and_defaults_mock_on(_cleanup_registry):
    _cleanup_registry("zzz_test_write", contract=StepContract(mutates=True, mock=MockSpec()))
    steps = build_steps_from_workflow_lines(["[function:zzz_test_write]"])
    assert steps[0]["mutates"] is True
    assert steps[0]["mock"] is True


def test_a_non_mutating_handler_is_not_stamped(_cleanup_registry):
    _cleanup_registry("zzz_test_read", contract=StepContract(mutates=False))
    steps = build_steps_from_workflow_lines(["[function:zzz_test_read]"])
    assert "mutates" not in steps[0]
    assert "mock" not in steps[0]
