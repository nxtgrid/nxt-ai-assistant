"""Typed OpenRouter Decisions API client, separate from chat completions."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from shared.utils.logging import get_logger

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
LOGGER = get_logger(__name__)


class DecisionContractError(Exception):
    """A Decisions request failed or returned an unusable answer."""


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True)
class DecisionResponse:
    payload: dict[str, Any]

    @property
    def resolved_model(self) -> str | None:
        return self.payload.get("model")

    @property
    def request_id(self) -> str | None:
        return self.payload.get("id")

    @property
    def usage(self) -> dict[str, Any]:
        value = self.payload.get("usage")
        return value if isinstance(value, dict) else {}

    def _answer(self, question_id: str, answer_type: str) -> dict[str, Any]:
        answers = self.payload.get("answers")
        answer = answers.get(question_id) if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or answer.get("type") != answer_type:
            raise DecisionContractError(f"Missing or invalid {answer_type} answer")
        return answer

    def choice(self, question_id: str, allowed_keys: set[str]) -> ChoiceAnswer:
        answer = self._answer(question_id, "choice")
        chosen = answer.get("choice")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if chosen not in allowed_keys or not isinstance(probabilities, dict):
            raise DecisionContractError("Invalid choice answer")
        if set(probabilities) != allowed_keys:
            raise DecisionContractError("Choice probabilities do not match options")
        if not all(_probability(value) for value in probabilities.values()) or not _probability(
            confidence
        ):
            raise DecisionContractError("Invalid choice probabilities")
        return ChoiceAnswer(chosen, probabilities, confidence)

    def noul(self, question_id: str) -> float:
        value = self._answer(question_id, "noul").get("noul")
        if not _probability(value):
            raise DecisionContractError("Invalid noul probability")
        return float(value)


def _probability(value: Any) -> bool:
    return (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


class OpenRouterDecisionClient:
    def __init__(
        self,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 3.0,
    ):
        self._api_key = (
            api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER_BEARER_TOKEN")
        )
        self._http = http_client
        self._owns_http = http_client is None
        self._timeout = timeout

    async def __aenter__(self) -> OpenRouterDecisionClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    async def decide(
        self,
        *,
        state: str | dict[str, Any],
        questions: dict[str, dict[str, Any]],
        model: str = "~typesafe/jev-latest",
    ) -> DecisionResponse:
        if not self._api_key:
            raise DecisionContractError("OpenRouter key is not configured")
        if not questions or not isinstance(questions, dict):
            raise DecisionContractError("Decisions questions are required")
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        started = time.monotonic()
        try:
            response = await self._http.post(
                DECISIONS_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "state": state, "questions": questions},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise DecisionContractError(f"Decisions HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, ValueError) as exc:
            raise DecisionContractError(
                f"Decisions transport or JSON error: {type(exc).__name__}"
            ) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise DecisionContractError("Invalid Decisions response")
        result = DecisionResponse(payload)
        LOGGER.info(
            "OpenRouter Decisions response id={} model={} latency_ms={} input_tokens={} cost={}",
            result.request_id,
            result.resolved_model,
            round((time.monotonic() - started) * 1000),
            result.usage.get("input_tokens"),
            result.usage.get("cost"),
        )
        return result
