"""Utilities for presenting numbered options and parsing user selections.

Provides consistent formatting and parsing across packet-level decisions
(resume/start fresh) and step-level questions (site disambiguation).

Usage:
    from shared.utils.option_parsing import normalize_numeric_input, format_numbered_options

    # Parsing user input
    user_input = "1️⃣"
    normalized = normalize_numeric_input(user_input)  # Returns "1"

    # Formatting options
    options = [
        ("Resume", "Retry from where it stopped"),
        ("Start fresh", "Begin a new package"),
    ]
    formatted = format_numbered_options(options)
"""

from typing import List, Optional, Tuple, Union

# Mapping of emoji numbers to plain digits
EMOJI_TO_DIGIT = {
    "0️⃣": "0",
    "1️⃣": "1",
    "2️⃣": "2",
    "3️⃣": "3",
    "4️⃣": "4",
    "5️⃣": "5",
    "6️⃣": "6",
    "7️⃣": "7",
    "8️⃣": "8",
    "9️⃣": "9",
    "🔟": "10",
}

# Reverse mapping for formatting
DIGIT_TO_EMOJI = {v: k for k, v in EMOJI_TO_DIGIT.items()}


def normalize_numeric_input(text: str) -> str:
    """Normalize user input by converting emoji numbers to plain digits.

    Args:
        text: User input that might contain emoji numbers

    Returns:
        Normalized string with emoji numbers converted to plain digits

    Examples:
        >>> normalize_numeric_input("1️⃣")
        '1'
        >>> normalize_numeric_input("  2  ")
        '2'
        >>> normalize_numeric_input("resume")
        'resume'
    """
    text = text.strip()
    return EMOJI_TO_DIGIT.get(text, text)


def parse_numeric_selection(
    text: str,
    max_option: int,
    valid_ids: Optional[List[int]] = None,
) -> Optional[int]:
    """Parse a user's numeric selection, handling both plain and emoji numbers.

    Tries to interpret the input as:
    1. An option number (1-based, up to max_option)
    2. A direct ID from valid_ids list (if provided)

    Args:
        text: User input
        max_option: Maximum valid option number (1-based)
        valid_ids: Optional list of valid IDs to match against

    Returns:
        The selected number/ID, or None if not parseable

    Examples:
        >>> parse_numeric_selection("1", 3)
        1
        >>> parse_numeric_selection("1️⃣", 3)
        1
        >>> parse_numeric_selection("206", 2, valid_ids=[201, 206])
        206
        >>> parse_numeric_selection("invalid", 3)
        None
    """
    normalized = normalize_numeric_input(text)

    if not normalized.isdigit():
        return None

    num = int(normalized)

    # Check if it's a valid option number (1-based)
    if 1 <= num <= max_option:
        return num

    # Check if it's a valid ID
    if valid_ids and num in valid_ids:
        return num

    return None


def format_numbered_options(
    options: List[Union[str, Tuple[str, str]]],
    use_emoji: bool = False,
    start_from: int = 1,
) -> str:
    """Format a list of options with consistent numbering.

    Args:
        options: List of option strings, or tuples of (label, description)
        use_emoji: Whether to use emoji numbers (1️⃣) or plain (1.)
        start_from: Starting number (default 1)

    Returns:
        Formatted string with numbered options

    Examples:
        >>> format_numbered_options(["Resume", "Start fresh"])
        '1. Resume\\n2. Start fresh'

        >>> format_numbered_options([("Resume", "Retry"), ("Fresh", "New")])
        '1. **Resume** - Retry\\n2. **Fresh** - New'
    """
    lines = []
    for i, opt in enumerate(options, start=start_from):
        num_str = DIGIT_TO_EMOJI.get(str(i), f"{i}.") if use_emoji else f"{i}."

        if isinstance(opt, tuple):
            label, desc = opt
            lines.append(f"{num_str} **{label}** - {desc}")
        else:
            lines.append(f"{num_str} {opt}")

    return "\n".join(lines)


def format_selection_prompt(
    options: List[Union[str, Tuple[str, str]]],
    header: str = "Would you like to:",
    footer: Optional[str] = None,
    use_emoji: bool = False,
) -> str:
    """Format a complete selection prompt with header, options, and footer.

    Args:
        options: List of option strings or (label, description) tuples
        header: Header text before options
        footer: Optional footer text (default: "Reply with 1, 2, ...")
        use_emoji: Whether to use emoji numbers

    Returns:
        Complete formatted prompt string
    """
    option_count = len(options)

    if footer is None:
        nums = ", ".join(str(i) for i in range(1, option_count + 1))
        footer = f"Reply with {nums}, or tell me what you'd like to do."

    formatted_options = format_numbered_options(options, use_emoji=use_emoji)

    return f"{header}\n{formatted_options}\n\n{footer}"


__all__ = [
    "normalize_numeric_input",
    "parse_numeric_selection",
    "format_numbered_options",
    "format_selection_prompt",
    "EMOJI_TO_DIGIT",
    "DIGIT_TO_EMOJI",
]
