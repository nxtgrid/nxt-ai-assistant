"""Tier -> model resolution reads exactly MODEL_THINKING/MODEL_FAST/MODEL_LITE."""

import pytest

from shared.llm.model_tiers import TIER_ENV_VARS, resolve_model


def test_tier_env_vars_are_exactly_the_three_tiers():
    assert set(TIER_ENV_VARS) == {"thinking", "fast", "lite"}
    assert TIER_ENV_VARS["thinking"] == "MODEL_THINKING"
    assert TIER_ENV_VARS["fast"] == "MODEL_FAST"
    assert TIER_ENV_VARS["lite"] == "MODEL_LITE"


def test_resolve_model_reads_the_right_env_var(monkeypatch):
    monkeypatch.setenv("MODEL_THINKING", "gemini-pro-latest")
    monkeypatch.setenv("MODEL_FAST", "gemini-flash-latest")
    monkeypatch.setenv("MODEL_LITE", "gemini-2.5-flash-lite")
    assert resolve_model("thinking") == "gemini-pro-latest"
    assert resolve_model("fast") == "gemini-flash-latest"
    assert resolve_model("lite") == "gemini-2.5-flash-lite"


def test_resolve_model_rejects_unknown_tier():
    with pytest.raises(ValueError, match="unknown tier"):
        resolve_model("medium")


def test_resolve_model_raises_on_unset_env_var(monkeypatch):
    monkeypatch.delenv("MODEL_LITE", raising=False)
    with pytest.raises(RuntimeError, match="MODEL_LITE"):
        resolve_model("lite")
