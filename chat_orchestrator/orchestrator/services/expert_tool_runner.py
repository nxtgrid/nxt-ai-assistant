"""Run expert workflows headlessly from persistent agents.

Fire-and-poll pattern: start_expert_workflow() creates a work packet and
launches execution in a background asyncio task. The calling agent sleeps
and wakes when the packet completes (via PG trigger on agent_events).
check_workflow_result() reads the packet status/outputs.

This module is the ONLY bridge between persistent agents and expert
workflows. Expert handlers must NOT import this module or anything from
orchestrator.graphs (enforced by architectural test).
"""

import asyncio
import logging
import os
from typing import Any, Callable, Dict, Optional

LOGGER = logging.getLogger(__name__)

# Only these experts can be invoked headlessly by agents
HEADLESS_ALLOWED_EXPERTS = {"lpp_expert", "gtr_expert"}

# Maximum call depth (agent -> expert). Experts cannot call other experts.
MAX_EXPERT_CALL_DEPTH = 1

# Maximum execution time for a single expert workflow
HEADLESS_TIMEOUT_SECONDS = 600  # 10 minutes

# Prevent GC of background workflow tasks (Python only keeps weak refs to tasks)
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


def make_input_resolver(prefilled_inputs: Dict[str, Any]) -> Callable[[str, str], Optional[str]]:
    """Create an input resolver callback from a dict of prefilled inputs.

    The resolver is called when a step needs user input. It checks the
    prefilled_inputs dict by step_name and returns the value if found.

    Args:
        prefilled_inputs: {step_name: response_string} for auto-resolving
                         interactive steps in headless mode.
    """

    def resolver(step_name: str, prompt: str) -> Optional[str]:
        value = prefilled_inputs.get(step_name)
        if value is not None:
            return str(value)
        return None

    return resolver


