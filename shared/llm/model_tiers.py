"""Resolves a prompt's named quality tier to the model id currently
configured for it.

Three tiers only, no exceptions -- thinking/fast/lite each map to one
environment variable, live-editable via anansi_app's Settings page (they're
registered in shared/config/flag_registry.py the same way GEMINI_MODEL was
before this). GEMINI_FALLBACK_MODEL is deliberately not a tier -- it's a
failure-fallback concept, renamed to FALLBACK_MODEL and read directly
wherever it's needed, not through this module.
"""

from __future__ import annotations

import os

TIER_ENV_VARS = {
    "thinking": "MODEL_THINKING",
    "fast": "MODEL_FAST",
    "lite": "MODEL_LITE",
}


def resolve_model(tier: str) -> str:
    """The model id currently configured for ``tier``.

    Raises ``ValueError`` for a tier outside the three, ``RuntimeError`` if
    the corresponding env var isn't set -- never silently falls back to an
    empty string, since that would surface as a confusing provider-side
    "model not found" error far from its actual cause.
    """
    env_var = TIER_ENV_VARS.get(tier)
    if env_var is None:
        raise ValueError(f"unknown tier {tier!r}; expected one of {sorted(TIER_ENV_VARS)}")
    value = os.getenv(env_var, "").strip()
    if not value:
        raise RuntimeError(f"{env_var} is not set; cannot resolve tier {tier!r}")
    return value
