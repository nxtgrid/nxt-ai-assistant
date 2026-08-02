from shared.utils.error_messages import ErrorCategory, categorize_error


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
