"""Safety check node for LangGraph.

This node runs post-generation safety checks:
1. Detects when the model claims to have escalated without calling the tool
2. Strips fabricated "Response from Support Team" blocks (impersonation guard)
"""

import os
import re
from typing import Any, Dict

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState
from shared.auth import get_auth_service


async def safety_check(state: ConversationState) -> Dict[str, Any]:
    """Check for escalation claim without actual tool call.

    This node catches cases where flash-lite (fallback model) claims
    to escalate but doesn't actually call the escalate_to_support tool.
    If detected, triggers automatic safety escalation.

    Args:
        state: Current conversation state with final_response and tool_calls

    Returns:
        State updates with safety_escalation_needed flag
    """
    final_response = state.get("final_response", "")
    tool_calls = state.get("accumulated_tool_calls") or state.get("tool_calls") or []
    user_context = state.get("user_context")
    session_id = state.get("session_id")
    user_input = state.get("user_input", "")
    # Use singleton auth service (not from state to avoid checkpointer serialization errors)
    auth_service = get_auth_service()

    # Guard: Strip fabricated support team responses (impersonation)
    stripped_response = _strip_impersonation(final_response)
    state_updates: Dict[str, Any] = {}
    if stripped_response != final_response:
        LOGGER.warning(
            "Impersonation guard triggered: stripped fabricated 'Response from Support Team' block"
        )
        final_response = stripped_response
        state_updates["final_response"] = final_response

    # If this session already has an active escalation (from a prior turn),
    # the bot may legitimately reference it — skip the false-positive check.
    if state.get("is_escalated_session"):
        LOGGER.debug("Session has active escalation from prior turn, skipping safety check")
        return {**state_updates, "safety_escalation_needed": False}

    # Check if escalate_to_support was actually called this turn
    escalation_tool_called = any(
        getattr(tc, "name", tc.get("name") if isinstance(tc, dict) else None)
        == "escalate_to_support"
        for tc in tool_calls
    )

    # If tool was called, check whether it actually succeeded
    if escalation_tool_called:
        tool_results = state.get("accumulated_tool_results") or []
        escalation_succeeded = any(
            getattr(tr, "name", tr.get("name") if isinstance(tr, dict) else None)
            == "escalate_to_support"
            and (
                getattr(tr, "success", None)
                if hasattr(tr, "success")
                else (tr.get("success") if isinstance(tr, dict) else False)
            )
            for tr in tool_results
        )

        if escalation_succeeded:
            LOGGER.debug("Escalation tool was called and succeeded, no safety check needed")
            return {**state_updates, "safety_escalation_needed": False}

        # Tool was called but FAILED — if bot still claims success, correct it
        if _detect_escalation_claim(final_response):
            LOGGER.warning(
                "Escalation tool was called but FAILED, yet bot claims success. "
                "Replacing response with failure message."
            )
            from shared.utils.error_messages import ErrorCategory, get_user_message

            final_response = get_user_message(ErrorCategory.ESCALATION, "failed")
            state_updates["final_response"] = final_response

        return {**state_updates, "safety_escalation_needed": False}

    # Check if response claims escalation without tool call
    if not _detect_escalation_claim(final_response):
        LOGGER.debug("No escalation claim detected in response")
        return {**state_updates, "safety_escalation_needed": False}

    # Safety check triggered!
    LOGGER.warning(
        "Escalation safety check triggered: Bot claimed escalation without tool call. "
        "Triggering automatic escalation."
    )

    try:
        from orchestrator.services.escalation_service import EscalationService

        safety_escalation_service = EscalationService(
            supabase_url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL"),
            supabase_key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY"),
        )

        if not safety_escalation_service.is_enabled():
            LOGGER.warning("Safety escalation skipped - escalation service not enabled")
            from shared.utils.error_messages import ErrorCategory, get_user_message

            state_updates["final_response"] = get_user_message(ErrorCategory.ESCALATION, "failed")
            return {**state_updates, "safety_escalation_needed": True}

        # Get organization short name
        org_short_name = None
        if user_context and user_context.organization_ids:
            org_short_name = await auth_service.get_organization_short_name(
                user_context.organization_ids[0]
            )

        # Extract summary from bot's response
        summary = _extract_escalation_summary(final_response)

        # Trigger the escalation
        safety_result = await safety_escalation_service.escalate_to_support(
            question_summary=summary,
            session_id=session_id,
            organization_id=(
                int(user_context.organization_ids[0])
                if user_context and user_context.organization_ids
                else None
            ),
            organization_short_name=org_short_name,
            customer_chat_id=user_context.chat_id if user_context else None,
            customer_topic_id=user_context.topic_id if user_context else None,
            customer_username=user_context.username if user_context else None,
            customer_email=user_context.user_email if user_context else None,
            conversation_context=(
                f"[SAFETY ESCALATION - Model claimed escalation without tool call]\n"
                f"User message: {user_input[:500]}"
            ),
            reason="safety_escalation",
        )

        if safety_result.get("success"):
            LOGGER.info("Safety escalation completed successfully")
        else:
            LOGGER.error(f"Safety escalation failed: {safety_result.get('error')}")
            # Auto-escalation failed AND bot claimed success — correct the response
            from shared.utils.error_messages import ErrorCategory, get_user_message

            state_updates["final_response"] = get_user_message(ErrorCategory.ESCALATION, "failed")

    except Exception as e:
        LOGGER.exception(f"Safety escalation error: {e}")
        # Exception during auto-escalation — correct the response
        from shared.utils.error_messages import ErrorCategory, get_user_message

        state_updates["final_response"] = get_user_message(ErrorCategory.ESCALATION, "failed")

    return {**state_updates, "safety_escalation_needed": True}


