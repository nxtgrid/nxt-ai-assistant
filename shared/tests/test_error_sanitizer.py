from shared.utils.error_messages import ErrorCategory
from shared.utils.error_sanitizer import categorize_and_sanitize_error


def test_backend_invalid_argument_error_is_system_not_rephrase():
    """Same bug as error_messages.categorize_error, duplicated in this module's
    own regex: a bare "invalid" substring matched upstream API status errors
    (Gemini's "400 INVALID_ARGUMENT") and mislabeled them as a user-input
    rephrase problem instead of a backend/config failure.
    """
    error = "400 INVALID_ARGUMENT: request has an invalid value for field generationConfig"

    category, _ = categorize_and_sanitize_error(error)

    assert category == ErrorCategory.SYSTEM


def test_genuine_parse_failure_is_still_rephrase():
    category, _ = categorize_and_sanitize_error("could not parse the request")

    assert category == ErrorCategory.REPHRASE


def test_malformed_input_is_still_rephrase():
    category, _ = categorize_and_sanitize_error("malformed input received")

    assert category == ErrorCategory.REPHRASE
