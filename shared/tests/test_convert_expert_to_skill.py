"""Converting a prompt-only expert into a skill.

See scripts/convert_expert_to_skill.py's module docstring for why there is
no fixed CONVERTIBLE_EXPERTS allowlist: the plan this implements named five
experts as "prompt-only" (grid_analyst, grid_monitor, site_visit_tracker,
signing, community_sizing); direct inspection of the live experts.definitions
doc found that premise wrong for all five, for two different reasons (real
[function:...] workflow steps for three of them, dead wake/persistent-agent
scaffolding for the other two -- see that module's docstring for the full
finding). Eligibility here is a mechanical check instead.
"""

import pytest

from scripts.convert_expert_to_skill import (
    expert_to_skill,
    has_function_steps,
    split_instructions_into_steps,
)


def test_plain_prose_has_no_function_steps():
    assert has_function_steps("You are the Grid Analyst. Do the thing.") is False


def test_a_function_marker_is_detected():
    assert has_function_steps("Step one.\n\n[function:fetch_month_metrics]\n\nStep two.") is True


def test_expert_to_skill_refuses_text_with_a_function_marker():
    """This is the real shape of grid_analyst/signing/community_sizing's
    actual doc sections, not a hypothetical -- see this module's docstring."""
    with pytest.raises(ValueError, match="function steps"):
        expert_to_skill("signing", "Get approval.\n\n[function:request_sign]\n\nConfirm sent.")


def test_a_single_block_becomes_a_one_step_skill():
    steps = split_instructions_into_steps("You are the Grid Analyst. Do the thing.")
    assert len(steps) == 1
    assert "Grid Analyst" in steps[0]["instruction"]


def test_numbered_instructions_become_separate_steps():
    text = (
        "You are the Grid Analyst.\n\n"
        "1. Analyze performance data\n"
        "2. Identify anomalies\n"
        "3. Generate recommendations\n"
    )
    steps = split_instructions_into_steps(text)
    assert len(steps) == 3
    assert "Analyze performance data" in steps[0]["instruction"]
    assert "Generate recommendations" in steps[2]["instruction"]


def test_the_preamble_is_prepended_to_the_first_step():
    """Dropping 'You are the Grid Analyst' would lose the whole persona."""
    text = "You are the Grid Analyst.\n\n1. Analyze data\n2. Report\n"
    steps = split_instructions_into_steps(text)
    assert "Grid Analyst" in steps[0]["instruction"]


def test_steps_are_indexed_from_zero():
    steps = split_instructions_into_steps("1. A\n2. B\n")
    assert [s["index"] for s in steps] == [0, 1]


def test_every_step_defaults_to_read_only():
    steps = split_instructions_into_steps("1. A\n2. B\n")
    assert all(s["allow_write"] is False for s in steps)


def test_converted_skill_starts_as_a_draft():
    skill = expert_to_skill("grid_analyst", "You are the Grid Analyst. Do the thing.")
    assert skill["status"] == "draft"
    assert skill["staff_only"] is True


def test_a_skill_with_no_instruction_text_is_refused():
    with pytest.raises(ValueError, match="no instruction text"):
        expert_to_skill("empty_expert", "")