def _strip_impersonation(response_text: str) -> str:
    """Strip fabricated 'Response from Support Team' blocks from LLM output.

    The bot can hallucinate support team responses by mimicking the template
    used by handle_support_reply() in escalation_service.py. This guard
    detects and removes those blocks since the bot should never generate them —
    real support responses are sent as separate messages by the escalation service.
    """
    if not response_text:
        return response_text

    # Match the template from escalation_service.handle_support_reply():
    #   💬 **Response from Support Team**
    #   _Name says:_
    #   <fabricated content>
    # Also match markdown variants (**, *, single emoji variations)
    pattern = re.compile(
        r"💬\s*\*{0,2}Response from Support(?:\s+Team)?\*{0,2}\s*\n"  # Header line
        r"(?:_[^_]+\s+says:_\s*\n)?"  # Optional "_Name says:_" line
        r"(?:.*\n?)*",  # Fabricated content to end
        re.IGNORECASE,
    )

    cleaned = pattern.sub("", response_text).rstrip()

    if cleaned != response_text:
        # If stripping left only whitespace, return a safe fallback
        if not cleaned.strip():
            return response_text.split("💬")[0].rstrip()

    return cleaned


def _detect_escalation_claim(response_text: str) -> bool:
    """Detect if the response claims to escalate without actually calling the tool.

    Returns True if the response contains affirmative escalation language
    (e.g., "I will escalate", "I have escalated") but NOT negations
    (e.g., "cannot escalate", "won't escalate").
    """
    import re

    if not response_text:
        return False

    text_lower = response_text.lower()

    # Patterns indicating the bot claims to escalate
    escalation_patterns = [
        r"i will (now )?escalate",
        r"i('ve| have) escalated",
        r"escalating (this|your) (request|issue|matter)",
        r"i('m| am) escalating",
        r"let me escalate",
        r"escalate this (to|for)",
        # Patterns implying completed handoff to staff without using "escalate"
        # Use past tense / present-perfect to avoid false positives on future intent
        # e.g., "I have forwarded" but NOT "I can forward" or "I will forward once..."
        r"i('ve| have) (forwarded|passed|reported|notified|alerted)",
        r"(has|have) been (forwarded|passed|reported|sent) to (the )?(staff|team|support)",
        r"notified (the )?(staff|team|support)",
    ]

    # Negation patterns that indicate NOT escalating
    negation_patterns = [
        r"cannot escalate",
        r"can't escalate",
        r"won't escalate",
        r"will not escalate",
        r"unable to escalate",
        r"don't need to escalate",
        r"no need to escalate",
    ]

    # Preparatory patterns: bot is gathering info BEFORE escalating, not claiming it happened
    preparatory_patterns = [
        r"(information|details?|info) .{0,60}(to|before|for).{0,30}escalat",
        r"proceed with the escalation",
        r"before i (can )?escalate",
        r"in order to escalate",
        r"need .{0,40}to escalate",
        r"to proceed with .{0,20}escalat",
        # Preparatory handoff language (conditional/future, not completed)
        r"(details?|information|info) .{0,60}(to help|for) (the |our )?(staff|team|support)",
        r"once .{0,60}(forward|pass|send|report) .{0,40}(staff|team|support)",
        r"i (can|will) (then )?(forward|pass|send|report) .{0,40}(staff|team|support)",
    ]

    # Check for escalation claim
    claimed_escalation = any(re.search(p, text_lower) for p in escalation_patterns)

    # Check for negation
    is_negation = any(re.search(p, text_lower) for p in negation_patterns)

    # Check for preparatory language (gathering info before escalating)
    is_preparatory = any(re.search(p, text_lower) for p in preparatory_patterns)

    return claimed_escalation and not is_negation and not is_preparatory


def _extract_escalation_summary(response_text: str) -> str:
    """Extract escalation summary from bot response.

    The bot typically formats escalations with a "Summary:" section.
    Falls back to first 200 chars of response if no summary found.
    """
    import re

    if not response_text:
        return "Escalation requested"

    # Try to extract "Summary: ..." section
    summary_match = re.search(
        r"(?:\*\*)?summary[:\s]*(?:\*\*)?[\s]*([^\n]+(?:\n[^\n*#]+)*)",
        response_text,
        re.IGNORECASE,
    )
    if summary_match:
        summary = summary_match.group(1).strip()
        # Clean up markdown
        summary = re.sub(r"\*+", "", summary)
        return summary[:500]  # Limit length

    # Fallback: extract first meaningful paragraph
    lines = [line.strip() for line in response_text.split("\n") if line.strip()]
    for line in lines:
        # Skip greetings and short lines
        if len(line) > 30 and not line.lower().startswith(("thank you", "hello", "hi ")):
            return line[:300]

    # Last resort: truncate response
    return response_text[:200]
