"""Every prompt in the library parses, and the protected set stays protected."""

import pytest

from shared.prompts import PROMPTS

NON_OVERRIDABLE = {
    "ticketing.correlation",
}

# Historically Google-Doc-driven (VERIFICATION_DOC_ID) with no bundled
# fallback at all; kept overridable so the doc keeps working exactly as
# before (Phase 1 parity), even though verification is safety-sensitive.
OVERRIDABLE = {
    "verification.criteria",
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


@pytest.mark.parametrize("prompt_id", sorted(NON_OVERRIDABLE))
def test_protected_prompts_are_not_overridable(prompt_id):
    assert PROMPTS.spec(prompt_id).overridable is False


@pytest.mark.parametrize("prompt_id", sorted(OVERRIDABLE))
def test_doc_driven_prompts_stay_overridable(prompt_id):
    assert PROMPTS.spec(prompt_id).overridable is True


def test_customer_system_still_has_a_system_instructions_section():
    assert PROMPTS.render("customer.system").system_text.strip()


def test_staff_system_still_has_a_system_instructions_section():
    assert PROMPTS.render("staff.system").system_text.strip()


def test_ticketing_correlation_isolates_only_the_system_instructions_block():
    """The correlator only ever reads the 'system_instructions' key: the
    original file's Root Cause Rules / Component Taxonomy / Examples H1
    sections were parsed but never sent to the LLM. Preserve that."""
    rendered = PROMPTS.render("ticketing.correlation")
    assert "Root Cause Rules" not in rendered.system_text
    assert "grouping an incoming infrastructure alert" in rendered.system_text
