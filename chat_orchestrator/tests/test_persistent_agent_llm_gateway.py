"""Tests for persistent-agent LLM gateway integration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.graphs import persistent_agent_graph as pag
from orchestrator.models.schemas import ToolCallResult
from orchestrator.services import tool_executor as tool_executor_module
from orchestrator.services import user_permissions as user_permissions_module
from shared.llm import (
    GenerateResult,
    GenerationOptions,
    LLMConversationState,
    LLMMessage,
    ToolCall,
    ToolResult,
    ToolSpec,
)


class FakeGenerationGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: list[LLMMessage],
        options: GenerationOptions,
        tools: list[ToolSpec] | None = None,
        tool_results: list[ToolResult] | None = None,
        conversation_state: LLMConversationState | None = None,
    ) -> GenerateResult:
        self.calls.append(
            {
                "messages": messages,
                "options": options,
                "tools": tools,
                "tool_results": tool_results,
                "conversation_state": conversation_state,
            }
        )
        if len(self.calls) == 1:
            return GenerateResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="call-read-status",
                        name="read_status",
                        args={"grid_id": "grid-1"},
                    )
                ],
                conversation_state=LLMConversationState(
                    messages=[LLMMessage(role="assistant", text="Checking status")]
                ),
            )
        return GenerateResult(text="Grid is stable.")


@pytest.mark.asyncio
async def test_think_and_act_uses_generation_gateway_for_tool_loop(monkeypatch):
    gateway = FakeGenerationGateway()

    class FailingDirectGeminiGateway:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("persistent agents must use the generation gateway factory")

    class FakePermissionsService:
        async def get_available_tools(self, user_context):
            return [
                {
                    "name": "read_status",
                    "description": "Read grid status",
                    "parameters": {
                        "type": "object",
                        "properties": {"grid_id": {"type": "string"}},
                        "required": ["grid_id"],
                    },
                }
            ]

    class FakeToolExecutor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def execute(self, function_call, metadata):
            assert function_call.name == "read_status"
            assert function_call.arguments == {"grid_id": "grid-1"}
            return ToolCallResult(name="read_status", success=True, output="42 connections")

    monkeypatch.setattr(pag, "GeminiGateway", FailingDirectGeminiGateway, raising=False)
    monkeypatch.setattr(
        pag,
        "get_default_generation_gateway",
        lambda **kwargs: gateway,
        raising=False,
    )
    async def noop_update(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(tool_executor_module, "ToolExecutor", FakeToolExecutor)
    monkeypatch.setattr(pag, "_update_work_packet", noop_update)
    monkeypatch.setattr(
        user_permissions_module,
        "get_permissions_service",
        lambda: FakePermissionsService(),
    )
    monkeypatch.setattr(
        pag,
        "get_settings",
        lambda: SimpleNamespace(
            google_api_key="test",
            gemini=SimpleNamespace(
                model="gemini-test",
                agent_pro_model="gemini-pro-test",
                max_output_tokens=1024,
                thinking_budget=0,
                get_effective_temperature=lambda: 0.2,
            ),
        ),
    )

    result = await pag.think_and_act(
        {
            "thread_id": "agent-1",
            "instance_id": "instance-1",
            "organization_id": 2,
            "system_instructions": "Watch the grid.",
            "current_events": [
                {
                    "event_type": "scheduled_wake",
                    "created_at": "2026-07-15T08:00:00Z",
                    "text": "Scheduled wake",
                }
            ],
            "available_tools": ["read_status"],
            "metadata": {},
        }
    )

    assert result["assessment"] == "Grid is stable."
    assert result["observations"] == ["read_status (ok)"] or result["observations"] == [
        {
            "tool": "read_status",
            "args": {"grid_id": "grid-1"},
            "success": True,
            "summary": "42 connections",
        }
    ]
    assert len(gateway.calls) == 2
    assert gateway.calls[0]["messages"][0] == LLMMessage(role="system", text="Watch the grid.")
    assert gateway.calls[0]["messages"][1].role == "user"
    assert "Scheduled wake" in (gateway.calls[0]["messages"][1].text or "")
    assert gateway.calls[0]["tools"] == [
        ToolSpec(
            name="read_status",
            description="Read grid status",
            parameters_json_schema={
                "type": "object",
                "properties": {"grid_id": {"type": "string"}},
                "required": ["grid_id"],
            },
        )
    ]
    assert gateway.calls[1]["conversation_state"] is not None
    assert gateway.calls[1]["tool_results"] == [
        ToolResult(
            call_id="call-read-status",
            name="read_status",
            result="42 connections",
        )
    ]
