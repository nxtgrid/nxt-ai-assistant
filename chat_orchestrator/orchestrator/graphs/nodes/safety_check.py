"""Safety check node for LangGraph.

This node runs post-generation safety checks:
1. Detects when the model claims to have escalated without calling the tool
2. Strips fabricated "Response from Support Team" blocks (impersonation guard)
3. Detects a raw tool-call invocation leaked into the response as plain text
   (e.g. "Call Tool: escalate_to_support(...)") instead of a native function
   call, and replaces it before it reaches the customer
"""

import json
import os
import re
from typing import Any, Dict

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState
from orchestrator.services.ticketing.attachment_capture import extract_media_file_ids
from shared.auth import get_auth_service
from shared.utils.error_messages import ErrorCategory, get_user_message


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
    # The model's response as generated. Every guard below may replace
    # `final_response` with a customer-safe message, so the internal
    # escalation summary must be derived from this instead -- see the summary
    # block further down.
    original_response = final_response
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

    # Guard: the model can emit a tool invocation as plain text (e.g.
    # "Call Tool: escalate_to_support(...)") instead of a native function call —
    # observed from the fallback model in production. That raw syntax must never
    # reach the customer. Recover the model's own summary/context from the leaked
    # call's keyword arguments (better than falling back to a truncated dump of
    # the raw syntax), replace the response with a safe message, and treat it the
    # same as a natural-language escalation claim below.
    known_tool_names = extract_declared_tool_names(state)
    raw_tool_call_leaked = _detect_raw_tool_call_leak(final_response, known_tool_names)
    leaked_kwargs: Dict[str, str] = {}
    if raw_tool_call_leaked:
        leaked_tool = _find_leaked_tool_name(final_response, known_tool_names)
        LOGGER.error(
            f"Raw tool-call syntax leaked into response (tool={leaked_tool}), "
            f"replacing with safe message: {final_response[:200]!r}"
        )
        leaked_kwargs = _extract_kwargs_from_tool_call_text(final_response, leaked_tool)
        final_response = get_user_message(ErrorCategory.ESCALATION, "failed")
        state_updates["final_response"] = final_response

    # If the session has an active escalation AND this turn was already handled by
    # auto-forwarding (escalation_forward_result is set), the bot's response is the
    # confirmation message — skip the false-positive check.
    # Do NOT skip when forward_result is None: that means forwarding failed and the
    # LLM processed the turn normally, so we still need to catch fabricated claims.
    if state.get("is_escalated_session") and state.get("escalation_forward_result"):
        LOGGER.debug("Session has active escalation and forward succeeded, skipping safety check")
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

        # Tool was called but FAILED — correct response if it claims success, then
        # fall through to trigger a backup escalation so the customer is not lost.
        LOGGER.warning("Escalation tool was called but FAILED — triggering backup escalation")
        if _detect_escalation_claim(final_response):
            final_response = get_user_message(ErrorCategory.ESCALATION, "failed")
            state_updates["final_response"] = final_response

        # Fall through to the backup escalation trigger below instead of returning early.

    # Check if response claims escalation without tool call.
    # When escalation_tool_called=True the guard below is always bypassed — backup fires
    # unconditionally for any tool failure, regardless of the bot's response content.
    # This is intentional: a failed tool call means the customer may not be escalated,
    # so we attempt backup even when the bot's response does not claim escalation.
    if (
        not escalation_tool_called
        and not raw_tool_call_leaked
        and not _detect_escalation_claim(final_response)
    ):
        LOGGER.debug("No escalation claim detected in response")
        return {**state_updates, "safety_escalation_needed": False}

    # Guard: if session_id is None the backup Telegram message has no mapping to attach to,
    # leaving support staff with an unanswerable thread. Fail gracefully.
    if not session_id:
        LOGGER.warning("Safety backup escalation skipped — session_id is None")
        state_updates["final_response"] = get_user_message(ErrorCategory.ESCALATION, "failed")
        return {**state_updates, "safety_escalation_needed": True}

    # Safety check triggered: either tool failed (fall-through above) or bot claimed
    # escalation without calling the tool at all.
    if not escalation_tool_called:
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
            state_updates["final_response"] = get_user_message(ErrorCategory.ESCALATION, "failed")
            return {**state_updates, "safety_escalation_needed": True}

        # Get organization short name
        org_short_name = None
        if user_context and user_context.organization_ids:
            org_short_name = await auth_service.get_organization_short_name(
                user_context.organization_ids[0]
            )

        # The escalation card's "Question:" -- the first thing support staff
        # read. Prefer the model's own summary recovered from a leaked raw
        # tool call, then its context, then the response as generated.
        #
        # Never `final_response`: by this point the guards above may have
        # replaced it with the generic "I tried to get help but ran into an
        # issue" message, which made every safety escalation arrive with that
        # error text as its question (2026-08-24 Hardrock incident). When the
        # response was raw call syntax there is no prose in it to summarise
        # either, so the customer's own message is the useful fallback.
        if raw_tool_call_leaked:
            fallback_summary = (user_input or "").strip() or _extract_escalation_summary(
                original_response
            )
        else:
            fallback_summary = _extract_escalation_summary(original_response)
        summary = (
            leaked_kwargs.get("question_summary")
            or leaked_kwargs.get("conversation_context")
            or fallback_summary
        )

        # Trigger the escalation
        if escalation_tool_called:
            esc_context = (
                f"[SAFETY ESCALATION - escalate_to_support tool was called but FAILED]\n"
                f"User message: {user_input[:500]}"
            )
        elif raw_tool_call_leaked:
            esc_context = (
                f"[SAFETY ESCALATION - raw tool-call text leaked from model instead of "
                f"a native function call]\n"
                f"User message: {user_input[:500]}"
            )
            if leaked_kwargs.get("conversation_context"):
                esc_context += f"\nModel context: {leaked_kwargs['conversation_context'][:500]}"
        else:
            esc_context = (
                f"[SAFETY ESCALATION - Model claimed escalation without tool call]\n"
                f"User message: {user_input[:500]}"
            )
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
            conversation_context=esc_context,
            reason="safety_escalation",
            media_file_ids=extract_media_file_ids(state.get("metadata", {})),
        )

        if safety_result.get("success"):
            LOGGER.info("Safety escalation completed successfully")
        else:
            LOGGER.error(f"Safety escalation failed: {safety_result.get('error')}")
            # Auto-escalation failed AND bot claimed success — correct the response
            state_updates["final_response"] = get_user_message(ErrorCategory.ESCALATION, "failed")

    except Exception as e:
        LOGGER.exception(f"Safety escalation error: {e}")
        # Exception during auto-escalation — correct the response
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


