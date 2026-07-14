"""LangGraph-based conversation orchestration.

This module provides a StateGraph implementation of the conversation flow,
replacing the imperative loop in ConversationOrchestrator.handle_chat().

Phase 1 + Phase 2 graph flow:

    [START] → [prepare] → [call_gemini] → [execute_tools]? → [verify]? → [respond] → [END]
                  ↑__________________|                          |
                  ↑_____________________________________________|  (regenerate on fail)
                                                                ↓
                                                        [escalate] → [respond] → [END]
                                                        (after max verification failures)

Nodes:
    - prepare: Build initial Gemini history and context
    - call_gemini: Call the Gemini API
    - execute_tools: Execute requested tool calls
    - verify: Verify response using LLM-as-judge (Phase 2)
    - escalate: Escalate to support after verification failures (Phase 2)
    - respond: Finalize and return response

Conditional routing:
    - After call_gemini: route to execute_tools if tool calls, else verify (or respond)
    - After execute_tools: loop back to call_gemini
    - After verify: respond if passed, regenerate if failed (up to max attempts), then escalate
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Literal, Optional, Set

from langgraph.graph import END, START, StateGraph

from orchestrator.clients.gemini import GeminiClient
from orchestrator.config.settings import AppSettings, get_settings, inject_reasoning_param
from orchestrator.graphs.state import ConversationState
from orchestrator.models.schemas import (
    ChatResponse,
    ConversationMessage,
    FunctionCall,
    ToolCallResult,
)
from orchestrator.services.loop_detector import LoopDetectionResult, detect_cross_request_loop
from orchestrator.services.tool_executor import ToolExecutor
from orchestrator.services.tool_registry import ToolRegistry
from orchestrator.utils.response_sanitizer import sanitize_tool_response
from shared.utils.error_messages import ErrorCategory, get_user_message
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


# finishReason values that indicate content was blocked and need special handling
# These typically result in empty content and should NOT be treated as "rephrase" errors
BLOCKED_FINISH_REASONS = frozenset(
    {
        "SAFETY",  # Safety filter blocked
        "RECITATION",  # Copyright/recitation block
        "PROHIBITED_CONTENT",  # Content policy violation
        "BLOCKLIST",  # Blocklist match
        "SPII",  # Sensitive PII detected
    }
)

# finishReason values that indicate model errors (transient, user should retry)
TRANSIENT_FINISH_REASONS = frozenset(
    {
        "MALFORMED_FUNCTION_CALL",  # Model failed to format function call correctly
    }
)

# finishReason values that should trigger automatic escalation for human review
ESCALATION_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "PROHIBITED_CONTENT",
    }
)


def extract_finish_reason(response: Dict[str, Any]) -> Optional[str]:
    """Extract finishReason from Gemini response.

    Handles cases where candidates array may be empty or missing.

    Args:
        response: Raw Gemini API response

    Returns:
        finishReason string if found, None otherwise
    """
    candidates = response.get("candidates", [])
    if not candidates:
        # Check promptFeedback for block reason (happens when prompt itself is blocked)
        prompt_feedback = response.get("promptFeedback", {})
        block_reason: Optional[str] = prompt_feedback.get("blockReason")
        if block_reason:
            LOGGER.warning(f"Prompt blocked with reason: {block_reason}")
            return block_reason
        return None

    finish_reason: Optional[str] = candidates[0].get("finishReason")
    return finish_reason


def get_error_for_finish_reason(finish_reason: str) -> Optional[str]:
    """Get user-facing error message for blocked finishReason.

    Args:
        finish_reason: The finishReason from Gemini response

    Returns:
        User-facing error message, or None if finishReason is normal
    """
    if finish_reason == "SAFETY":
        return get_user_message(ErrorCategory.SYSTEM, "safety_blocked")
    elif finish_reason == "PROHIBITED_CONTENT":
        return get_user_message(ErrorCategory.SYSTEM, "content_blocked")
    elif finish_reason == "RECITATION":
        return get_user_message(ErrorCategory.SYSTEM, "recitation_blocked")
    elif finish_reason in ("BLOCKLIST", "SPII"):
        return get_user_message(ErrorCategory.SYSTEM, "content_blocked")
    return None


# Type alias for escalation/training image handlers
EscalationHandler = Callable[[FunctionCall, Dict[str, Any]], ToolCallResult]


class ConversationGraphBuilder:
    """Builder for creating the conversation StateGraph.

    This class encapsulates the graph construction and provides methods for
    injecting dependencies (Gemini client, tool executor, etc.).
    """

    def __init__(
        self,
        gemini_client: GeminiClient,
        registry: Optional[ToolRegistry] = None,
        executor: Optional[ToolExecutor] = None,
        settings: Optional[AppSettings] = None,
        escalation_handler: Optional[EscalationHandler] = None,
        training_image_handler: Optional[EscalationHandler] = None,
    ) -> None:
        """Initialize the graph builder with dependencies.

        Args:
            gemini_client: Client for Gemini API calls
            registry: Tool registry for available tools
            executor: Tool executor for running tool calls
            settings: Application settings
            escalation_handler: Handler for escalation tool calls
            training_image_handler: Handler for training image tool calls
        """
        self._settings = settings or get_settings()
        self._registry = registry or ToolRegistry(self._settings)
        self._executor = executor or ToolExecutor(self._registry, self._settings)
        self._gemini = gemini_client
        self._escalation_handler = escalation_handler
        self._training_image_handler = training_image_handler

    def build(self) -> StateGraph:
        """Build and return the compiled conversation graph.

        Returns:
            A compiled StateGraph ready for invocation
        """
        builder = StateGraph(ConversationState)

        # Add nodes (Phase 1)
        builder.add_node("prepare", self._prepare_node)
        builder.add_node("call_gemini", self._call_gemini_node)
        builder.add_node("execute_tools", self._execute_tools_node)
        builder.add_node("respond", self._respond_node)

        # Add nodes (Phase 2 - verification and escalation)
        builder.add_node("verify", self._verify_node)
        builder.add_node("escalate", self._escalate_node)

        # Add edges
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "call_gemini")
        builder.add_conditional_edges(
            "call_gemini",
            self._route_after_gemini,
            {
                "execute_tools": "execute_tools",
                "verify": "verify",
                "respond": "respond",
            },
        )
        builder.add_edge("execute_tools", "call_gemini")

        # Phase 2: Verification routing
        builder.add_conditional_edges(
            "verify",
            self._route_after_verify,
            {
                "respond": "respond",
                "regenerate": "prepare",  # Loop back to prepare with feedback
                "escalate": "escalate",
            },
        )

        # Phase 2: Escalation always leads to respond
        builder.add_edge("escalate", "respond")

        builder.add_edge("respond", END)

        return builder.compile()

    async def _prepare_node(self, state: ConversationState) -> Dict[str, Any]:
        """Prepare initial context and Gemini history.

        This node:
        1. Builds the initial Gemini message history
        2. Prepares the user payload with context
        3. Initializes the history_messages list
        """
        LOGGER.debug("Preparing conversation context")

        gemini_history: List[Dict[str, Any]] = []
        history_messages: List[ConversationMessage] = list(state.get("conversation_history", []))

        # Add context message as first user message if provided
        context_message = state.get("context_message")
        if context_message:
            context_payload = {"role": "user", "parts": [{"text": context_message}]}
            gemini_history.append(context_payload)
            LOGGER.info(f"Added context message: {len(context_message)} chars")

        # Add verification feedback if this is a regeneration attempt
        verification_feedback = state.get("verification_feedback")
        if verification_feedback:
            feedback_text = (
                f"[IMPORTANT - REVISION REQUIRED]\n"
                f"Your previous response was flagged by quality verification:\n"
                f"{verification_feedback}\n\n"
                f"Please provide a revised response that addresses these concerns."
            )
            feedback_payload = {"role": "user", "parts": [{"text": feedback_text}]}
            gemini_history.append(feedback_payload)
            LOGGER.info(
                f"Added verification feedback for regeneration: {len(verification_feedback)} chars"
            )

        # Add conversation history (skip for slash commands to prevent context bleed)
        parsed_command = state.get("parsed_command")

        if parsed_command:
            LOGGER.info(
                f"Slash command '{parsed_command}' detected — skipping conversation history"
            )
        else:
            # Use thread-filtered history if available (thread disentanglement),
            # otherwise fall back to full conversation history
            thread_filtered = state.get("thread_filtered_history")
            if thread_filtered is not None:
                conversation_history = thread_filtered
                LOGGER.info(f"Using thread-filtered history ({len(conversation_history)} messages)")
            else:
                conversation_history = state.get("conversation_history", [])

            # Cross-request loop detection (runs on raw history, before context
            # filtering can remove the repetitive turns it needs to detect)
            loop_result = detect_cross_request_loop(conversation_history)

            from orchestrator.services.context_filter import is_context_filter_enabled

            # Skip ContextFilterService when thread filtering is active
            # (thread filtering provides more precise results, avoid double-filtering)
            if is_context_filter_enabled() and conversation_history and thread_filtered is None:
                try:
                    from orchestrator.services.context_filter import ContextFilterService

                    filter_service = ContextFilterService()
                    filter_result = await filter_service.filter_history(
                        incoming_message=state.get("user_input", ""),
                        candidate_messages=conversation_history,
                    )
                    await filter_service.aclose()

                    if filter_result.confidence > 0.7:
                        kept = [
                            conversation_history[i]
                            for i in filter_result.relevant_indices
                            if i < len(conversation_history)
                        ]
                        LOGGER.info(
                            f"Context filter kept {len(kept)}/{len(conversation_history)} messages"
                        )
                        conversation_history = kept
                    else:
                        LOGGER.info(
                            f"Context filter low confidence ({filter_result.confidence:.2f}), "
                            "keeping all messages"
                        )
                except Exception as e:
                    LOGGER.warning(f"Context filter error (fail-open): {e}")

            for message in conversation_history:
                gemini_history.append(self._to_gemini_message(message))

            if loop_result.hint:
                gemini_history.append({"role": "user", "parts": [{"text": loop_result.hint}]})

            if loop_result.should_escalate:
                await self._escalate_for_loop(state=state, loop_result=loop_result)

        # Build and add current user message
        user_payload = await self._build_user_payload(state)
        gemini_history.append(user_payload)

        # Validate and clean history
        gemini_history = self._validate_conversation_structure(gemini_history)

        # Add current user message to history for database saving
        current_user_message = ConversationMessage(role="user", content=state["user_input"])
        history_messages.append(current_user_message)

        # Prepare tools payload
        tools_payload = state.get("tools_payload") or self._registry.tools_payload()
        unlocked_tools = state.get("unlocked_tools", [])
        parsed_command = state.get("parsed_command")

        LOGGER.info(
            f"_prepare_node: parsed_command={parsed_command}, "
            f"unlocked_tools={unlocked_tools}, "
            f"tools_payload has {len(tools_payload) if tools_payload else 0} groups"
        )

        if tools_payload:
            tools_payload = self._inject_reasoning_to_tools(tools_payload)
            tools_payload = self._filter_command_gated_tools(tools_payload, unlocked_tools)

            # In exclusive mode, filter conversation history to remove function calls
            # for tools that aren't currently available (prevents UNEXPECTED_TOOL_CALL errors)
            if unlocked_tools:
                available_tool_names = self._extract_tool_names(tools_payload)
                gemini_history = self._filter_history_for_available_tools(
                    gemini_history, available_tool_names
                )

        # Compute tool allowlist for hallucination guard
        allowed_tool_names = (
            sorted(self._extract_tool_names(tools_payload)) if tools_payload else []
        )

        return {
            "gemini_history": gemini_history,
            "history_messages": history_messages,
            "tools_payload": tools_payload,
            "allowed_tool_names": allowed_tool_names,
        }

    async def _call_gemini_node(self, state: ConversationState) -> Dict[str, Any]:
        """Call the Gemini API and extract response.

        This node:
        1. Builds the Gemini payload
        2. Calls the API
        3. Extracts function calls or final text
        4. Updates token counts
        """
        current_round = state.get("current_round", 0)
        max_rounds = state.get("max_rounds", self._settings.max_tool_rounds)

        LOGGER.debug(f"Calling Gemini (round {current_round + 1}/{max_rounds})")

        # Check if we've exceeded max rounds
        if current_round >= max_rounds:
            # Staff: synthesize a partial answer from accumulated tool results instead of
            # returning a generic system error, so the investigation isn't wasted.
            user_ctx = state.get("user_context")
            is_staff = bool(getattr(user_ctx, "is_staff", False))
            if is_staff and state.get("accumulated_tool_results"):
                try:
                    synthesized = await self._synthesize_partial_answer(state)
                    if synthesized:
                        LOGGER.info(
                            "Max rounds exceeded for staff session — returning synthesized "
                            f"partial answer ({len(synthesized)} chars, "
                            f"{len(state['accumulated_tool_results'])} tool results)"
                        )
                        return {
                            "final_response": synthesized,
                            "should_continue": False,
                            "error_category": ErrorCategory.SYSTEM.value,
                            "error": "Max tool rounds exceeded — returned partial synthesis",
                        }
                except Exception as synth_err:
                    LOGGER.exception(
                        f"Partial-answer synthesis failed, falling back to canned error: {synth_err}"
                    )

            return {
                "error": "Max tool rounds exceeded without final response from Gemini",
                "error_category": ErrorCategory.SYSTEM.value,
                "final_response": get_user_message(ErrorCategory.SYSTEM, "internal_error"),
                "should_continue": False,
                "reply_markup": self._make_escalation_offer_markup(state),
            }

        # Apply command model override if set (e.g., /editdoc uses deep thinking model).
        # IMPORTANT: shallow copy the model config to avoid mutating the shared singleton.
        command_model = state.get("command_model_override", "")
        if command_model and self._gemini._model_config.model != command_model:
            import copy

            LOGGER.info(
                f"Applying command model override: {self._gemini._model_config.model} → {command_model}"
            )
            self._gemini._model_config = copy.copy(self._gemini._model_config)
            self._gemini._model_config.model = command_model

        # Build payload
        include_tools = bool(state.get("tools_payload"))
        payload = self._build_payload(
            state["gemini_history"],
            state.get("tools_payload") if include_tools else None,
            state.get("system_instructions"),
        )

        # Log payload summary
        total_chars = sum(
            len(str(part.get("text", "")))
            for msg in payload.get("contents", [])
            for part in msg.get("parts", [])
        )
        system_instruction_chars = len(
            payload.get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")
        )
        # Count actual function declarations, not just wrapper objects
        tools_list = payload.get("tools", [])
        num_functions = 0
        tool_names = []
        for tool_wrapper in tools_list:
            if "functionDeclarations" in tool_wrapper:
                for func in tool_wrapper["functionDeclarations"]:
                    num_functions += 1
                    tool_names.append(func.get("name", "unknown"))
            else:
                num_functions += 1
                tool_names.append(tool_wrapper.get("name", "unknown"))
        LOGGER.info(
            f"Gemini request (round {current_round + 1}/{max_rounds}): "
            f"{len(payload.get('contents', []))} messages, "
            f"{total_chars:,} chars in contents, "
            f"{system_instruction_chars:,} chars in system instruction, "
            f"{num_functions} function declarations"
        )
        if num_functions > 0:
            LOGGER.debug(f"Tools being sent to Gemini: {', '.join(sorted(tool_names))}")

        # Call Gemini
        response = await self._gemini.generate_content(payload)

        # Extract and handle finishReason (important for Gemini 3+ and safety blocks)
        finish_reason = extract_finish_reason(response)
        if finish_reason:
            if finish_reason not in ("STOP", "MAX_TOKENS"):
                LOGGER.warning(f"Gemini finishReason: {finish_reason}")
            elif finish_reason == "MAX_TOKENS":
                LOGGER.info("Gemini hit MAX_TOKENS limit - response may be truncated")

        # Check for blocked content BEFORE other processing
        # This prevents blocked responses from being treated as "rephrase" errors
        if finish_reason and finish_reason in BLOCKED_FINISH_REASONS:
            blocked_error = get_error_for_finish_reason(finish_reason)
            LOGGER.warning(
                f"Content blocked by Gemini (finishReason={finish_reason}). "
                f"Returning safe error message."
            )

            # Trigger escalation for review if this is a safety/prohibited block
            if finish_reason in ESCALATION_FINISH_REASONS:
                await self._escalate_for_blocked_content(
                    state=state,
                    finish_reason=finish_reason,
                    user_input=state.get("user_input", ""),
                )

            return {
                "final_response": blocked_error,
                "raw_gemini_responses": [response],
                "total_input_tokens": state.get("total_input_tokens", 0),
                "total_output_tokens": state.get("total_output_tokens", 0),
                "should_continue": False,
                "finish_reason": finish_reason,
            }

        # Track raw response
        raw_responses = list(state.get("raw_gemini_responses", []))
        raw_responses.append(response)

        # Extract and accumulate token usage
        usage = response.get("usageMetadata", {})
        input_tokens = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
        total_input = state.get("total_input_tokens", 0) + input_tokens
        total_output = state.get("total_output_tokens", 0) + output_tokens

        if usage:
            LOGGER.info(
                f"Gemini tokens (round {current_round + 1}): "
                f"input={input_tokens}, output={output_tokens}, "
                f"cumulative: input={total_input}, output={total_output}"
            )

        # Extract function calls
        function_calls = self._extract_function_calls(response)

        if function_calls:
            LOGGER.info(f"Gemini requested {len(function_calls)} tool calls")

            # Extract and log reasoning from each tool call
            for call in function_calls:
                reasoning = call.arguments.pop("reasoning", None)
                if reasoning:
                    LOGGER.info(f"[TOOL REASONING] {call.name}: {reasoning}")

            return {
                "pending_tool_calls": function_calls,
                "raw_gemini_responses": raw_responses,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "current_round": current_round + 1,
                "should_continue": True,
            }
        else:
            # No function calls - extract final text
            final_text = self._extract_text(response) or ""

            # Handle empty response
            if not final_text and not state.get("accumulated_tool_calls"):
                LOGGER.warning(
                    "Gemini returned empty response on first round (no text, no tool calls). "
                    f"Input tokens: {total_input}, output tokens: {total_output}. "
                    "Retrying once..."
                )
                # Retry the request once
                response = await self._gemini.generate_content(payload)
                raw_responses.append(response)

                # Check if retry was also blocked
                retry_finish_reason = extract_finish_reason(response)
                if retry_finish_reason and retry_finish_reason in BLOCKED_FINISH_REASONS:
                    blocked_error = get_error_for_finish_reason(retry_finish_reason)
                    LOGGER.warning(
                        f"Retry also blocked (finishReason={retry_finish_reason}). "
                        "Returning safe error."
                    )
                    if retry_finish_reason in ESCALATION_FINISH_REASONS:
                        await self._escalate_for_blocked_content(
                            state=state,
                            finish_reason=retry_finish_reason,
                            user_input=state.get("user_input", ""),
                        )
                    return {
                        "final_response": blocked_error,
                        "raw_gemini_responses": raw_responses,
                        "total_input_tokens": total_input,
                        "total_output_tokens": total_output,
                        "should_continue": False,
                        "finish_reason": retry_finish_reason,
                    }

                function_calls = self._extract_function_calls(response)
                if function_calls:
                    LOGGER.info(f"Retry succeeded with {len(function_calls)} tool calls")
                    for call in function_calls:
                        reasoning = call.arguments.pop("reasoning", None)
                        if reasoning:
                            LOGGER.info(f"[TOOL REASONING] {call.name}: {reasoning}")

                    return {
                        "pending_tool_calls": function_calls,
                        "raw_gemini_responses": raw_responses,
                        "total_input_tokens": total_input,
                        "total_output_tokens": total_output,
                        "current_round": current_round + 1,
                        "should_continue": True,
                    }

                final_text = self._extract_text(response) or ""
                if not final_text:
                    # Check for transient model errors
                    retry_reason = extract_finish_reason(response)
                    if retry_reason and retry_reason in TRANSIENT_FINISH_REASONS:
                        LOGGER.error(
                            f"Gemini model error (finishReason={retry_reason}). "
                            f"Response: {response}"
                        )
                        final_text = get_user_message(
                            ErrorCategory.TRANSIENT, "service_unavailable"
                        )
                    else:
                        LOGGER.error(
                            f"Gemini returned empty response on retry. Response: {response}"
                        )
                        final_text = get_user_message(ErrorCategory.REPHRASE, "empty_response")
                else:
                    LOGGER.info("Retry succeeded with text response")

            # Handle empty text after tool calls
            accumulated_calls = state.get("accumulated_tool_calls", [])
            if not final_text and accumulated_calls:
                last_call = accumulated_calls[-1]
                LOGGER.warning(
                    f"Gemini returned empty text after {len(accumulated_calls)} tool calls. "
                    f"Last tool: {last_call.name}. Response: {response}"
                )

                # Add fallback responses when Gemini returns empty text after tool calls
                accumulated_results = state.get("accumulated_tool_results", [])
                last_result = accumulated_results[-1] if accumulated_results else None

                if last_call.name == "escalate_to_support":
                    if last_result and last_result.output.get("success"):
                        final_text = get_user_message(ErrorCategory.ESCALATION, "success")
                        LOGGER.info("Added fallback escalation confirmation message")
                    else:
                        final_text = get_user_message(ErrorCategory.ESCALATION, "failed")
                        LOGGER.warning("Escalation failed, added fallback error message")

                # Fallback for JIRA actions (change_status, add_comment, get_issue, etc.)
                elif last_call.name.startswith("jira_") and last_result:
                    if last_result.success:
                        # Parse JSON output to extract message/issue_key/status
                        result_data = {}
                        if isinstance(last_result.output, str):
                            try:
                                result_data = json.loads(last_result.output)
                            except (json.JSONDecodeError, TypeError):
                                pass
                        elif isinstance(last_result.output, dict):
                            result_data = last_result.output

                        # Special handling for jira_get_issue - format ticket details
                        if last_call.name == "jira_get_issue" and result_data:
                            final_text = self._format_jira_ticket_details(result_data)
                            LOGGER.info("Added fallback ticket details for jira_get_issue")
                        else:
                            message = result_data.get("message", "")
                            issue_key = result_data.get("issue_key", "")
                            new_status = result_data.get("new_status", "")

                            if message:
                                final_text = f"✅ {message}"
                            elif issue_key and new_status:
                                final_text = f"✅ {issue_key} has been updated to **{new_status}**."
                            elif issue_key:
                                final_text = f"✅ {issue_key} has been updated successfully."
                            else:
                                final_text = "✅ JIRA action completed successfully."
                            LOGGER.info(f"Added fallback JIRA success message for {last_call.name}")
                    else:
                        error = last_result.error or "Unknown error"
                        final_text = f"❌ JIRA action failed: {error}"
                        LOGGER.warning(f"JIRA action failed, added fallback error: {error}")

                # Fallback for schedule tools (list_user_schedules, create_schedule, etc.)
                # The schedule MCP server returns pre-formatted text, so just use it directly
                elif last_call.name.startswith("schedule_") and last_result:
                    if last_result.success:
                        # MCP server returns formatted text directly in output
                        if isinstance(last_result.output, str) and last_result.output.strip():
                            final_text = last_result.output
                            LOGGER.info(f"Using pre-formatted output for {last_call.name}")
                        else:
                            # Fallback if output is empty/missing
                            final_text = "✅ Schedule action completed."
                            LOGGER.info(f"Added generic fallback for {last_call.name}")
                    else:
                        error = last_result.error or "Unknown error"
                        final_text = f"❌ Schedule action failed: {error}"
                        LOGGER.warning(f"Schedule tool {last_call.name} failed: {error}")

                # Fallback for knowledge tools (summarize_knowledge, list_document_types)
                elif last_call.name.startswith("knowledge_") and last_result:
                    # MCP tools return: {"success": True, "result": [{"type": "text", "text": "..."}]}
                    result_text = ""
                    if last_result.raw_response:
                        mcp_result = last_result.raw_response.get("result", [])
                        if isinstance(mcp_result, list) and mcp_result:
                            # Extract text from first TextContent item
                            first_item = mcp_result[0]
                            if isinstance(first_item, dict):
                                result_text = first_item.get("text", "")
                    # Also check output field directly
                    if not result_text and last_result.output:
                        if isinstance(last_result.output, str):
                            result_text = last_result.output
                        elif isinstance(last_result.output, dict):
                            result_text = last_result.output.get("text", "")

                    if result_text:
                        final_text = result_text
                        LOGGER.info(f"Added fallback for {last_call.name} result")
                    else:
                        final_text = "Knowledge base search completed but no results found."
                        LOGGER.warning(f"Knowledge tool {last_call.name} returned empty result")

            LOGGER.info(f"Gemini returned final response after {current_round + 1} rounds")

            return {
                "final_response": final_text,
                "raw_gemini_responses": raw_responses,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "current_round": current_round + 1,
                "should_continue": False,
                "pending_tool_calls": [],
            }

    async def _execute_tools_node(self, state: ConversationState) -> Dict[str, Any]:
        """Execute pending tool calls.

        This node:
        1. Executes all pending tool calls (with parallelism if enabled)
        2. Updates Gemini history with function calls and responses
        3. Accumulates tool calls and results
        """
        function_calls = state.get("pending_tool_calls", [])
        if not function_calls:
            return {}

        LOGGER.debug(f"Executing {len(function_calls)} tool calls")

        # Get current state
        gemini_history = list(state.get("gemini_history", []))
        history_messages = list(state.get("history_messages", []))
        accumulated_calls = list(state.get("accumulated_tool_calls", []))
        accumulated_results = list(state.get("accumulated_tool_results", []))
        metadata = dict(state.get("metadata", {}))

        # Enrich metadata with user context for tool execution
        user_context = state.get("user_context")
        user_permissions = state.get("user_permissions")
        if user_context:
            metadata["user_email"] = user_context.user_email
            metadata["user_name"] = user_context.username
            metadata["original_chat_id"] = user_context.chat_id
            metadata["topic_id"] = user_context.topic_id
            metadata["session_id"] = state.get("session_id")
            metadata["thread_id"] = state.get("thread_id")
            metadata["organization_name"] = user_context.organization_name
            metadata["organization_id"] = (
                int(user_context.organization_ids[0]) if user_context.organization_ids else None
            )
            # For preference tool calls — pass telegram_id and user_input via metadata
            metadata["telegram_id"] = (
                user_context.user_id if user_context.source == "telegram" else None
            )
            metadata["user_input"] = state.get("user_input", "")
        if user_permissions:
            metadata["user_permissions"] = user_permissions

        # Accumulate calls
        accumulated_calls.extend(function_calls)

        # Build functionCall parts for Gemini history
        # thoughtSignature must be a part-level sibling of functionCall, not nested inside it
        gemini_parts = []
        for call in function_calls:
            fc_data: Dict[str, Any] = {"name": call.name, "args": call.arguments}
            part_data: Dict[str, Any] = {"functionCall": fc_data}
            if call.thought_signature:
                part_data["thoughtSignature"] = call.thought_signature
            gemini_parts.append(part_data)

        gemini_history.append({"role": "model", "parts": gemini_parts})
        history_messages.extend(
            ConversationMessage(role="model", function_call=call) for call in function_calls
        )

        # Hallucination guard: verify each tool was presented to Gemini
        allowed_names = set(state.get("allowed_tool_names", []))
        calls_to_execute = function_calls  # default: execute all
        blocked_results: List[ToolCallResult] = []

        if allowed_names:
            calls_to_execute = []
            for call in function_calls:
                if call.name in allowed_names:
                    calls_to_execute.append(call)
                else:
                    LOGGER.warning(
                        f"TOOL HALLUCINATION BLOCKED: Gemini called '{call.name}' "
                        f"which was not in the {len(allowed_names)} tools provided"
                    )
                    blocked_results.append(
                        ToolCallResult(
                            name=call.name,
                            success=False,
                            output={
                                "error": (
                                    f"Tool '{call.name}' is not available. "
                                    "Use only the tools provided to you."
                                )
                            },
                        )
                    )
            if blocked_results:
                LOGGER.warning(
                    f"Hallucination guard blocked {len(blocked_results)}/{len(function_calls)} "
                    f"tool calls: {[r.name for r in blocked_results]}"
                )

            # Break out of the loop if all calls were hallucinated — sending errors back to
            # Gemini just causes it to retry the same nonexistent tools indefinitely, burning
            # through the LangGraph recursion limit and producing a generic crash error.
            if not calls_to_execute:
                safe_names = [repr(r.name[:80]) for r in blocked_results]
                LOGGER.error(
                    "All tool calls were hallucinated; terminating to avoid recursion limit. "
                    f"Blocked: {safe_names}"
                )
                return {
                    "gemini_history": gemini_history,
                    "history_messages": history_messages,
                    "accumulated_tool_calls": accumulated_calls,
                    "accumulated_tool_results": accumulated_results,
                    "error": "Gemini requested unavailable tools",
                    "error_category": ErrorCategory.SYSTEM.value,
                    "final_response": get_user_message(ErrorCategory.SYSTEM, "internal_error"),
                    "reply_markup": self._make_escalation_offer_markup(state),
                }

        # Execute only verified calls
        exec_results = await self._execute_tool_calls(calls_to_execute, metadata)

        # Merge results in original order (blocked get error, executed get real result)
        results: List[ToolCallResult] = []
        blocked_iter = iter(blocked_results)
        exec_iter = iter(exec_results)
        _fallback = ToolCallResult(name="unknown", success=False, output={"error": "No result"})
        for call in function_calls:
            if allowed_names and call.name not in allowed_names:
                results.append(next(blocked_iter, _fallback))
            else:
                results.append(next(exec_iter, _fallback))

        accumulated_results.extend(results)

        # Build function response parts for Gemini history
        response_parts = []
        for call, result in zip(function_calls, results):
            response_output = self._prepare_tool_response(result.output)

            # Build functionResponse with optional thoughtSignature (part-level sibling)
            func_response: Dict[str, Any] = {
                "name": call.name,
                "response": response_output,
            }
            resp_part: Dict[str, Any] = {"functionResponse": func_response}
            if call.thought_signature:
                resp_part["thoughtSignature"] = call.thought_signature

            response_parts.append(resp_part)
            history_messages.append(ConversationMessage(role="tool", tool_result=result))

        gemini_history.append({"role": "user", "parts": response_parts})

        # Check if escalation was triggered AND succeeded
        # Only set escalation_triggered when the tool actually succeeded,
        # otherwise Gemini generates "I've escalated" even on failure
        escalation_triggered = False
        for call, result in zip(function_calls, results):
            if call.name == "escalate_to_support" and result.success:
                escalation_triggered = True
                break

        return {
            "gemini_history": gemini_history,
            "history_messages": history_messages,
            "accumulated_tool_calls": accumulated_calls,
            "accumulated_tool_results": accumulated_results,
            "pending_tool_calls": [],
            "escalation_triggered": escalation_triggered
            or state.get("escalation_triggered", False),
        }

    async def _respond_node(self, state: ConversationState) -> Dict[str, Any]:
        """Finalize the response.

        This node:
        1. Adds the final model message to history
        2. Prepares the final ChatResponse data
        """
        final_text = state.get("final_response", "")
        gemini_history = list(state.get("gemini_history", []))
        history_messages = list(state.get("history_messages", []))

        # Include token metadata in the final model message
        token_metadata = {
            "input_tokens": state.get("total_input_tokens", 0),
            "output_tokens": state.get("total_output_tokens", 0),
            "total_tokens": state.get("total_input_tokens", 0)
            + state.get("total_output_tokens", 0),
            "gemini_rounds": state.get("current_round", 0),
        }

        final_message = ConversationMessage(
            role="model", content=final_text, metadata=token_metadata
        )
        history_messages.append(final_message)
        gemini_history.append(self._to_gemini_message(final_message))

        return {
            "history_messages": history_messages,
            "gemini_history": gemini_history,
        }

    async def _verify_node(self, state: ConversationState) -> Dict[str, Any]:
        """Verify response using LLM-as-judge pattern (Phase 2).

        This node:
        1. Calls the verification service with the response
        2. Updates verification state based on result
        3. Increments verification attempt counter
        """
        from orchestrator.services.verification_service import ResponseVerificationService

        final_response = state.get("final_response", "")
        verification_instructions = state.get("verification_instructions")
        user_input = state.get("user_input", "")
        attempt = state.get("verification_attempt", 0) + 1

        LOGGER.info(f"Verifying response (attempt {attempt})")

        if not verification_instructions:
            LOGGER.warning("No verification instructions - skipping verification")
            return {
                "verification_passed": True,
                "verification_attempt": attempt,
            }

        try:
            verification_service = ResponseVerificationService()

            # Build conversation context for verification
            context_parts = []
            history_messages = state.get("history_messages", [])
            if history_messages:
                recent_messages = history_messages[-5:]  # Last 5 messages
                context_parts.append(
                    "\n".join(
                        f"{msg.role}: {msg.content[:200]}" for msg in recent_messages if msg.content
                    )
                )

            # Include tool calls and their results so the verifier knows what actions were taken
            # and whether they succeeded — without this, the verifier can't confirm claims like
            # "the date was updated" and flags them as potential inaccuracies.
            accumulated_calls = state.get("accumulated_tool_calls") or []
            accumulated_results = state.get("accumulated_tool_results") or []
            if accumulated_calls:
                tool_summaries = []
                for i, tc in enumerate(accumulated_calls):
                    name = getattr(tc, "name", tc.get("name") if isinstance(tc, dict) else "?")
                    result = accumulated_results[i] if i < len(accumulated_results) else None
                    if result is not None:
                        success = getattr(result, "success", None)
                        error = getattr(result, "error", None)
                        if success:
                            tool_summaries.append(f"- {name}: succeeded")
                        else:
                            tool_summaries.append(f"- {name}: failed ({error or 'unknown error'})")
                    else:
                        tool_summaries.append(f"- {name}: called")
                context_parts.append("TOOLS CALLED THIS TURN:\n" + "\n".join(tool_summaries))

            # Flag if session has an active escalation from a prior turn
            if state.get("is_escalated_session"):
                context_parts.append(
                    "NOTE: This session has an active escalation from a prior turn. "
                    "The bot may legitimately reference a previous escalation."
                )

            conversation_context = "\n".join(context_parts) if context_parts else None

            # Get available tools for verification context
            tools_payload = state.get("tools_payload")

            result = await verification_service.verify_response(
                original_message=user_input,
                response_text=final_response,
                verification_instructions=verification_instructions,
                conversation_context=conversation_context,
                available_tools=tools_payload,
            )

            await verification_service.aclose()

            if result.passed:
                LOGGER.info(f"Response passed verification (attempt {attempt})")
                return {
                    "verification_passed": True,
                    "verification_attempt": attempt,
                    "verification_feedback": None,
                    "verification_categories": [],
                }
            else:
                LOGGER.warning(
                    f"Response failed verification (attempt {attempt}): "
                    f"categories={result.categories}, feedback={result.feedback[:200]}"
                )
                return {
                    "verification_passed": False,
                    "verification_attempt": attempt,
                    "verification_feedback": result.feedback,
                    "verification_categories": result.categories,
                }

        except Exception as e:
            LOGGER.exception(f"Verification error: {e}")
            # Fail open - allow response through if verification errors
            return {
                "verification_passed": True,
                "verification_attempt": attempt,
                "verification_feedback": f"Verification error: {str(e)}",
            }

    def _make_escalation_offer_markup(self, state: ConversationState) -> Optional[Dict[str, Any]]:
        """Return escalation offer keyboard for customer SYSTEM errors; None for staff."""
        from shared.utils.telegram_buttons import (
            build_escalation_offer_keyboard,
            is_inline_buttons_enabled,
        )

        if not is_inline_buttons_enabled():
            return None
        user_context = state.get("user_context")
        if user_context and user_context.is_staff:
            return None
        session_id = state.get("session_id")
        if not session_id:
            return None
        return build_escalation_offer_keyboard(session_id)

    @staticmethod
    def _extract_org_id(user_context) -> int | None:
        """Safely extract organization_id from user_context as int.

        Reads from user_context.organization_ids (the authoritative source) rather
        than metadata["organization_id"], which is only set on a local copy inside
        _execute_tools_node and never persisted to graph state.
        """
        if user_context and user_context.organization_ids:
            try:
                return int(user_context.organization_ids[0])
            except (ValueError, TypeError):
                LOGGER.warning(
                    "Non-integer organization_id in user_context: %r",
                    user_context.organization_ids[0],
                )
        return None

    async def _escalate_node(self, state: ConversationState) -> Dict[str, Any]:
        """Escalate verification failure to support team (Phase 2).

        This node is called after max verification attempts have failed.
        It escalates to the support team and updates the response with
        a user-facing message.
        """
        from orchestrator.services.escalation_service import EscalationService

        LOGGER.info("Escalating verification failure to support team")

        user_input = state.get("user_input", "")
        final_response = state.get("final_response", "")
        verification_feedback = state.get("verification_feedback", "")
        session_id = state.get("session_id")
        user_context = state.get("user_context")
        metadata = state.get("metadata", {})

        # Extract customer info from user_context
        customer_chat_id = user_context.chat_id if user_context else None
        customer_username = user_context.username if user_context else None
        organization_name = user_context.organization_name if user_context else None

        customer_topic_id = metadata.get("topic_id")
        organization_id = self._extract_org_id(user_context)

        try:
            escalation_service = EscalationService()

            if escalation_service.is_enabled():
                result = await escalation_service.escalate_verification_failure(
                    original_message=user_input,
                    failed_response=final_response,
                    verification_feedback=verification_feedback,
                    session_id=session_id or "",
                    customer_chat_id=customer_chat_id,
                    customer_topic_id=customer_topic_id,
                    customer_username=customer_username,
                    organization_short_name=organization_name,
                    organization_id=organization_id,
                    thread_id=state.get("thread_id"),
                )

                if result.get("success"):
                    LOGGER.info("Verification failure escalated successfully")
                else:
                    LOGGER.error(f"Failed to escalate: {result.get('error')}")
            else:
                LOGGER.warning("Escalation service not enabled")
                result = {"success": False, "error": "Escalation service not configured"}

        except Exception as e:
            LOGGER.exception(f"Error escalating verification failure: {e}")
            result = {"success": False, "error": str(e)}

        # Update final response with escalation message
        escalation_message = get_user_message(ErrorCategory.ESCALATION, "verification_failed")

        # Add verification metadata to the final message
        history_messages = list(state.get("history_messages", []))
        if history_messages:
            last_message = history_messages[-1]
            if last_message.role == "model":
                last_message.metadata["verification_feedback"] = verification_feedback
                last_message.metadata["verification_categories"] = state.get(
                    "verification_categories", []
                )
                last_message.metadata["escalated"] = True

        return {
            "final_response": escalation_message,
            "escalation_triggered": result.get("success", False),
            "escalation_result": result,
            "history_messages": history_messages,
        }

    async def _escalate_for_blocked_content(
        self,
        state: ConversationState,
        finish_reason: str,
        user_input: str,
    ) -> None:
        """Escalate blocked content for human review.

        Called when Gemini returns SAFETY or PROHIBITED_CONTENT finishReason.
        This creates an escalation so staff can review whether the block
        was appropriate or if the user's request was legitimate.

        Args:
            state: Current conversation state
            finish_reason: The finishReason that triggered the block
            user_input: The user's original message
        """
        from orchestrator.services.escalation_service import EscalationService

        LOGGER.info(f"Escalating blocked content for review (finishReason={finish_reason})")

        session_id = state.get("session_id")
        user_context = state.get("user_context")
        metadata = state.get("metadata", {})

        customer_chat_id = user_context.chat_id if user_context else None
        customer_topic_id = metadata.get("topic_id")
        customer_username = user_context.username if user_context else None
        customer_email = user_context.user_email if user_context else None
        organization_name = user_context.organization_name if user_context else None
        organization_id = self._extract_org_id(user_context)

        try:
            escalation_service = EscalationService()

            if escalation_service.is_enabled():
                result = await escalation_service.escalate_to_support(
                    question_summary=f"[CONTENT BLOCKED - {finish_reason}] User message was blocked by AI safety filters",
                    session_id=session_id or "",
                    organization_id=organization_id,
                    organization_short_name=organization_name,
                    customer_chat_id=customer_chat_id,
                    customer_topic_id=customer_topic_id,
                    customer_username=customer_username,
                    customer_email=customer_email,
                    conversation_context=(
                        f"[AUTO-ESCALATION: Gemini blocked with finishReason={finish_reason}]\n\n"
                        f"User message: {user_input[:500]}\n\n"
                        "Please review if this was a legitimate request that was incorrectly blocked."
                    ),
                    reason="content_blocked",
                    thread_id=state.get("thread_id"),
                )

                if result.get("success"):
                    LOGGER.info(f"Content block escalation sent (finishReason={finish_reason})")
                else:
                    LOGGER.error(f"Failed to escalate content block: {result.get('error')}")
            else:
                LOGGER.warning(
                    f"Escalation service not enabled - content block ({finish_reason}) not reported"
                )

        except Exception as e:
            # Don't fail the request if escalation fails - the user already got an error message
            LOGGER.exception(f"Error escalating content block: {e}")

    async def _escalate_for_loop(
        self,
        state: ConversationState,
        loop_result: LoopDetectionResult,
    ) -> None:
        """Escalate a persistent cross-request loop for human review.

        Called when the loop hint has been injected multiple times but the model
        continues repeating itself. Creates an escalation so staff can intervene.
        """
        from orchestrator.services.escalation_service import EscalationService

        LOGGER.info(
            f"Escalating persistent loop ({loop_result.consecutive_similar_turns} similar turns)"
        )

        session_id = state.get("session_id")
        user_context = state.get("user_context")
        metadata = state.get("metadata", {})
        user_input = state.get("user_input", "")

        customer_chat_id = user_context.chat_id if user_context else None
        customer_topic_id = metadata.get("topic_id")
        customer_username = user_context.username if user_context else None
        customer_email = user_context.user_email if user_context else None
        organization_name = user_context.organization_name if user_context else None
        organization_id = self._extract_org_id(user_context)

        try:
            escalation_service = EscalationService()

            if escalation_service.is_enabled():
                result = await escalation_service.escalate_to_support(
                    question_summary=(
                        f"[LOOP DETECTED] Bot stuck in repetitive loop "
                        f"({loop_result.consecutive_similar_turns} identical responses)"
                    ),
                    session_id=session_id or "",
                    organization_id=organization_id,
                    organization_short_name=organization_name,
                    customer_chat_id=customer_chat_id,
                    customer_topic_id=customer_topic_id,
                    customer_username=customer_username,
                    customer_email=customer_email,
                    conversation_context=(
                        f"[AUTO-ESCALATION: Cross-request loop detected]\n\n"
                        f"The bot has repeated the same response "
                        f"{loop_result.consecutive_similar_turns} times in a row.\n"
                        f"Latest user message: {user_input[:500]}\n\n"
                        f"The loop-breaking hint was injected but the model "
                        f"continues repeating. Human intervention needed."
                    ),
                    reason="loop_detected",
                    thread_id=state.get("thread_id"),
                )

                if result.get("success"):
                    LOGGER.info("Loop escalation sent successfully")
                else:
                    LOGGER.error(f"Failed to escalate loop: {result.get('error')}")
            else:
                LOGGER.warning("Escalation service not enabled - loop not reported")

        except Exception as e:
            LOGGER.exception(f"Error escalating loop: {e}")

    def _route_after_gemini(
        self, state: ConversationState
    ) -> Literal["execute_tools", "verify", "respond"]:
        """Route after Gemini call based on state.

        Routes to:
        - execute_tools: if there are pending tool calls
        - verify: if verification is enabled and we have a final response
        - respond: if verification is disabled or skipped
        """
        if state.get("pending_tool_calls"):
            return "execute_tools"

        # Check if verification is enabled and should run
        verification_enabled = state.get("verification_enabled", False)
        user_context = state.get("user_context")
        is_staff = user_context.is_staff if user_context else False

        # Skip verification for staff users
        if verification_enabled and not is_staff and state.get("final_response"):
            return "verify"

        return "respond"

    def _route_after_verify(
        self, state: ConversationState
    ) -> Literal["respond", "regenerate", "escalate"]:
        """Route after verification based on result.

        Routes to:
        - respond: if verification passed
        - regenerate: if verification failed and we can retry
        - escalate: if verification failed max times
        """
        verification_passed = state.get("verification_passed")

        if verification_passed:
            return "respond"

        # Check if we can retry
        attempt = state.get("verification_attempt", 0)
        max_attempts = state.get("max_verification_attempts", 2)

        if attempt < max_attempts:
            LOGGER.info(
                f"Verification failed (attempt {attempt}/{max_attempts}), regenerating response"
            )
            return "regenerate"

        # Max attempts reached, escalate
        LOGGER.warning(f"Verification failed {attempt} times, escalating to support")
        return "escalate"

    async def _build_user_payload(self, state: ConversationState) -> Dict[str, Any]:
        """Construct user message including optional context and media."""
        context_blocks: List[str] = []

        # Add user context
        user_context = state.get("user_context")
        if user_context:
            user_context_text = self._format_user_context(user_context)
            if user_context_text:
                context_blocks.append(user_context_text)

        # Add entity context if present
        entity_context = state.get("entity_context")
        if entity_context:
            entity_context_text = self._format_entity_context(entity_context)
            if entity_context_text:
                context_blocks.append(entity_context_text)

        # Compose text with context
        user_input = state["user_input"]
        composed_text = "\n\n".join(context_blocks + [user_input]) if context_blocks else user_input

        # Build parts list (text + media)
        parts: List[Dict[str, Any]] = [{"text": composed_text}]

        # Add media attachments
        for media in state.get("media", []):
            media_part = self._build_media_part(media)
            if media_part:
                parts.append(media_part)

        return {
            "role": "user",
            "parts": parts,
        }

    def _format_user_context(self, user_context) -> str:
        """Format user context as system instruction."""
        parts = [
            f"User: {user_context.username or 'Anonymous'}",
            f"Source: {user_context.source}",
        ]
        if user_context.organization_name:
            parts.append(f"Organization: {user_context.organization_name}")
        if user_context.is_group:
            parts.append("Chat Type: Group")
        if user_context.roles:
            parts.append(f"Roles: {', '.join(user_context.roles)}")
        if user_context.is_staff:
            parts.append("Mode: Staff")

        return "[User Context]\n" + "\n".join(parts)

    def _format_entity_context(self, entity_context) -> str:
        """Format entity context for the model."""
        parts = []
        if entity_context.customer_id:
            parts.append(f"Customer ID: {entity_context.customer_id}")
        if entity_context.meter_id:
            parts.append(f"Meter ID: {entity_context.meter_id}")
        if entity_context.grid_id:
            parts.append(f"Grid ID: {entity_context.grid_id}")
        if entity_context.site_id:
            parts.append(f"Site ID: {entity_context.site_id}")
        if entity_context.installation_id:
            parts.append(f"Installation ID: {entity_context.installation_id}")

        for key, value in entity_context.additional_context.items():
            parts.append(f"{key}: {value}")

        if parts:
            return "[Entity Context]\n" + "\n".join(parts)
        return ""

    def _build_media_part(self, media) -> Optional[Dict[str, Any]]:
        """Build Gemini media part from MediaAttachment."""
        if media.type in ("image", "video", "audio"):
            if media.data:
                return {
                    "inline_data": {
                        "mime_type": media.mime_type or "image/jpeg",
                        "data": media.data,
                    }
                }
            elif media.url:
                LOGGER.warning(f"Media URL not yet supported, skipping: {media.url}")
                return None
        return None

    async def _synthesize_partial_answer(self, state: ConversationState) -> Optional[str]:
        """Ask Gemini to summarize what was gathered when the tool budget is exhausted.

        Used for staff sessions when MAX_TOOL_ROUNDS is hit, so the investigation context
        already in gemini_history isn't discarded. Called WITHOUT tools so Gemini is forced
        to produce final text instead of requesting more calls.
        """
        history = list(state.get("gemini_history") or [])
        if not history:
            return None

        synthesis_prompt = (
            "You've used your full tool-call budget without producing a final answer. "
            "Do NOT request any more tools — based ONLY on the tool results above, write a "
            "concise answer to the user's original question. Explicitly call out:\n"
            "• What you were able to confirm\n"
            "• What you couldn't complete (and why)\n"
            "• Any tickets, grids, meters, or items that still need follow-up\n\n"
            "Format for Telegram: short, scannable, use bullet points."
        )
        history.append({"role": "user", "parts": [{"text": synthesis_prompt}]})

        payload = self._build_payload(
            history=history,
            tools_payload=None,
            system_instructions=state.get("system_instructions"),
        )

        response = await self._gemini.generate_content(payload)
        text = self._extract_text(response)
        return text or None

    def _build_payload(
        self,
        history: List[Dict[str, Any]],
        tools_payload: Optional[List[Dict[str, Any]]],
        system_instructions: Optional[str],
    ) -> Dict[str, Any]:
        """Create generateContent payload with optional tool definitions."""
        generation_config: Dict[str, Any] = {
            "candidateCount": self._settings.gemini.candidate_count,
            "topK": self._settings.gemini.top_k,
            "topP": self._settings.gemini.top_p,
            "maxOutputTokens": self._settings.gemini.max_output_tokens,
        }

        # Temperature: only include if explicitly set (Gemini 3+ recommends default)
        effective_temp = self._settings.gemini.get_effective_temperature()
        if effective_temp is not None:
            generation_config["temperature"] = effective_temp

        # Thinking config: -1 = dynamic (omit), 0 = off, >0 = cap
        # Pro/deep-thinking models skip the budget cap (let the model think freely)
        current_model = self._gemini._model_config.model
        model_lower = current_model.lower()
        is_pro_model = model_lower.endswith("-pro") or "-pro-" in model_lower
        thinking_budget = self._settings.gemini.thinking_budget
        if is_pro_model:
            # Pro models: no thinking budget cap — let them think freely
            pass
        elif thinking_budget >= 0:
            if self._settings.gemini._is_gemini_3_or_later(current_model):
                generation_config["thinkingConfig"] = {"thinkingLevel": "medium"}
            else:
                generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}

        payload: Dict[str, Any] = {
            "generationConfig": generation_config,
        }

        if system_instructions:
            payload["systemInstruction"] = {"parts": [{"text": system_instructions}]}

        if tools_payload:
            payload["tools"] = tools_payload

        payload["contents"] = history

        return payload

    async def _handle_preference_call(
        self,
        call: FunctionCall,
        metadata: Dict[str, Any],
    ) -> ToolCallResult:
        """Handle a single user-preference tool call.

        Uses metadata (not state) for user_email, telegram_id, and user_input
        since this runs inside _execute_tool_calls which has no state access.
        """
        try:
            from orchestrator.services.user_preferences_service import (
                UserPreferencesService,
                get_preferences_service,
            )

            prefs_service = get_preferences_service()
            canonical_id = UserPreferencesService.resolve_canonical_id(
                metadata.get("user_email"), metadata.get("telegram_id")
            )

            if not canonical_id:
                return ToolCallResult(
                    name=call.name,
                    success=False,
                    output={"error": "Cannot identify user for preferences."},
                )

            args = call.arguments or {}
            if call.name == "store_user_preference":
                pref_result = await prefs_service.store_preference(
                    canonical_user_id=canonical_id,
                    preference_key=args.get("preference_key", "other"),
                    preference_value=args.get("preference_value", ""),
                    raw_expression=metadata.get("user_input", ""),
                )
            elif call.name == "list_user_preferences":
                prefs = await prefs_service.get_all(canonical_id)
                pref_result = {
                    "preferences": [
                        {
                            "key": p["preference_key"],
                            "value": p["preference_value"],
                            "set_on": p.get("created_at", ""),
                        }
                        for p in prefs
                    ],
                    "count": len(prefs),
                }
            elif call.name == "delete_user_preference":
                pref_result = await prefs_service.delete_preference(
                    canonical_user_id=canonical_id,
                    preference_key=args.get("preference_key", ""),
                )
            else:
                pref_result = {"error": f"Unknown preference tool: {call.name}"}

            return ToolCallResult(
                name=call.name,
                success=True,
                output=pref_result,
            )
        except Exception as e:
            LOGGER.exception(f"Error handling preference tool {call.name}: {e}")
            return ToolCallResult(
                name=call.name,
                success=False,
                output={"error": "Preference operation failed. Please try again."},
            )

    async def _handle_expert_meta_tool_call(
        self,
        call: FunctionCall,
        metadata: Dict[str, Any],
    ) -> ToolCallResult:
        """Handle a single expert meta-tool call (Phase D, read-only).

        Dispatches expert_list_steps/expert_find_packet/expert_get_packet_state
        to orchestrator.services.expert_meta_tools. These are fast, read-only
        lookups -- each is awaited directly and its result returned
        synchronously within this tool-call turn, exactly like
        _handle_preference_call above. There is deliberately no fire-and-poll
        background-task mechanism here (that pattern in expert_tool_runner.py
        exists for a different caller -- persistent agents launching a FULL,
        potentially multi-minute workflow run -- not for these sub-second
        introspection reads).

        expert_id resolution: expert_list_steps requires the LLM to pass
        expert_id explicitly as a tool argument (see its schema in
        prepare_tools.py). There's no synchronous packet_type -> expert_id
        reverse map available here the way PACKET_TYPE_TO_COMMAND is for
        slash commands (built once at import time from the synchronous
        command_registry) -- the expert-side equivalent,
        ExpertInstructionsProvider.get_expert_for_packet_type, is async (it
        may fetch the Expert Instructions Google Doc) and isn't something we
        want to call here just to validate/derive an argument the LLM
        already has visibility into via its own system instructions (which
        enumerate each expert's owned packet types). expert_find_packet and
        expert_get_packet_state don't need expert_id at all: packets are
        looked up directly by packet_type/key_entity or by packet_id.
        """
        from orchestrator.services import expert_meta_tools

        args = call.arguments or {}
        try:
            if call.name == "expert_list_steps":
                output = await expert_meta_tools.list_steps(
                    expert_id=args.get("expert_id", ""),
                    packet_type=args.get("packet_type", ""),
                )
            elif call.name == "expert_find_packet":
                # Staff-only tool (gated in prepare_tools.py), so organization_id
                # should always be present in metadata for a real staff user;
                # fall back to the canonical staff org if it's ever missing
                # (e.g. a user_context with no organization_ids) rather than
                # passing None through to a DB filter.
                import os

                organization_id = metadata.get("organization_id") or int(
                    os.getenv("STAFF_ORG_ID", "2")
                )
                output = await expert_meta_tools.find_packet(
                    packet_type=args.get("packet_type", ""),
                    key_entity=args.get("key_entity", ""),
                    organization_id=organization_id,
                )
            elif call.name == "expert_get_packet_state":
                output = await expert_meta_tools.get_packet_state(
                    packet_id=args.get("packet_id", ""),
                    keys=args.get("keys") or None,
                )
            elif call.name == "expert_run_steps":
                # Staff-only tool (gated in prepare_tools.py); same organization_id
                # fallback as expert_find_packet above.
                import os

                organization_id = metadata.get("organization_id") or int(
                    os.getenv("STAFF_ORG_ID", "2")
                )
                output = await expert_meta_tools.run_steps(
                    steps=args.get("steps") or [],
                    packet_id=args.get("packet_id") or None,
                    expert_id=args.get("expert_id") or None,
                    packet_type=args.get("packet_type") or None,
                    key_entity=args.get("key_entity") or None,
                    param_overrides_json=args.get("param_overrides_json") or None,
                    packet_inputs_json=args.get("packet_inputs_json") or None,
                    force=bool(args.get("force", False)),
                    confirmation_token=args.get("confirmation_token") or None,
                    organization_id=organization_id,
                    user_email=metadata.get("user_email"),
                    session_id=metadata.get("session_id"),
                )
            else:
                output = {"error": f"Unknown expert meta-tool: {call.name}"}

            # expert_run_steps outcomes (blocked / needs_user_input mid-loop
            # stop / mid-loop failure) carry an explicit "success" key that
            # must win over the plain "error" absence check below -- e.g. a
            # "blocked" refusal has no "error" key at all but is definitely
            # not a success. The other three (read-only) meta-tools never set
            # an explicit "success" key, so this falls through to the
            # original "error" not in output check for them unchanged.
            if isinstance(output, dict) and "success" in output:
                tool_success = bool(output["success"])
            else:
                tool_success = "error" not in output

            return ToolCallResult(
                name=call.name,
                success=tool_success,
                output=output,
            )
        except Exception as e:
            LOGGER.exception(f"Error handling expert meta-tool {call.name}: {e}")
            return ToolCallResult(
                name=call.name,
                success=False,
                output={"error": "Expert workflow lookup failed. Please try again."},
            )

    async def _execute_tool_calls(
        self,
        function_calls: List[FunctionCall],
        metadata: Dict[str, Any],
    ) -> List[ToolCallResult]:
        """Execute one or more tool calls, using parallelism when enabled."""
        if not function_calls:
            return []

        # Separate special tool calls from regular ones
        from orchestrator.graphs.nodes.prepare_tools import (
            EXPERT_META_TOOL_NAMES,
            PREFERENCE_TOOL_NAMES,
        )

        escalation_calls = [fc for fc in function_calls if fc.name == "escalate_to_support"]
        training_image_calls = [fc for fc in function_calls if fc.name == "fetch_training_image"]
        preference_calls = [fc for fc in function_calls if fc.name in PREFERENCE_TOOL_NAMES]
        expert_meta_tool_calls = [fc for fc in function_calls if fc.name in EXPERT_META_TOOL_NAMES]
        special_names = (
            {"escalate_to_support", "fetch_training_image"}
            | PREFERENCE_TOOL_NAMES
            | EXPERT_META_TOOL_NAMES
        )
        regular_calls = [fc for fc in function_calls if fc.name not in special_names]

        results = []

        # Handle escalation calls via special handler
        for call in escalation_calls:
            if self._escalation_handler:
                try:
                    escalation_result = await self._escalation_handler(call, metadata)
                    results.append(escalation_result)
                except Exception as e:
                    LOGGER.exception(f"Error handling escalation: {e}")
                    results.append(
                        ToolCallResult(
                            name=call.name,
                            success=False,
                            output={"success": False, "error": str(e)},
                        )
                    )
            else:
                # No handler configured - call EscalationService directly
                LOGGER.info("escalate_to_support: calling EscalationService directly")
                try:
                    from orchestrator.services.escalation_service import EscalationService

                    esc_service = EscalationService()
                    args = call.arguments or {}
                    esc_result = await esc_service.escalate_to_support(
                        question_summary=args.get("question_summary", "Escalation requested"),
                        session_id=metadata.get("session_id"),
                        # Safe here: this code only executes inside _execute_tools_node where
                        # metadata is already enriched with organization_id from user_context.
                        organization_id=metadata.get("organization_id"),
                        organization_short_name=metadata.get("organization_name"),
                        customer_chat_id=metadata.get("original_chat_id"),
                        customer_topic_id=metadata.get("topic_id"),
                        customer_username=metadata.get("user_name"),
                        customer_email=metadata.get("user_email"),
                        conversation_context=args.get("conversation_context"),
                        reason=args.get("reason"),
                        action_type=args.get("action_type"),
                        thread_id=metadata.get("thread_id"),
                    )
                    if not esc_result.get("success"):
                        # Add explicit instruction so Gemini doesn't claim success
                        esc_result["user_message"] = (
                            "ESCALATION FAILED. Tell the customer: "
                            "'I was unable to escalate your request right now due to a "
                            "technical issue. Please try again in a few minutes, or "
                            "contact support directly.' "
                            "Do NOT say you have escalated."
                        )
                    results.append(
                        ToolCallResult(
                            name=call.name,
                            success=esc_result.get("success", False),
                            output=esc_result,
                        )
                    )
                except Exception as e:
                    LOGGER.exception(f"Direct escalation failed: {e}")
                    results.append(
                        ToolCallResult(
                            name=call.name,
                            success=False,
                            output={
                                "success": False,
                                "error": str(e),
                                "user_message": (
                                    "ESCALATION FAILED. Tell the customer: "
                                    "'I was unable to escalate your request right now due to a "
                                    "technical issue. Please try again in a few minutes, or "
                                    "contact support directly.' "
                                    "Do NOT say you have escalated."
                                ),
                            },
                        )
                    )

        # Handle training image calls via special handler
        for call in training_image_calls:
            if self._training_image_handler:
                try:
                    image_result = await self._training_image_handler(call, metadata)
                    results.append(image_result)
                except Exception as e:
                    LOGGER.exception(f"Error handling training image fetch: {e}")
                    results.append(
                        ToolCallResult(
                            name=call.name,
                            success=False,
                            output={"success": False, "error": str(e)},
                        )
                    )
            else:
                LOGGER.warning(
                    "fetch_training_image called but no training_image_handler configured"
                )
                results.append(
                    ToolCallResult(
                        name=call.name,
                        success=False,
                        output={"error": "Training image fetch is not available."},
                    )
                )

        # Handle user preference tool calls
        for call in preference_calls:
            result = await self._handle_preference_call(call, metadata)
            results.append(result)

        # Handle expert meta-tool calls (read-only workflow introspection; Phase D)
        for call in expert_meta_tool_calls:
            result = await self._handle_expert_meta_tool_call(call, metadata)
            results.append(result)

        # Handle regular tool calls via executor
        if regular_calls:
            if self._settings.allow_parallel_calls and len(regular_calls) > 1:
                regular_results = await asyncio.gather(
                    *[self._executor.execute(call, metadata) for call in regular_calls]
                )
            else:
                regular_results = [
                    await self._executor.execute(call, metadata) for call in regular_calls
                ]
            results.extend(regular_results)

        return results

    def _prepare_tool_response(self, response_output: Any) -> Dict[str, Any]:
        """Prepare tool response for Gemini API format."""
        import json

        # Handle JSON string output
        if isinstance(response_output, str):
            try:
                response_output = json.loads(response_output)
            except (json.JSONDecodeError, ValueError):
                pass

        # Sanitize sensitive fields
        response_output = sanitize_tool_response(response_output)

        # Wrap error-only responses
        if (
            isinstance(response_output, dict)
            and "error" in response_output
            and len(response_output) == 1
        ):
            response_output = {
                "error_occurred": True,
                "error_message": response_output["error"],
                "details": "Tool execution failed",
            }

        # Ensure dict format for Gemini (functionResponse.response must be an object)
        if isinstance(response_output, str):
            return {"result": response_output}

        if isinstance(response_output, list):
            return {"result": response_output}

        if isinstance(response_output, dict):
            return response_output

        # Fallback for any other type
        return {"result": str(response_output)}

    @staticmethod
    def _inject_reasoning_to_tools(
        tools_payload: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Inject 'reasoning' parameter into dynamic tool schemas."""
        import copy

        modified = []
        for tool_wrapper in tools_payload:
            if "functionDeclarations" in tool_wrapper:
                modified_declarations = []
                for func in tool_wrapper["functionDeclarations"]:
                    func_copy = copy.deepcopy(func)
                    if "parameters" in func_copy:
                        func_copy["parameters"] = inject_reasoning_param(func_copy["parameters"])
                    else:
                        func_copy["parameters"] = inject_reasoning_param(
                            {"type": "OBJECT", "properties": {}, "required": []}
                        )
                    modified_declarations.append(func_copy)
                modified.append({"functionDeclarations": modified_declarations})
            else:
                # Non-functionDeclarations tools (e.g., google_search grounding)
                # — pass through unchanged, don't inject parameters
                modified.append(copy.deepcopy(tool_wrapper))

        return modified

    @staticmethod
    def _filter_command_gated_tools(
        tools_payload: List[Dict[str, Any]],
        unlocked_tools: List[str],
    ) -> List[Dict[str, Any]]:
        """Filter tools based on command context.

        Two modes:
        1. Exclusive mode (unlocked_tools non-empty): ONLY include tools in the list
        2. Gated mode (unlocked_tools empty): Exclude hardcoded command-gated tools
        """
        # Tools that require explicit command to unlock (dangerous actions)
        COMMAND_GATED_TOOLS = {
            "equipment_control_restart_inverter",
            "equipment_control_restart_comms_chain",
            "customer_retry_commissioning",
            "customer_unassign_meter",
            "customer_set_meter_power_limit",
            "customer_resend_meter_token",
            "customer_turn_meter_on",
            "customer_turn_meter_off",
        }

        # Separate exact names from prefix patterns (e.g., "prefix:grafana_")
        exact_tools = set()
        prefix_patterns = []
        for entry in unlocked_tools:
            if entry.startswith("prefix:"):
                prefix_patterns.append(entry[len("prefix:") :])
            else:
                exact_tools.add(entry)

        exclusive_mode = bool(unlocked_tools)

        # Log entry for debugging
        LOGGER.info(
            f"_filter_command_gated_tools called: exclusive_mode={exclusive_mode}, "
            f"unlocked_tools={unlocked_tools}"
        )

        def should_include(tool_name: str) -> bool:
            if exclusive_mode:
                # Exclusive mode: include tools in the exact set or matching a prefix
                if tool_name in exact_tools:
                    return True
                return any(tool_name.startswith(p) for p in prefix_patterns)
            else:
                # Gated mode: exclude command-gated tools
                return tool_name not in COMMAND_GATED_TOOLS

        filtered = []
        included_tools = []
        excluded_tools = []

        for tool_wrapper in tools_payload:
            if "functionDeclarations" in tool_wrapper:
                filtered_declarations = []
                for func in tool_wrapper["functionDeclarations"]:
                    tool_name = func.get("name", "")
                    if should_include(tool_name):
                        filtered_declarations.append(func)
                        included_tools.append(tool_name)
                    else:
                        excluded_tools.append(tool_name)

                if filtered_declarations:
                    filtered.append({"functionDeclarations": filtered_declarations})
            else:
                tool_name = tool_wrapper.get("name", "")
                if should_include(tool_name):
                    filtered.append(tool_wrapper)
                    included_tools.append(tool_name)
                else:
                    excluded_tools.append(tool_name)

        if exclusive_mode:
            LOGGER.info(
                f"Exclusive tools mode: included {len(included_tools)} tools: {included_tools}"
            )
            LOGGER.info(f"Exclusive tools mode: excluded {len(excluded_tools)} tools")
        else:
            if excluded_tools:
                LOGGER.debug(f"Gated mode: excluded {excluded_tools}")

        return filtered

    @staticmethod
    def _extract_tool_names(tools_payload: List[Dict[str, Any]]) -> Set[str]:
        """Extract all tool names from tools_payload.

        Args:
            tools_payload: Gemini tools format with functionDeclarations

        Returns:
            Set of tool names currently available
        """
        tool_names: Set[str] = set()
        for tool_wrapper in tools_payload:
            if "functionDeclarations" in tool_wrapper:
                for func in tool_wrapper["functionDeclarations"]:
                    name = func.get("name", "")
                    if name:
                        tool_names.add(name)
            else:
                name = tool_wrapper.get("name", "")
                if name:
                    tool_names.add(name)
        return tool_names

    @staticmethod
    def _filter_history_for_available_tools(
        history: List[Dict[str, Any]],
        available_tools: Set[str],
    ) -> List[Dict[str, Any]]:
        """Filter conversation history to remove function calls for unavailable tools.

        This prevents UNEXPECTED_TOOL_CALL errors when using exclusive_tools mode.
        When tools are filtered (e.g., /grids only has grid tools), the conversation
        history may contain function calls for tools like JIRA that are no longer
        available. Gemini sees these in history and tries to continue using them,
        resulting in UNEXPECTED_TOOL_CALL errors.

        This method removes function call/response pairs for tools not in available_tools.

        Args:
            history: Gemini conversation history
            available_tools: Set of currently available tool names

        Returns:
            Filtered history with unavailable tool calls removed
        """
        if not available_tools:
            return history

        filtered: List[Dict[str, Any]] = []
        i = 0
        removed_count = 0

        while i < len(history):
            msg = history[i]
            parts = msg.get("parts", [])

            # Check if this is a function call message
            if len(parts) == 1 and "functionCall" in parts[0]:
                func_name = parts[0]["functionCall"].get("name", "")

                # If tool is not available, skip this message and its response
                if func_name not in available_tools:
                    LOGGER.debug(f"Filtering out function call for unavailable tool: {func_name}")
                    removed_count += 1
                    i += 1

                    # Also skip the matching function response if it exists
                    if i < len(history):
                        next_msg = history[i]
                        next_parts = next_msg.get("parts", [])
                        if next_parts and "functionResponse" in next_parts[0]:
                            LOGGER.debug(
                                f"Filtering out matching function response for: {func_name}"
                            )
                            removed_count += 1
                            i += 1
                    continue

            # Check if this is an orphaned function response (shouldn't happen but be safe)
            if len(parts) == 1 and "functionResponse" in parts[0]:
                func_name = parts[0]["functionResponse"].get("name", "")
                if func_name not in available_tools:
                    LOGGER.debug(f"Filtering out orphaned function response: {func_name}")
                    removed_count += 1
                    i += 1
                    continue

            filtered.append(msg)
            i += 1

        if removed_count > 0:
            LOGGER.info(
                f"Filtered {removed_count} messages for unavailable tools "
                f"(available: {len(available_tools)} tools)"
            )

        return filtered

    @staticmethod
    def _format_jira_ticket_details(result_data: Dict[str, Any]) -> str:
        """Format JIRA ticket details for display when Gemini doesn't respond.

        Args:
            result_data: The output from jira_get_issue tool

        Returns:
            Formatted ticket details string
        """
        key = result_data.get("key", "Unknown")
        summary = result_data.get("summary", "No summary")
        status = result_data.get("status", "Unknown")
        priority = result_data.get("priority", "Unknown")
        assignee = result_data.get("assignee", "Unassigned")
        reporter = result_data.get("reporter", "Unknown")
        description = result_data.get("description", "")
        created = result_data.get("created", "")
        updated = result_data.get("updated", "")
        comments = result_data.get("comments", [])

        lines = [
            f"**{key}**: {summary}",
            "",
            f"**Status:** {status}",
            f"**Priority:** {priority}",
            f"**Assignee:** {assignee}",
            f"**Reporter:** {reporter}",
        ]

        if created:
            lines.append(f"**Created:** {created}")
        if updated:
            lines.append(f"**Updated:** {updated}")

        if description:
            # Truncate long descriptions
            desc_preview = description[:500] + "..." if len(description) > 500 else description
            lines.extend(["", "**Description:**", desc_preview])

        if comments:
            lines.extend(["", f"**Recent Comments ({len(comments)}):**"])
            # Show last 3 comments
            for comment in comments[-3:]:
                author = comment.get("author", "Unknown")
                body = comment.get("body", "")
                created_at = comment.get("created", "")
                body_preview = body[:200] + "..." if len(body) > 200 else body
                lines.append(f"- **{author}** ({created_at}): {body_preview}")

        return "\n".join(lines)

    @staticmethod
    def _extract_function_calls(response: Dict[str, Any]) -> List[FunctionCall]:
        """Parse Gemini response and extract requested tool calls."""
        calls: List[FunctionCall] = []
        for candidate in response.get("candidates", []):
            content = candidate.get("content", {})
            parts = content.get("parts", [])
            for part in parts:
                if "functionCall" in part:
                    data = part["functionCall"]
                    # thoughtSignature is a part-level sibling of functionCall, not nested inside it
                    thought_sig = part.get("thoughtSignature") or data.get("thoughtSignature")
                    calls.append(
                        FunctionCall(
                            name=data.get("name", ""),
                            arguments=data.get("args", {}),
                            thought_signature=thought_sig,
                        )
                    )
                elif "functionCalls" in part:
                    for call in part["functionCalls"]:
                        thought_sig = call.get("thoughtSignature")
                        calls.append(
                            FunctionCall(
                                name=call.get("name", ""),
                                arguments=call.get("args", {}),
                                thought_signature=thought_sig,
                            )
                        )
        return calls

    @staticmethod
    def _extract_text(response: Dict[str, Any]) -> Optional[str]:
        """Retrieve the first non-thought text part from the Gemini response.

        When thinking is enabled, Gemini returns thought parts (thought=true)
        before the actual answer. We must skip those to avoid leaking
        internal reasoning into user-facing messages.
        """
        for candidate in response.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                if "text" in part and not part.get("thought"):
                    return str(part["text"])
        return None

    @staticmethod
    def _validate_conversation_structure(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Validate and clean conversation history to meet Gemini API requirements."""
        if not history:
            return history

        cleaned_history: List[Dict[str, Any]] = []
        i = 0
        removed_orphans = 0
        removed_no_signature = 0

        while i < len(history):
            msg = history[i]
            parts = msg.get("parts", [])
            has_function_call = any("functionCall" in part for part in parts)

            if has_function_call:
                next_msg = history[i + 1] if i + 1 < len(history) else None

                if next_msg:
                    next_parts = next_msg.get("parts", [])
                    has_function_response = any("functionResponse" in part for part in next_parts)

                    if has_function_response:
                        # Gemini 3+ requires thoughtSignature on function call parts.
                        # Legacy history saved before the fix may be missing signatures.
                        # Strip these pairs to avoid 400 INVALID_ARGUMENT errors.
                        missing_sig = any(
                            "functionCall" in part and "thoughtSignature" not in part
                            for part in parts
                        )
                        if missing_sig:
                            func_name = parts[0].get("functionCall", {}).get("name", "unknown")
                            LOGGER.warning(
                                f"Removing function call+response pair missing thoughtSignature. "
                                f"Function: {func_name}"
                            )
                            i += 2  # Skip both the call and its response
                            removed_no_signature += 1
                            continue

                        cleaned_history.append(msg)
                        i += 1
                        continue
                    else:
                        LOGGER.warning(
                            f"Removing orphaned function call (no matching function response). "
                            f"Function: {parts[0].get('functionCall', {}).get('name', 'unknown')}"
                        )
                        i += 1
                        removed_orphans += 1
                        continue
                else:
                    LOGGER.warning(
                        f"Removing function call at end of history (no response). "
                        f"Function: {parts[0].get('functionCall', {}).get('name', 'unknown')}"
                    )
                    i += 1
                    removed_orphans += 1
                    continue

            cleaned_history.append(msg)
            i += 1

        original_count = len(history)
        cleaned_count = len(cleaned_history)
        if original_count != cleaned_count:
            details = []
            if removed_orphans:
                details.append(f"{removed_orphans} orphaned")
            if removed_no_signature:
                details.append(f"{removed_no_signature} missing thoughtSignature")
            LOGGER.info(
                f"Cleaned conversation history: {original_count} → {cleaned_count} messages "
                f"({', '.join(details)})"
            )

        return cleaned_history

    @staticmethod
    def _to_gemini_message(message: ConversationMessage) -> Dict[str, Any]:
        """Convert internal message representation to Gemini format."""
        parts: List[Dict[str, Any]] = []

        if message.content is not None:
            if message.timestamp and message.role == "user":
                text = f"[{message.timestamp}] {message.content}"
            else:
                text = message.content
            parts.append({"text": text})

        if message.function_call is not None:
            fc_data: Dict[str, Any] = {
                "name": message.function_call.name,
                "args": message.function_call.arguments,
            }
            # thoughtSignature must be a part-level sibling of functionCall
            part_data: Dict[str, Any] = {"functionCall": fc_data}
            if message.function_call.thought_signature:
                part_data["thoughtSignature"] = message.function_call.thought_signature
            parts.append(part_data)

        if message.tool_result is not None:
            import json

            response_output = message.tool_result.output
            if isinstance(response_output, str):
                try:
                    response_output = json.loads(response_output)
                except (json.JSONDecodeError, ValueError):
                    pass

            response_output = sanitize_tool_response(response_output)

            if (
                isinstance(response_output, dict)
                and "error" in response_output
                and len(response_output) == 1
            ):
                response_output = {
                    "error_occurred": True,
                    "error_message": response_output["error"],
                    "details": "Tool execution failed",
                }

            if isinstance(response_output, str):
                response_output = {"result": response_output}
            elif isinstance(response_output, list):
                # Gemini requires functionResponse.response to be an object, not a list.
                # Wrap list results (e.g., from scan_doc_comments returning a JSON array).
                response_output = {"result": response_output}

            parts.append(
                {
                    "functionResponse": {
                        "name": message.tool_result.name,
                        "response": response_output,
                    }
                }
            )

        gemini_role = "user" if message.role == "tool" else message.role
        return {"role": gemini_role, "parts": parts}


def build_conversation_graph(
    gemini_client: GeminiClient,
    registry: Optional[ToolRegistry] = None,
    executor: Optional[ToolExecutor] = None,
    settings: Optional[AppSettings] = None,
    escalation_handler: Optional[EscalationHandler] = None,
    training_image_handler: Optional[EscalationHandler] = None,
) -> StateGraph:
    """Factory function to build a conversation graph.

    Args:
        gemini_client: Client for Gemini API calls
        registry: Tool registry for available tools
        executor: Tool executor for running tool calls
        settings: Application settings
        escalation_handler: Handler for escalation tool calls
        training_image_handler: Handler for training image tool calls

    Returns:
        A compiled StateGraph ready for invocation
    """
    builder = ConversationGraphBuilder(
        gemini_client=gemini_client,
        registry=registry,
        executor=executor,
        settings=settings,
        escalation_handler=escalation_handler,
        training_image_handler=training_image_handler,
    )
    return builder.build()


def state_to_chat_response(state: ConversationState) -> ChatResponse:
    """Convert final graph state to ChatResponse.

    Args:
        state: The final state after graph execution

    Returns:
        A ChatResponse with the conversation results
    """
    return ChatResponse(
        final_text=state.get("final_response", ""),
        tool_calls=state.get("accumulated_tool_calls", []),
        tool_results=state.get("accumulated_tool_results", []),
        raw_responses=state.get("raw_gemini_responses", []),
        history=state.get("history_messages", []),
    )


__all__ = [
    "ConversationGraphBuilder",
    "build_conversation_graph",
    "state_to_chat_response",
]
