"""Regression coverage for the verification service's cleanup path.

2026-08-24 incident: ResponseVerificationService.aclose() dereferenced a
``_client`` attribute that no longer existed after the service migrated from a
private httpx client to a shared GenerationGateway. Every call raised
AttributeError. In ConversationGraphBuilder._verify_node that call sat inline
between verify_response() and the result handling, so the exception fell
through to the node's fail-open handler — meaning the LLM-as-judge verdict was
computed, paid for, and then discarded on every single customer turn, and no
response could ever fail verification.
"""

import pytest

from orchestrator.services.verification_service import ResponseVerificationService


@pytest.mark.asyncio
async def test_aclose_does_not_raise_on_a_freshly_constructed_service():
    """__init__ never sets _client; aclose() must still be safe to call."""
    service = ResponseVerificationService(api_key="test-key")

    await service.aclose()  # must not raise AttributeError


@pytest.mark.asyncio
async def test_aclose_is_idempotent():
    service = ResponseVerificationService(api_key="test-key")

    await service.aclose()
    await service.aclose()


@pytest.mark.asyncio
async def test_async_context_manager_exit_does_not_raise():
    """__aexit__ delegates to aclose(); expert_handler uses this path."""
    async with ResponseVerificationService(api_key="test-key") as service:
        assert service is not None


@pytest.mark.asyncio
async def test_aclose_closes_and_clears_a_client_when_one_exists():
    """The close path still works if a client is ever reintroduced."""

    class _FakeClient:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    service = ResponseVerificationService(api_key="test-key")
    fake = _FakeClient()
    service._client = fake

    await service.aclose()

    assert fake.closed is True
    assert service._client is None


# ---------------------------------------------------------------------------
# The node-level contract: a cleanup failure must not flip the verdict.
# _verify_node touches no instance state, so it can be exercised without
# building the builder's full dependency graph.
# ---------------------------------------------------------------------------


class _BrokenCleanupService:
    """Mimics the shipped bug: verdict computes fine, cleanup explodes."""

    def __init__(self, *args, **kwargs):
        pass

    async def verify_response(self, **kwargs):
        from orchestrator.services.verification_service import VerificationResult

        return VerificationResult(
            passed=False,
            feedback="Response exposes internal tool names.",
            categories=["internal_leak"],
        )

    async def aclose(self):
        raise AttributeError("'ResponseVerificationService' object has no attribute '_client'")


@pytest.mark.asyncio
async def test_failed_verdict_survives_a_broken_cleanup(monkeypatch):
    from orchestrator.graphs.conversation_graph import ConversationGraphBuilder

    monkeypatch.setattr(
        "orchestrator.services.verification_service.ResponseVerificationService",
        _BrokenCleanupService,
    )

    builder = object.__new__(ConversationGraphBuilder)
    state = {
        "final_response": "Call Tool: escalate_to_support\n{}",
        "verification_instructions": "Never expose tool names.",
        "user_input": "how do I export a year of data?",
        "verification_attempt": 0,
    }

    result = await builder._verify_node(state)

    assert result["verification_passed"] is False, (
        "a failure in aclose() must not be able to convert a FAIL verdict into a PASS"
    )
    assert result["verification_categories"] == ["internal_leak"]
