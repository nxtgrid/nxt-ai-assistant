"""Expert router node for LangGraph.

Routes to expert handler if there's an active work packet for the session,
or if the user's input matches an expert's domain (e.g., /analyze command).

This node runs after prepare_context and determines whether the request
should be handled by an expert subagent or continue through the normal
Gemini conversation flow.

Routing priority:
1. Resume decision input (1/2/3 or keywords) -> handle resume action for existing packet
2. Active packets (in_progress, awaiting_input) -> resume immediately
3. Expert command + resumable packet for SAME site -> ask user if they want to retry
4. Expert command + similar completed work (same site within 14d) -> ask user if they want existing
5. Expert command match (/lpp, /kpi, etc.) -> create new packet
6. No match -> continue to normal Gemini flow

Usage in graph:
    builder.add_node("expert_router", expert_router)
    builder.add_conditional_edges(
        "expert_router",
        route_after_expert_check,
        {
            "expert": "expert_handler",
            "ask_resume": "ask_resume_failed",
            "ask_duplicate": "ask_about_duplicate",
            "continue": "parse_command",
        },
    )
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from orchestrator.graphs.state import ConversationState
from orchestrator.services.command_registry import get_command, get_expert_command_mapping
from orchestrator.services.expert_instructions_provider import ExpertInstructionsProvider
from orchestrator.services.lpp_parameter_help import (
    detect_lpp_parameter_help_request,
    format_lpp_parameter_help,
)
from orchestrator.services.pending_decision_service import (
    DECISION_TYPE_CANCEL,
    DECISION_TYPE_DUPLICATE,
    DECISION_TYPE_RESUME,
    RESOLUTION_ABANDON,
    RESOLUTION_CANCEL,
    RESOLUTION_RESUME,
    RESOLUTION_RUN_NEW,
    RESOLUTION_START_FRESH,
    RESOLUTION_VIEW_EXISTING,
    PendingDecisionService,
)
from orchestrator.services.work_packet_service import WorkPacketService
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger
from shared.utils.option_parsing import normalize_numeric_input

LOGGER = get_logger(__name__)

# Get expert commands from unified registry
# Maps "/command" -> "packet_type" (e.g., "/lpp" -> "light_preliminary_package")
EXPERT_COMMANDS = get_expert_command_mapping()

# NL keyword triggers for expert commands — phrases that unambiguously mean a
# specific expert workflow even when the user doesn't type the slash command.
# Each entry: (canonical_command, packet_type, [trigger_phrases])
# Only add entries where false-positive risk is negligible.
NL_EXPERT_TRIGGERS: list[tuple[str, str, list[str]]] = [
    (
        "/lpp",
        "light_preliminary_package",
        ["light preliminary package", " lpp ", " an lpp", "generate lpp", "create lpp", "make lpp"],
    ),
]

# Experts that require staff permissions (organization_id == STAFF_ORG_ID)
# NOTE: Command-level staff_only is already checked in command_parser.py
# This is an additional expert-level check for specific expert implementations
STAFF_ONLY_EXPERTS = {"package_generator"}


def _compute_related_session_ids(session_id: str, user_context: Any) -> List[str]:
    """Compute all session IDs that could belong to this user's chat context.

    In Telegram groups with topics, a message sent without replying (no topic_id)
    produces a different session_id than one sent within a topic thread. This
    function returns both variants so packet/decision lookups succeed regardless
    of whether the user replied in-thread or sent a standalone message.

    Returns:
        List of unique session IDs to check, primary session_id first.
    """
    if not user_context or not hasattr(user_context, "chat_id") or not user_context.chat_id:
        return [session_id] if session_id else []

    from orchestrator.utils.session_id import generate_session_id

    ids = [session_id] if session_id else []

    # If we DON'T have topic_id, the user sent a standalone message.
    # Generate the topic-variant session IDs for any known topic threads
    # by generating the chat-level session (which we already have).
    # But we also need to find packets created WITH a topic_id.
    # Unfortunately we don't know which topic_id was used — so instead,
    # we generate the chat-level session and rely on sessions_involved
    # containing it (from the expert_handler fix).

    # If we DO have topic_id, also generate the chat-level session
    # (without topic_id) to find packets created from standalone messages.
    if user_context.topic_id:
        chat_level = generate_session_id(
            source=user_context.source,
            chat_id=user_context.chat_id,
            user_id=user_context.user_id,
        )
        if chat_level and chat_level not in ids:
            ids.append(chat_level)
    else:
        # No topic_id — we ARE the chat-level session.
        # The session_id is already chat-level, so sessions_involved
        # should match if the packet stored the parent session.
        pass

    return ids


async def _is_expert_resumable(expert_id: Optional[str]) -> bool:
    """Check if an expert is resumable from its config.

    Resumable experts will re-run the previous step when resumed from failure,
    because it's unclear whether the previous step's output caused the issue.

    Args:
        expert_id: Expert ID to check

    Returns:
        True if expert is resumable
    """
    if not expert_id:
        return False
    try:
        provider = ExpertInstructionsProvider()
        config = await provider.get_expert_config(expert_id)
        if config:
            return bool(config.resumable)
    except Exception as e:
        LOGGER.warning(f"Could not check resumable for expert {expert_id}: {e}")
    return False


async def _handle_pending_command(
    session_id: str,
    user_context: Any,
    base_result: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Delegate /pending command to the dedicated handler module."""
    from orchestrator.services.pending_command_handler import handle_pending_command

    related_sessions = _compute_related_session_ids(session_id, user_context)
    result: Optional[Dict[str, Any]] = await handle_pending_command(
        session_id, user_context, related_sessions, base_result
    )
    return result


