"""Parameter confirmation prompts and user input handling.

Handles the interactive parameter confirmation flow:
1. Formatting confirmation prompts for users
2. Parsing user responses (number selection, continue, auto)
3. Validating and converting user-provided values
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, List, Optional

from shared.utils.logging import get_logger

if TYPE_CHECKING:
    from orchestrator.experts.parameter_resolver import ResolvedParameter

LOGGER = get_logger(__name__)


class ConfirmationAction(Enum):
    """Action types from user confirmation response."""

    CONTINUE = "continue"  # Proceed with current values
    AUTO = "auto"  # Continue all remaining steps automatically
    SELECT_PARAM = "select_param"  # User selected a parameter to modify
    SET_VALUE = "set_value"  # User provided a new value
    CANCEL = "cancel"  # Abort the current workflow
    NEW_COMMAND = "new_command"  # User entered a new slash command
    INVALID = "invalid"  # Invalid input


@dataclass
class ConfirmationResponse:
    """Parsed user response to confirmation prompt."""

    action: ConfirmationAction
    param_index: Optional[int] = None  # For SELECT_PARAM
    new_value: Any = None  # For SET_VALUE
    error_message: Optional[str] = None  # For INVALID


def _format_param_name(name: str) -> str:
    """Format parameter name for user-friendly display.

    Strips editable_ prefix and converts snake_case to Title Case.
    """
    # Strip editable_ prefix if present
    EDITABLE_PREFIX = "editable_"
    if name.startswith(EDITABLE_PREFIX):
        name = name[len(EDITABLE_PREFIX) :]

    return name.replace("_", " ").title()


def format_confirmation_prompt(
    step_name: str,
    step_index: int,
    total_steps: int,
    description: str,
    parameters: List["ResolvedParameter"],
) -> str:
    """Format the parameter confirmation prompt for user display.

    Args:
        step_name: Handler name (e.g., "generate_powerplant_design")
        step_index: 0-based index of current step
        total_steps: Total number of steps in workflow
        description: Human-readable step description
        parameters: List of resolved parameters with current values

    Returns:
        Formatted prompt string
    """
    # Format step name for display
    display_name = step_name.replace("_", " ").title()

    lines = [
        f"*Step {step_index + 1}/{total_steps}: {display_name}*",
        description,
        "",
        "*Parameters:*",
    ]

    for i, param in enumerate(parameters, 1):
        value_str = _format_value_for_display(param.current_value)
        required_mark = " [required]" if param.required else ""
        modified_mark = " *" if param.is_modified else ""
        editable_mark = "" if param.editable else " (read-only)"

        # Format parameter name for display (convert snake_case to Title Case)
        display_param_name = _format_param_name(param.name)

        # Show source if not default/override
        source_info = f" ({param.source})" if param.source not in ("override",) else ""

        lines.append(
            f"  {i}. *{display_param_name}*: {value_str}{source_info}{required_mark}{modified_mark}{editable_mark}"
        )
        if param.description:
            lines.append(f"     {param.description}")

    lines.extend(
        [
            "",
            "*Commands:*",
            f"  - Enter number (1-{len(parameters)}) to modify a parameter",
            "  - 'c' or 'continue' - proceed with current values",
            "  - 'a' or 'auto' - continue ALL remaining steps automatically",
            "  - 'cancel' - abort this workflow",
        ]
    )

    return "\n".join(lines)


def _format_value_for_display(value: Any) -> str:
    """Format a parameter value for user display.

    Args:
        value: The value to format

    Returns:
        Formatted string representation
    """
    if value is None:
        return "_None_"
    elif isinstance(value, str):
        if len(value) > 50:
            return f'"{value[:47]}..."'
        return f'"{value}"'
    elif isinstance(value, bool):
        return "Yes" if value else "No"
    elif isinstance(value, (list, dict)):
        json_str = json.dumps(value, default=str)
        if len(json_str) > 50:
            return f"{json_str[:47]}..."
        return json_str
    else:
        return str(value)


def format_param_edit_prompt(param: "ResolvedParameter") -> str:
    """Format prompt for editing a specific parameter.

    Args:
        param: The parameter being edited

    Returns:
        Prompt string asking for new value
    """
    current = _format_value_for_display(param.current_value)
    display_name = _format_param_name(param.name)

    lines = [
        f"*Editing: {display_name}*",
        f"Current value: {current}",
        param.description if param.description else "",
        "",
        "Enter new value:",
        "Type 'cancel' to keep current value",
    ]

    return "\n".join(line for line in lines if line)


def parse_confirmation_response(
    user_input: str,
    num_parameters: int,
    is_editing_param: bool = False,
    editing_param_type: Optional[str] = None,
) -> ConfirmationResponse:
    """Parse user's response to confirmation prompt.

    Args:
        user_input: Raw user input string
        num_parameters: Number of parameters in the prompt
        is_editing_param: True if user is entering a value for a parameter
        editing_param_type: Type of parameter being edited (for validation)

    Returns:
        ConfirmationResponse with parsed action and data
    """
    user_input = user_input.strip()

    # Handle value editing mode
    if is_editing_param:
        return _parse_value_input(user_input, editing_param_type)

    # Check for new slash command - abort current workflow and process new command
    if user_input.startswith("/"):
        return ConfirmationResponse(
            action=ConfirmationAction.NEW_COMMAND,
            new_value=user_input,  # Store the command for re-processing
        )

    # Normalize input for command matching
    normalized = user_input.lower()

    # Check for 'cancel' / 'abort' - abort current workflow
    if normalized in ("cancel", "abort", "quit", "exit", "stop"):
        return ConfirmationResponse(action=ConfirmationAction.CANCEL)

    # Check for 'continue'
    if normalized in ("c", "continue", "y", "yes", "ok"):
        return ConfirmationResponse(action=ConfirmationAction.CONTINUE)

    # Check for 'auto'
    if normalized in ("a", "auto", "all"):
        return ConfirmationResponse(action=ConfirmationAction.AUTO)

    # Check if user selected a parameter number
    if user_input.isdigit():
        param_num = int(user_input)
        if 1 <= param_num <= num_parameters:
            return ConfirmationResponse(
                action=ConfirmationAction.SELECT_PARAM,
                param_index=param_num - 1,  # Convert to 0-based
            )
        else:
            return ConfirmationResponse(
                action=ConfirmationAction.INVALID,
                error_message=f"Invalid number. Please enter 1-{num_parameters}.",
            )

    # Invalid input
    return ConfirmationResponse(
        action=ConfirmationAction.INVALID,
        error_message=f"Didn't understand '{user_input}'. Enter a number, 'c' to continue, or 'a' for auto.",
    )


def _parse_value_input(
    user_input: str,
    param_type: Optional[str],
) -> ConfirmationResponse:
    """Parse user input when editing a parameter value.

    Args:
        user_input: Raw user input
        param_type: Expected parameter type

    Returns:
        ConfirmationResponse with SET_VALUE action or error
    """
    # Handle cancel
    if user_input.lower() in ("cancel", "c", "back"):
        return ConfirmationResponse(action=ConfirmationAction.CONTINUE)

    # Try to convert to expected type
    try:
        value = _convert_value(user_input, param_type)
        return ConfirmationResponse(
            action=ConfirmationAction.SET_VALUE,
            new_value=value,
        )
    except ValueError as e:
        return ConfirmationResponse(
            action=ConfirmationAction.INVALID,
            error_message=str(e),
        )


def _convert_value(value_str: str, param_type: Optional[str]) -> Any:
    """Convert string input to the expected parameter type.

    Args:
        value_str: User's input string
        param_type: Expected type (string, int, float, bool, list, dict)

    Returns:
        Converted value

    Raises:
        ValueError: If conversion fails
    """
    if not param_type or param_type == "string":
        return value_str

    if param_type == "int":
        try:
            return int(value_str)
        except ValueError:
            raise ValueError(f"'{value_str}' is not a valid integer")

    if param_type == "float":
        try:
            return float(value_str)
        except ValueError:
            raise ValueError(f"'{value_str}' is not a valid number")

    if param_type == "bool":
        lower = value_str.lower()
        if lower in ("true", "yes", "y", "1", "on"):
            return True
        elif lower in ("false", "no", "n", "0", "off"):
            return False
        else:
            raise ValueError(f"'{value_str}' is not a valid boolean (use yes/no)")

    if param_type in ("list", "dict"):
        try:
            parsed = json.loads(value_str)
            if param_type == "list" and not isinstance(parsed, list):
                raise ValueError(f"Expected a list, got {type(parsed).__name__}")
            if param_type == "dict" and not isinstance(parsed, dict):
                raise ValueError(f"Expected a dict, got {type(parsed).__name__}")
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")

    # Unknown type - return as string
    return value_str


def format_value_change_confirmation(
    param_name: str,
    old_value: Any,
    new_value: Any,
) -> str:
    """Format confirmation message after user changes a value.

    Args:
        param_name: Name of the parameter
        old_value: Previous value
        new_value: New value

    Returns:
        Confirmation message string
    """
    old_str = _format_value_for_display(old_value)
    new_str = _format_value_for_display(new_value)
    display_name = _format_param_name(param_name)
    return f"*{display_name}* updated: {old_str} -> {new_str}"


__all__ = [
    "ConfirmationAction",
    "ConfirmationResponse",
    "format_confirmation_prompt",
    "format_param_edit_prompt",
    "parse_confirmation_response",
    "format_value_change_confirmation",
]
