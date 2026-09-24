import json

import httpx
import pytest

from shared.llm.openrouter_decisions import DecisionContractError, OpenRouterDecisionClient


@pytest.mark.asyncio
async def test_decisions_uses_alpha_wire_contract():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.13-20260917",
                "answers": {
                    "issue": {
                        "type": "choice",
                        "choice": "meter",
                        "probabilities": {"meter": 0.9, "other": 0.1},
                        "confidence": 0.8,
                    }
                },
                "usage": {"input_tokens": 100, "output_tokens": 5, "cost": 0.00001},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = OpenRouterDecisionClient(api_key="example-key", http_client=http)
        result = await client.decide(
            state={"message": "Example fault"},
            questions={
                "issue": {
                    "type": "choice",
                    "instructions": "Which category?",
                    "criteria": {"meter": "Meter fault", "other": "Anything else"},
                }
            },
            model="~typesafe/jev-latest",
        )
    assert seen[0].url == "https://openrouter.ai/api/alpha/decisions"
    assert seen[0].headers["Authorization"] == "Bearer example-key"
    assert json.loads(seen[0].content) == {
        "model": "~typesafe/jev-latest",
        "state": {"message": "Example fault"},
        "questions": {
            "issue": {
                "type": "choice",
                "instructions": "Which category?",
                "criteria": {"meter": "Meter fault", "other": "Anything else"},
            }
        },
    }
    assert result.choice("issue", {"meter", "other"}).choice == "meter"
    assert result.resolved_model == "typesafe/jev-1.13-20260917"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"answers": {}},
        {"answers": {"issue": {"type": "noul", "noul": 0.9}}},
        {
            "answers": {
                "issue": {
                    "type": "choice",
                    "choice": "unknown",
                    "probabilities": {"unknown": 1},
                    "confidence": 1,
                }
            }
        },
        {
            "answers": {
                "issue": {
                    "type": "choice",
                    "choice": "meter",
                    "probabilities": {"meter": "nan"},
                    "confidence": 1,
                }
            }
        },
    ],
)
async def test_rejects_bad_choice(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as http:
        result = await OpenRouterDecisionClient(api_key="key", http_client=http).decide(
            state="text",
            questions={
                "issue": {
                    "type": "choice",
                    "instructions": "Category?",
                    "criteria": {"meter": "Meter fault"},
                }
            },
        )
    with pytest.raises(DecisionContractError):
        result.choice("issue", {"meter"})


@pytest.mark.asyncio
async def test_http_402_is_sanitized():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(402, text="secret customer payload")
        )
    ) as http:
        with pytest.raises(DecisionContractError, match="402") as exc:
            await OpenRouterDecisionClient(api_key="secret-key", http_client=http).decide(
                state="customer text",
                questions={"ok": {"type": "noul", "instructions": "Is it ok?"}},
            )
    assert "secret-key" not in str(exc.value)
    assert "customer" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500])
async def test_retryable_http_errors_are_reported_without_retry(status):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status, text="private response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DecisionContractError, match=str(status)):
            await OpenRouterDecisionClient(api_key="key", http_client=http).decide(
                state="private state", questions={"ok": {"type": "noul", "instructions": "Okay?"}}
            )
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_transport_timeout_and_non_json_are_sanitized():
    def timeout(_request):
        raise httpx.ReadTimeout("private request data")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        with pytest.raises(DecisionContractError, match="ReadTimeout") as exc:
            await OpenRouterDecisionClient(api_key="key", http_client=http).decide(
                state="private state", questions={"ok": {"type": "noul", "instructions": "Okay?"}}
            )
    assert "private" not in str(exc.value)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not json"))
    ) as http:
        with pytest.raises(DecisionContractError, match="JSON"):
            await OpenRouterDecisionClient(api_key="key", http_client=http).decide(
                state="text", questions={"ok": {"type": "noul", "instructions": "Okay?"}}
            )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, True, "0.5"])
def test_noul_rejects_invalid_probability(value):
    from shared.llm.openrouter_decisions import DecisionResponse

    response = DecisionResponse({"answers": {"ok": {"type": "noul", "noul": value}}})
    with pytest.raises(DecisionContractError):
        response.noul("ok")


@pytest.mark.asyncio
async def test_primary_credential_precedes_compatibility_alias(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "primary-example-key")
    monkeypatch.setenv("OPEN_ROUTER_BEARER_TOKEN", "legacy-example-key")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"answers": {"ok": {"type": "noul", "noul": 0.5}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await OpenRouterDecisionClient(http_client=http).decide(
            state="text", questions={"ok": {"type": "noul", "instructions": "Okay?"}}
        )
    assert result.noul("ok") == 0.5
    assert seen[0].headers["Authorization"] == "Bearer primary-example-key"