async def expert_router(state: ConversationState) -> Dict[str, Any]:
    """Route to expert handler if applicable.

    Checks (in order):
    1. Is the user responding to a resume prompt? -> Handle decision
    2. Is there an active work packet for this session? -> Resume
    3. Does input match expert command AND is there a resumable packet for SAME site? -> Ask user
    4. Does input match expert command AND is there similar completed work? -> Ask user
    5. Does the user input match an expert command? -> Create new packet

    Sets state:
    - expert_routing_decision: "expert" | "ask_resume" | "ask_duplicate" | "continue"
    - active_work_packet: Dict if found (for resume)
    - resumable_packet: Dict if found (for ask_resume)
    - similar_work_packet: Dict if found (for ask_duplicate)
    - matched_expert_id: str if routing to expert
    - expert_command: Original command if triggered by command

    Args:
        state: Current conversation state

    Returns:
        State updates for expert routing
    """
    session_id = state.get("session_id")
    user_input = state.get("user_input", "").strip()
    original_user_input = state.get("original_input") or user_input
    user_context = state.get("user_context")

    # Debug: Log key state values for routing decisions
    LOGGER.info(
        f"expert_router entry: session={session_id}, input='{user_input[:50]}', "
        f"awaiting_duplicate={state.get('awaiting_duplicate_decision')}, "
        f"awaiting_resume={state.get('awaiting_resume_decision')}"
    )

    # Default to continue (no expert routing)
    result: Dict[str, Any] = {
        "expert_routing_decision": "continue",
        "active_work_packet": None,
        "resumable_packet": None,
        "similar_work_packet": None,
        "matched_expert_id": None,
        "expert_command": None,
        "expert_raw_request": None,
        "expert_key_entity": None,  # Site/entity name extracted from command
    }

    if detect_lpp_parameter_help_request(user_input):
        return {
            **result,
            "final_response": format_lpp_parameter_help(user_input),
        }

    # Skip expert routing if no session
    if not session_id:
        return result

    # =====================================================================
    # Direct command: /pending — list active workflows, bypass LLM entirely
    # =====================================================================
    if user_input.lower().startswith("/pending"):
        pending_result = await _handle_pending_command(session_id, user_context, result)
        if pending_result is not None:
            return pending_result

    try:
        # Initialize services
        packet_service = WorkPacketService()
        expert_provider = ExpertInstructionsProvider()
        decision_service = PendingDecisionService()

        # Get organization ID for similarity checks
        org_id = None
        if user_context and hasattr(user_context, "organization_ids"):
            if user_context.organization_ids:
                org_id = int(user_context.organization_ids[0])

        # Compute all related session IDs (handles topic_id presence/absence)
        related_sessions = _compute_related_session_ids(session_id, user_context)

        # =====================================================================
        # Check -1: Pending decision for this session (HIGHEST priority)
        # This checks the pending_decisions table, NOT work_packets or graph state.
        # Decisions are created by ask_about_duplicate and ask_resume_failed nodes.
        # Try all related session IDs (topic-level and chat-level).
        # =====================================================================
        pending_decision = None
        for sid in related_sessions:
            pending_decision = await decision_service.get_pending_decision(sid)
            if pending_decision:
                break

        if pending_decision:
            decision_type = pending_decision["decision_type"]
            context = pending_decision["context"]
            decision_id = pending_decision["id"]

            LOGGER.info(
                f"Found pending decision {decision_id} (type={decision_type}, session={session_id})"
            )

            # If user sends a new slash command, abandon the pending decision and let
            # the new command through. This prevents "/lpp Test Chikanda" from being
            # swallowed by a pending duplicate/resume decision for a different site,
            # which would cause a subsequent "1" to run the WRONG site.
            input_lower_for_check = user_input.lower().strip()
            canonical_for_check = (
                input_lower_for_check.split("@")[0]
                if input_lower_for_check.startswith("/")
                else input_lower_for_check
            )
            is_new_slash_command = (
                input_lower_for_check.startswith("/") and canonical_for_check != "/cancel"
            )
            if is_new_slash_command:
                LOGGER.info(
                    f"New slash command '{user_input[:50]}' overrides pending decision "
                    f"{decision_id} (type={decision_type}) — abandoning decision"
                )
                await decision_service.resolve_decision(decision_id, "abandoned_by_new_command")
                # Fall through to normal command processing below

            elif decision_type == DECISION_TYPE_DUPLICATE:
                is_resumable = context.get("is_resumable", False)
                parsed = _parse_duplicate_decision(user_input, is_resumable=is_resumable)
                if parsed:
                    # Resolve the decision in database
                    await decision_service.resolve_decision(decision_id, parsed)
                    LOGGER.info(f"Resolved duplicate decision {decision_id}: {parsed}")

                    # Handle based on user's choice
                    return await _handle_duplicate_decision_from_pending(parsed, context, result)
                else:
                    # Couldn't parse - keep decision pending, re-prompt
                    LOGGER.info(f"Could not parse duplicate decision from: '{user_input}'")
                    if is_resumable:
                        error_msg = (
                            "I didn't understand your choice. "
                            "Please reply with **1** to run new, **2** to resume, "
                            "or **3** to cancel."
                        )
                    else:
                        error_msg = (
                            "I didn't understand your choice. "
                            "Please reply with **1** to run new, or **2** to cancel."
                        )
                    return {
                        **result,
                        "expert_routing_decision": "continue",
                        "final_response": error_msg,
                    }

            elif decision_type == DECISION_TYPE_RESUME:
                parsed = _parse_resume_decision(user_input)
                if parsed:
                    # Resolve the decision in database
                    await decision_service.resolve_decision(decision_id, parsed)
                    LOGGER.info(f"Resolved resume decision {decision_id}: {parsed}")

                    # Handle based on user's choice
                    return await _handle_resume_decision_from_pending(
                        parsed, context, result, packet_service, session_id
                    )
                else:
                    # Couldn't parse - keep decision pending, re-prompt
                    LOGGER.info(f"Could not parse resume decision from: '{user_input}'")
                    return {
                        **result,
                        "expert_routing_decision": "continue",
                        "final_response": (
                            "I didn't understand your choice. "
                            "Please reply with **1** to resume, **2** to start fresh, "
                            "or **3** to abandon and do something else."
                        ),
                    }

            elif decision_type == DECISION_TYPE_CANCEL:
                # User is responding to "which workflow to cancel?" prompt
                cancel_packets = context.get("active_packets", [])
                selected = _parse_cancel_selection(user_input, len(cancel_packets))

                if selected is not None:
                    await decision_service.resolve_decision(decision_id, f"cancel_{selected}")

                    if selected == 0:
                        # User chose to not cancel anything
                        LOGGER.info("User chose not to cancel any workflow")
                        return {
                            **result,
                            "expert_routing_decision": "continue",
                            "final_response": "OK, nothing cancelled.",
                        }

                    # Cancel the selected packet (1-indexed)
                    packet_idx = selected - 1
                    if packet_idx < len(cancel_packets):
                        packet = cancel_packets[packet_idx]
                        packet_id = packet["packet_id"]
                        packet_type = packet.get("packet_type", "workflow")
                        await packet_service.cancel_packet(
                            packet_id, "Cancelled by user", session_id=session_id
                        )
                        LOGGER.info(f"User selected and cancelled packet: {packet_id}")
                        return {
                            **result,
                            "expert_routing_decision": "continue",
                            "final_response": f"Cancelled {packet_type} ({packet_id[:8]}). What would you like to do?",
                        }
                    else:
                        return {
                            **result,
                            "expert_routing_decision": "continue",
                            "final_response": "Invalid selection. Nothing cancelled.",
                        }
                else:
                    # Couldn't parse - re-prompt
                    num_options = len(cancel_packets)
                    LOGGER.info(f"Could not parse cancel selection from: '{user_input}'")
                    return {
                        **result,
                        "expert_routing_decision": "continue",
                        "final_response": (
                            f"I didn't understand your choice. "
                            f"Please reply with a number (1-{num_options}) to cancel that workflow, "
                            f"or **0** to keep all running."
                        ),
                    }

        # =====================================================================
        # Check 0: Active packets in this session (HIGHEST priority)
        # This MUST come before resume decision parsing because user input
        # like "2" should go to awaiting_input packets, not be parsed as
        # a resume menu choice.
        #
        # EXCEPTION: New commands (starting with /) should NOT be captured by
        # active packets - they take priority. This allows users to run new
        # commands without being stuck in an active workflow.
        # Cancel/abort keywords: if 1 active packet, cancel it directly.
        # If multiple, ask user which one. If none, intercept to prevent
        # Gemini from misinterpreting "cancel" as a schedule cancellation.
        # =====================================================================
        # Try all related session IDs for active packet lookup
        active_packets = []
        for sid in related_sessions:
            active_packets = await packet_service.get_active_packets_for_session(sid)
            if active_packets:
                break

        # Check if user wants to cancel/abandon or is sending a new command
        input_lower = user_input.lower().strip()
        # Strip @botname suffix for Telegram group chats (e.g., "/cancel@YourBot" → "/cancel")
        canonical_command = (
            input_lower.split("@")[0] if input_lower.startswith("/") else input_lower
        )
        is_new_command = input_lower.startswith("/") and canonical_command != "/cancel"
        is_cancel_request = (
            input_lower
            in (
                "cancel",
                "abort",
                "stop",
                "nevermind",
                "never mind",
            )
            or canonical_command == "/cancel"
        )

        if active_packets and not is_new_command and not is_cancel_request:
            # Resume the most recent active packet
            packet = active_packets[0]

            # Thread disentanglement: if the current message is in a different thread
            # than the active packet, route to main LLM instead of resuming the packet.
            # This prevents "what's the weather?" from being consumed by an unrelated workflow.
            current_thread = state.get("thread_id")
            packet_thread = (packet.get("packet_state") or {}).get("thread_id")
            if current_thread and packet_thread and current_thread != packet_thread:
                LOGGER.info(
                    f"Thread mismatch: message thread={current_thread}, "
                    f"packet thread={packet_thread} — routing to main LLM"
                )
                return {**result, "expert_routing_decision": "continue"}
            else:
                LOGGER.info(
                    f"Found active packet for session: {packet['packet_id']} "
                    f"(status: {packet['packet_status']})"
                )

                return {
                    **result,
                    "expert_routing_decision": "expert",
                    "active_work_packet": packet,
                    "matched_expert_id": packet["assigned_expert"],
                }
        elif active_packets and is_cancel_request:
            if len(active_packets) == 1:
                # Exactly one active packet — cancel it directly
                packet = active_packets[0]
                LOGGER.info(
                    f"User cancelled sole active packet: {packet['packet_id']} with '{user_input}'"
                )
                await packet_service.cancel_packet(
                    packet["packet_id"], "Cancelled by user", session_id=session_id
                )
                return {
                    **result,
                    "expert_routing_decision": "continue",
                    "final_response": (
                        f"Cancelled the active {packet.get('packet_type', 'workflow')} "
                        f"({packet['packet_id'][:8]}). What would you like to do?"
                    ),
                }
            else:
                # Multiple active packets — ask user which one to cancel
                options_lines = []
                cancel_context_packets = []
                for i, pkt in enumerate(active_packets, 1):
                    pkt_type = pkt.get("packet_type", "workflow")
                    pkt_inputs = pkt.get("packet_inputs") or {}
                    entity = pkt_inputs.get("key_entity") or pkt_inputs.get("site_name") or ""
                    label = f"{pkt_type}"
                    if entity:
                        label += f" ({entity})"
                    options_lines.append(f"**{i}.** {label}")
                    cancel_context_packets.append(
                        {
                            "packet_id": pkt["packet_id"],
                            "packet_type": pkt_type,
                            "entity": entity,
                        }
                    )

                options_text = "\n".join(options_lines)
                prompt_msg = (
                    f"You have {len(active_packets)} active workflows. "
                    f"Which one would you like to cancel?\n\n"
                    f"{options_text}\n\n"
                    f"Reply with the number, or **0** to keep all running."
                )

                await decision_service.create_decision(
                    session_id=session_id,
                    decision_type=DECISION_TYPE_CANCEL,
                    context={"active_packets": cancel_context_packets},
                    prompt=prompt_msg,
                )
                LOGGER.info(
                    f"Multiple active packets ({len(active_packets)}) — asking user which to cancel"
                )
                return {
                    **result,
                    "expert_routing_decision": "continue",
                    "final_response": prompt_msg,
                }
        elif active_packets and is_new_command:
            # User is sending a new command - log but don't capture
            packet = active_packets[0]
            LOGGER.info(
                f"New command '{user_input[:50]}' overrides active packet {packet['packet_id']} - "
                f"packet will remain in {packet['packet_status']} status"
            )
        elif not active_packets and is_cancel_request:
            # No active workflows — intercept "cancel" so Gemini doesn't
            # misinterpret it as a schedule cancellation or other action
            LOGGER.info("User typed cancel but no active workflows to cancel")
            return {
                **result,
                "expert_routing_decision": "continue",
                "final_response": "No active workflows to cancel.",
            }

        # =====================================================================
        # Check 0.1: Auto-resumable packets (interrupted by deployment/SIGTERM)
        # If the session has a failed packet with auto_resumable=True, resume it
        # automatically without showing the "Resume / Start fresh / Abandon" prompt.
        # This covers both: packets the startup scan marked, and newly-interrupted ones.
        #
        # Skipped for scheduled executions — a bot-initiated wake (e.g. a scheduled
        # agent message going out) must not silently consume the user's workflow.
        # After 2 hours the packet is stale enough that the user gets the normal
        # "Resume?" prompt instead of a silent restart.
        # =====================================================================
        is_scheduled = (state.get("metadata") or {}).get("scheduled_execution", False)
        if not active_packets and not is_scheduled:
            auto_resumable_packets = await packet_service.get_resumable_packets_for_session(
                session_id,
                max_age_hours=2,
            )
            auto_resumable_packet = next(
                (
                    p
                    for p in auto_resumable_packets
                    if (p.get("packet_state") or {}).get("auto_resumable")
                ),
                None,
            )
            if auto_resumable_packet:
                # Don't auto-resume user_agent packets in human conversations — they were
                # launched by a scheduled agent and should only resume in that context.
                # Also skip if the user sent a new expert command.
                packet_expert = auto_resumable_packet.get("assigned_expert")
                packet_is_scheduled = (auto_resumable_packet.get("packet_state") or {}).get(
                    "scheduled_execution"
                )
                if packet_expert == "user_agent" or packet_is_scheduled:
                    LOGGER.info(
                        "Auto-resume skipped for packet %s (expert=%s, scheduled=%s) — "
                        "user_agent and scheduled packets must resume via explicit prompt",
                        auto_resumable_packet["packet_id"],
                        packet_expert,
                        packet_is_scheduled,
                    )
                    # Fall through to normal routing / resume prompt
                elif user_input and user_input.strip().startswith("/"):
                    LOGGER.info(
                        "Auto-resume bypassed: user sent new command '%s' "
                        "while interrupted packet %s exists — new command takes precedence",
                        user_input.strip()[:60],
                        auto_resumable_packet["packet_id"],
                    )
                    # Fall through to normal routing
                else:
                    expert_id = auto_resumable_packet.get("assigned_expert")
                    LOGGER.info(
                        "Auto-resuming deployment-interrupted packet %s (expert=%s, steps=%d)",
                        auto_resumable_packet["packet_id"],
                        expert_id,
                        len(auto_resumable_packet.get("steps_completed") or []),
                    )
                    # reset_failed_packet clears error state and sets status to in_progress
                    await packet_service.reset_failed_packet(
                        auto_resumable_packet["packet_id"],
                        session_id=session_id,
                        rerun_previous_step=False,  # Idempotency guards handle re-entry safely
                    )
                    return {
                        **result,
                        "expert_routing_decision": "expert",
                        "active_work_packet": auto_resumable_packet,
                        "matched_expert_id": expert_id,
                        "awaiting_resume_decision": False,
                        "user_input_consumed": True,
                    }

        # =====================================================================
        # Check 1: Is user input a resume decision? (1/2/3 or keywords)
        # Only checked if NO active packets - this is for failed/blocked packets.
        # =====================================================================
        decision = _parse_resume_decision(user_input)
        if decision:
            # User input looks like a resume decision - look for resumable packet
            resumable_packets = await packet_service.get_resumable_packets_for_session(
                session_id,
                max_age_hours=24,
            )

            if resumable_packets:
                resumable_packet = resumable_packets[0]
                LOGGER.info(
                    f"Processing resume decision: '{user_input}' -> {decision} "
                    f"for packet {resumable_packet['packet_id']}"
                )

                if decision == "resume":
                    # User wants to retry - reset packet to in_progress
                    # For resumable experts, also back up one step to re-run
                    # the previous step (in case its output caused the failure)
                    expert_id = resumable_packet.get("assigned_expert")
                    expert_is_resumable = await _is_expert_resumable(expert_id)

                    await packet_service.reset_failed_packet(
                        resumable_packet["packet_id"],
                        session_id=session_id,
                        rerun_previous_step=expert_is_resumable,
                    )
                    return {
                        **result,
                        "expert_routing_decision": "expert",
                        "active_work_packet": resumable_packet,
                        "matched_expert_id": expert_id,
                        "awaiting_resume_decision": False,
                        # Mark that user_input was consumed by the decision
                        "user_input_consumed": True,
                    }

                elif decision == "start_fresh":
                    # User wants to start fresh - cancel old packet and create new
                    await packet_service.cancel_packet(
                        resumable_packet["packet_id"],
                        reason="User chose to start fresh",
                        session_id=session_id,
                    )
                    LOGGER.info(f"Cancelled packet {resumable_packet['packet_id']}, starting fresh")

                    # Get original request and entity from the cancelled packet
                    packet_inputs = resumable_packet.get("packet_inputs") or {}
                    original_request = packet_inputs.get("raw_request", "")
                    original_entity = packet_inputs.get("key_entity") or packet_inputs.get(
                        "site_name"
                    )
                    packet_type = resumable_packet.get("packet_type")
                    expert_id = resumable_packet.get("assigned_expert")

                    # Cancel ALL other active/failed packets for same entity + org
                    org_id = resumable_packet.get("organization_id")
                    if packet_type and original_entity and org_id:
                        stale = await packet_service.cancel_stale_packets_for_entity(
                            packet_type=packet_type,
                            key_entity=original_entity,
                            organization_id=org_id,
                            exclude_packet_id=resumable_packet["packet_id"],
                            reason="Superseded by start-fresh request",
                            session_id=session_id,
                        )
                        if stale:
                            LOGGER.info(
                                f"Also cancelled {stale} other {packet_type} "
                                f"packets for {original_entity}"
                            )

                    if packet_type and expert_id and original_request:
                        LOGGER.info(
                            f"Starting fresh with original request: '{original_request}', "
                            f"entity: {original_entity}"
                        )
                        return {
                            **result,
                            "expert_routing_decision": "expert",
                            "active_work_packet": None,  # Will create new packet
                            "matched_expert_id": expert_id,
                            "expert_command": original_request,
                            "expert_packet_type": packet_type,
                            "expert_key_entity": original_entity,
                            "awaiting_resume_decision": False,
                            "resumable_packet": None,
                            # Mark that user_input was consumed by the decision
                            "user_input_consumed": True,
                        }
                    # If no packet info, fall through to normal flow

                elif decision == "abandon":
                    # User wants to abandon and do something else
                    await packet_service.cancel_packet(
                        resumable_packet["packet_id"],
                        reason="User abandoned",
                        session_id=session_id,
                    )
                    LOGGER.info(
                        f"Abandoned packet {resumable_packet['packet_id']}, continuing to normal flow"
                    )
                    return {
                        **result,
                        "expert_routing_decision": "continue",
                        "awaiting_resume_decision": False,
                        "resumable_packet": None,
                    }

        # =====================================================================
        # Check 2: User input matches expert command
        # (Moved BEFORE resumable packet check so we can match on type/site)
        # =====================================================================
        input_lower = user_input.lower()
        matched_command = None
        matched_packet_type = None
        expert_raw_request = None

        for command, packet_type in EXPERT_COMMANDS.items():
            if input_lower.startswith(command):
                matched_command = command
                matched_packet_type = packet_type
                break

        # NL fallback: detect natural-language phrasings (e.g. "generate an LPP for Gabu")
        # that the startswith check misses. Synthesise the canonical slash command so the
        # rest of the routing path (key_entity extraction, duplicate check, etc.) is identical.
        if not matched_command:
            padded = f" {input_lower} "  # pad so triggers like " lpp " match at word boundaries
            for nl_command, nl_packet_type, triggers in NL_EXPERT_TRIGGERS:
                if any(t in padded for t in triggers):
                    key_entity = None
                    if nl_packet_type == "light_preliminary_package":
                        from orchestrator.services.command_parser import parse_lpp_anchor_args

                        anchor = parse_lpp_anchor_args(user_input)
                        if anchor:
                            key_entity = f"{anchor['latitude']},{anchor['longitude']}"

                    if not key_entity:
                        key_entity = _extract_key_entity(user_input, nl_packet_type)
                    if key_entity:
                        expert_raw_request = original_user_input
                        user_input = f"{nl_command} {key_entity}"
                        input_lower = user_input.lower()
                        matched_command = nl_command
                        matched_packet_type = nl_packet_type
                        LOGGER.info(
                            f"NL expert routing: '{user_input[:60]}' matched "
                            f"{nl_command} → synthetic command '{user_input}'"
                        )
                        break

        if not matched_command:
            intent_route = state.get("planned_expert_route")
            if intent_route:
                route_args = intent_route.get("args") or intent_route.get("key_entity") or ""
                user_input = f"{intent_route['command']} {route_args}".strip()
                input_lower = user_input.lower()
                matched_command = intent_route["command"]
                matched_packet_type = intent_route["packet_type"]
                expert_raw_request = intent_route.get("raw_request") or original_user_input
                LOGGER.info(
                    f"Planned expert routing matched {matched_command} "
                    f"for packet_type={matched_packet_type}"
                )

        # =====================================================================
        # Check 3: Resumable packets - ONLY if same type AND same site
        # =====================================================================
        if matched_packet_type:
            # Extract site from current user input
            current_key_entity = _extract_key_entity(user_input, matched_packet_type)
            LOGGER.debug(
                f"Check 3: extracted key_entity='{current_key_entity}' "
                f"from input for packet_type={matched_packet_type}"
            )

            resumable_packets = await packet_service.get_resumable_packets_for_session(
                session_id,
                max_age_hours=24,
            )
            LOGGER.info(f"Check 3: found {len(resumable_packets)} resumable packets for session")

            if resumable_packets:
                packet = resumable_packets[0]
                packet_type = packet.get("packet_type")
                packet_inputs = packet.get("packet_inputs") or {}

                # Use stored key_entity if available, otherwise fall back to parsing
                old_key_entity = packet_inputs.get("key_entity")
                if not old_key_entity and packet_type:
                    old_request = packet_inputs.get("raw_request", "")
                    old_key_entity = _extract_key_entity(old_request, packet_type)

                # Only offer to resume if SAME packet type AND SAME site
                if (
                    packet_type == matched_packet_type
                    and current_key_entity
                    and old_key_entity
                    and current_key_entity.lower() == old_key_entity.lower()
                ):
                    LOGGER.info(
                        f"Found matching resumable packet for session: {packet['packet_id']} "
                        f"(status: {packet['packet_status']}, site: {old_key_entity})"
                    )

                    return {
                        **result,
                        "expert_routing_decision": "ask_resume",
                        "resumable_packet": packet,
                        "matched_expert_id": packet["assigned_expert"],
                        "expert_command": user_input,  # Keep current user input for "start fresh"
                        "expert_raw_request": expert_raw_request,
                        "expert_packet_type": matched_packet_type,
                        "expert_key_entity": current_key_entity,  # Site name from NEW request
                    }
                else:
                    LOGGER.info(
                        f"Resumable packet {packet['packet_id']} exists but doesn't match "
                        f"current request (packet_type: {packet_type} vs {matched_packet_type}, "
                        f"site: {old_key_entity} vs {current_key_entity}) - skipping"
                    )

        LOGGER.info(
            f"Expert router command check: input='{user_input[:50]}', "
            f"matched_command={matched_command}, matched_packet_type={matched_packet_type}"
        )

        if matched_packet_type:
            # Find which expert handles this packet type
            expert_id = await expert_provider.get_expert_for_packet_type(matched_packet_type)
            LOGGER.info(f"Expert router: expert_id for {matched_packet_type} = {expert_id}")

            if expert_id:
                # Check staff-only restriction
                if expert_id in STAFF_ONLY_EXPERTS:
                    # Use is_staff flag (properly set as boolean in resolve_auth)
                    is_staff = user_context and getattr(user_context, "is_staff", False)
                    if not is_staff:
                        LOGGER.info(
                            f"Expert {expert_id} is staff-only, user not authorized "
                            f"(is_staff={is_staff}, org_ids: {getattr(user_context, 'organization_ids', None)})"
                        )
                        # Continue to normal flow (will show "unknown command" or similar)
                        return result
                # =============================================================
                # Check 4: Similar completed work (deduplication)
                # =============================================================
                key_entity = _extract_key_entity(user_input, matched_packet_type)
                if key_entity:
                    similar_packets = await packet_service.find_similar_completed(
                        packet_type=matched_packet_type,
                        key_entity=key_entity,
                        since_days=14,
                        organization_id=org_id,
                    )

                    if similar_packets:
                        packet = similar_packets[0]
                        LOGGER.info(
                            f"Found similar completed work: {packet['packet_id']} "
                            f"(completed: {packet.get('completed_at', 'unknown')[:10]})"
                        )

                        return {
                            **result,
                            "expert_routing_decision": "ask_duplicate",
                            "similar_work_packet": packet,
                            "matched_expert_id": expert_id,
                            # Pass FULL user input (e.g., "/lpp ExampleGrid" not just "/lpp")
                            "expert_command": user_input,
                            "expert_raw_request": expert_raw_request,
                            "expert_packet_type": matched_packet_type,
                            "expert_key_entity": key_entity,  # Site/entity name for packet
                        }

                # No similar work found, create new packet
                LOGGER.info(
                    f"User input matches expert command: {matched_command} "
                    f"-> {expert_id} for {matched_packet_type}"
                )

                return {
                    **result,
                    "expert_routing_decision": "expert",
                    "active_work_packet": None,  # Will create new packet
                    "matched_expert_id": expert_id,
                    # Pass FULL user input as expert_command (e.g., "/lpp ExampleGrid" not just "/lpp")
                    "expert_command": user_input,
                    "expert_raw_request": expert_raw_request,
                    "expert_packet_type": matched_packet_type,
                    "expert_key_entity": key_entity,  # Site/entity name for packet
                }

        # No expert routing needed
        LOGGER.info("Expert router: no routing match, returning 'continue'")
        return result

    except Exception as e:
        LOGGER.exception(f"Error in expert router: {e}")
        # On error, continue with normal flow
        return result


