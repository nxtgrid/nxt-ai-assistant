"""Settings and conservative provisional gates for Jev decisions."""

from __future__ import annotations

import os
import re

JEV_LATEST = "~typesafe/jev-latest"
JEV_PINNED = "typesafe/jev-1.13"


def valid_jev_model(value: str) -> bool:
    return bool(re.fullmatch(r"~?typesafe/jev-(?:latest|[0-9]+(?:\.[0-9]+)*)", value or ""))


def jev_model() -> str:
    model = os.getenv("JEV_DECISIONS_MODEL", JEV_LATEST).strip()
    if not valid_jev_model(model):
        raise ValueError("JEV_DECISIONS_MODEL must be a Jev model")
    return model


def is_jev_enabled() -> bool:
    enabled = os.getenv("JEV_DECISIONS_ENABLED", "false").lower() in {"true", "1", "yes", "on"}
    key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER_BEARER_TOKEN")
    return enabled and bool(key and key.strip())


# Conservative starting gates. Must be recalibrated on labeled examples before
# enabling in production; do not carry over thresholds from an LLM JSON answer.
CHOICE_CONFIDENCE_MIN = 0.9
NOUL_TRUE_MIN = 0.9
NOUL_FALSE_MAX = 0.1
JUDGE_FAST_PASS_MAX = 0.05
CONTEXT_DROP_MAX = 0.1
