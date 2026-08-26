"""Every prompt in the library parses, and prompts with a specific override policy keep it."""

import pytest

from shared.prompts import PromptLibrary
from shared.prompts.components import COMPONENT_LABELS

# A bare PromptLibrary(), not the shared.prompts.PROMPTS singleton: PROMPTS
# resolves DB/GDoc overrides whenever real credentials happen to be in the
# environment (e.g. a chat_orchestrator/.env copied into a worktree for
# unrelated reasons), which would make these content assertions check
# whatever's live instead of the bundled file being tested -- especially
# risky here, since customer.system/staff.system/ticketing.correlation are
# exactly the prompts most likely to carry a real live override. See
# chat_orchestrator/tests/test_prompt_parity.py and this repo's CLAUDE.md
# ("A local .env with real credentials makes some tests silently
# non-hermetic").
PROMPTS = PromptLibrary()

# Historically Google-Doc-driven (VERIFICATION_DOC_ID) with no bundled
# fallback at all; kept overridable so the doc keeps working exactly as
# before (Phase 1 parity), even though verification is safety-sensitive.
OVERRIDABLE = {
    "verification.criteria",
}

# Both unlocked for ops/eng drafting, but correlation keeps publish eng-only
# -- it feeds alert-grouping decisions and used to be overridable: false for
# that reason (see correlation_rules.py's docstring). Drafting is open to
# both; only eng can promote a correlation change to live. See
# docs/superpowers/specs/2026-08-08-prompt-permissions-design.md.
TICKETING_ACCESS = {
    "ticketing.jira_issue_types": (["eng", "ops"], ["eng", "ops"]),
    "ticketing.correlation": (["eng", "ops"], ["eng"]),
}


def test_library_is_not_empty():
    assert len(PROMPTS.ids()) >= 6


def test_every_prompt_parses_and_has_a_description():
    for prompt_id in PROMPTS.ids():
        spec = PROMPTS.spec(prompt_id)
        assert spec.description.strip(), f"{prompt_id} has an empty description"


def test_every_prompt_declares_an_owner_we_recognise():
    for prompt_id in PROMPTS.ids():
        assert PROMPTS.spec(prompt_id).owner in {"ops", "eng"}


def test_every_prompt_declares_a_component_we_recognise():
    """Every bundled prompt must land in a real admin-UI group, not the
    trailing "Uncategorized" bucket -- an unset or misspelled ``component``
    silently orphans a prompt from its natural section."""
    for prompt_id in PROMPTS.ids():
        assert PROMPTS.spec(prompt_id).component in COMPONENT_LABELS, prompt_id


@pytest.mark.parametrize("prompt_id", sorted(TICKETING_ACCESS))
def test_ticketing_prompts_have_the_expected_access(prompt_id):
    spec = PROMPTS.spec(prompt_id)
    edit, publish = TICKETING_ACCESS[prompt_id]
    assert spec.overridable is True
    assert sorted(spec.access.edit) == edit
    assert sorted(spec.access.publish) == publish


@pytest.mark.parametrize("prompt_id", sorted(OVERRIDABLE))
def test_doc_driven_prompts_stay_overridable(prompt_id):
    assert PROMPTS.spec(prompt_id).overridable is True


def test_customer_system_still_has_a_system_instructions_section():
    assert PROMPTS.render("customer.system").system_text.strip()


def test_staff_system_still_has_a_system_instructions_section():
    assert PROMPTS.render("staff.system").system_text.strip()


def test_ticketing_correlation_keeps_all_policy_in_system_instructions():
    rendered = PROMPTS.render("ticketing.correlation")
    assert "Root Cause Rules" in rendered.system_text
    assert "Failure Topology" in rendered.system_text
    assert "Component Taxonomy" in rendered.system_text
    assert "assess an incoming infrastructure alert" in rendered.system_text
    assert rendered.context_text is None
