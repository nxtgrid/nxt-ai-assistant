"""
Telegram Slash Command Parser

Transforms /commands based on the unified command registry.
- Tool commands: Transformed into natural language prompts for the LLM
- Expert commands: Passed through unchanged to expert_router
"""

from __future__ import annotations

import os
import re as _re
from typing import TYPE_CHECKING, List, Tuple

from orchestrator.services.command_registry import (
    COMMAND_REGISTRY,
    TICKETS_WITH_ARGS_TEMPLATE,
    CommandDefinition,
    get_command,
)
from shared.utils.logging import get_logger

if TYPE_CHECKING:
    from orchestrator.models.schemas import UserContext

LOGGER = get_logger(__name__)


# Template for unrecognized/unauthorized commands
UNRECOGNIZED_COMMAND_TEMPLATE = (
    "The command /{command} was not recognized. "
    "Please let the user know this command is not available "
    "and offer to help with their request in another way."
)


class CommandParser:
    """
    Parses Telegram slash commands using the unified command registry.

    - Tool commands are transformed into LLM prompts
    - Expert commands are passed through to expert_router
    - Staff-only commands are blocked for non-staff users
    """

    def __init__(self) -> None:
        """Initialize the command parser."""
        self._registry = COMMAND_REGISTRY

    def is_command(self, text: str) -> bool:
        """Check if text starts with a slash command."""
        return text.strip().startswith("/")

    def parse_command(self, text: str) -> Tuple[str, str]:
        """
        Parse a command message into command name and arguments.

        Args:
            text: The full message text (e.g., "/ticket OPS-123")

        Returns:
            Tuple of (command_name, arguments)
        """
        text = text.strip()
        if not text.startswith("/"):
            return ("", text)

        # Remove leading slash and split on first space
        parts = text[1:].split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Handle @bot_username suffix (e.g., /tickets@mybot)
        if "@" in command:
            command = command.split("@")[0]

        return (command, args)

    def get_command(self, command: str) -> CommandDefinition | None:
        """
        Get command definition by name.

        Args:
            command: Command name (without slash)

        Returns:
            CommandDefinition if found, None otherwise
        """
        return get_command(command)

    async def process_command(
        self,
        text: str,
        user_context: "UserContext",
    ) -> Tuple[str, bool, List[str], str, int]:
        """
        Process a command and return the transformed prompt.

        Args:
            text: The full message text
            user_context: User context with is_staff flag

        Returns:
            Tuple of (processed_text, is_command, unlocked_tools, model_override, max_tool_rounds)
            - If tool command: (prompt, True, exclusive_tools, model_override or "", max_tool_rounds_override or 0)
            - If expert command: (original text, False, [], "", 0)
            - If unrecognized/unauthorized: (error prompt, True, [], "", 0)
            - If not a command: (original text, False, [], "", 0)
        """
        if not self.is_command(text):
            return (text, False, [], "", 0)

        command, args = self.parse_command(text)
        LOGGER.info(f"Processing command: /{command} with args: '{args}'")

        # Look up command definition
        cmd_def = self.get_command(command)

        if not cmd_def:
            # Unrecognized command
            LOGGER.info(f"Unrecognized command: /{command}")
            return (UNRECOGNIZED_COMMAND_TEMPLATE.format(command=command), True, [], "", 0)

        # Check if command is staff-only and user is not staff
        if cmd_def.staff_only and not user_context.is_staff:
            LOGGER.info(f"Non-staff user attempted staff-only command /{command}")
            return (UNRECOGNIZED_COMMAND_TEMPLATE.format(command=command), True, [], "", 0)

        # Direct commands: Pass through unchanged — handled in expert_router
        # before any LLM or expert processing.
        if cmd_def.command_type == "direct":
            LOGGER.info(f"Passing direct command /{command} to expert_router")
            return (text, False, [], "", 0)

        # Expert commands: Pass through unchanged to expert_router.
        # If we reach here, expert_router already ran and returned "continue"
        # (expert is disabled). Fall back to tool-based flow if prompt_template exists.
        if cmd_def.command_type == "expert":
            if cmd_def.prompt_template:
                LOGGER.info(f"Expert /{command} disabled, falling back to tool-based flow")
            else:
                LOGGER.info(f"Passing expert command /{command} to expert_router")
                return (text, False, [], "", 0)

        # Tool commands: Check if args are required but missing
        if cmd_def.requires_args and not args:
            LOGGER.info(f"Command /{command} requires args but none provided")
            return (
                cmd_def.args_hint or f"/{command} requires additional arguments.",
                True,
                [],
                "",
                0,
            )

        # Build the prompt from template
        if cmd_def.command == "help":
            prompt = self._build_help_prompt(user_context)
        elif cmd_def.command == "commands":
            prompt = self._build_commands_prompt(user_context)
        else:
            prompt = self._build_prompt(cmd_def, args)
        unlocked_tools = cmd_def.exclusive_tools or []

        # Resolve model override from env var name to actual model string
        model_override = ""
        if cmd_def.model_override:
            model_override = os.getenv(cmd_def.model_override, "")
            if model_override:
                LOGGER.info(f"Command /{command} uses model override: {model_override}")

        max_tool_rounds = cmd_def.max_tool_rounds_override
        if max_tool_rounds:
            LOGGER.info(f"Command /{command} uses max_tool_rounds override: {max_tool_rounds}")

        LOGGER.info(f"Command /{command} transformed to prompt: {prompt[:100]}...")
        if unlocked_tools:
            LOGGER.info(f"Command /{command} unlocks tools: {unlocked_tools}")

        return (prompt, True, unlocked_tools, model_override, max_tool_rounds)

    def _build_prompt(self, cmd_def: CommandDefinition, args: str) -> str:
        """
        Build the natural language prompt from template.

        Args:
            cmd_def: Command definition
            args: User-provided arguments

        Returns:
            The formatted prompt string
        """
        # Special case: /tickets with args uses search template
        if cmd_def.command == "tickets" and args:
            result: str = TICKETS_WITH_ARGS_TEMPLATE.format(args=args)
            return result

        # Use the command's prompt template
        prompt: str = cmd_def.prompt_template.format(args=args)
        return prompt

    def _build_help_prompt(self, user_context: "UserContext") -> str:
        """Build a dynamic help prompt describing bot capabilities."""
        return (
            "The user wants to know what you can do. "
            "Summarize your capabilities based on your system instructions — "
            "what kind of questions you can answer, what tasks you can perform, etc. "
            "Keep it concise (5-10 lines max). "
            "Describe capabilities in natural language only. "
            "Do NOT list slash commands — users interact via natural language."
        )

    def _build_commands_prompt(self, user_context: "UserContext") -> str:
        """Build a dynamic /commands prompt with the full command list."""
        is_staff = user_context.is_staff

        seen_descriptions: set = set()
        command_lines: list = []
        for cmd in self._registry.values():
            if cmd.staff_only and not is_staff:
                continue
            if cmd.description in seen_descriptions:
                continue
            seen_descriptions.add(cmd.description)
            command_lines.append(f"/{cmd.command} — {cmd.description}")

        if not command_lines:
            return "Tell the user no commands are available."

        commands_block = "\n".join(sorted(command_lines))
        return (
            f"List these available slash commands to the user:\n\n{commands_block}\n\n"
            "Format each as: /command — description."
        )

    def get_all_commands(self) -> List[CommandDefinition]:
        """
        Get all registered commands.

        Returns:
            List of all CommandDefinitions
        """
        return list(self._registry.values())


