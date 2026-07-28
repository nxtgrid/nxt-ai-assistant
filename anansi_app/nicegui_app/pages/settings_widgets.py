"""Widget selection and validation for the settings page.

Kept free of ``ui.*`` construction so the decisions -- which widget, which
placeholder, which error -- are unit-testable without a browser.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Optional

from shared.config.flag_registry import Flag, FlagType

# Flags whose value is a comma-separated list and which read far better as
# removable chips than as one long comma-run in a text box.
_CSV_LIST_FLAGS = frozenset(
    {
        "ALLOWED_VIEWER_EMAILS",
        "EQUIPMENT_CONTROL_ALLOWED_USERS",
        "GRID_DESIGN_ALLOWED_USERS",
        "GRID_DESIGN_EDITORS",
        "GRID_PROCUREMENT_EDITORS",
        "NO_REPLY_CHAT_IDS",
    }
)
# OPENROUTER_PROVIDER_ORDER is deliberately absent: it is also a comma-separated
# list, but its options are fetched live from OpenRouter per selected model, so
# it keeps the bespoke picker in settings.py rather than a generic chip input
# that would drop the discovered routes.


class RenderMode(Enum):
    SWITCH = "switch"
    NUMBER = "number"
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    TEXT = "text"
    TEXTAREA = "textarea"
    SECRET = "secret"
    READ_ONLY = "read_only"


def render_mode(flag: Flag) -> RenderMode:
    """Which widget a flag gets. Read-only and secret win over the value type."""
    if not flag.editable:
        return RenderMode.READ_ONLY
    if flag.secret:
        return RenderMode.SECRET
    if flag.type is FlagType.BOOL:
        return RenderMode.SWITCH
    if flag.type is FlagType.JSON:
        return RenderMode.TEXTAREA
    if flag.choices:
        return RenderMode.SELECT
    if flag.name in _CSV_LIST_FLAGS:
        return RenderMode.MULTI_SELECT
    if flag.type in (FlagType.INT, FlagType.FLOAT):
        return RenderMode.NUMBER
    return RenderMode.TEXT


def secret_placeholder(is_set: bool) -> str:
    """Placeholder for a secret field. Never derived from the actual value."""
    return "••••••••  (set)" if is_set else "not set"


def validate(flag: Flag, value: Any) -> Optional[str]:
    """Return an error message for ``value``, or None when it is acceptable."""
    if flag.choices and str(value) not in flag.choices:
        return f"{flag.display_label}: must be one of {', '.join(flag.choices)}"
    if flag.type is FlagType.JSON:
        try:
            json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return f"{flag.display_label}: invalid JSON"
    if flag.type in (FlagType.INT, FlagType.FLOAT) and value not in (None, ""):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f"{flag.display_label}: must be a number"
        if flag.minimum is not None and number < flag.minimum:
            return f"{flag.display_label}: must be at least {flag.minimum:g}"
        if flag.maximum is not None and number > flag.maximum:
            return f"{flag.display_label}: must be at most {flag.maximum:g}"
    return None
