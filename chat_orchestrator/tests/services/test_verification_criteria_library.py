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
from shared.prompts import PromptLibrary


def test_get_verification_criteria_returns_the_bundled_default():
    """Compares against a bare PromptLibrary(), not shared.prompts.PROMPTS:
    _get_verification_criteria() is `return PROMPTS.text("verification.criteria")`
    verbatim, so comparing against that same live singleton would be
    circular -- it would pass no matter what the resolved text says,
    including a live DB/GDoc override picked up from real credentials in the
    environment (e.g. a chat_orchestrator/.env copied in for unrelated
    reasons; verification.criteria is one of the historically
    Google-Doc-driven prompts, so this isn't hypothetical). Comparing against
    a bare, always-bundled library instead makes this test actually prove
    what its name claims -- that the function's output matches the committed
    bundled default -- regardless of environment."""
    criteria = _get_verification_criteria()
    assert criteria.strip()
    assert criteria == PromptLibrary().text("verification.criteria")


def test_get_verification_criteria_has_no_local_cache_global():
    """The old _verification_criteria_cache had no TTL and the admin
    'Reload cache' button couldn't clear it. GDocStore now owns the one
    cache; there must be nothing left here to go stale."""
    import orchestrator.api.app as app_module

    assert not hasattr(app_module, "_verification_criteria_cache")
