import pytest

from shared.utils.error_messages import (
    ErrorCategory,
    categorize_error,
    get_user_message,
    is_llm_billing_block,
)

# What Gemini returns once the prepaid balance behind the API key is gone.
GEMINI_PREPAY_402 = (
    "402 RESOURCE_EXHAUSTED. {'error': {'code': 402, 'message': "
    "'Your prepayment credits are depleted. Please go to AI Studio at "
    "https://ai.studio/projects to manage your project and billing. "
    "Learn more at https://ai.google.dev/gemini-api/docs/billing#prepay. ', "
    "'status': 'RESOURCE_EXHAUSTED'}}"
)


def test_backend_invalid_argument_error_is_system_not_rephrase():
    """Regression test: a Gemini "400 INVALID_ARGUMENT" (bad generationConfig,
    unsupported model field, etc.) is a backend/config bug, not something the
    user can fix by rephrasing their message. The old broad "invalid" substring
    match mislabeled this as REPHRASE, showing users "I had trouble
    understanding that format" for every single message regardless of content.
    """
    error = RuntimeError(
        "400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': "
        "\"Unable to submit request because it has an invalid value.\", "
        "'status': 'INVALID_ARGUMENT'}}"
    )

    category, _ = categorize_error(error)

    assert category == ErrorCategory.SYSTEM


def test_unsupported_provider_config_error_is_system_not_rephrase():
    error = ValueError("Unsupported LLM_PROVIDER='bogus'; expected 'gemini' or 'openrouter'")

    category, _ = categorize_error(error)

    assert category == ErrorCategory.SYSTEM


def test_genuine_parse_failure_is_still_rephrase():
    error = ValueError("Could not parse JSON response from model")

    category, message = categorize_error(error)

    assert category == ErrorCategory.REPHRASE
    assert message == "I had trouble understanding that format. Could you rephrase it?"


def test_malformed_json_is_still_rephrase():
    error = ValueError("malformed json in request body")

    category, _ = categorize_error(error)

    assert category == ErrorCategory.REPHRASE


def test_context_length_exceeded_gets_actionable_message_not_generic():
    """Regression test: a Gemini 400 for exceeding the token limit (e.g. from a
    tool result that's too large) used to fall through to the generic "something
    went wrong" message, giving no hint that a narrower query would help.
    """
    error = RuntimeError(
        "400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': "
        "'The input token count exceeds the maximum number of tokens allowed "
        "1048576.', 'status': 'INVALID_ARGUMENT'}}"
    )

    category, message = categorize_error(error)

    assert category == ErrorCategory.SYSTEM
    assert "too much data" in message


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(GEMINI_PREPAY_402),
        # Earlier Gemini releases sent the same depleted-balance text under 429.
        RuntimeError(GEMINI_PREPAY_402.replace("402", "429")),
        RuntimeError("HTTP/1.1 402 Payment Required"),
        RuntimeError("Your prepay credits are depleted. Buy credits to continue."),
        GEMINI_PREPAY_402,
    ],
)
def test_llm_billing_block_is_recognised(error):
    assert is_llm_billing_block(error) is True


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(
            "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
            "'Quota exceeded for metric generate_content_requests per minute.', "
            "'status': 'RESOURCE_EXHAUSTED'}}"
        ),
        RuntimeError("400 INVALID_ARGUMENT. {'error': {'code': 400}}"),
        RuntimeError("Invoice 402 not found"),
        RuntimeError("request timed out"),
        None,
        "",
    ],
)
def test_other_errors_are_not_llm_billing_blocks(error):
    assert is_llm_billing_block(error) is False


# Nothing about money reaches a customer: they cannot act on it and it is not
# theirs to know. Checked on every customer-facing LLM-outage message.
_BILLING_WORDS = ("billing", "credit", "payment", "prepay", "balance", "quota", "ai studio", "402")


def _assert_no_billing_language(message):
    leaked = [word for word in _BILLING_WORDS if word in message.lower()]
    assert not leaked, f"{leaked} leaked into: {message}"


def test_llm_billing_block_is_a_temporary_issue_not_the_generic_error():
    category, message = categorize_error(RuntimeError(GEMINI_PREPAY_402))

    assert category == ErrorCategory.TRANSIENT
    assert "temporary" in message.lower()
    assert message != get_user_message(ErrorCategory.SYSTEM, "internal_error")
    _assert_no_billing_language(message)


def test_llm_billing_block_sent_as_429_is_not_mistaken_for_a_rate_limit():
    """Retrying "in a few moments" cannot fix a billing block."""
    _, message = categorize_error(RuntimeError(GEMINI_PREPAY_402.replace("402", "429")))

    assert message == get_user_message(ErrorCategory.TRANSIENT, "llm_unavailable")
    assert message != get_user_message(ErrorCategory.TRANSIENT, "rate_limit")


def test_ordinary_rate_limit_keeps_the_high_traffic_message():
    error = RuntimeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
        "'Quota exceeded for metric generate_content_requests per minute.', "
        "'status': 'RESOURCE_EXHAUSTED'}}"
    )

    category, message = categorize_error(error)

    assert category == ErrorCategory.TRANSIENT
    assert message == get_user_message(ErrorCategory.TRANSIENT, "rate_limit")


def test_plain_llm_outage_message_promises_no_human_follow_up():
    """Shown when nothing was escalated (staff, escalation unavailable): it must
    not claim support has the message, because then nobody is coming."""
    message = get_user_message(ErrorCategory.TRANSIENT, "llm_unavailable")

    assert "temporary" in message.lower()
    assert "escalat" not in message.lower()
    assert "support" not in message.lower()
    _assert_no_billing_language(message)


def test_escalated_llm_outage_message_says_so_and_stays_neutral():
    message = get_user_message(ErrorCategory.ESCALATION, "llm_unavailable")

    assert "temporary" in message.lower()
    assert "escalated" in message.lower()
    _assert_no_billing_language(message)
