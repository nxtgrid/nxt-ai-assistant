"""Verification criteria now source from the shared prompt library, with no
env-var "not configured" gate and no local module-level cache of their own.

Removing the VERIFICATION_DOC_ID fail-closed gate was a deliberate,
user-directed change: previously, an unset env var made verification block
outgoing messages outright. Now verification.criteria always resolves (DB
override, then Google Doc, then a real bundled default), so "not configured"
is no longer a state that exists here -- the remaining fail-closed paths are
an empty resolved body, an LLM error, or any other exception, all still
intact.
"""

from orchestrator.api.app import _get_verification_criteria
from shared.prompts import PROMPTS


def test_get_verification_criteria_returns_the_bundled_default():
    """No CHAT_DB_URL / doc binding is configured in the test environment,
    so this resolves to the bundled prompt body -- proving the function
    reaches the library rather than returning "" or raising."""
    criteria = _get_verification_criteria()
    assert criteria.strip()
    assert criteria == PROMPTS.text("verification.criteria")


def test_get_verification_criteria_has_no_local_cache_global():
    """The old _verification_criteria_cache had no TTL and the admin
    'Reload cache' button couldn't clear it. GDocStore now owns the one
    cache; there must be nothing left here to go stale."""
    import orchestrator.api.app as app_module

    assert not hasattr(app_module, "_verification_criteria_cache")