def _extract_key_entity(user_input: str, packet_type: str) -> Optional[str]:
    """Extract the key entity from user input for similarity matching.

    This extracts identifiers like grid names, site names, ticket IDs
    that can be used to find similar previously completed work.

    Args:
        user_input: Full user input string
        packet_type: Type of packet being requested

    Returns:
        Key entity string or None if not extractable
    """
    input_lower = user_input.lower()

    if packet_type == "grid_analysis":
        # Extract grid name after common patterns
        patterns = ["grid ", "analyze ", "analyse ", "for ", "on "]
        for pattern in patterns:
            if pattern in input_lower:
                idx = input_lower.index(pattern) + len(pattern)
                # Take next word(s) as potential grid name
                remaining = user_input[idx:].strip()
                words = remaining.split()
                if words:
                    # Take up to 2 words (e.g., "GridA", "New Site")
                    entity = " ".join(words[:2]).strip(".,!?\"'")
                    if entity and len(entity) > 2:  # Skip very short matches
                        return entity

    elif packet_type == "kpi_report":
        # Extract grid name(s) or report type
        patterns = ["for ", "grids ", "grid ", "site "]
        for pattern in patterns:
            if pattern in input_lower:
                idx = input_lower.index(pattern) + len(pattern)
                remaining = user_input[idx:].strip()
                words = remaining.split()
                if words:
                    entity = words[0].strip(".,!?\"'")
                    if entity and len(entity) > 2:
                        return entity

    elif packet_type == "design_task":
        # Extract site/location name
        patterns = ["design for ", "design ", "site ", "location "]
        for pattern in patterns:
            if pattern in input_lower:
                idx = input_lower.index(pattern) + len(pattern)
                remaining = user_input[idx:].strip()
                words = remaining.split()
                if words:
                    entity = " ".join(words[:2]).strip(".,!?\"'")
                    if entity and len(entity) > 2:
                        return entity

    elif packet_type == "light_preliminary_package":
        # Extract site name after /lpp command
        patterns = ["/lpp ", "lpp ", "for ", "site "]
        for pattern in patterns:
            if pattern in input_lower:
                idx = input_lower.index(pattern) + len(pattern)
                remaining = user_input[idx:].strip()
                # For comma-separated multi-site args, take only the first site
                # as the key entity (e.g., "/lpp Site1, Site2" → "Site1")
                if "," in remaining:
                    remaining = remaining.split(",")[0].strip()
                words = remaining.split()
                if words:
                    # Take up to 3 words for site name (e.g., "New Site Alpha")
                    entity = " ".join(words[:3]).strip(".,!?\"'")
                    if entity and len(entity) > 2:
                        return entity

    return None


