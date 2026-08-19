"""Tests for the skill builder's support endpoints:
POST /skills/validate (Phase 4, wraps skill_validation.validate_skill_steps,
no LLM), POST /skills/summarize (Phase 4, wraps
skill_summary.generate_skill_summary, one LLM call), and POST
/skills/dispatch-schedule (Phase 5, wraps
skill_schedule_dispatch.dispatch_skill_schedule -- see
docs/superpowers/plans/2026-08-06-user-designed-skills.md). All three had
no HTTP caller before their respective phase -- see those modules'
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
    SkillDispatchScheduleRequest,
    SkillStepPayload,
    SkillSummarizeRequest,
    SkillValidateRequest,
    dispatch_skill_schedule_endpoint,
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

    @pytest.mark.asyncio
    async def test_a_function_step_payload_needs_neither_name_nor_instruction(self):
        """A [function] step's SkillStepPayload predates P3 name/instruction
        as required fields -- both must now be optional, or constructing this
        payload itself raises pydantic.ValidationError before validate_skill
        is ever called. A deliberately-fake handler name (never legitimately
        exposed, unlike a real handler -- see Task 13) keeps this test about
        the payload shape parsing, not about the registry's live contents;
        a real, non-empty error list, not [], still proves it reached
        validate_skill_steps."""
        body = SkillValidateRequest(
            steps=[SkillStepPayload(index=0, kind="function", handler="not_a_real_handler")]
        )

        response = await validate_skill(_authed_request(), body)

        assert len(response.errors) == 1
        assert response.errors[0].step_index == 0
        assert "not_a_real_handler" in response.errors[0].message

    @pytest.mark.asyncio
    async def test_exposed_handlers_reach_validate_skill_steps(self):
        body = SkillValidateRequest(
            steps=[SkillStepPayload(index=0, kind="function", handler="not_a_real_handler")]
        )

        with patch(
            "orchestrator.experts.step_registry.get_step_registry"
        ) as mock_registry:
            mock_registry.return_value.builder_exposed_handlers.return_value = [
                "fetch_grafana_kpis"
            ]
            response = await validate_skill(_authed_request(), body)

        assert len(response.errors) == 1
        assert "not_a_real_handler" in response.errors[0].message


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


class TestDispatchSkillScheduleEndpoint:
    @pytest.mark.asyncio
    async def test_no_auth_header_is_rejected(self):
        from fastapi import HTTPException

        body = SkillDispatchScheduleRequest(schedule_id="sched-1")

        with pytest.raises(HTTPException) as exc_info:
            await dispatch_skill_schedule_endpoint(_FakeRequest(), body)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_delegates_to_dispatch_skill_schedule_and_returns_its_result(self):
        body = SkillDispatchScheduleRequest(schedule_id="sched-1")
        mock_dispatch = AsyncMock(
            return_value={"dispatched": 2, "skipped": 1, "failed": 0, "reason": None}
        )

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.dispatch_skill_schedule", mock_dispatch
        ):
            response = await dispatch_skill_schedule_endpoint(_authed_request(), body)

        mock_dispatch.assert_awaited_once_with("sched-1")
        assert response.dispatched == 2
        assert response.skipped == 1
        assert response.failed == 0
        assert response.reason is None
