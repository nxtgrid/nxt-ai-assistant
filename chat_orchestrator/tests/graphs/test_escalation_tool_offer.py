"""Who is offered escalate_to_support, and what its description tells the model.

2026-10-18 audit of production escalations:
- Staff sessions filed escalations into the support group -- that is, to
  themselves. The bundled staff prompt already says not to tell staff anything
  will be escalated; the tool was still offered to them.
- The description's catch-all, escalate "any action beyond available read-only
  tools", had the model escalating plain data questions it couldn't answer
  ("battery discharge hours yesterday", "delivery success rate") instead of
  saying the data isn't available. Each of those also stopped the bot answering
  that chat until someone closed the escalation.
"""

import importlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.models.schemas import UserContext

# orchestrator.graphs.nodes re-exports the prepare_tools function under the
# submodule's name, shadowing it -- import the module itself.
prepare_tools_module = importlib.import_module("orchestrator.graphs.nodes.prepare_tools")
ESCALATION_TOOL_DEF = prepare_tools_module.ESCALATION_TOOL_DEF


def _user(*, is_staff: bool) -> UserContext:
    return UserContext(user_id="1001", user_email="person@example.com", is_staff=is_staff)


async def _offered_tool_names(user_context: UserContext) -> set[str]:
    permissions = MagicMock()
    permissions.get_available_tools = AsyncMock(return_value=[])
    with patch.object(prepare_tools_module, "get_permissions_service", return_value=permissions):
        result = await prepare_tools_module.prepare_tools({"user_context": user_context})
    return {tool["name"] for tool in result["available_tools"] if "name" in tool}


@pytest.mark.asyncio
async def test_staff_are_not_offered_the_escalation_tool():
    assert "escalate_to_support" not in await _offered_tool_names(_user(is_staff=True))


@pytest.mark.asyncio
async def test_customers_are_still_offered_the_escalation_tool():
    assert "escalate_to_support" in await _offered_tool_names(_user(is_staff=False))


def test_description_has_no_catch_all_for_anything_the_tools_cannot_do():
    assert "beyond available read-only tools" not in ESCALATION_TOOL_DEF["description"]


def test_description_tells_the_model_not_to_escalate_data_questions():
    sentences = re.split(r"(?<=[.!?:])\s+|\n+", ESCALATION_TOOL_DEF["description"])

    assert any(
        "not escalate" in sentence.lower() and "data" in sentence.lower() for sentence in sentences
    )


def test_reason_descriptions_do_not_invite_escalating_data_questions():
    reason_doc = ESCALATION_TOOL_DEF["parameters"]["properties"]["reason"]["description"]
    lines = [
        line
        for line in reason_doc.splitlines()
        if line.startswith(("- could_not_answer", "- out_of_scope"))
    ]

    assert len(lines) == 2
    assert all("not for data" in line for line in lines)
