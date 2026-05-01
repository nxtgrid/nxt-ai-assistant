"""Utility for detecting if user input is a new request vs step response.

When a workflow step is waiting for user input, we need to distinguish between:
1. A valid response to the pending prompt (e.g., KPI values, selection number)
2. A new unrelated request (e.g., "Can you plot the connections trend?")

This module provides heuristics to detect case #2 so the workflow can
pause and route to the main LLM instead of re-prompting.
"""

import re
from typing import List, Optional

# Question starters that indicate a new request
QUESTION_STARTERS = [
    "can you",
    "could you",
    "would you",
    "will you",
    "please",
    "show me",
    "tell me",
    "what is",
    "what are",
    "what's",
    "where is",
    "where are",
    "where's",
    "how do",
    "how can",
    "how to",
    "why is",
    "why are",
    "why does",
    "when is",
    "when does",
    "who is",
    "who are",
    "is there",
    "are there",
    "do you",
    "does it",
    "i want",
    "i need",
    "i'd like",
    "help me",
    "get me",
    "find me",
    "list",
    "fetch",
    "retrieve",
    "check",
    "look up",
    "search",
    "analyze",
    "generate",
    "create",
    "plot",
    "graph",
    "chart",
]

# Patterns that strongly indicate a question/new request
QUESTION_PATTERNS = [
    r"\?$",  # Ends with question mark
    r"^(can|could|would|will|should|do|does|is|are|was|were|have|has|did)\s",  # Question word start
]


def looks_like_new_request(
    user_input: str,
    pending_prompt: Optional[str] = None,
    expected_patterns: Optional[List[str]] = None,
) -> bool:
    """Detect if user input appears to be a new request rather than a step response.

    Uses multiple heuristics:
    1. Ends with question mark
    2. Starts with question words or action verbs
    3. Contains question starters anywhere
    4. Is significantly longer than typical responses
    5. Doesn't match expected patterns (if provided)

    Args:
        user_input: The user's message
        pending_prompt: The prompt the step showed (for context)
        expected_patterns: Optional regex patterns that valid responses should match

    Returns:
        True if input looks like a new request, False if it looks like a step response
    """
    if not user_input:
        return False

    input_lower = user_input.lower().strip()

    # Very short inputs are usually responses (numbers, yes/no, etc.)
    if len(input_lower) <= 10:
        return False

    # Check for question mark at end (strong signal)
    if input_lower.endswith("?"):
        return True

    # Check for question patterns
    for pattern in QUESTION_PATTERNS:
        if re.search(pattern, input_lower, re.IGNORECASE):
            return True

    # Check for question/action starters
    for starter in QUESTION_STARTERS:
        if input_lower.startswith(starter):
            return True
        # Also check if it appears early in the message (first 30 chars)
        if starter in input_lower[:30]:
            return True

    # If expected patterns provided, check if input doesn't match any
    if expected_patterns:
        matches_expected = any(
            re.search(pattern, user_input, re.IGNORECASE) for pattern in expected_patterns
        )
        if not matches_expected and len(input_lower) > 20:
            # Long input that doesn't match expected format
            return True

    # Long inputs (>50 chars) without expected patterns are likely new requests
    if len(input_lower) > 50 and not expected_patterns:
        return True

    return False


def looks_like_kpi_input(user_input: str) -> bool:
    """Check if input looks like KPI values (for GTR manual input).

    Expected format: "GridName: KPI1=value, KPI2=value"
    or just numbers/values.

    Args:
        user_input: The user's message

    Returns:
        True if input looks like KPI data
    """
    # Contains = sign (key=value format)
    if "=" in user_input:
        return True

    # Contains numbers with units (like "11.5h" or "52%")
    if re.search(r"\d+\.?\d*\s*[%h]", user_input, re.IGNORECASE):
        return True

    # Contains colon followed by numbers (like "ExampleGrid: 11.5")
    if re.search(r":\s*\d", user_input):
        return True

    # Just numbers
    if re.match(r"^[\d\s.,]+$", user_input.strip()):
        return True

    return False


def looks_like_selection(user_input: str, max_options: int = 10) -> bool:
    """Check if input looks like a menu selection.

    Args:
        user_input: The user's message
        max_options: Maximum valid option number

    Returns:
        True if input looks like a selection (number or keyword)
    """
    input_stripped = user_input.strip().lower()

    # Single digit or small number
    if input_stripped.isdigit() and int(input_stripped) <= max_options:
        return True

    # Common selection keywords
    selection_keywords = [
        "yes",
        "no",
        "ok",
        "okay",
        "done",
        "ready",
        "skip",
        "cancel",
        "abort",
        "approve",
        "reject",
        "1",
        "2",
        "3",
    ]
    if input_stripped in selection_keywords:
        return True

    return False
