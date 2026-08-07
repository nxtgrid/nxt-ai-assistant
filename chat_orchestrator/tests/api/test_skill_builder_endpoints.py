"""Tests for the skill builder's two support endpoints (Phase 4 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md): POST
/skills/validate (wraps skill_validation.validate_skill_steps, no LLM) and
POST /skills/summarize (wraps skill_summary.generate_skill_summary, one LLM
call). Both had no HTTP caller before this phase -- see those modules'
docstrings.

Calls the endpoint functions directly with a minimal request stub, matching
tests/api/test_notify_ticketing.py's established convention for this file's
sibling endpoint (handle_notify), rather than spinning up a full TestClient.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.api.app import (
    SkillStepPayload,
    SkillSummarizeRequest,
    SkillValidateRequest,
    summarize_skill,
    validate_skill,
)


class _FakeRequest:
    def __init__(self, headers: dict | None = None) -> None:
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")


def _authed_request() -> _FakeRequest:
    return _FakeRequest(headers={"X-Api-Key": "test-key"})


def _step(index: int, name: str, instruction: str, **extra) -> SkillStepPayload:
    return SkillStepPayload(index=index, name=name, instruction=instruction, **extra)


class TestValidateSkillEndpoint:
    @pytest.mark.asyncio
    async def test_no_auth_header_is_rejected(self):
        from fastapi import HTTPException

        body = SkillValidateRequest(steps=[])

        with pytest.raises(HTTPException) as exc_info:
            await validate_skill(_FakeRequest(), body)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_steps_return_no_errors(self):
        body = SkillValidateRequest(
            steps=[
                _step(0, "find", "List all open tickets. -> {{tickets}}", output_var="tickets"),
                _step(1, "summarize", "Summarize {{tickets}}."),
            ]
        )

        response = await validate_skill(_authed_request(), body)

        assert response.errors == []

    @pytest.mark.asyncio
    async def test_undeclared_read_surfaces_as_an_error(self):
        body = SkillValidateRequest(
            steps=[_step(0, "summarize", "Summarize {{missing_var}}.")],
        )

        response = await validate_skill(_authed_request(), body)

        assert len(response.errors) == 1
        assert response.errors[0].step_index == 0
        assert response.errors[0].severity == "error"
        assert "missing_var" in response.errors[0].message

    @pytest.mark.asyncio
    async def test_declared_inputs_are_passed_through(self):
        # A read of a declared skill input, with no earlier writing step,
        # must NOT be flagged -- proves declared_inputs actually reaches
        # validate_skill_steps rather than being silently dropped.
        body = SkillValidateRequest(
            steps=[_step(0, "summarize", "Summarize {{grid_name}}.")],
            declared_inputs=["grid_name"],
        )

        response = await validate_skill(_authed_request(), body)

        assert response.errors == []


class TestSummarizeSkillEndpoint:
    @pytest.mark.asyncio
    async def test_no_auth_header_is_rejected(self):
        from fastapi import HTTPException

        body = SkillSummarizeRequest(steps=[])

        with pytest.raises(HTTPException) as exc_info:
            await summarize_skill(_FakeRequest(), body)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_the_generated_summary(self):
        mock_gateway = SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(text="Finds and closes stale tickets."))
        )
        body = SkillSummarizeRequest(
            steps=[_step(0, "find", "List all open tickets.")], title="Ticket Cleanup"
        )

        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            return_value=mock_gateway,
        ):
            response = await summarize_skill(_authed_request(), body)

        assert response.summary == "Finds and closes stale tickets."

    @pytest.mark.asyncio
    async def test_empty_steps_returns_empty_summary_without_calling_llm(self):
        mock_gateway_factory = AsyncMock()
        body = SkillSummarizeRequest(steps=[])

        with patch(
            "orchestrator.experts.skill_summary.get_default_generation_gateway",
            mock_gateway_factory,
        ):
            response = await summarize_skill(_authed_request(), body)

        assert response.summary == ""
        mock_gateway_factory.assert_not_called()
