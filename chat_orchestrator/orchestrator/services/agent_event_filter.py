"""Pre-filter for persistent agent events.

Determines whether an incoming Telegram message should wake a persistent
agent. Runs BEFORE queuing an event, avoiding unnecessary Gemini API costs.

Most messages in alert groups are noise. The agent may go days without
being woken — that is correct behavior.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class AgentEventFilter:
    """Determines if a Telegram message should wake a persistent agent.

    Pattern matching is intentionally broad for equipment alerts (high
    priority) and narrower for staff messages (medium priority). The LLM
    handles nuance; the filter just gates whether to spend a Gemini call.
    """

    # Messages from these patterns wake the agent, categorized by event_type.
    # Order matters: first match wins.
    WAKE_PATTERNS: Dict[str, List[str]] = {
        "equipment_alert": [
            # VRM / equipment monitoring alerts
            r"(?i)(alarm|alert|fault|error|warning|critical)",
            r"(?i)(offline|disconnected|communication\s*loss|comms?\s*down)",
            r"(?i)(battery.*lockout|mppt.*under|inverter.*error)",
            r"(?i)(voltage|frequency|overload|over\s*temp|temperature\s*high)",
            r"(?i)(grid\s*(down|off|failure)|power\s*(out|failure|loss))",
            r"(?i)(low\s*battery|soc\s*below|discharge|charge\s*error)",
        ],
        "jira_notification": [
            # JIRA automation rule messages
            r"(?i)(jira|ticket|issue)\s*(created|updated|assigned|resolved|closed|reopened)",
            r"\b[A-Z]{2,10}-\d{1,6}\b",  # JIRA issue keys like OGH-142, GRID-23
        ],
        "staff_message": [
            # Staff discussing grid/equipment issues
            r"(?i)(power|outage|down\b|broken|not\s*working|maintenance)",
            r"(?i)(clean(ed|ing)?|panel[s]?|solar|inverter|battery|meter)",
            r"(?i)(visit|technician|engineer|check|inspect|dispatch)",
            r"(?i)(consumption|production|generation|load|demand)",
            r"(?i)(trend|degrad|declin|drop(ped)?|increas|spike)",
        ],
    }

    # Messages that NEVER wake the agent, regardless of other patterns
    IGNORE_PATTERNS: List[str] = [
        r"(?i)^(ok|okay|thanks|thank you|noted|sure|yes|no|good|great|done|copy)[\s!.]*$",
        r"^[\U0001F44D\U00002705\U0001F44C\U00002764\U0001F64F\U0001F389]+$",  # Emoji-only
        r"(?i)^/",  # Slash commands (handled by normal chat flow)
        r"^\s*$",  # Empty/whitespace
    ]

    def should_wake_agent(
        self,
        message_text: Optional[str],
        sender_info: Optional[Dict] = None,
    ) -> Tuple[bool, str]:
        """Determine if a message should wake a persistent agent.

        Args:
            message_text: The message text (may be None for media-only)
            sender_info: Optional sender information dict

        Returns:
            (should_wake, event_type) — e.g. (True, "equipment_alert")
            or (False, "") if no wake needed
        """
        if not message_text or not message_text.strip():
            return False, ""

        text = message_text.strip()

        # Check ignore patterns first
        for pattern in self.IGNORE_PATTERNS:
            if re.match(pattern, text):
                return False, ""

        # Check wake patterns in priority order
        for event_type, patterns in self.WAKE_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, text):
                    LOGGER.debug(
                        f"Event filter matched: event_type={event_type}, "
                        f"pattern={pattern[:40]}, text={text[:80]}"
                    )
                    return True, event_type

        # Default: don't wake. The agent may be idle for days.
        return False, ""


# Module-level singleton
_filter_instance: Optional[AgentEventFilter] = None


def get_event_filter() -> AgentEventFilter:
    """Get the singleton event filter instance."""
    global _filter_instance
    if _filter_instance is None:
        _filter_instance = AgentEventFilter()
    return _filter_instance
