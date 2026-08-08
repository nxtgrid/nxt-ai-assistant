"""Tests for the versioned correlation policy and operational context."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing import correlation_rules


class TestGetCorrelationInstructions:
    def test_correlation_instructions_are_loaded_from_the_prompt_library(self, monkeypatch):
        from shared.prompts.types import PromptSource, RenderedPrompt

        monkeypatch.setattr(
            correlation_rules.PROMPTS,
            "render",
            lambda prompt_id: RenderedPrompt(
                prompt_id=prompt_id,
                system_text="from bundled file",
                context_text=None,
                source=PromptSource.BUNDLED,
                version=None,
                checksum="c",
            ),
        )

        sections = correlation_rules.get_correlation_instructions()

        assert sections == {"system_instructions": "from bundled file"}

    def test_the_ticketing_correlation_prompt_requires_eng_to_publish(self):
        """Drafting a grouping-rule change is open to ops/eng, but only eng
        can publish one live -- the guarantee this module's docstring used
        to describe (a PR-only lock) is now a permission gate enforced by
        the prompt spec's access.publish, not a fallback-of-a-fallback here.
        See docs/superpowers/specs/2026-08-08-prompt-permissions-design.md."""
        from shared.prompts import PROMPTS

        spec = PROMPTS.spec("ticketing.correlation")
        assert spec.overridable is True
        assert sorted(spec.access.edit) == ["eng", "ops"]
        assert spec.access.publish == ["eng"]

    def test_uses_real_bundled_file_by_default(self):
        """Sanity check against the actual shipped file (no monkeypatching) --
        confirms alert_correlation_instructions.md exists and parses."""
        sections = correlation_rules.get_correlation_instructions()

        assert "system_instructions" in sections
        assert len(sections["system_instructions"]) > 0


class TestGetRagContext:
    @pytest.mark.asyncio
    async def test_correlation_never_requests_rag_context_without_a_versioned_policy_hook(
        self, monkeypatch
    ):
        monkeypatch.setenv("rag__enabled", "true")
        calls: List[Dict[str, Any]] = []

        class _FakeRagProvider:
            async def retrieve_as_text(self, **kwargs):
                calls.append(kwargs)
                return ["deployment-supplied context"]

        assert await correlation_rules.get_rag_context(
            "MPPT Q7II", rag_provider=_FakeRagProvider()
        ) == []
        assert calls == []


class TestCorrelationPolicy:
    def test_default_policy_versions_the_existing_safety_bounds(self):
        policy = correlation_rules.DEFAULT_CORRELATION_POLICY

        assert policy.confidence_floor == 0.75
        assert policy.llm_timeout_seconds == 12
        assert policy.open_candidate_window_hours == 168
        assert policy.maximum_candidate_count == 15

    def test_policy_is_frozen(self):
        with pytest.raises(FrozenInstanceError):
            correlation_rules.DEFAULT_CORRELATION_POLICY.confidence_floor = 0.1

    def test_grid_lock_budget_exceeds_llm_budget(self):
        policy = correlation_rules.DEFAULT_CORRELATION_POLICY
        assert policy.grid_lock_timeout_seconds > policy.llm_timeout_seconds


class _FakeAuthService:
    def __init__(self, facts: Optional[Dict[str, Any]] = None, error: Optional[Exception] = None):
        self.facts = facts or {}
        self.error = error
        self.calls: List[str] = []

    async def get_grid_operational_facts(self, grid_name: str) -> Dict[str, Any]:
        self.calls.append(grid_name)
        if self.error:
            raise self.error
        return self.facts


class TestGetGridOperationalContext:
    @pytest.mark.asyncio
    async def test_returns_facts_from_auth_service(self):
        fake = _FakeAuthService(facts={"is_hps_on": False, "dcu_offline_count": 3})

        result = await correlation_rules.get_grid_operational_context("Kudi", auth_service=fake)

        assert result == {"is_hps_on": False, "dcu_offline_count": 3}
        assert fake.calls == ["Kudi"]

    @pytest.mark.asyncio
    async def test_empty_dict_on_error(self):
        fake = _FakeAuthService(error=RuntimeError("db down"))

        result = await correlation_rules.get_grid_operational_context("Kudi", auth_service=fake)

        assert result == {}
