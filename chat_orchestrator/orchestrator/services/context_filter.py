"""Context filter service for conversation history management.

Uses a single cheap LLM call (gemini-2.5-flash-lite) to decide which
candidate history messages are relevant to the incoming message.

Fail-open: if the LLM call fails, times out, or returns low confidence,
all candidate messages are kept (current behavior preserved).

Gated behind CONTEXT_FILTER_ENABLED env var (default: false).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from orchestrator.models.schemas import ConversationMessage
from shared.llm import GeminiGateway, GenerationOptions, LLMMessage
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

CONTEXT_FILTER_PROMPT = """You are a conversation relevance filter. Given an incoming message and a numbered
list of prior conversation messages, decide which history messages are relevant
for understanding or responding to the incoming message.

Include messages that:
- Discuss the same subject, entity, or topic as the incoming message
- Provide necessary context (e.g., a question the incoming message follows up on)
- Are part of the same conversational thread

Exclude messages that:
- Discuss completely unrelated topics
- Are outputs from earlier, unrelated interactions
- Are tool calls/results for different subjects

Incoming message: "{incoming_message}"

Candidate history (index: role - content):
{formatted_candidates}

Respond ONLY with JSON:
{{"relevant_indices": [0, 1, 2], "confidence": 0.85}}"""


@dataclass
class ContextFilterResult:
    """Result of context filtering."""

    relevant_indices: List[int] = field(default_factory=list)
    confidence: float = 0.0


class ContextFilterService:
    """Filters conversation history for relevance to the incoming message."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        gateway: Optional[GeminiGateway] = None,
    ) -> None:
        self._api_key = api_key or os.getenv("GOOGLE_API_KEY", "")
        self._model = model or os.getenv("VERIFICATION_MODEL", "gemini-2.5-flash-lite")
        self._gateway = gateway or GeminiGateway(
            api_key=self._api_key,
            default_model=self._model,
        )

    async def aclose(self) -> None:
        return None

    async def filter_history(
        self,
        incoming_message: str,
        candidate_messages: List[ConversationMessage],
    ) -> ContextFilterResult:
        """Filter history messages for relevance to the incoming message.

        Args:
            incoming_message: The current user message
            candidate_messages: History messages loaded from DB

        Returns:
            ContextFilterResult with relevant indices and confidence.
            On failure, returns all indices (fail-open).
        """
        if not candidate_messages:
            return ContextFilterResult()

        all_indices = list(range(len(candidate_messages)))

        if not self._api_key:
            LOGGER.debug("Context filter: no API key, returning all messages")
            return ContextFilterResult(relevant_indices=all_indices, confidence=0.0)

        # Format candidate messages for the prompt
        formatted = []
        for i, msg in enumerate(candidate_messages):
            content = (msg.content or "")[:200]
            if msg.function_call:
                content = f"[tool call: {msg.function_call.name}]"
            elif msg.tool_result:
                content = f"[tool result: {msg.tool_result.name}]"
            formatted.append(f"{i}: {msg.role} - {content}")

        formatted_candidates = "\n".join(formatted)

        prompt = CONTEXT_FILTER_PROMPT.format(
            incoming_message=incoming_message[:500],
            formatted_candidates=formatted_candidates,
        )

        try:
            result_text = await self._call_gemini(prompt)
            result = self._parse_result(result_text, len(candidate_messages))
            result = self._enforce_tool_pairs(result, candidate_messages)

            LOGGER.info(
                f"Context filter: kept {len(result.relevant_indices)}/{len(candidate_messages)} "
                f"messages, confidence={result.confidence:.2f}"
            )
            return result

        except Exception as e:
            LOGGER.warning(f"Context filter failed (fail-open, keeping all): {e}")
            return ContextFilterResult(relevant_indices=all_indices, confidence=0.0)

    async def _call_gemini(self, prompt: str) -> str:
        """Make a lightweight Gemini API call."""
        result = await self._gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(
                model=self._model,
                temperature=0.1,
                max_output_tokens=256,
                response_format="json",
            ),
        )
        return result.text

    def _parse_result(self, text: str, num_candidates: int) -> ContextFilterResult:
        """Parse JSON response from the LLM."""
        if not text:
            return ContextFilterResult(
                relevant_indices=list(range(num_candidates)),
                confidence=0.0,
            )

        try:
            # Handle markdown code fences
            clean_text = text.strip()
            if clean_text.startswith("```"):
                clean_text = clean_text.split("```")[1]
                if clean_text.startswith("json"):
                    clean_text = clean_text[4:]
                clean_text = clean_text.strip()

            data: Dict[str, Any] = json.loads(clean_text)

            relevant_indices = data.get("relevant_indices", list(range(num_candidates)))
            confidence = float(data.get("confidence", 0.0))

            # Validate indices are in range
            valid_indices = [i for i in relevant_indices if 0 <= i < num_candidates]
            if not valid_indices:
                valid_indices = list(range(num_candidates))

            return ContextFilterResult(
                relevant_indices=valid_indices,
                confidence=confidence,
            )

        except (json.JSONDecodeError, ValueError, TypeError) as e:
            LOGGER.warning(f"Failed to parse context filter response: {e}")
            return ContextFilterResult(
                relevant_indices=list(range(num_candidates)),
                confidence=0.0,
            )

    def _enforce_tool_pairs(
        self,
        result: ContextFilterResult,
        candidate_messages: List[ConversationMessage],
    ) -> ContextFilterResult:
        """Ensure function_call / tool_result pairs are kept together.

        If a function_call at index N is included, its tool_result at N+1 must also
        be included (and vice versa).
        """
        indices = set(result.relevant_indices)
        num = len(candidate_messages)

        for i in list(indices):
            msg = candidate_messages[i]
            if msg.function_call and (i + 1) < num:
                next_msg = candidate_messages[i + 1]
                if next_msg.tool_result:
                    indices.add(i + 1)
            elif msg.tool_result and i > 0:
                prev_msg = candidate_messages[i - 1]
                if prev_msg.function_call:
                    indices.add(i - 1)

        return ContextFilterResult(
            relevant_indices=sorted(indices),
            confidence=result.confidence,
        )


def is_context_filter_enabled() -> bool:
    """Check if context filter is enabled via environment variable."""
    return os.getenv("CONTEXT_FILTER_ENABLED", "false").lower() == "true"


__all__ = ["ContextFilterService", "ContextFilterResult", "is_context_filter_enabled"]
