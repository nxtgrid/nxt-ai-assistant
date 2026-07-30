"""Service prompts are in the library with the intended override policy."""

import pytest

from shared.prompts import PROMPTS

OVERRIDABLE = {"conversation.summarize", "procedure.suggest"}
LOCKED = {
    "context_filter.relevance",
    "thread_assignment.classify",
    "intent_router.route",
    "procedure.match",
    "verification.sanitize",
    "verification.sanitize_system",
}


@pytest.mark.parametrize("prompt_id", sorted(OVERRIDABLE | LOCKED))
def test_service_prompt_exists(prompt_id):
    assert prompt_id in PROMPTS.ids()


@pytest.mark.parametrize("prompt_id", sorted(LOCKED))
def test_locked_service_prompts_are_locked(prompt_id):
    assert PROMPTS.spec(prompt_id).overridable is False


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
