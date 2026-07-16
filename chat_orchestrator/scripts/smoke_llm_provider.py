#!/usr/bin/env python3
"""Smoke-test the configured generation provider.

Runs a minimal text-only generation through ``shared.llm.get_default_generation_gateway``.
It intentionally avoids orchestrator graph setup so it can be used during deploy checks.
"""

from __future__ import annotations

import asyncio
import os
import sys

from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway


async def main() -> int:
    provider = os.getenv("LLM_PROVIDER", "gemini")
    model = (
        os.getenv("OPENROUTER_MODEL")
        if provider.strip().lower() in {"openrouter", "open-router"}
        else os.getenv("GEMINI_MODEL")
    )
    gateway = get_default_generation_gateway(default_model=model)
    result = await gateway.generate(
        [LLMMessage(role="user", text="Reply with exactly: ok")],
        GenerationOptions(model=model, max_output_tokens=16, temperature=0.0),
    )
    print(f"provider={provider} model={model or 'default'} text={result.text!r}")
    return 0 if "ok" in result.text.lower() else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