# Structural signals that a model wrote a tool invocation as prose instead of
# emitting a native function call. These are tool-name agnostic on purpose: the
# 2026-08-24 incident leaked a tool the guard did know about, in a syntax it did
# not, so anchoring on any single syntax just moves the blind spot.
_TOOL_CALL_MARKERS = (
    # "Call Tool: x", "Tool Call: x", "functionCall: x", "tool_call = x"
    r"(?:^|\n)\s*(?:call\s+tool|tool\s+call|tool_call|function\s*call|invoke\s+tool)\s*[:=]",
    # Gemini's python-style tool harness leaking through verbatim
    r"\bdefault_api\s*\.",
    # Fenced tool blocks (```tool_code, ```tool_call, ```function_call)
    r"```\s*(?:tool_code|tool_call|tool_use|function_call)",
)

# Fallback tool names for when the turn's declared payload is unavailable.
# Only the orchestrator's own built-ins — MCP tool names vary per deployment and
# arrive via tools_payload, which is why known_tool_names is the primary source.
_CORE_TOOL_NAMES = frozenset(
    {
        "escalate_to_support",
        "fetch_training_image",
        "store_user_preference",
        "list_user_preferences",
        "delete_user_preference",
        "start_expert_workflow",
        "expert_list_steps",
        "expert_find_packet",
        "expert_get_packet_state",
        "expert_run_steps",
    }
)

# JSON keys a model uses to name the tool it is "calling" when it serialises the
# whole invocation as an object instead of calling it.
_TOOL_NAME_JSON_KEYS = r"tool|tool_name|name|function|function_name"


def extract_declared_tool_names(state: Dict[str, Any]) -> set:
    """Tool names declared to the model this turn, for leak detection.

    Sourced from ``tools_payload`` so the guard covers every tool actually put
    in front of the model — including per-deployment MCP tools this module has
    no static knowledge of — rather than a hardcoded list that silently rots.
    """
    names = set(_CORE_TOOL_NAMES)
    for func in state.get("tools_payload") or []:
        name = func.get("name") if isinstance(func, dict) else None
        if name:
            names.add(name)
    return names


