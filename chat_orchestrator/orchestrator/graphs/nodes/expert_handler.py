"""Expert handler node for LangGraph.

Executes expert workflows with hybrid LLM/function steps.
Auth context flows from main orchestrator via ConversationState.

This node is invoked when expert_router decides to route to an expert.
It either:
1. Creates a new work packet if none exists
2. Resumes an existing packet

The workflow is executed step by step, with progress tracked in the database.

Usage in graph:
    builder.add_node("expert_handler", expert_handler)
    builder.add_edge("expert_handler", "safety_check")
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from orchestrator.clients.gemini import GeminiClient
from orchestrator.config.settings import get_settings
from orchestrator.experts.step_context import StepContext
from orchestrator.experts.workflow_executor import WorkflowExecutor
from orchestrator.graphs.nodes.expert_router import parse_expert_command
from orchestrator.graphs.state import ConversationState
from orchestrator.mini_app.schemas import build_view_state_url
from orchestrator.models.schemas import ToolCallResult
from orchestrator.services.expert_instructions_provider import ExpertInstructionsProvider
from orchestrator.services.verification_service import ResponseVerificationService
from orchestrator.services.work_packet_service import WorkPacketService
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger
from shared.utils.telegram_buttons import build_webapp_keyboard

LOGGER = get_logger(__name__)

# Import input detection for centralized new-request detection
from orchestrator.experts.input_detection import looks_like_new_request

# Cancel keywords for detecting when user wants to abort a workflow
# Same keywords used in parameter_confirmation.py for consistency
CANCEL_KEYWORDS = {"cancel", "abort", "quit", "exit", "stop"}


def _build_tool_executor_metadata(
    state: ConversationState,
    packet: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build non-LLM-controllable metadata for expert workflow tool calls."""
    metadata: Dict[str, Any] = dict(state.get("metadata", {}))
    user_context = state.get("user_context")
    user_permissions = state.get("user_permissions")

    if user_context:
        metadata.update(
            {
                "user_email": user_context.user_email,
                "user_name": user_context.username,
                "original_chat_id": user_context.chat_id,
                "topic_id": user_context.topic_id,
                "session_id": state.get("session_id"),
                "thread_id": state.get("thread_id"),
                "organization_name": user_context.organization_name,
                "telegram_id": user_context.user_id if user_context.source == "telegram" else None,
                "user_input": state.get("user_input", ""),
            }
        )
        if user_context.organization_ids:
            metadata["organization_id"] = int(user_context.organization_ids[0])
        if not user_permissions:
            user_permissions = {
                "email": user_context.user_email,
                "organization_ids": user_context.organization_ids,
                "grid_ids": user_context.grid_ids,
                "meter_ids": user_context.meter_ids,
                "roles": user_context.roles,
                "is_admin": user_context.is_admin,
                "is_staff": user_context.is_staff,
            }

    if packet:
        if not metadata.get("user_email") and packet.get("requested_by_email"):
            metadata["user_email"] = packet.get("requested_by_email")
        packet_org_id = packet.get("organization_id")
        if metadata.get("organization_id") is None and packet_org_id is not None:
            metadata["organization_id"] = int(packet_org_id)
        if not user_permissions and packet_org_id is not None:
            user_permissions = {
                "email": packet.get("requested_by_email"),
                "organization_ids": [str(packet_org_id)],
                "grid_ids": [],
                "meter_ids": [],
                "roles": [],
                "is_admin": False,
                "is_staff": False,
            }

    if user_permissions:
        metadata["user_permissions"] = user_permissions

    return metadata


def _is_cancel_request(user_input: str) -> bool:
    """Check if user input is a cancel request.

    Args:
        user_input: The user's input string

    Returns:
        True if user wants to cancel the workflow
    """
    if not user_input:
        return False
    return user_input.strip().lower() in CANCEL_KEYWORDS


import re