# --- /lpp GPS-anchor route (Route B) ---

_NUMBER_RE = r"-?\d+(?:\.\d+)?"
_BARE_LATLON_RE = _re.compile(rf"^\s*({_NUMBER_RE})\s*,\s*({_NUMBER_RE})\s*$")
_EMBEDDED_LATLON_RE = _re.compile(rf"(?<![\w.-])({_NUMBER_RE})\s*,\s*({_NUMBER_RE})(?![\w.-])")
_LPP_TECHNOLOGY_ALIASES = (
    ("deye", ("deye", "ess", "hybrid ess", "hybrid_ess")),
    ("victron", ("victron", "quattro")),
)


def _valid_latlon(lat_s: str, lon_s: str) -> bool:
    try:
        lat, lon = float(lat_s), float(lon_s)
    except ValueError:
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def parse_lpp_anchor_args(args: str) -> dict | None:
    """Parse /lpp args for a GPS-anchor (Route B) invocation.

    Recognizes either form:
      - anchor:<lat>,<lon> [name:"Community Name"]   (explicit, supports a name)
      - bare coordinates:  <lat>,<lon>  or  <lat>, <lon>   (whole string, no name)
      - embedded decimal coordinates in natural language LPP requests

    Returns {latitude, longitude, community_name?} or None when the args are not
    a GPS anchor (site name or multi-site list fall through to the submission route).
    """
    if not args:
        return None

    m = _re.search(r"anchor:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", args, _re.IGNORECASE)
    if m and _valid_latlon(m.group(1), m.group(2)):
        result: dict = {"latitude": m.group(1), "longitude": m.group(2)}
        name_m = _re.search(r'name:\s*"([^"]+)"|name:\s*(\S+)', args, _re.IGNORECASE)
        if name_m:
            result["community_name"] = name_m.group(1) or name_m.group(2)
        return result

    bare = _BARE_LATLON_RE.match(args)
    if bare and _valid_latlon(bare.group(1), bare.group(2)):
        return {"latitude": bare.group(1), "longitude": bare.group(2)}

    for embedded in _EMBEDDED_LATLON_RE.finditer(args):
        lat_s, lon_s = embedded.group(1), embedded.group(2)
        if "." not in lat_s and "." not in lon_s:
            continue
        if _valid_latlon(lat_s, lon_s):
            return {"latitude": lat_s, "longitude": lon_s}

    return None


def parse_lpp_technology_family(text: str) -> str | None:
    """Extract the requested LPP power-plant technology family from text.

    The LPP workflow has downstream support for ``technology_family`` but the
    natural-language router may reduce the request to a synthetic ``/lpp``
    command. Keep this parser deterministic so vendor/architecture requests
    survive packet creation and resume/start-fresh flows.
    """
    if not text:
        return None

    normalized = text.lower().replace("-", " ").replace("_", " ")
    for family, aliases in _LPP_TECHNOLOGY_ALIASES:
        for alias in aliases:
            alias_text = alias.replace("_", " ")
            if _re.search(rf"(?<!\w){_re.escape(alias_text)}(?!\w)", normalized):
                return family
    return None


__all__ = [
    "CommandParser",
    "UNRECOGNIZED_COMMAND_TEMPLATE",
    "parse_lpp_anchor_args",
    "parse_lpp_technology_family",
]