def _parse_resume_decision(user_input: str) -> Optional[str]:
    """Parse user's response to resume prompt.

    Handles:
    - "1", "resume", "retry" -> "resume"
    - "2", "start fresh", "new" -> "start_fresh"
    - "3", "abandon", "cancel", "stop" -> "abandon"

    Args:
        user_input: User's response text

    Returns:
        One of: "resume", "start_fresh", "abandon", or None if not parseable
    """
    import re

    text = user_input.lower().strip()

    # /cancel is always an abandon, even during resume decisions
    canonical = text.split("@")[0] if text.startswith("/") else text
    if canonical == "/cancel":
        return "abandon"

    # Other commands are not resume decisions - let them pass through
    if text.startswith("/"):
        return None

    # Normalize emoji numbers to plain digits
    normalized = normalize_numeric_input(text)

    # Check for numeric responses
    if normalized == "1":
        return "resume"
    if normalized == "2":
        return "start_fresh"
    if normalized == "3":
        return "abandon"

    # Check for keyword responses (whole word match to avoid false positives)
    # e.g., "knowledge" should not match "no"
    resume_keywords = ["resume", "retry", "continue", "yes"]
    start_fresh_keywords = ["start fresh", "new", "fresh", "start over", "begin"]
    abandon_keywords = ["abandon", "cancel", "stop", "no", "nevermind", "never mind"]

    def word_match(keyword: str, text: str) -> bool:
        """Check if keyword exists as a whole word in text."""
        pattern = r"\b" + re.escape(keyword) + r"\b"
        return bool(re.search(pattern, text))

    for keyword in resume_keywords:
        if word_match(keyword, text):
            return "resume"

    for keyword in start_fresh_keywords:
        if word_match(keyword, text):
            return "start_fresh"

    for keyword in abandon_keywords:
        if word_match(keyword, text):
            return "abandon"

    return None