def _fix_hallucinated_values(
    text: str,
    accumulated_results: Dict[str, Any],
    packet_state: Dict[str, Any],
) -> str:
    """Replace hallucinated values in LLM response with actual workflow data.

    LLMs sometimes make up URLs, IDs, and other values instead of using the
    actual data from workflow results. This function detects common patterns
    and replaces them with real values.

    Args:
        text: Response text from LLM
        accumulated_results: Results from workflow steps
        packet_state: Current packet state

    Returns:
        Text with hallucinated values replaced by real ones
    """
    # Collect actual values from results and state
    actual_values: Dict[str, Any] = {}

    # From copy_lpp_template result
    template_result = accumulated_results.get("copy_lpp_template", {})
    if isinstance(template_result, dict):
        if template_result.get("document_url"):
            actual_values["document_url"] = template_result["document_url"]
        if template_result.get("document_id"):
            actual_values["document_id"] = template_result["document_id"]
        if template_result.get("document_title"):
            actual_values["document_title"] = template_result["document_title"]

    # From generate_distribution_map result
    map_result = accumulated_results.get("generate_distribution_map", {})
    if isinstance(map_result, dict):
        if map_result.get("site_id"):
            actual_values["site_id"] = map_result["site_id"]
        if map_result.get("site_name"):
            actual_values["site_name"] = map_result["site_name"]

    # From packet_state (fallback)
    for key in ["document_url", "document_id", "site_id", "site_name", "document_title"]:
        if key not in actual_values and packet_state.get(key):
            actual_values[key] = packet_state[key]

    result_text = text

    # Replace fake Google Docs/Sheets/Slides URLs with actual URL
    if actual_values.get("document_url"):
        actual_url = actual_values["document_url"]
        # Pattern for fake Google URLs (made up IDs)
        fake_url_patterns = [
            r"https?://docs\.google\.com/(?:document|spreadsheets|presentation)/d/[a-zA-Z0-9_-]+(?:/[^\s\)]*)?",
            r"https?://drive\.google\.com/(?:file/d|open\?id=)[a-zA-Z0-9_-]+(?:/[^\s\)]*)?",
        ]
        for pattern in fake_url_patterns:
            # Only replace if the URL is not the actual one
            matches = re.findall(pattern, result_text)
            for match in matches:
                if actual_url not in match:
                    result_text = result_text.replace(match, actual_url)
                    LOGGER.debug(f"Replaced hallucinated URL: {match[:50]}... -> {actual_url}")

    # Replace fake site IDs (common patterns: 12345, 00000, sequential numbers)
    if actual_values.get("site_id"):
        actual_site_id = str(actual_values["site_id"])
        # Pattern: "site ID: 12345" or "Site ID 12345" or "(12345)"
        fake_id_pattern = r"(?:site\s*(?:ID|Id|id)[:\s]*|ID[:\s]*|\()([\d]{4,8})(?:\)|[,\.\s]|$)"
        for match in re.finditer(fake_id_pattern, result_text, re.IGNORECASE):
            fake_id = match.group(1)
            # Don't replace if it's the actual ID
            if fake_id != actual_site_id and fake_id in ["12345", "123456", "00000", "11111"]:
                result_text = result_text.replace(fake_id, actual_site_id)
                LOGGER.debug(f"Replaced hallucinated site ID: {fake_id} -> {actual_site_id}")

    return result_text


def _extract_base64_from_text(
    text: str,
) -> tuple[str, List[ToolCallResult]]:
    """Extract base64 image data from response text.

    LLMs sometimes include raw base64 data in their responses when asked to
    "include the image". This function detects and extracts such data,
    returning cleaned text and image attachments.

    Args:
        text: Response text that may contain base64 data

    Returns:
        Tuple of (cleaned_text, list of ToolCallResult with images)
    """
    images: List[ToolCallResult] = []

    # Pattern 1: data:image/...;base64,... format
    data_uri_pattern = r"data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=]+)"

    # Pattern 2: Raw base64 that looks like image data (long alphanumeric string)
    # Base64 images are typically >1000 chars, start with specific patterns for PNG/JPEG
    # PNG starts with iVBORw0KGgo, JPEG starts with /9j/
    raw_b64_pattern = r"(?:^|\s)((?:iVBORw0KGgo|/9j/)[A-Za-z0-9+/=]{500,})(?:\s|$)"

    cleaned_text = text

    # Extract data URI format
    for match in re.finditer(data_uri_pattern, text):
        mime_subtype = match.group(1)
        b64_data = match.group(2)
        mime_type = f"image/{mime_subtype}"

        images.append(
            ToolCallResult(
                name="extracted_image",
                success=True,
                output="Image extracted from response",
                raw_response={
                    "result": [{"type": "image", "data": b64_data, "mimeType": mime_type}]
                },
            )
        )
        # Remove the data URI from text
        cleaned_text = cleaned_text.replace(match.group(0), "[Image]")

    # Extract raw base64 patterns
    for match in re.finditer(raw_b64_pattern, text):
        b64_data = match.group(1)

        # Determine mime type from prefix
        if b64_data.startswith("iVBORw0KGgo"):
            mime_type = "image/png"
        else:
            mime_type = "image/jpeg"

        images.append(
            ToolCallResult(
                name="extracted_image",
                success=True,
                output="Image extracted from response",
                raw_response={
                    "result": [{"type": "image", "data": b64_data, "mimeType": mime_type}]
                },
            )
        )
        # Remove the raw base64 from text
        cleaned_text = cleaned_text.replace(match.group(1), "[Image]")

    # Clean up any leftover artifacts like "Here is the map: [Image]" -> "Here is the map:"
    cleaned_text = re.sub(r"\[Image\]\s*\[Image\]", "[Image]", cleaned_text)
    cleaned_text = re.sub(r":\s*\[Image\]", ":", cleaned_text)

    if images:
        LOGGER.info(f"Extracted {len(images)} base64 images from response text")

    return cleaned_text.strip(), images


