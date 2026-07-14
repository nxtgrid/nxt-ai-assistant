from unittest.mock import MagicMock

import pytest

from orchestrator.models.schemas import ToolCallResult
from orchestrator.services.tool_executor import ToolExecutor


@pytest.mark.asyncio
async def test_call_tool_merges_default_metadata(monkeypatch):
    captured = {}

    async def fake_execute(self, call, metadata):
        captured["call"] = call
        captured["metadata"] = metadata
        return ToolCallResult(name=call.name, success=True, output="ok")

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)

    executor = ToolExecutor(
        registry=None,
        settings=MagicMock(),
        default_metadata={
            "user_email": "staff@example.com",
            "user_permissions": {"organization_ids": ["2"]},
            "session_id": "session-1",
        },
    )

    result = await executor.call_tool(
        "grid_design_design_and_bom",
        {"grid_name": "Pankshin"},
        metadata={"session_id": "session-override"},
    )

    assert result == "ok"
    assert captured["call"].name == "grid_design_design_and_bom"
    assert captured["metadata"] == {
        "user_email": "staff@example.com",
        "user_permissions": {"organization_ids": ["2"]},
        "session_id": "session-override",
    }