def _parse_duplicate_decision(user_input: str, is_resumable: bool = False) -> Optional[str]:
    """Parse user's response to duplicate work prompt.

    Options depend on whether the expert is resumable:

    Non-resumable:
    - "1", "new", "fresh", "run" -> RESOLUTION_RUN_NEW
    - "2", "cancel", "stop", "no" -> RESOLUTION_CANCEL

    Resumable:
    - "1", "new", "fresh", "run" -> RESOLUTION_RUN_NEW
    - "2", "resume", "continue" -> RESOLUTION_RESUME
    - "3", "cancel", "stop", "no" -> RESOLUTION_CANCEL

    Uses normalize_numeric_input to handle numeric input variants.

    Args:
        user_input: User's response text
        is_resumable: Whether the expert is resumable (affects option mapping)

    Returns:
        One of: RESOLUTION_RUN_NEW, RESOLUTION_RESUME, RESOLUTION_CANCEL, or None
    """
    text = user_input.lower().strip()

    # Normalize emoji numbers to plain digits
    normalized = normalize_numeric_input(text)

    # Check for numeric responses - mapping depends on is_resumable
    # Note: explicit str() casts to satisfy mypy since constants are imported from another module
    if normalized == "1":
        return str(RESOLUTION_RUN_NEW)

    if is_resumable:
        # Resumable: 1=run_new, 2=resume, 3=cancel
        if normalized == "2":
            return str(RESOLUTION_RESUME)
        if normalized == "3":
            return str(RESOLUTION_CANCEL)
    else:
        # Non-resumable: 1=run_new, 2=cancel
        if normalized == "2":
            return str(RESOLUTION_CANCEL)

    # Check for keyword responses (work regardless of is_resumable)
    new_keywords = ["new", "fresh", "run", "start", "create", "different"]
    resume_keywords = ["resume", "continue", "retry"]
    cancel_keywords = ["cancel", "stop", "no", "nevermind", "never mind", "abort"]

    for keyword in new_keywords:
        if keyword in text:
            return str(RESOLUTION_RUN_NEW)

    # Resume keywords only apply if expert is resumable
    if is_resumable:
        for keyword in resume_keywords:
            if keyword in text:
                return str(RESOLUTION_RESUME)

    for keyword in cancel_keywords:
        if keyword in text:
            return str(RESOLUTION_CANCEL)

    return None


