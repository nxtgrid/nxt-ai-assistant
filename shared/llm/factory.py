"""Factory helpers for the default LLM gateway implementations."""

from __future__ import annotations

import os

from shared.llm.gemini import GeminiGateway


def get_default_generation_gateway() -> GeminiGateway:
    return GeminiGateway(
        api_key=os.getenv("GOOGLE_API_KEY"),
        default_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        fallback_model=os.getenv("GEMINI_FALLBACK_MODEL"),
    )


def get_default_embedding_gateway() -> GeminiGateway:
    return GeminiGateway(api_key=os.getenv("GOOGLE_API_KEY"))
