"""Parse command node for LangGraph.

This node processes Telegram slash commands and natural language
trigger matching, transforming user input accordingly.
"""

import os
from typing import Any, Dict

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState


async def parse_command(state: ConversationState) -> Dict[str, Any]:
    """Process Telegram slash commands.

    This node:
    1. Checks if source is Telegram and input starts with /
    2. Calls CommandParser.process_command()
    3. Updates user_input with transformed text
    4. Returns list of unlocked tools

    Args:
        state: Current conversation state

    Returns:
        State updates with user_input, parsed_command, and unlocked_tools
    """
    user_context = state.get("user_context")
    user_input = state.get("user_input", "")
    original_input = user_input

    # Only process commands from Telegram
    if not user_context or user_context.source != "telegram":
        LOGGER.debug("Not a Telegram source, skipping command parsing")
        return {
            "original_input": original_input,
            "parsed_command": None,
            "unlocked_tools": [],
        }

    # Check if input is a slash command
    if not user_input.strip().startswith("/"):
        # Try natural language trigger matching
        from orchestrator.services.command_registry import match_nl_trigger

        is_staff = user_context.is_staff if user_context else False
        nl_match = match_nl_trigger(user_input, is_staff=is_staff)
        if nl_match:
            LOGGER.info(f"NL trigger matched: '{user_input[:50]}' → /{nl_match.command}")
            # Build the prompt as if the user typed the slash command
            from orchestrator.services.command_parser import CommandParser

            parser = CommandParser()
            # Use the full user input as args (it contains the doc name, etc.)
            prompt = parser._build_prompt(nl_match, user_input)
            unlocked = nl_match.exclusive_tools or []
            model_ov = ""
            if nl_match.model_override:
                model_ov = os.getenv(nl_match.model_override, "")

            result = {
                "user_input": prompt,
                "original_input": original_input,
                "parsed_command": f"/{nl_match.command}",
                "unlocked_tools": unlocked,
            }
            if model_ov:
                result["command_model_override"] = model_ov
                LOGGER.info(f"NL trigger model override: {model_ov}")
            return result

        LOGGER.debug("Input does not start with / and no NL trigger matched")
        return {
            "original_input": original_input,
            "parsed_command": None,
            "unlocked_tools": [],
        }

    # Import parser
    from orchestrator.services.command_parser import CommandParser

    parser = CommandParser()
    processed_input, is_command, unlocked_tools, model_override = await parser.process_command(
        text=user_input,
        user_context=user_context,
    )

    if is_command:
        # Extract the command name (e.g., "/grids" from "/grids list")
        command_name = user_input.strip().split()[0]

        LOGGER.info(f"Transformed command {command_name} to: {processed_input[:100]}...")
        if unlocked_tools:
            LOGGER.info(f"Command unlocked tools: {unlocked_tools}")

        result = {
            "user_input": processed_input,
            "original_input": original_input,
            "parsed_command": command_name,
            "unlocked_tools": unlocked_tools or [],
        }
        if model_override:
            result["command_model_override"] = model_override
            LOGGER.info(f"Command model override: {model_override}")
        return result
    else:
        LOGGER.debug("Input starts with / but is not a recognized command")
        return {
            "original_input": original_input,
            "parsed_command": None,
            "unlocked_tools": [],
        }