def _parse_cancel_selection(user_input: str, num_options: int) -> Optional[int]:
    """Parse user's response to cancel selection prompt.

    Handles:
    - "0" -> 0 (keep all running)
    - "1", "2", ... -> 1-indexed selection
    - "cancel" / "none" / "nevermind" -> 0 (keep all running)

    Args:
        user_input: User's response text
        num_options: Number of cancel options presented

    Returns:
        Integer selection (0 = keep all, 1..N = cancel that packet), or None if not parseable
    """
    text = user_input.lower().strip()

    # Normalize emoji numbers
    normalized = normalize_numeric_input(text)

    # Try numeric parsing
    try:
        num = int(normalized)
        if 0 <= num <= num_options:
            return num
    except ValueError:
        pass

    # Keyword matching
    keep_keywords = ["none", "nevermind", "never mind", "keep", "no", "cancel", "/cancel", "0"]
    for keyword in keep_keywords:
        if keyword in text:
            return 0

    return None


async def _handle_duplicate_decision_from_pending(
    parsed: str,
    context: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Handle a duplicate decision parsed from user input.

    Args:
        parsed: The parsed decision (RESOLUTION_RUN_NEW, RESOLUTION_RESUME, RESOLUTION_CANCEL)
        context: Decision context from pending_decisions table
        result: Base result dict to extend

    Returns:
        State updates for the routing decision
    """
    similar_packet = context.get("similar_work_packet")

    if parsed == RESOLUTION_RUN_NEW:
        # User wants to run new analysis - continue with expert
        LOGGER.info("User chose to run new analysis despite existing work")
        expert_id = context.get("matched_expert_id")
        expert_command = context.get("expert_command")
        packet_type = context.get("expert_packet_type")
        key_entity = context.get("expert_key_entity")

        return {
            **result,
            "expert_routing_decision": "expert",
            "active_work_packet": None,  # Will create new packet
            "matched_expert_id": expert_id,
            "expert_command": expert_command,
            "expert_packet_type": packet_type,
            "expert_key_entity": key_entity,
            "awaiting_duplicate_decision": False,
            "similar_work_packet": None,
            # Mark that user_input was consumed by the decision
            # expert_handler should NOT pass it to resume_from_input
            "user_input_consumed": True,
        }

    elif parsed == RESOLUTION_RESUME:
        # User wants to resume the existing completed work
        # This means re-running from where it was (or re-running with modifications)
        LOGGER.info(
            f"User chose to resume existing work: {similar_packet.get('packet_id') if similar_packet else 'unknown'}"
        )

        if similar_packet:
            expert_id = context.get("matched_expert_id")
            packet_type = context.get("expert_packet_type")
            key_entity = context.get("expert_key_entity")

            # Set flag to indicate we're resuming existing work
            # The expert_handler will use the similar_packet as a starting point
            return {
                **result,
                "expert_routing_decision": "expert",
                "active_work_packet": similar_packet,  # Resume this packet
                "matched_expert_id": expert_id,
                "expert_packet_type": packet_type,
                "expert_key_entity": key_entity,
                "awaiting_duplicate_decision": False,
                "similar_work_packet": None,
                "resume_from_completed": True,  # Flag for special handling
                # Mark that user_input was consumed by the decision
                # expert_handler should NOT pass it to resume_from_input
                "user_input_consumed": True,
            }

        # No packet to resume - fall through to cancel
        LOGGER.warning("User chose resume but no similar_work_packet in context")

    elif parsed == RESOLUTION_CANCEL:
        # User wants to do something else
        LOGGER.info("User cancelled duplicate decision - returning to normal flow")
        return {
            **result,
            "expert_routing_decision": "continue",
            "final_response": "Okay, what would you like to do instead?",
            "awaiting_duplicate_decision": False,
            "similar_work_packet": None,
        }

    # Legacy support for RESOLUTION_VIEW_EXISTING (deprecated)
    elif parsed == RESOLUTION_VIEW_EXISTING:
        external_url = similar_packet.get("external_url") if similar_packet else None
        outputs = (similar_packet.get("packet_outputs") or {}) if similar_packet else {}
        summary = outputs.get("summary", "")

        if summary:
            summary = sanitize_error_for_user(summary, context="")

        if external_url:
            response = f"Here's the existing report: {external_url}"
            if summary:
                response += f"\n\n**Summary:**\n{summary[:500]}"
        else:
            response = "Here's the summary of the existing work:"
            if summary:
                response += f"\n\n{summary}"
            else:
                response += "\n\n(No summary available)"

        return {
            **result,
            "expert_routing_decision": "continue",
            "final_response": response,
            "awaiting_duplicate_decision": False,
            "similar_work_packet": None,
        }

    # Fallback - return continue
    return result


async def _handle_resume_decision_from_pending(
    parsed: str,
    context: Dict[str, Any],
    result: Dict[str, Any],
    packet_service: WorkPacketService,
    session_id: str,
) -> Dict[str, Any]:
    """Handle a resume decision parsed from user input.

    Args:
        parsed: The parsed decision ("resume", "start_fresh", or "abandon")
        context: Decision context from pending_decisions table
        result: Base result dict to extend
        packet_service: WorkPacketService instance
        session_id: Current session ID

    Returns:
        State updates for the routing decision
    """
    resumable_packet = context.get("resumable_packet")

    if not resumable_packet:
        LOGGER.warning("Resume decision but no resumable_packet in context")
        return result

    if parsed == RESOLUTION_RESUME:
        # User wants to retry - reset packet to in_progress
        # For resumable experts, also back up one step to re-run
        # the previous step (in case its output caused the failure)
        expert_id = resumable_packet.get("assigned_expert")
        expert_is_resumable = await _is_expert_resumable(expert_id)

        await packet_service.reset_failed_packet(
            resumable_packet["packet_id"],
            session_id=session_id,
            rerun_previous_step=expert_is_resumable,
        )
        return {
            **result,
            "expert_routing_decision": "expert",
            "active_work_packet": resumable_packet,
            "matched_expert_id": expert_id,
            "awaiting_resume_decision": False,
            "resumable_packet": None,
            # Mark that user_input was consumed by the decision
            "user_input_consumed": True,
        }

    elif parsed == RESOLUTION_START_FRESH:
        # User wants to start fresh - cancel old packet and create new
        await packet_service.cancel_packet(
            resumable_packet["packet_id"],
            reason="User chose to start fresh",
            session_id=session_id,
        )
        LOGGER.info(f"Cancelled packet {resumable_packet['packet_id']}, starting fresh")

        # Get original request and entity from the cancelled packet
        packet_inputs = resumable_packet.get("packet_inputs") or {}
        original_request = packet_inputs.get("raw_request", "")
        original_entity = packet_inputs.get("key_entity") or packet_inputs.get("site_name")
        packet_type = resumable_packet.get("packet_type")
        expert_id = resumable_packet.get("assigned_expert")

        # Also check context for preserved values
        if not packet_type:
            packet_type = context.get("expert_packet_type")
        if not expert_id:
            expert_id = context.get("matched_expert_id")
        if not original_entity:
            original_entity = context.get("expert_key_entity")
        if not original_request:
            original_request = context.get("expert_command", "")

        # Cancel ALL other active/failed packets for same entity + org
        org_id = resumable_packet.get("organization_id")
        if packet_type and original_entity and org_id:
            stale = await packet_service.cancel_stale_packets_for_entity(
                packet_type=packet_type,
                key_entity=original_entity,
                organization_id=org_id,
                exclude_packet_id=resumable_packet["packet_id"],
                reason="Superseded by start-fresh request",
                session_id=session_id,
            )
            if stale:
                LOGGER.info(
                    f"Also cancelled {stale} other {packet_type} packets for {original_entity}"
                )

        if packet_type and expert_id and original_request:
            LOGGER.info(
                f"Starting fresh with original request: '{original_request}', "
                f"entity: {original_entity}"
            )
            return {
                **result,
                "expert_routing_decision": "expert",
                "active_work_packet": None,  # Will create new packet
                "matched_expert_id": expert_id,
                "expert_command": original_request,
                "expert_packet_type": packet_type,
                "expert_key_entity": original_entity,
                "awaiting_resume_decision": False,
                "resumable_packet": None,
                # Mark that user_input was consumed by the decision
                "user_input_consumed": True,
            }
        # If no packet info, fall through to normal flow

    elif parsed == RESOLUTION_ABANDON:
        # User wants to abandon and do something else
        await packet_service.cancel_packet(
            resumable_packet["packet_id"],
            reason="User abandoned",
            session_id=session_id,
        )
        LOGGER.info(f"Abandoned packet {resumable_packet['packet_id']}, continuing to normal flow")
        return {
            **result,
            "expert_routing_decision": "continue",
            "awaiting_resume_decision": False,
            "resumable_packet": None,
        }

    # Fallback
    return result


def parse_expert_command(user_input: str) -> Dict[str, Any]:
    """Parse expert command and extract parameters.

    Handles formats like:
    - /analyze grid ExampleGrid
    - /analyze ExampleGrid last 7 days
    - /kpi weekly
    - /report monthly grids GridA, GridB

    Args:
        user_input: Full user input string

    Returns:
        Dict with parsed command, packet_type, raw_args, and command_def
    """
    parts = user_input.strip().split(None, 1)
    command = parts[0].lower() if parts else ""
    raw_args = parts[1] if len(parts) > 1 else ""

    # Get packet_type from unified registry
    packet_type = EXPERT_COMMANDS.get(command)

    # Also get the full command definition for additional metadata
    cmd_name = command.lstrip("/")
    cmd_def = get_command(cmd_name)

    result = {
        "command": command,
        "packet_type": packet_type,
        "raw_args": raw_args,
        "command_def": cmd_def,
    }

    return result


__all__ = [
    "expert_router",
    "parse_expert_command",
    "EXPERT_COMMANDS",
    "STAFF_ONLY_EXPERTS",
    "_extract_key_entity",
]