async def start_expert_workflow(
    expert_id: str,
    packet_type: str,
    inputs: Dict[str, Any],
    agent_instance_id: str,
    agent_thread_id: str,
    organization_id: int = int(os.getenv("STAFF_ORG_ID", "2")),
    user_email: str = "agent@system",
    call_depth: int = 0,
    prefilled_inputs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Start an expert workflow in the background. Returns immediately.

    Creates a work packet, launches the workflow executor in a background
    asyncio task, and returns the packet_id. The calling agent should save
    the packet_id in its metadata and sleep. A PG trigger on packet_status
    change will insert into agent_events to wake the agent.

    Args:
        expert_id: Expert to invoke (must be in HEADLESS_ALLOWED_EXPERTS)
        packet_type: Packet type matching expert's definition
        inputs: All required inputs for the workflow
        agent_instance_id: UUID of the calling agent instance
        agent_thread_id: Thread ID of the calling agent
        organization_id: Org context for tool permissions
        user_email: Email for tool context (from agent's user_context)
        prefilled_inputs: Optional dict for auto-resolving needs_input steps

    Returns:
        {"success": True, "packet_id": str, "status": "started"}
        or {"success": False, "error": str}
    """
    # Enforce call depth limit
    if call_depth >= MAX_EXPERT_CALL_DEPTH:
        return {
            "success": False,
            "error": f"Call depth {call_depth} exceeds maximum ({MAX_EXPERT_CALL_DEPTH}). "
            f"Experts cannot invoke other experts.",
        }

    # Validate expert is allowed
    if expert_id not in HEADLESS_ALLOWED_EXPERTS:
        return {
            "success": False,
            "error": f"Expert '{expert_id}' is not available for headless execution. "
            f"Allowed: {', '.join(sorted(HEADLESS_ALLOWED_EXPERTS))}",
        }

    try:
        from orchestrator.services.expert_instructions_provider import ExpertInstructionsProvider
        from orchestrator.services.work_packet_service import WorkPacketService

        # Load expert config
        provider = ExpertInstructionsProvider()
        expert_config = await provider.get_expert_config(expert_id)
        if not expert_config:
            return {"success": False, "error": f"Expert '{expert_id}' not found in definitions"}

        # Validate packet type
        workflow = expert_config.get_workflow(packet_type)
        if not workflow:
            available = list(expert_config.packet_types) if expert_config.packet_types else []
            return {
                "success": False,
                "error": f"Packet type '{packet_type}' not found for expert '{expert_id}'. "
                f"Available: {available}",
            }

        # Create work packet
        packet_service = WorkPacketService()
        packet = await packet_service.create_packet(
            packet_type=packet_type,
            packet_title=f"[Agent] {expert_id}: {packet_type}",
            packet_goal=f"Headless execution by agent {agent_thread_id}",
            assigned_expert=expert_id,
            packet_inputs=inputs,
            session_id=agent_thread_id,
            organization_id=organization_id,
            requested_by_email=user_email,
        )

        packet_id = packet["packet_id"]

        # Write agent tracking fields to packet state so the PG trigger
        # can wake the agent on completion (see 20260317_expert_completion_trigger.sql)
        await packet_service.update_state(
            packet_id,
            {
                "invoked_by_agent": True,
                "agent_instance_id": agent_instance_id,
                "agent_thread_id": agent_thread_id,
            },
            session_id=agent_thread_id,
        )

        LOGGER.info(
            f"Created headless work packet {packet_id} for expert {expert_id} "
            f"(agent: {agent_thread_id})"
        )

        # Launch background execution (retain reference to prevent GC)
        task = asyncio.create_task(
            _execute_headless(
                expert_config=expert_config,
                packet=packet,
                packet_type=packet_type,
                inputs=inputs,
                organization_id=organization_id,
                user_email=user_email,
                prefilled_inputs=prefilled_inputs or {},
                agent_instance_id=agent_instance_id,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        return {
            "success": True,
            "packet_id": packet_id,
            "status": "started",
            "message": f"Expert workflow '{expert_id}/{packet_type}' started. "
            f"Use check_workflow_result(packet_id='{packet_id}') to check status.",
        }

    except Exception as e:
        LOGGER.exception(f"Failed to start expert workflow: {e}")
        return {"success": False, "error": str(e)}


async def _execute_headless(
    expert_config: Any,
    packet: Dict[str, Any],
    packet_type: str,
    inputs: Dict[str, Any],
    organization_id: int,
    user_email: str,
    prefilled_inputs: Dict[str, Any],
    agent_instance_id: str,
) -> None:
    """Background task: execute workflow headlessly with timeout."""
    from orchestrator.clients.factory import create_chat_llm_client
    from orchestrator.config.settings import GeminiModelConfig, get_settings
    from orchestrator.experts.step_context import StepContext
    from orchestrator.experts.workflow_executor import WorkflowExecutor
    from orchestrator.services.tool_executor import ToolExecutor
    from orchestrator.services.tool_registry import ToolRegistry
    from orchestrator.services.work_packet_service import WorkPacketService

    packet_id = packet["packet_id"]

    try:
        settings = get_settings()
        packet_service = WorkPacketService()

        # Build headless StepContext (user_context=None suppresses Telegram)
        context = StepContext(
            packet_id=packet_id,
            packet_type=packet_type,
            packet_goal=packet.get("packet_goal", ""),
            packet_inputs=inputs,
            packet_state=packet.get("packet_state", {}),
            current_step="",
            steps_completed=[],
            session_id=packet.get("requested_in_session", ""),
            user_email=user_email,
            organization_id=organization_id,
            user_context=None,  # Suppresses Telegram messages
            call_depth=1,  # Agent-invoked
        )

        # Build MCP executor with agent's permissions
        registry = ToolRegistry()
        mcp_executor = ToolExecutor(registry=registry, settings=settings)
        context.mcp_executor = mcp_executor

        # Build Gemini client for LLM steps
        model_config = GeminiModelConfig()
        gemini_client = create_chat_llm_client(settings, model_config)

        # Build input resolver for needs_input steps
        input_resolver = make_input_resolver(prefilled_inputs)

        # Create executor with resolver
        executor = WorkflowExecutor(
            gemini_client=gemini_client,
            packet_service=packet_service,
            mcp_executor=mcp_executor,
            input_resolver=input_resolver,
        )

        # Execute with timeout
        result_text, result_data = await asyncio.wait_for(
            executor.execute_workflow(
                expert_config=expert_config,
                packet=packet,
                context=context,
            ),
            timeout=HEADLESS_TIMEOUT_SECONDS,
        )

        # Mark completed
        if result_data.get("error"):
            await packet_service.fail_packet(
                packet_id,
                error_message=result_data["error"],
                session_id=packet.get("requested_in_session", ""),
            )
            LOGGER.warning(f"Headless workflow {packet_id} failed: {result_data['error']}")
        elif result_data.get("needs_user_input"):
            await packet_service.fail_packet(
                packet_id,
                error_message=f"Workflow requires user input that was not prefilled: {result_text}",
                session_id=packet.get("requested_in_session", ""),
            )
            LOGGER.warning(f"Headless workflow {packet_id} needs unprefilled input")
        else:
            await packet_service.complete_packet(
                packet_id,
                outputs=result_data,
                session_id=packet.get("requested_in_session", ""),
            )
            LOGGER.info(f"Headless workflow {packet_id} completed successfully")

    except asyncio.TimeoutError:
        LOGGER.error(f"Headless workflow {packet_id} timed out after {HEADLESS_TIMEOUT_SECONDS}s")
        try:
            packet_service = WorkPacketService()
            await packet_service.fail_packet(
                packet_id,
                error_message=f"Timed out after {HEADLESS_TIMEOUT_SECONDS} seconds",
                session_id=packet.get("requested_in_session", ""),
            )
        except Exception:
            pass
    except Exception as e:
        LOGGER.exception(f"Headless workflow {packet_id} crashed: {e}")
        try:
            packet_service = WorkPacketService()
            await packet_service.fail_packet(
                packet_id,
                error_message=str(e),
                session_id=packet.get("requested_in_session", ""),
            )
        except Exception:
            pass


async def check_workflow_result(packet_id: str) -> Dict[str, Any]:
    """Check the status and result of a previously started expert workflow.

    Returns the packet status, outputs (if completed), or error (if failed).
    """
    try:
        from orchestrator.services.work_packet_service import WorkPacketService

        packet_service = WorkPacketService()
        packet = await packet_service.get_packet(packet_id)

        if not packet:
            return {"success": False, "error": f"Packet '{packet_id}' not found"}

        status = packet.get("packet_status", "unknown")
        result: Dict[str, Any] = {
            "success": True,
            "packet_id": packet_id,
            "status": status,
            "expert_id": packet.get("assigned_expert", ""),
            "packet_type": packet.get("packet_type", ""),
        }

        if status == "completed":
            result["outputs"] = packet.get("packet_outputs", {})
            result["completed_at"] = packet.get("completed_at")
        elif status == "failed":
            # Check packet_state for error details
            state = packet.get("packet_state", {})
            result["error"] = state.get("error") or packet.get("error_message") or "Unknown error"
        elif status in ("pending", "in_progress"):
            result["current_step"] = packet.get("current_step", "")
            result["steps_completed"] = packet.get("steps_completed", [])
        elif status == "awaiting_input":
            result["error"] = "Workflow is waiting for user input (not available in headless mode)"

        return result

    except Exception as e:
        LOGGER.exception(f"Failed to check workflow result: {e}")
        return {"success": False, "error": str(e)}
