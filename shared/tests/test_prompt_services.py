"""Service prompts are in the library with the intended override policy."""

import pytest

from shared.prompts import PromptLibrary

# A bare PromptLibrary(), not the shared.prompts.PROMPTS singleton: PROMPTS
# resolves DB/GDoc overrides whenever real credentials happen to be in the
# environment (e.g. a chat_orchestrator/.env copied into a worktree for
# unrelated reasons), which would make these content assertions check
# whatever's live instead of the bundled file being tested. See
# chat_orchestrator/tests/test_prompt_parity.py and this repo's CLAUDE.md
# ("A local .env with real credentials makes some tests silently
# non-hermetic").
PROMPTS = PromptLibrary()

OVERRIDABLE = {"conversation.summarize", "procedure.suggest"}

# Unlocked (was overridable: false) but with no ops/eng grant added -- only
# an admin can edit/publish these, via access.py's is_prompt_admin() bypass.
# See docs/superpowers/specs/2026-08-08-prompt-permissions-design.md.
ADMIN_ONLY = {
    "context_filter.relevance",
    "thread_assignment.classify",
    "intent_router.route",
    "procedure.match",
    "verification.sanitize",
    "verification.sanitize_system",
}


@pytest.mark.parametrize("prompt_id", sorted(OVERRIDABLE | ADMIN_ONLY))
def test_service_prompt_exists(prompt_id):
    assert prompt_id in PROMPTS.ids()


@pytest.mark.parametrize("prompt_id", sorted(ADMIN_ONLY))
def test_admin_only_service_prompts_have_no_team_grants(prompt_id):
    spec = PROMPTS.spec(prompt_id)
    assert spec.overridable is True
    assert spec.access.edit == []
    assert spec.access.publish == []


@pytest.mark.parametrize("prompt_id", sorted(OVERRIDABLE))
def test_ops_editable_service_prompts_are_overridable(prompt_id):
    assert PROMPTS.spec(prompt_id).overridable is True


def test_context_filter_json_example_has_single_braces():
    text = PROMPTS.text(
        "context_filter.relevance",
        incoming_message="hi",
        formatted_candidates="0: user - hello",
    )
    assert '{"relevant_indices": [0, 1, 2], "confidence": 0.85}' in text
    assert "{{" not in text


def test_thread_assignment_json_example_has_single_braces():
    text = PROMPTS.text(
        "thread_assignment.classify",
        threads_text="Thread t1:\n  user: hi",
        user_input="follow up",
    )
    assert '{"thread_id": "<thread_id or NEW>"' in text
    assert "{{" not in text
    assert "follow up" in text


def test_intent_router_json_example_has_single_braces():
    text = PROMPTS.text(
        "intent_router.route",
        now_str="2026-07-30 12:00 UTC",
        user_input_repr=repr("build me an lpp for site X"),
    )
    assert '"should_route_to_expert": false' in text
    assert "{{" not in text
    assert "'build me an lpp for site X'" in text


def test_verification_sanitize_json_example_has_single_braces():
    text = PROMPTS.text(
        "verification.sanitize", response_text="internal step foo() failed", context="LPP expert"
    )
    assert '"has_technical_details": true or false' in text
    assert "{{" not in text
    assert "internal step foo() failed" in text
    assert "LPP expert" in text


def test_verification_sanitize_system_has_no_variables():
    spec = PROMPTS.spec("verification.sanitize_system")
    assert spec.variables == []
    text = PROMPTS.text("verification.sanitize_system")
    assert "response quality checker" in text


def test_conversation_summarize_renders_with_messages_variable():
    text = PROMPTS.text("conversation.summarize", messages="user: hi\nassistant: hello")
    assert "user: hi" in text


def test_procedure_suggest_renders_all_variables():
    text = PROMPTS.text(
        "procedure.suggest",
        next_number=3,
        existing_list="- Procedure 1: Foo",
        content="support example text",
    )
    assert "Procedure 3" in text
    assert "Procedure 1: Foo" in text
    assert "support example text" in text


def test_procedure_match_renders_both_variables():
    text = PROMPTS.text(
        "procedure.match",
        procedure_descriptions="PROCEDURE 1: Foo",
        content="support example text",
    )
    assert "PROCEDURE 1: Foo" in text
    assert "support example text" in text