def _strip_technical_artifacts(text: str) -> str:
    """Remove technical artifacts that LLMs sometimes include in responses.

    LLMs may include mermaid diagrams, Python code blocks, or other
    implementation details that shouldn't be shown to end users.

    Args:
        text: Response text that may contain technical artifacts

    Returns:
        Cleaned text with artifacts removed
    """
    cleaned = text

    # Remove mermaid diagrams (```mermaid ... ``` or graph TD/LR/etc blocks)
    # Pattern 1: Fenced mermaid blocks
    cleaned = re.sub(
        r"```mermaid\s*\n[\s\S]*?```",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Pattern 2: Inline graph TD/LR/TB/BT diagrams (not in code fence)
    # These start with "graph TD" or similar and contain arrows like -->
    cleaned = re.sub(
        r"(?:^|\n)\s*graph\s+(?:TD|LR|TB|BT|RL)\s*\n(?:.*?(?:-->|---|\|).*?\n)+",
        "\n",
        cleaned,
        flags=re.MULTILINE,
    )

    # Remove Python code blocks that look like implementation code
    # Pattern: ```python ... import ... ``` (code with imports)
    cleaned = re.sub(
        r"```python\s*\n[\s\S]*?(?:import|from|def|class)\s+[\s\S]*?```",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Remove standalone base64 variable assignments
    # Pattern: variable_name = 'iVBORw0KGgo...'
    cleaned = re.sub(
        r"[a-zA-Z_][a-zA-Z0-9_]*\s*=\s*['\"][A-Za-z0-9+/=]{100,}['\"]",
        "",
        cleaned,
    )

    # Remove IPython display calls
    cleaned = re.sub(
        r"(?:from\s+IPython\.display\s+import.*?\n|display\s*\([^)]+\))",
        "",
        cleaned,
    )

    # Clean up multiple blank lines left behind
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    # Clean up lines that only have dashes (section separators often left behind)
    cleaned = re.sub(r"^\s*-{3,}\s*$", "", cleaned, flags=re.MULTILINE)

    if cleaned != text:
        LOGGER.info("Stripped technical artifacts from LLM response")

    return cleaned.strip()


def _strip_buttons_block(text: str) -> str:
    """Remove [BUTTONS]...[/BUTTONS] blocks from expert workflow responses.

    Expert workflows complete all actions automatically, so LLM-generated
    buttons (e.g. "Generate Final BOM", "Notify Engineering Team") are
    misleading — they suggest actions that either already happened or
    don't exist. Stripping them prevents fake buttons from reaching users.
    """
    from shared.utils.telegram_buttons import BUTTONS_BLOCK_PATTERN

    cleaned = BUTTONS_BLOCK_PATTERN.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if cleaned != text:
        LOGGER.info("Stripped [BUTTONS] block from expert workflow response")
    return cleaned.strip()


def _extract_images_from_workflow(
    accumulated_results: Dict[str, Any],
) -> List[ToolCallResult]:
    """Extract images from workflow step results.

    Scans accumulated results for image data (base64-encoded) and creates
    ToolCallResult entries in the MCP format so they can be sent to Telegram.

    Args:
        accumulated_results: Results from completed workflow steps

    Returns:
        List of ToolCallResult with images in MCP format
    """
    images: List[ToolCallResult] = []

    # Known image fields to look for
    image_fields = ["map_image_b64", "chart_image_b64", "image_b64", "screenshot_b64"]

    for step_name, step_result in accumulated_results.items():
        if not isinstance(step_result, dict):
            continue

        for field in image_fields:
            image_data = step_result.get(field)
            if image_data and isinstance(image_data, str):
                # Determine caption from step result or step name
                caption = step_result.get("site_name", step_name.replace("_", " ").title())

                # Create ToolCallResult with MCP image format
                images.append(
                    ToolCallResult(
                        name=f"workflow_{step_name}",
                        success=True,
                        output=f"Generated image: {caption}",
                        raw_response={
                            "result": [
                                {
                                    "type": "image",
                                    "data": image_data,
                                    "mimeType": "image/png",
                                }
                            ]
                        },
                    )
                )
                LOGGER.debug(f"Extracted image from {step_name}.{field}")

    return images


async def _sanitize_error_response(response: str, context: str = "expert workflow") -> str:
    """Sanitize error responses using LLM judge to remove technical details.

    Only called for error/failure responses, not successful ones.
    """
    try:
        async with ResponseVerificationService() as verifier:
            result: str = await verifier.sanitize_technical_response(response, context)
            return result
    except Exception as e:
        LOGGER.warning(f"Failed to sanitize error response: {e}")
        # Fall back to basic sanitization
        return sanitize_error_for_user(response, context)


async def expert_handler(state: ConversationState) -> Dict[str, Any]:
    """Execute expert work on a packet using hybrid workflow.

    Auth context:
    - user_context comes from resolve_auth via state
    - mcp_executor already configured with user_context in prepare_tools
    - packet stores original requester for multi-session consistency

    Args:
        state: Current conversation state with expert routing info

    Returns:
        State updates including final_response and packet info
    """
    # Initialize services
    packet_service = WorkPacketService()
    expert_provider = ExpertInstructionsProvider()

    # Get routing info from state
    expert_id = state.get("matched_expert_id")
    packet = state.get("active_work_packet")
    session_id = state.get("session_id")
    user_input = state.get("user_input", "")
    user_context = state.get("user_context")
    _chat_id = getattr(user_context, "chat_id", None)

    if not expert_id:
        LOGGER.error("expert_handler called without matched_expert_id")
        return {
            "expert_error": "No expert matched",
            "final_response": await _sanitize_error_response(
                "I couldn't determine which expert should handle this request.",
                "routing",
            ),
        }

    # Get expert configuration
    expert_config = await expert_provider.get_expert_config(expert_id)
    if not expert_config:
        LOGGER.error(f"Expert configuration not found: {expert_id}")
        return {
            "expert_error": f"Expert not found: {expert_id}",
            "final_response": await _sanitize_error_response(
                f"The {expert_id} expert is not available.",
                "expert configuration",
            ),
        }

    # Create or resume packet
    if not packet:
        packet = await _create_new_packet(
            state=state,
            packet_service=packet_service,
            expert_id=expert_id,
            expert_config=expert_config,
        )

        if not packet:
            return {
                "expert_error": "Failed to create work packet",
                "final_response": await _sanitize_error_response(
                    "I couldn't start the analysis. Please try again.",
                    "packet creation",
                ),
            }

    # Check if packet is awaiting input and we have new input
    # BUT: If user_input was consumed by a decision (e.g., duplicate/resume decision),
    # don't pass it to the workflow - it was meant for the decision, not for step input
    user_input_consumed = state.get("user_input_consumed", False)
    if packet.get("packet_status") == "awaiting_input" and user_input and not user_input_consumed:
        # Global cancel detection - check before resuming workflow
        if _is_cancel_request(user_input):
            LOGGER.info(f"User cancelled workflow for packet {packet['packet_id']}")
            await packet_service.fail_packet(
                packet["packet_id"],
                "Workflow cancelled by user",
                session_id,
            )
            return {
                "final_response": "Workflow cancelled.",
                "active_work_packet": None,
                "expert_executed": False,
            }

        # Centralized new-request detection - check if input looks like a new question
        # This prevents users from getting stuck when they ask an unrelated question
        # while a workflow step is waiting for input (e.g., manual KPIs, site selection)
        if looks_like_new_request(user_input):
            LOGGER.info(
                f"User input appears to be new request while workflow awaiting input: "
                f"'{user_input[:50]}...' - redirecting to main LLM"
            )
            # Keep the workflow paused (don't resume or fail it)
            # Just redirect to main LLM to handle the new question
            return {
                "final_response": None,
                "active_work_packet": packet,
                "expert_executed": False,
                "redirect_to_main_llm": True,
                "redirect_reason": "Input appears to be a new question - pausing workflow",
            }

        LOGGER.info(f"Resuming packet {packet['packet_id']} with user input")

        # Remove webapp buttons (Edit Parameters / View State) from the previous message
        # since the workflow is continuing and those buttons should no longer be interactive
        pkt_state = packet.get("packet_state") or {}
        buttons_msg_id = pkt_state.get("buttons_message_id")
        buttons_chat_id = pkt_state.get("buttons_chat_id")
        if buttons_msg_id and buttons_chat_id:
            try:
                from shared.utils.telegram_buttons import remove_buttons_from_message

                await remove_buttons_from_message(buttons_chat_id, buttons_msg_id)
                LOGGER.debug(
                    f"Removed buttons from message {buttons_msg_id} in chat {buttons_chat_id}"
                )
            except Exception as btn_err:
                LOGGER.warning(f"Failed to remove buttons on resume: {btn_err}")

        packet = await packet_service.resume_from_input(
            packet["packet_id"],
            user_input,
            session_id,
        )
    elif user_input_consumed:
        LOGGER.info(
            f"Skipping user_input for packet {packet['packet_id']} - "
            f"input was consumed by decision handling"
        )

    # Create workflow executor and tools FIRST (needed by step_context)
    # Use singleton settings (not from state to avoid checkpointer serialization errors)
    settings = get_settings()

    try:
        # Use expert-specific model if configured, otherwise fall back to main bot model
        model_config = settings.gemini
        expert_model = expert_config.model.strip() if expert_config.model else None

        if expert_model:
            LOGGER.info(
                f"Using expert-specific model: {expert_model} (fallback: {model_config.model})"
            )
            # Create a modified config with the expert's model
            model_config = model_config.model_copy(update={"model": expert_model})
        else:
            LOGGER.info(f"Expert has no model override, using main bot model: {model_config.model}")

        gemini = GeminiClient(
            api_key=settings.google_api_key,
            model_config=model_config,
        )
    except Exception as e:
        LOGGER.error(f"Failed to create Gemini client: {e}")
        return {
            "expert_error": f"Gemini client error: {e}",
            "final_response": await _sanitize_error_response(
                "I'm having trouble connecting to the AI service.",
                "AI service",
            ),
        }

    # Create tool executor locally (not stored in state to avoid checkpointer serialization errors)
    tool_executor = state.get("tool_executor")
    if not tool_executor:
        LOGGER.debug("Creating tool executor locally (expected - not stored in state)")
        try:
            from orchestrator.services.tool_executor import ToolExecutor
            from orchestrator.services.tool_registry import ToolRegistry

            registry = ToolRegistry(settings)
            tool_executor = ToolExecutor(
                registry,
                settings,
                default_metadata=_build_tool_executor_metadata(state, packet),
            )
            LOGGER.info("Created tool executor for expert handler")
        except Exception as e:
            LOGGER.error(f"Failed to create tool executor: {e}")
            # For workflows that require tools, fail early instead of silently continuing
            packet_type = state.get("expert_packet_type") or packet.get("packet_type") or "unknown"
            if packet_type in ("kpi_report", "grid_analysis"):
                return {
                    "expert_error": f"Tool executor unavailable: {e}",
                    "final_response": await _sanitize_error_response(
                        "I couldn't start the report - tool services are unavailable. Please try again.",
                        "tool services",
                    ),
                }
            # Continue without tools for other workflows that may not need them

    # Build StepContext with full auth context (including tool_executor)
    step_context = _build_step_context(
        state=state,
        packet=packet,
        expert_config=expert_config,
        tool_executor=tool_executor,  # Pass the executor we just created
    )

    # Add expert config sections to accumulated_results
    # This allows function steps to access packet-specific sections like ### Input Data Key Column
    if expert_config and hasattr(expert_config, "raw_sections"):
        # Pass raw_sections dict so function steps can access any section
        step_context.accumulated_results["expert_raw_sections"] = expert_config.raw_sections
        LOGGER.debug(
            f"Added expert_raw_sections to accumulated_results: {list(expert_config.raw_sections.keys())}"
        )

    # Pre-fetch context-aware RAG based on packet type and inputs
    try:
        rag_context = await _fetch_expert_rag_context(
            step_context=step_context,
            packet=packet,
            expert_config=expert_config,
        )
        step_context.rag_context = rag_context
        if rag_context:
            LOGGER.info(f"Pre-fetched {len(rag_context)} RAG chunks for {packet['packet_type']}")
    except Exception as e:
        LOGGER.warning(f"Failed to fetch expert RAG context: {e}")

    executor = WorkflowExecutor(
        gemini_client=gemini,
        packet_service=packet_service,
        mcp_executor=tool_executor,
    )

    # Execute workflow
    try:
        final_response, extra_state = await executor.execute_workflow(
            expert_config=expert_config,
            packet=packet,
            context=step_context,
            on_progress=None,
        )
    except Exception as e:
        LOGGER.exception(f"Workflow execution failed: {e}")

        # Store detailed error context for potential resume
        error_state = {
            "last_error": str(e),
            "error_step": step_context.current_step,
            "error_time": datetime.utcnow().isoformat(),
            "steps_completed_at_error": step_context.steps_completed.copy(),
            "accumulated_results_at_error": step_context.accumulated_results.copy(),
        }

        await packet_service.fail_packet(
            packet["packet_id"],
            f"Workflow error: {str(e)}",
            session_id,
            error_state=error_state,
        )

        # Use LLM judge to sanitize error for user
        sanitized_response = await _sanitize_error_response(
            str(e),
            f"{expert_id} workflow",
        )

        failure_result: Dict[str, Any] = {
            "expert_error": str(e),  # Keep original for logging
            "final_response": sanitized_response,
            "active_work_packet": packet,
        }
        state_url = build_view_state_url(packet["packet_id"])
        if state_url:
            failure_result["reply_markup"] = build_webapp_keyboard(
                "View State", state_url, chat_id=_chat_id
            )
        return failure_result

    # Check if workflow step failed (not exception, but StepResult.failure)
    if extra_state.get("error"):
        LOGGER.warning(f"Workflow step failed: {extra_state.get('error')}")
        # Use LLM judge to sanitize the error response
        sanitized_response = await _sanitize_error_response(
            final_response,
            f"{expert_id} workflow",
        )
        step_failure_result: Dict[str, Any] = {
            "expert_error": extra_state.get("error"),
            "final_response": sanitized_response,
            "active_work_packet": packet,
            "expert_executed": False,
        }
        state_url = build_view_state_url(packet["packet_id"])
        if state_url:
            step_failure_result["reply_markup"] = build_webapp_keyboard(
                "View State", state_url, chat_id=_chat_id
            )
        return step_failure_result

    # Check if workflow needs user input
    if extra_state.get("needs_user_input"):
        result = {
            "final_response": final_response,
            "active_work_packet": packet,
            "expert_executed": False,
            "expert_awaiting_input": True,
        }
        if extra_state.get("reply_markup"):
            result["reply_markup"] = extra_state["reply_markup"]
        return result

    # Check if workflow detected unrelated input and wants to redirect to main LLM
    if extra_state.get("redirect_to_main_llm"):
        LOGGER.info(
            f"Workflow requested redirect to main LLM: {extra_state.get('redirect_reason')}"
        )
        return {
            "final_response": None,  # No response yet - main LLM will handle
            "active_work_packet": packet,
            "expert_executed": False,
            "redirect_to_main_llm": True,
            "redirect_reason": extra_state.get("redirect_reason"),
        }

    # Extract images from workflow results to send with response
    workflow_images = _extract_images_from_workflow(extra_state.get("accumulated_results", {}))
    if workflow_images:
        LOGGER.info(f"Found {len(workflow_images)} images in workflow results")

    # Also extract any base64 that the LLM included in its response text
    cleaned_response, text_images = _extract_base64_from_text(final_response)
    if text_images:
        LOGGER.info(f"Extracted {len(text_images)} base64 images from response text")
        workflow_images.extend(text_images)
        final_response = cleaned_response

    # Strip technical artifacts (mermaid diagrams, code blocks, etc.)
    final_response = _strip_technical_artifacts(final_response)

    # Strip [BUTTONS] blocks — expert workflows don't generate actionable buttons.
    # The LLM sometimes hallucinates buttons for non-existent actions.
    final_response = _strip_buttons_block(final_response)

    # Fix hallucinated values (URLs, IDs) with actual data from workflow
    final_response = _fix_hallucinated_values(
        final_response,
        extra_state.get("accumulated_results", {}),
        step_context.packet_state,
    )

    # Workflow complete - mark packet as completed if not already failed
    refreshed_packet = await packet_service.get_packet(packet["packet_id"])
    if refreshed_packet and refreshed_packet.get("packet_status") not in (
        "completed",
        "failed",
    ):
        # Generate outputs from accumulated results
        outputs = extra_state.get("accumulated_results", {})

        # Create summary output
        summary_output = {
            "summary": final_response,
            "steps_executed": list(outputs.keys()),
        }

        # Add external doc reference if created
        for step_name, step_result in outputs.items():
            if isinstance(step_result, dict):
                if step_result.get("document_url"):
                    summary_output["external_doc"] = {
                        "system": "google_docs",
                        "url": step_result.get("document_url"),
                        "doc_id": step_result.get("document_id"),
                    }
                    break

        await packet_service.complete_packet(
            packet["packet_id"],
            outputs=summary_output,
            external_url=summary_output.get("external_doc", {}).get("url"),
            session_id=session_id,
        )

    # Build return state with images if any
    result_state: Dict[str, Any] = {
        "final_response": final_response,
        "active_work_packet": refreshed_packet or packet,
        "expert_executed": True,
        "expert_awaiting_input": False,
    }

    # Attach View State button to completion response
    state_url = build_view_state_url(packet["packet_id"])
    if state_url:
        result_state["reply_markup"] = build_webapp_keyboard(
            "View State", state_url, chat_id=_chat_id
        )

    # Add workflow images to accumulated_tool_results so they get sent to Telegram
    if workflow_images:
        # Merge with any existing tool results from the workflow
        existing_results = state.get("accumulated_tool_results", [])
        result_state["accumulated_tool_results"] = existing_results + workflow_images

    return result_state


def _build_lpp_packet_inputs(
    packet_type: str,
    effective_request: str,
    expert_command: str | None,
    key_entity: str | None,
    args: str,
) -> dict:
    """Build packet inputs, detecting the LPP GPS-anchor (Route B) route from args."""
    inputs: dict = {
        "raw_request": effective_request,
        "parsed_command": expert_command,
        "key_entity": key_entity,
        "args": args,
    }

    if packet_type == "light_preliminary_package":
        from orchestrator.services.command_parser import parse_lpp_anchor_args

        anchor = parse_lpp_anchor_args(args)
        if anchor:
            inputs.update(anchor)  # latitude, longitude, community_name
            return inputs  # anchor route: do NOT set site_name from args

    if key_entity:
        inputs["site_name"] = key_entity
        inputs["grid_name"] = key_entity
    return inputs


async def _create_new_packet(
    state: ConversationState,
    packet_service: WorkPacketService,
    expert_id: str,
    expert_config: Any,
) -> Optional[Dict[str, Any]]:
    """Create a new work packet from user request.

    Args:
        state: Current conversation state
        packet_service: Packet service for database operations
        expert_id: Assigned expert ID
        expert_config: Expert configuration

    Returns:
        Created packet or None if failed
    """
    session_id = state.get("session_id")
    user_input = state.get("user_input", "")
    user_context = state.get("user_context")
    expert_command = state.get("expert_command")
    packet_type = state.get("expert_packet_type")

    # Use expert_command as the goal if available (e.g., after "start fresh")
    # expert_command contains the original request like "/report monthly ExampleGrid"
    # user_input might just be "2" (the start fresh selection)
    effective_request = expert_command if expert_command else user_input

    # Parse command for packet type if not already set
    if not packet_type and expert_command:
        parsed = parse_expert_command(effective_request)
        packet_type = parsed.get("packet_type")

    # Default packet type from expert's first supported type
    if not packet_type and expert_config.packet_types:
        packet_type = expert_config.packet_types[0]

    if not packet_type:
        LOGGER.error("Could not determine packet type")
        return None

    # Extract user info
    user_email = user_context.user_email if user_context else None
    org_id = None
    if user_context and user_context.organization_ids:
        org_id = int(user_context.organization_ids[0])

    # Generate title
    date_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    packet_title = f"{expert_config.display_name}: {date_str}"

    try:
        # Get key_entity from state (extracted by expert_router)
        key_entity = state.get("expert_key_entity")

        # Build packet inputs - include site_name for LPP and other workflows
        # Extract args (everything after the command name)
        args = ""
        if expert_command and effective_request:
            # Remove leading slash and command name to get args
            parts = effective_request.strip().split(None, 1)
            if len(parts) > 1:
                args = parts[1]  # Everything after "/command"

        packet_inputs_data = _build_lpp_packet_inputs(
            packet_type=packet_type,
            effective_request=effective_request,
            expert_command=expert_command,
            key_entity=key_entity,
            args=args,
        )

        # Auto-cancel any existing active packets of the same type for this session
        # This prevents stuck workflows and user confusion
        cancelled_count = await packet_service.cancel_active_packets_of_type(
            session_id=session_id,
            packet_type=packet_type,
            reason="Superseded by new workflow request",
        )
        if cancelled_count > 0:
            LOGGER.info(
                f"Auto-cancelled {cancelled_count} existing {packet_type} packet(s) "
                f"before creating new one"
            )

        # Include parent (chat-level) session_id so the packet is findable
        # even when the user replies without a topic/thread context.
        parent_session_ids = []
        if user_context and user_context.topic_id and user_context.chat_id:
            from orchestrator.utils.session_id import generate_parent_session_id

            parent_sid = generate_parent_session_id(
                source=user_context.source,
                chat_id=user_context.chat_id,
                topic_id=user_context.topic_id,
                user_id=user_context.user_id,
            )
            if parent_sid:
                parent_session_ids.append(parent_sid)

        packet = await packet_service.create_packet(
            packet_type=packet_type,
            packet_title=packet_title,
            packet_goal=effective_request,
            assigned_expert=expert_id,
            packet_inputs=packet_inputs_data,
            session_id=session_id,
            additional_session_ids=parent_session_ids,
            requested_by_email=user_email,
            organization_id=org_id,
        )

        # Store thread_id on packet state for thread-aware routing
        thread_id = state.get("thread_id")
        if thread_id:
            await packet_service.update_state(packet["packet_id"], {"thread_id": thread_id})

        # Start the packet
        workflow = expert_config.get_workflow(packet_type)
        first_step = "execute"

        if workflow:
            # Parse first step name
            from orchestrator.experts.workflow_executor import WorkflowExecutor

            temp_executor = WorkflowExecutor(None, None, None)  # type: ignore
            steps = temp_executor.parse_workflow(workflow)
            if steps:
                first_step = steps[0].name

        packet = await packet_service.start_packet(
            packet["packet_id"],
            first_step=first_step,
            session_id=session_id,
        )

        LOGGER.info(f"Created new packet: {packet['packet_id']}")
        return packet  # type: ignore[no-any-return]

    except Exception as e:
        LOGGER.exception(f"Failed to create work packet: {e}")
        return None


def _build_step_context(
    state: ConversationState,
    packet: Dict[str, Any],
    expert_config: Any,
    tool_executor: Optional[Any] = None,
) -> StepContext:
    """Build StepContext from state and packet.

    Args:
        state: Current conversation state
        packet: Work packet data
        expert_config: Expert configuration
        tool_executor: Optional ToolExecutor instance (created by caller)

    Returns:
        Configured StepContext with full auth and RAG access
    """
    import os

    user_context = state.get("user_context")
    session_id = state.get("session_id", "")
    user_input = state.get("user_input", "")

    # Use passed tool_executor, or fall back to state if not provided
    if tool_executor is None:
        tool_executor = state.get("tool_executor")

    # Get RAG context from state (prepared by prepare_context node)
    rag_context = state.get("rag_context")
    context_message = state.get("context_message")

    # Initialize RAG provider for on-demand queries
    rag_provider = None
    try:
        from orchestrator.services.rag_provider import RAGProvider

        rag_provider = RAGProvider(
            rag_supabase_url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL"),
            rag_supabase_key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY"),
            auth_supabase_url=os.getenv("AUTH_SUPABASE_URL"),
            auth_supabase_anon_key=os.getenv("AUTH_SUPABASE_ANON_KEY"),
        )
    except Exception as e:
        LOGGER.warning(f"Could not initialize RAG provider for expert: {e}")

    return StepContext(
        # Packet info
        packet_id=packet["packet_id"],
        packet_type=packet["packet_type"],
        packet_goal=packet["packet_goal"],
        packet_inputs=packet.get("packet_inputs") or {},
        packet_state=packet.get("packet_state") or {},
        # Workflow progress
        current_step=packet.get("current_step") or "execute",
        steps_completed=packet.get("steps_completed") or [],
        accumulated_results=(packet.get("packet_state") or {}).get("accumulated_results", {}),
        # Session context
        session_id=session_id,
        user_email=user_context.user_email if user_context else None,
        organization_id=(
            int(user_context.organization_ids[0])
            if user_context and user_context.organization_ids
            else None
        ),
        user_context=user_context,
        # Packet's original auth
        packet_requester_email=packet.get("requested_by_email"),
        packet_organization_id=packet.get("organization_id"),
        # Tool access
        mcp_executor=tool_executor,
        available_tools=expert_config.tools if expert_config else [],
        # User interaction
        user_input=user_input,
        # RAG access - pre-fetched context and on-demand provider
        rag_context=rag_context,
        context_message=context_message,
        rag_provider=rag_provider,
    )


async def _fetch_expert_rag_context(
    step_context: StepContext,
    packet: Dict[str, Any],
    expert_config: Any,
) -> List[str]:
    """Fetch context-aware RAG based on packet type and inputs.

    This pre-fetches relevant context that the expert will need,
    based on the packet type:
    - grid_analysis: Grid logbook, maintenance history, recent issues
    - kpi_report: KPI definitions, reporting standards
    - design_task: Design rules, component specs, approval requirements

    Args:
        step_context: The step context with RAG provider
        packet: Work packet data
        expert_config: Expert configuration

    Returns:
        List of relevant RAG chunks
    """
    if not step_context.rag_provider:
        return []

    packet_type = packet.get("packet_type", "")
    packet_inputs = packet.get("packet_inputs") or {}
    packet_goal = packet.get("packet_goal", "")
    results = []

    try:
        if packet_type == "grid_analysis":
            # Fetch grid-specific context
            grid_info = packet_inputs.get("grid", {})
            grid_name = grid_info.get("grid_name") or ""

            if grid_name:
                # Grid logbook and maintenance history
                grid_docs = await step_context.query_rag(
                    f"{grid_name} logbook maintenance history",
                    top_k=3,
                    filters={"source": "grid_logbook"},
                )
                results.extend(grid_docs)

                # Recent issues for this grid
                issue_docs = await step_context.query_rag(
                    f"{grid_name} issues problems faults",
                    top_k=2,
                )
                results.extend(issue_docs)

            # General troubleshooting guides based on analysis focus
            focus = packet_inputs.get("analysis_focus", "")
            if focus:
                focus_docs = await step_context.query_rag(
                    f"{focus} troubleshooting guide documentation",
                    top_k=2,
                )
                results.extend(focus_docs)

        elif packet_type == "kpi_report":
            # KPI definitions and reporting standards
            kpi_docs = await step_context.query_rag(
                "KPI definitions metrics reporting standards",
                top_k=3,
            )
            results.extend(kpi_docs)

            # Report templates
            template_docs = await step_context.query_rag(
                "report template format guidelines",
                top_k=2,
            )
            results.extend(template_docs)

        elif packet_type == "design_task":
            # Design rules and standards
            design_docs = await step_context.query_rag(
                "design rules standards specifications",
                top_k=3,
            )
            results.extend(design_docs)

            # Component specifications
            component_docs = await step_context.query_rag(
                "component specifications technical requirements",
                top_k=2,
            )
            results.extend(component_docs)

            # Approval requirements
            approval_docs = await step_context.query_rag(
                "approval process requirements sign-off",
                top_k=2,
            )
            results.extend(approval_docs)

        else:
            # Generic: Use packet goal to find relevant context
            if packet_goal:
                generic_docs = await step_context.query_rag(
                    packet_goal[:200],  # Truncate long goals
                    top_k=5,
                )
                results.extend(generic_docs)

    except Exception as e:
        LOGGER.warning(f"Error fetching RAG context for {packet_type}: {e}")

    # Deduplicate while preserving order
    seen = set()
    unique_results = []
    for doc in results:
        if doc not in seen:
            seen.add(doc)
            unique_results.append(doc)

    return unique_results


__all__ = ["expert_handler"]