def _detect_raw_tool_call_leak(response_text: str, known_tool_names=None) -> bool:
    """Detect a tool invocation leaked into the response as plain text.

    The orchestrator relies entirely on native provider function-calling (Gemini
    ``functionCall`` parts / OpenRouter ``tool_calls``) — there is no text-based
    tool-call scheme, so nothing else strips this and it reaches the customer
    as-is.

    Detection keys on call *structure* rather than one tool name in one syntax:
    a tool-call marker anywhere, or any known tool name in call position —
    followed by ``(`` (``name(arg=1)``) or ``{`` (``name\n{"arg": 1}``, the
    shape that caused the 2026-08-24 leak), or named as the tool inside a
    serialised call object. A bare prose mention of a tool name with no call
    structure is deliberately not a match.
    """
    if not response_text:
        return False

    for marker in _TOOL_CALL_MARKERS:
        if re.search(marker, response_text, re.IGNORECASE):
            return True

    for name in known_tool_names or _CORE_TOOL_NAMES:
        escaped = re.escape(name)
        # name( ... )  or  name { ... }  — \s* spans the newline in the JSON form
        if re.search(rf"\b{escaped}\s*[(\{{]", response_text):
            return True
        # {"tool": "name", ...} / {"name": "name", "arguments": {...}}
        if re.search(rf'"(?:{_TOOL_NAME_JSON_KEYS})"\s*:\s*"{escaped}"', response_text):
            return True

    return False


def _find_leaked_tool_name(response_text: str, known_tool_names=None) -> str:
    """Return which tool the leaked call names, defaulting to escalate_to_support."""
    for name in known_tool_names or _CORE_TOOL_NAMES:
        escaped = re.escape(name)
        if re.search(rf"\b{escaped}\s*[(\{{]", response_text) or re.search(
            rf'"(?:{_TOOL_NAME_JSON_KEYS})"\s*:\s*"{escaped}"', response_text
        ):
            return name
    return "escalate_to_support"


def _extract_first_json_object(text: str, start: int):
    """Return the brace-balanced JSON object starting at/after ``start``, or None."""
    open_idx = text.find("{", start)
    if open_idx == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(open_idx, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
    return None


def _extract_balanced_call_args(text: str, start: int):
    """Return the argument text inside the call whose ``(`` follows ``start``.

    Brace/quote-aware for the same reason ``_extract_first_json_object`` is:
    the call is embedded in free-form model output, so it may be wrapped
    (``[Call Tool: name(...)]``) or trailed by prose, and its arguments may
    themselves contain parentheses. Matching to end-of-string instead —
    ``\\)\\s*$`` — silently recovered nothing for any leak that did not end at
    the closing paren, which is how the 2026-08-24 bracket-wrapped leak lost
    the model's own context.
    """
    open_idx = text.find("(", start)
    if open_idx == -1:
        return None

    depth = 0
    quote = None
    escaped = False
    for i in range(open_idx, len(text)):
        char = text[i]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
    return None


def _extract_kwargs_from_tool_call_text(response_text: str, tool_name: str) -> Dict[str, str]:
    """Best-effort recovery of arguments from a leaked raw tool-call string.

    Used so the internal escalation notice carries the model's own summary
    (e.g. ``question_summary``) instead of a truncated dump of the raw call
    syntax. Handles both leak shapes seen in production — Python-style keyword
    arguments and a JSON argument object. Returns {} if neither parses.
    """
    if not response_text:
        return {}

    # Shape 1: name(key='value', ...)
    name_pos = response_text.find(tool_name)
    args_text = (
        _extract_balanced_call_args(response_text, name_pos + len(tool_name))
        if name_pos != -1
        else None
    )
    if args_text:
        kwargs: Dict[str, str] = {}
        for kw_match in re.finditer(
            r"(\w+)\s*=\s*'([^']*)'|(\w+)\s*=\s*\"([^\"]*)\"", args_text
        ):
            key = kw_match.group(1) or kw_match.group(3)
            value = kw_match.group(2) if kw_match.group(1) else kw_match.group(4)
            kwargs[key] = value
        if kwargs:
            return kwargs

    # Shape 2: name\n{"key": "value", ...} — the 2026-08-24 leak
    name_pos = response_text.find(tool_name)
    raw_json = _extract_first_json_object(response_text, name_pos if name_pos != -1 else 0)
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, dict):
            # Unwrap {"name": ..., "arguments": {...}} envelopes
            for envelope_key in ("arguments", "args", "parameters"):
                inner = parsed.get(envelope_key)
                if isinstance(inner, dict):
                    parsed = inner
                    break
            return {k: v for k, v in parsed.items() if isinstance(v, str)}

    return {}


def _detect_escalation_claim(response_text: str) -> bool:
    """Detect if the response claims to escalate without actually calling the tool.

    Returns True if the response contains affirmative escalation language
    (e.g., "I will escalate", "I have escalated") but NOT negations
    (e.g., "cannot escalate", "won't escalate").
    """
    import re

    if not response_text:
        return False

    # Models emit typographic apostrophes (U+2019) routinely, which silently
    # defeated every "i've"/"i'm"/"can't" pattern below and disabled the backup
    # escalation for any claim phrased with one. Normalise before matching.
    text_lower = response_text.lower()
    for curly in ("\u2019", "\u2018", "\u02bc", "\u2032", "`"):
        text_lower = text_lower.replace(curly, "'")

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
