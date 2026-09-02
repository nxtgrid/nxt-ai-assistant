"""parse_command's source gate.

The node was Telegram-only. The anansi_app chat widget sends source="web" and
is meant to behave like a Telegram personal chat, so without "web" here a
staff user's "/grids" reaches the model as literal prose and every
natural-language trigger silently stops matching.

"api" stays excluded: that source covers n8n, the scheduler and direct API
integrations, none of which type slash commands, and NL-trigger matching on
machine-generated text would fire by accident.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.graphs.nodes.parse_command import parse_command
from orchestrator.models.schemas import UserContext


def _state(source: str, text: str = "/grids") -> dict:
    return {
        "user_input": text,
        "user_context": UserContext(user_id="u1", user_email="admin@example.com", source=source),
    }


def _fake_parser(processed: str = "List every grid and its status"):
    parser = MagicMock()
    parser.process_command = AsyncMock(return_value=(processed, True, ["grid_tool"], "", None))
    return parser


@pytest.mark.asyncio
async def test_web_source_reaches_the_command_parser():
    with patch(
        "orchestrator.services.command_parser.CommandParser", return_value=_fake_parser()
    ):
        result = await parse_command(_state("web"))

    assert result["parsed_command"] == "/grids"
    assert result["user_input"] == "List every grid and its status"
    assert result["unlocked_tools"] == ["grid_tool"]


@pytest.mark.asyncio
async def test_telegram_source_still_reaches_the_command_parser():
    with patch(
        "orchestrator.services.command_parser.CommandParser", return_value=_fake_parser()
    ):
        result = await parse_command(_state("telegram"))

    assert result["parsed_command"] == "/grids"


@pytest.mark.asyncio
async def test_api_source_still_skips_command_parsing():
    result = await parse_command(_state("api"))

    assert result["parsed_command"] is None
    assert result["unlocked_tools"] == []
    assert result["original_input"] == "/grids"


@pytest.mark.asyncio
async def test_missing_user_context_skips_command_parsing():
    result = await parse_command({"user_input": "/grids", "user_context": None})

    assert result["parsed_command"] is None
