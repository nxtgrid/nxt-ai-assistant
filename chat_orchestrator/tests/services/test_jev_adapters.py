"""Offline behavior tests for the optional OpenRouter Decisions route."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.models.schemas import ConversationMessage, FunctionCall, ToolCallResult
from orchestrator.services.context_filter import ContextFilterService
from orchestrator.services.thread_assignment import ThreadAssignmentService, classify_issue_type
from orchestrator.services.ticketing.update_render import classify_significance
from orchestrator.services.verification_service import ResponseVerificationService
from shared.llm.openrouter_decisions import (
    DecisionContractError,
    DecisionResponse,
    OpenRouterDecisionClient,
)


@pytest.fixture
def jev_enabled(monkeypatch):
    monkeypatch.setenv("JEV_DECISIONS_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "example-key")
    monkeypatch.setenv("JEV_DECISIONS_MODEL", "~typesafe/jev-latest")


@pytest.mark.asyncio
async def test_issue_type_uses_jev_choice(jev_enabled, monkeypatch):
    decide = AsyncMock(
        return_value=DecisionResponse(
            {
                "answers": {
                    "issue_type": {
                        "type": "choice",
                        "choice": "meter",
                        "confidence": 0.99,
                        "probabilities": {
                            "token": 0,
                            "hps": 0,
                            "meter": 0.99,
                            "transaction": 0,
                            "commissioning": 0,
                            "other": 0.01,
                        },
                    }
                }
            }
        )
    )
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    assert await classify_issue_type("Example meter display is blank") == "meter"
    assert decide.await_count == 1


@pytest.mark.asyncio
async def test_significant_comment_uses_jev_noul(jev_enabled, monkeypatch):
    decide = AsyncMock(
        return_value=DecisionResponse({"answers": {"significant": {"type": "noul", "noul": 0.99}}})
    )
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    gateway = SimpleNamespace(generate=AsyncMock())
    assert (
        await classify_significance(gateway, "lite", "The outage was resolved and supply restored")
        is True
    )
    gateway.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_verifier_fast_passes_only_clear_reply(jev_enabled, monkeypatch):
    names = (
        "missing_answer",
        "unsupported_fact",
        "internal_detail",
        "guessing",
        "tone_or_clarity",
        "safety",
        "criteria_violation",
    )
    decide = AsyncMock(
        return_value=DecisionResponse(
            {"answers": {name: {"type": "noul", "noul": 0.01} for name in names}}
        )
    )
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    service = ResponseVerificationService(
        api_key="example-key", gateway=SimpleNamespace(generate=AsyncMock())
    )
    result = await service.verify_response("Can you help?", "Yes, I can help.", "Be clear.")
    assert result.passed is True
    service._gateway.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_thread_choice_uses_jev(jev_enabled, monkeypatch):
    decide = AsyncMock(
        return_value=DecisionResponse(
            {
                "answers": {
                    "thread": {
                        "type": "choice",
                        "choice": "thread_a",
                        "confidence": 0.98,
                        "probabilities": {"thread_a": 0.98, "thread_b": 0.01, "NEW": 0.01},
                    }
                }
            }
        )
    )
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    history = [
        ConversationMessage(role="user", content="Meter issue", thread_id="thread_a"),
        ConversationMessage(role="user", content="Payment issue", thread_id="thread_b"),
    ]
    result = await ThreadAssignmentService().assign_thread("Back to the meter issue", history)
    assert result.thread_id == "thread_a"
    assert result.method == "jev"


@pytest.mark.asyncio
async def test_context_filter_batches_relevance_questions(jev_enabled, monkeypatch):
    decide = AsyncMock(
        return_value=DecisionResponse(
            {
                "answers": {
                    "candidate_0": {"type": "noul", "noul": 0.95},
                    "candidate_1": {"type": "noul", "noul": 0.02},
                }
            }
        )
    )
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    service = ContextFilterService(api_key="fake", gateway=SimpleNamespace(generate=AsyncMock()))
    messages = [
        ConversationMessage(role="user", content="Meter fault"),
        ConversationMessage(role="user", content="Unrelated payment"),
    ]
    result = await service.filter_history("What about the meter?", messages)
    assert result.relevant_indices == [0]
    assert set(decide.await_args.kwargs["questions"]) == {"candidate_0", "candidate_1"}


@pytest.mark.asyncio
async def test_significance_falls_back_to_legacy_on_http_402(jev_enabled, monkeypatch):
    monkeypatch.setattr(
        OpenRouterDecisionClient,
        "decide",
        AsyncMock(side_effect=DecisionContractError("Decisions HTTP 402")),
    )
    gateway = SimpleNamespace(
        generate=AsyncMock(return_value=SimpleNamespace(text='{"significant": true}'))
    )
    assert (
        await classify_significance(gateway, "lite", "The outage was resolved and supply restored")
        is True
    )
    gateway.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_verifier_uses_legacy_feedback_when_jev_flags_risk(jev_enabled, monkeypatch):
    names = (
        "missing_answer",
        "unsupported_fact",
        "internal_detail",
        "guessing",
        "tone_or_clarity",
        "safety",
        "criteria_violation",
    )
    answers = {name: {"type": "noul", "noul": 0.01} for name in names}
    answers["internal_detail"] = {"type": "noul", "noul": 0.8}
    monkeypatch.setattr(
        OpenRouterDecisionClient,
        "decide",
        AsyncMock(return_value=DecisionResponse({"answers": answers})),
    )
    gateway = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                text='{"passed": false, "feedback": "Remove the internal tool name", "categories": ["sensitive_info"]}'
            )
        )
    )
    service = ResponseVerificationService(api_key="example-key", gateway=gateway)
    result = await service.verify_response(
        "What happened?", "The internal tool failed.", "Be clear."
    )
    assert result.passed is False
    assert result.feedback == "Remove the internal tool name"


@pytest.mark.asyncio
async def test_context_filter_falls_back_if_jev_would_drop_everything(jev_enabled, monkeypatch):
    decide = AsyncMock(
        return_value=DecisionResponse({"answers": {"candidate_0": {"type": "noul", "noul": 0.01}}})
    )
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    gateway = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(text='{"relevant_indices": [0], "confidence": 0.8}')
        )
    )
    service = ContextFilterService(api_key="fake", gateway=gateway)
    result = await service.filter_history(
        "Meter?", [ConversationMessage(role="user", content="Meter fault")]
    )
    assert result.relevant_indices == [0]
    gateway.generate.assert_awaited_once()


def test_context_fallback_gateway_uses_active_provider_credential(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("GOOGLE_API_KEY", "stale-google-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    captured = {}

    def gateway_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(generate=AsyncMock())

    monkeypatch.setattr(
        "orchestrator.services.context_filter.get_default_generation_gateway", gateway_factory
    )
    ContextFilterService(model="google/gemini-2.5-flash")
    assert captured["api_key"] is None


@pytest.mark.asyncio
async def test_verifier_never_fast_passes_truncated_inputs(jev_enabled, monkeypatch):
    decide = AsyncMock()
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    gateway = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                text='{"passed": false, "feedback": "Review full text", "categories": ["safety"]}'
            )
        )
    )
    service = ResponseVerificationService(api_key="example-key", gateway=gateway)
    result = await service.verify_response("Question?", "A" * 4001, "Be clear.")
    assert result.passed is False
    decide.assert_not_awaited()
    gateway.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_verifier_checks_available_tools_and_custom_criteria(jev_enabled, monkeypatch):
    names = (
        "missing_answer",
        "unsupported_fact",
        "internal_detail",
        "guessing",
        "tone_or_clarity",
        "safety",
        "criteria_violation",
        "tool_awareness",
    )
    answers = {name: {"type": "noul", "noul": 0.01} for name in names}
    answers["criteria_violation"] = {"type": "noul", "noul": 0.9}
    decide = AsyncMock(return_value=DecisionResponse({"answers": answers}))
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    gateway = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                text='{"passed": false, "feedback": "Follow the custom rule", "categories": ["safety"]}'
            )
        )
    )
    service = ResponseVerificationService(api_key="example-key", gateway=gateway)
    result = await service.verify_response(
        "Can you check?",
        "I cannot check.",
        "Always use the available checker.",
        available_tools=[{"name": "check_status", "description": "Check current status"}],
    )
    assert result.passed is False
    assert "tool_awareness" in decide.await_args.kwargs["questions"]
    assert "check_status" in str(decide.await_args.kwargs["state"])
    gateway.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_flag_off_and_missing_key_never_call_jev(monkeypatch):
    decide = AsyncMock()
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    gateway = SimpleNamespace(
        generate=AsyncMock(return_value=SimpleNamespace(text='{"significant": true}'))
    )
    monkeypatch.setenv("JEV_DECISIONS_ENABLED", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "example-key")
    assert await classify_significance(gateway, "lite", "Power has been restored") is True
    monkeypatch.setenv("JEV_DECISIONS_ENABLED", "true")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPEN_ROUTER_BEARER_TOKEN", raising=False)
    assert await classify_significance(gateway, "lite", "Power has been restored") is True
    decide.assert_not_awaited()
    assert gateway.generate.await_count == 2


@pytest.mark.asyncio
async def test_broadcast_jev_has_no_missing_answer_question(jev_enabled, monkeypatch):
    names = (
        "unsupported_fact",
        "internal_detail",
        "guessing",
        "tone_or_clarity",
        "safety",
        "criteria_violation",
    )
    decide = AsyncMock(
        return_value=DecisionResponse(
            {"answers": {name: {"type": "noul", "noul": 0.01} for name in names}}
        )
    )
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    gateway = SimpleNamespace(generate=AsyncMock())
    service = ResponseVerificationService(api_key="example-key", gateway=gateway)
    result = await service.verify_response(
        "", "Service resumes tomorrow.", "Be clear.", mode="broadcast"
    )
    assert result.passed is True
    assert "missing_answer" not in decide.await_args.kwargs["questions"]
    gateway.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_activity_forces_legacy_judge(jev_enabled, monkeypatch):
    decide = AsyncMock()
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    gateway = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(text='{"passed": true, "feedback": "", "categories": []}')
        )
    )
    service = ResponseVerificationService(api_key="example-key", gateway=gateway)
    result = await service.verify_response(
        "What happened?",
        "The issue is fixed.",
        "Be accurate.",
        conversation_context="TOOLS CALLED: status checker succeeded",
    )
    assert result.passed is True
    decide.assert_not_awaited()
    gateway.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_issue_type_low_confidence_uses_existing_classifier(jev_enabled, monkeypatch):
    decide = AsyncMock(
        return_value=DecisionResponse(
            {
                "answers": {
                    "issue_type": {
                        "type": "choice",
                        "choice": "meter",
                        "confidence": 0.4,
                        "probabilities": {
                            "token": 0.1,
                            "hps": 0.1,
                            "meter": 0.4,
                            "transaction": 0.1,
                            "commissioning": 0.1,
                            "other": 0.2,
                        },
                    }
                }
            }
        )
    )
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    gateway = SimpleNamespace(
        generate=AsyncMock(return_value=SimpleNamespace(text='{"issue_type": "transaction"}'))
    )
    monkeypatch.setattr(
        "orchestrator.services.thread_assignment.get_default_generation_gateway",
        lambda **_: gateway,
    )
    assert await classify_issue_type("Example payment query") == "transaction"
    gateway.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_significance_middle_band_uses_existing_classifier(jev_enabled, monkeypatch):
    monkeypatch.setattr(
        OpenRouterDecisionClient,
        "decide",
        AsyncMock(
            return_value=DecisionResponse(
                {"answers": {"significant": {"type": "noul", "noul": 0.5}}}
            )
        ),
    )
    gateway = SimpleNamespace(
        generate=AsyncMock(return_value=SimpleNamespace(text='{"significant": false}'))
    )
    assert (
        await classify_significance(gateway, "lite", "An example status update was posted") is False
    )
    gateway.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_thread_choice_uses_existing_classifier(jev_enabled, monkeypatch):
    decide = AsyncMock(
        return_value=DecisionResponse(
            {
                "answers": {
                    "thread": {
                        "type": "choice",
                        "choice": "unknown",
                        "confidence": 0.99,
                        "probabilities": {
                            "thread_a": 0.01,
                            "thread_b": 0.01,
                            "NEW": 0.01,
                            "unknown": 0.97,
                        },
                    }
                }
            }
        )
    )
    monkeypatch.setattr(OpenRouterDecisionClient, "decide", decide)
    gateway = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(text='{"thread_id": "thread_b", "confidence": 0.9}')
        )
    )
    monkeypatch.setattr(
        "orchestrator.services.thread_assignment.get_default_generation_gateway",
        lambda **_: gateway,
    )
    history = [
        ConversationMessage(role="user", content="First issue", thread_id="thread_a"),
        ConversationMessage(role="user", content="Second issue", thread_id="thread_b"),
    ]
    result = await ThreadAssignmentService().assign_thread("Second issue", history)
    assert result.thread_id == "thread_b"
    assert result.method == "llm"
    gateway.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_context_answer_uses_existing_filter(jev_enabled, monkeypatch):
    monkeypatch.setattr(
        OpenRouterDecisionClient,
        "decide",
        AsyncMock(
            return_value=DecisionResponse(
                {"answers": {"candidate_0": {"type": "noul", "noul": 1.2}}}
            )
        ),
    )
    gateway = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(text='{"relevant_indices": [0], "confidence": 0.9}')
        )
    )
    result = await ContextFilterService(api_key="example-key", gateway=gateway).filter_history(
        "Example?", [ConversationMessage(role="user", content="Example context")]
    )
    assert result.relevant_indices == [0]
    gateway.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_uncertainty_keeps_tool_pair(jev_enabled, monkeypatch):
    monkeypatch.setattr(
        OpenRouterDecisionClient,
        "decide",
        AsyncMock(
            return_value=DecisionResponse(
                {
                    "answers": {
                        "candidate_0": {"type": "noul", "noul": 0.01},
                        "candidate_1": {"type": "noul", "noul": 0.5},
                        "candidate_2": {"type": "noul", "noul": 0.01},
                    }
                }
            )
        ),
    )
    gateway = SimpleNamespace(generate=AsyncMock())
    messages = [
        ConversationMessage(role="user", content="Example question"),
        ConversationMessage(role="model", function_call=FunctionCall(name="example_tool", args={})),
        ConversationMessage(
            role="tool",
            tool_result=ToolCallResult(name="example_tool", success=True, output="Example result"),
        ),
    ]
    result = await ContextFilterService(api_key="example-key", gateway=gateway).filter_history(
        "Follow up?", messages
    )
    assert result.relevant_indices == [1, 2]
    gateway.generate.assert_not_awaited()
