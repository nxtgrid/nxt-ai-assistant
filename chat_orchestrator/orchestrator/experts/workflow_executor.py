"""Executes expert workflows with hybrid LLM/function steps.

Workflows are defined in Google Docs and can contain two types of steps:
1. [llm] steps - Reasoning and synthesis by Gemini
2. [function:handler_name] steps - Hard logic executed by registered handlers

Workflow Format in Google Doc:
    ### Workflow
    1. [llm] understand_request - Parse user intent and identify grid
    2. [function:fetch_month_metrics] - Get last 30 days from Grafana
    3. [function:analyze_failures_loop] - Analyze each alert
    4. [llm] synthesize_findings - Combine all data into findings
    5. [function:create_analysis_doc] - Generate Google Doc report
    6. [llm] prepare_response - Format user-facing summary

Usage:
    executor = WorkflowExecutor(
        gemini_client=gemini,
        packet_service=packet_service,
        mcp_executor=tool_executor,
    )

    response, state = await executor.execute_workflow(
        expert_config=config,
        packet=packet,
        context=step_context,
    )
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

# Import handlers module to trigger @register_step decorators
# Without this import, Python's import system skips __init__.py when importing
# directly from step_registry, leaving handlers unregistered
import orchestrator.experts.handlers  # noqa: F401
from orchestrator.experts.parameter_confirmation import (
    ConfirmationAction,
    format_confirmation_prompt,
    format_param_edit_prompt,
    format_value_change_confirmation,
    parse_confirmation_response,
)
from orchestrator.experts.parameter_resolver import (
    PacketParameterSchema,
    ResolvedParameter,
    get_parameter_resolver,
)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import (
    get_step_contract,
    get_step_handler,
    get_step_registry,
)
from orchestrator.mini_app.schemas import build_mini_app_url, build_view_state_url
from shared.grid_design.artifact_log import sweep_state_for_artifacts
from shared.grid_design.db import Repository
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.langfuse_utils import langfuse_observe, update_trace
from shared.utils.logging import get_logger
from shared.utils.telegram_buttons import (
    build_multi_webapp_keyboard,
    build_step_input_keyboard,
    build_webapp_keyboard,
    is_inline_buttons_enabled,
    parse_numbered_options,
)

if TYPE_CHECKING:
    from orchestrator.clients.gemini import GeminiClient
    from orchestrator.services.expert_instructions_provider import ExpertConfig
    from orchestrator.services.work_packet_service import WorkPacketService

LOGGER = get_logger(__name__)

# Mirrors shared.grid_design.artifact_log._DRIVE_ID_SUFFIX -- a packet_state key
# ending in this suffix is a Drive file ID whose "existence" can also be
# satisfied by a non-empty gd_designs.artifacts[artifact_type] entry (see
# validate_step_prerequisites's Tier 3 check).
_DRIVE_ID_SUFFIX = "_drive_id"


class StepStatus(Enum):
    """Status of a workflow step execution."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepExecutionRecord:
    """Record of a single step's execution."""

    step_name: str
    step_type: str  # "llm" or "function"
    description: str
    status: StepStatus = StepStatus.PENDING
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    result_summary: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "step_name": self.step_name,
            "step_type": self.step_type,
            "description": self.description,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "result_summary": self.result_summary,
        }


@dataclass
class ExecutionSummary:
    """Summary of workflow execution for logging and LLM context."""

    packet_id: str
    packet_type: str
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0
    step_records: List[StepExecutionRecord] = field(default_factory=list)
    final_status: str = "in_progress"  # "completed", "failed", "paused"
    failure_reason: Optional[str] = None
    all_steps: Optional[List["ParsedStep"]] = field(default=None, repr=False)

    def add_record(self, record: StepExecutionRecord) -> None:
        """Add a step execution record."""
        self.step_records.append(record)
        if record.status == StepStatus.SUCCESS:
            self.completed_steps += 1
        elif record.status == StepStatus.FAILED:
            self.failed_steps += 1
        elif record.status == StepStatus.SKIPPED:
            self.skipped_steps += 1

    def to_llm_context(self) -> str:
        """Format summary for LLM prompt context."""
        lines = [
            "## Workflow Execution Summary",
            f"- Packet: {self.packet_id} ({self.packet_type})",
            f"- Status: {self.final_status}",
            f"- Steps: {self.completed_steps}/{self.total_steps} completed",
        ]

        if self.failed_steps > 0:
            lines.append(f"- Failed: {self.failed_steps} step(s)")

        if self.failure_reason:
            lines.append(f"- Failure reason: {self.failure_reason}")

        lines.append("\n### Step Details:")
        for record in self.step_records:
            status_icon = {
                StepStatus.SUCCESS: "✓",
                StepStatus.FAILED: "✗",
                StepStatus.SKIPPED: "○",
                StepStatus.PENDING: "·",
                StepStatus.IN_PROGRESS: "▶",
            }.get(record.status, "?")

            line = f"{status_icon} [{record.step_type}] {record.step_name}"
            if record.duration_ms:
                line += f" ({record.duration_ms}ms)"
            if record.error:
                line += f" - ERROR: {record.error}"
            elif record.result_summary:
                line += f" - {record.result_summary}"
            lines.append(line)

        return "\n".join(lines)

    def to_dict(self, all_steps: Optional[List["ParsedStep"]] = None) -> Dict[str, Any]:
        """Convert to dictionary for storage.

        Args:
            all_steps: If provided, overrides self.all_steps for this call.
                Pending records are appended for steps not yet in step_records.
        """
        records = list(self.step_records)
        effective_steps = all_steps if all_steps is not None else self.all_steps
        if effective_steps:
            recorded_names = {r.step_name for r in records}
            for step in effective_steps:
                if step.name not in recorded_names:
                    records.append(
                        StepExecutionRecord(
                            step_name=step.name,
                            step_type=step.step_type,
                            description=step.description,
                            status=StepStatus.PENDING,
                        )
                    )
        return {
            "packet_id": self.packet_id,
            "packet_type": self.packet_type,
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps,
            "skipped_steps": self.skipped_steps,
            "final_status": self.final_status,
            "failure_reason": self.failure_reason,
            "steps": [r.to_dict() for r in records],
        }


@dataclass
class ParsedStep:
    """A parsed workflow step."""

    index: int
    step_type: str  # "llm" or "function"
    name: str  # Step name or function name
    description: str
    serial: bool = False  # If True, run one site at a time in multi-site mode


@dataclass
class StepLoopSignal:
    """What the caller of _execute_one_step should do next in the while-loop."""

    action: str  # "advance" | "retry" | "return" | "break"
    # populated only when action == "return". The response text is Optional[str]
    # (not str) because the redirect_to_main_llm path returns None for it -- this
    # mirrors a pre-existing mypy debt in _execute_workflow_inner's own signature
    # (declared -> Tuple[str, Dict[str, Any]] despite that same None case).
    return_value: Optional[Tuple[Optional[str], Dict[str, Any]]] = None
    final_response: Optional[str] = None  # populated only by the LLM-step success path


@dataclass
class PrereqReport:
    """Read-only report: what does step_name need that isn't currently available?

    Produced by `WorkflowExecutor.validate_step_prerequisites` -- a pure
    reporting method (no DB writes, no packet_state mutation) that answers
    "if I ran this step right now, what's missing?" so a later task (out-of-
    order step execution) can decide whether to auto-run a producer step
    first, ask the user, or refuse.
    """

    step_name: str
    satisfied: bool  # True iff missing_state/missing_results/missing_params are all empty
    missing_state: Tuple[str, ...] = ()
    missing_results: Tuple[str, ...] = ()
    missing_params: Tuple[str, ...] = ()
    # Informational only: `optional_consumes_state` keys (see step_contracts.py)
    # that aren't currently available via any tier. This never affects
    # `satisfied` and is never searched for a producer_chain entry -- these are
    # opportunistic reads with in-handler fallback logic (not real
    # prerequisites), so there's nothing for run_single_step to auto-produce.
    missing_optional_state: Tuple[str, ...] = ()
    # For each missing item (state key or result step name) that SOME other
    # registered step's contract claims to produce, maps it to the step
    # name(s) that could produce it. Missing items with no known producer are
    # absent from this dict (not mapped to an empty tuple) -- callers should
    # treat "not in producer_chain" as "no known way to satisfy this
    # automatically".
    producer_chain: Dict[str, Tuple[str, ...]] = field(default_factory=dict)


class WorkflowExecutor:
    """Executes expert workflows with hybrid LLM/function steps.

    Handles:
    - Parsing workflow definitions from Google Doc format
    - Executing function steps via registered handlers
    - Executing LLM steps via Gemini with full context
    - Progress tracking and state updates
    - User input pauses and resumption
    """

    def __init__(
        self,
        gemini_client: "GeminiClient",
        packet_service: "WorkPacketService",
        mcp_executor: Any,
        input_resolver: Optional[Callable[[str, str], Optional[str]]] = None,
    ):
        """Initialize the workflow executor.

        Args:
            gemini_client: Gemini client for LLM steps
            packet_service: Service for packet state updates
            mcp_executor: Tool executor for MCP calls
            input_resolver: Optional callback for headless execution.
                When a step calls needs_input(), the resolver is called with
                (step_name, prompt). Return a string to auto-resolve, or None
                to fail the step. When None (default), needs_input pauses
                for user interaction as before.
        """
        self.gemini = gemini_client
        self.packet_service = packet_service
        self.mcp_executor = mcp_executor
        self._input_resolver = input_resolver
        # Tracks the step name currently executing — used by CancelledError handler
        # to record which step was interrupted.
        self._current_step_name: Optional[str] = None
        self._current_packet_id: Optional[str] = None
        self._current_session_id: Optional[str] = None

    def parse_workflow(self, workflow_lines: List[str]) -> List[ParsedStep]:
        """Parse workflow lines into structured steps.

        Formats supported:
        - "1. [llm] step_name - description"
        - "2. [function:handler_name] - description"
        - "3. step_name - description" (defaults to LLM)
        - "[llm] step_name" (no numbering)
        - "[function:name]" (no numbering)

        Args:
            workflow_lines: Lines from the workflow section

        Returns:
            List of ParsedStep objects
        """
        steps: List[ParsedStep] = []

        for i, line in enumerate(workflow_lines):
            line = line.strip()
            if not line:
                continue

            step = self._parse_step_line(line, i)
            if step:
                steps.append(step)

        return steps

    def _parse_step_line(self, line: str, index: int) -> Optional[ParsedStep]:
        """Parse a single workflow step line.

        Args:
            line: Raw line from workflow
            index: Line index for step ordering

        Returns:
            ParsedStep or None if line is not a valid step
        """
        # Detect and strip [serial] tag before other parsing
        serial = "[serial]" in line.lower()
        if serial:
            line = re.sub(r"\[serial\]", "", line, flags=re.IGNORECASE).strip()

        # Remove leading numbers: "1. ..." or "1) ..."
        if line and line[0].isdigit():
            parts = line.split(".", 1)
            if len(parts) > 1:
                line = parts[1].strip()
            else:
                parts = line.split(")", 1)
                if len(parts) > 1:
                    line = parts[1].strip()

        # Check for [llm] prefix
        if "[llm]" in line:
            step_type = "llm"
            after_type = line.split("[llm]", 1)[-1].strip()
            parts = after_type.split(" - ", 1)
            name = parts[0].strip()
            description = parts[1].strip() if len(parts) > 1 else name

        # Check for [function:name] prefix
        elif "[function:" in line:
            step_type = "function"
            start = line.index("[function:") + len("[function:")
            end = line.index("]", start)
            name = line[start:end].strip()
            description = line[end + 1 :].strip().lstrip("- ").strip()
            if not description:
                description = name

        # Default to LLM step
        else:
            step_type = "llm"
            parts = line.split(" - ", 1)
            name = parts[0].strip()
            description = parts[1].strip() if len(parts) > 1 else name

        # Skip empty names
        if not name:
            return None

        return ParsedStep(
            index=index,
            step_type=step_type,
            name=name,
            description=description,
            serial=serial,
        )

    @langfuse_observe(name="expert-workflow")
    async def execute_workflow(
        self,
        expert_config: "ExpertConfig",
        packet: Dict[str, Any],
        context: StepContext,
        on_progress: Optional[Callable[[str], Any]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Execute all remaining steps in workflow.

        Args:
            expert_config: Expert configuration with workflow definition
            packet: Current packet data
            context: Step execution context
            on_progress: Optional callback for progress updates

        Returns:
            Tuple of (final_response_text, state_updates)
        """
        # Track packet/session for CancelledError handler (SIGTERM graceful shutdown)
        self._current_packet_id = packet.get("packet_id")
        self._current_session_id = context.session_id
        try:
            result: Tuple[str, Dict[str, Any]] = await self._execute_workflow_inner(
                expert_config, packet, context, on_progress
            )
            return result
        except asyncio.CancelledError:
            # Process is shutting down via SIGTERM. Write interrupted state before dying.
            # asyncio.shield protects the DB write from a second cancel() while it's
            # awaiting — ensuring the state is written even if cancel() fires again.
            await self._mark_interrupted()
            raise  # Always re-raise CancelledError

    async def _mark_interrupted(self) -> None:
        """Best-effort: write auto_resumable=True state. Swallow all errors — process is dying."""
        try:
            await asyncio.wait_for(
                self.packet_service.interrupt_packet(
                    packet_id=self._current_packet_id,
                    interrupted_step=self._current_step_name,
                    session_id=self._current_session_id,
                ),
                timeout=5.0,
            )
        except Exception:
            pass  # Best-effort during shutdown

    @langfuse_observe(name="expert-workflow-inner")
    async def _execute_workflow_inner(
        self,
        expert_config: "ExpertConfig",
        packet: Dict[str, Any],
        context: StepContext,
        on_progress: Optional[Callable[[str], Any]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Inner workflow execution — called by execute_workflow which handles CancelledError."""
        update_trace(
            metadata={
                "packet_type": packet.get("packet_type"),
                "packet_id": packet.get("packet_id"),
            },
        )
        packet_type = packet["packet_type"]
        workflow_lines = expert_config.get_workflow(packet_type)
        steps = self.parse_workflow(workflow_lines)

        # Extract chat_id for button type selection (web_app only in private chats)
        _wf_chat_id = (
            getattr(context.user_context, "chat_id", None) if context.user_context else None
        )

        if not steps:
            LOGGER.warning(f"No workflow defined for {packet_type}")
            steps = [ParsedStep(0, "llm", "execute", "Execute the task based on user request")]

        if packet_type == "light_preliminary_package":
            geo_source = (
                "community"
                if (context.get_state("geo_source") == "community" or context.get_input("latitude"))
                else None
            )
            if geo_source == "community" and context.get_state("geo_source") != "community":
                context.packet_state["geo_source"] = "community"
            steps = self._inject_lpp_entry_steps(steps, geo_source)

        # Initialize execution summary for tracking
        execution_summary = ExecutionSummary(
            packet_id=packet["packet_id"],
            packet_type=packet_type,
            total_steps=len(steps),
            all_steps=steps,
        )

        # Find current step index (resume from where we left off)
        completed = set(packet.get("steps_completed", []) or [])
        current_idx = 0
        for i, step in enumerate(steps):
            if step.name not in completed:
                current_idx = i
                break
            else:
                # Mark already completed steps in summary
                record = StepExecutionRecord(
                    step_name=step.name,
                    step_type=step.step_type,
                    description=step.description,
                    status=StepStatus.SUCCESS,
                    result_summary="(completed previously)",
                )
                execution_summary.add_record(record)
        else:
            # All steps already completed - packet was resumed after finishing
            LOGGER.info(
                f"All {len(steps)} steps already completed for packet {packet.get('packet_id')}. "
                "Building summary from previous results."
            )

            # Build summary from accumulated results
            accumulated_results = context.accumulated_results.copy()

            # Mark packet as completed if not already
            try:
                outputs = {
                    "summary": f"Workflow completed previously with {len(steps)} steps.",
                    "steps_executed": [s.name for s in steps],
                }
                await self.packet_service.complete_packet(
                    packet["packet_id"],
                    outputs=outputs,
                    session_id=context.session_id,
                )
            except Exception as e:
                LOGGER.warning(f"Could not mark packet as completed: {e}")

            # Return a user-friendly summary
            summary_parts = [f"✅ This workflow already completed {len(steps)} steps:"]
            for step in steps:
                summary_parts.append(f"  • {step.description}")

            # Check for document URL in accumulated results
            doc_url = None
            for key, value in accumulated_results.items():
                if isinstance(value, dict):
                    doc_url = value.get("document_url") or value.get("external_url")
                    if doc_url:
                        break

            if doc_url:
                summary_parts.append(f"\n📄 Document: {doc_url}")

            return "\n".join(summary_parts), {
                "accumulated_results": accumulated_results,
                "already_completed": True,
            }

        accumulated_results = context.accumulated_results.copy()
        final_response = ""

        # Execute remaining steps
        # Outer while loop allows re-executing a step when a step signals
        # action="retry" (headless input-resolver retry). Kept as a
        # first-class action so a future run_single_step entry point can
        # reuse the same signal contract -- see _execute_one_step for a note
        # on why the one original code path that looked like a retry
        # actually behaves as action="break" instead.
        _step_idx = current_idx
        while _step_idx < len(steps):
            step = steps[_step_idx]
            signal = await self._execute_one_step(
                step,
                steps,
                expert_config,
                packet,
                context,
                accumulated_results,
                execution_summary,
                on_progress,
                _wf_chat_id,
            )
            if signal.final_response is not None:
                final_response = signal.final_response
            if signal.action == "return":
                return signal.return_value
            if signal.action == "break":
                break
            if signal.action == "retry":
                continue  # Re-run while loop with same _step_idx

            # Advance to next step
            _step_idx += 1

            # Mark step complete
            next_step = steps[step.index + 1].name if step.index + 1 < len(steps) else None
            await self.packet_service.complete_step(
                packet["packet_id"],
                step.name,
                next_step=next_step,
                session_id=context.session_id,
            )

        # Log successful execution summary
        execution_summary.final_status = "completed"
        packet_uuid = packet.get("id") or packet.get("packet_id")
        if packet_uuid:
            await self._log_execution_summary(packet_uuid, execution_summary, context.session_id)

        # If workflow ended with a function step (no LLM response), auto-generate summary
        if not final_response or not final_response.strip():
            final_response = self._build_completion_summary(
                packet_type=packet_type,
                accumulated_results=accumulated_results,
                execution_summary=execution_summary,
                context=context,
            )

        return final_response, {
            "accumulated_results": accumulated_results,
            "execution_summary": execution_summary.to_dict(),
        }

    async def _execute_one_step(
        self,
        step: ParsedStep,
        steps: List[ParsedStep],
        expert_config: "ExpertConfig",
        packet: Dict[str, Any],
        context: StepContext,
        accumulated_results: Dict[str, Any],
        execution_summary: ExecutionSummary,
        on_progress: Optional[Callable[[str], Any]],
        wf_chat_id: Any,
        allow_multi_site_handoff: bool = True,
    ) -> StepLoopSignal:
        """Execute exactly one workflow step (function or LLM). Extracted from the
        single-site loop in _execute_workflow_inner so the same per-step logic can
        later be invoked out of order by run_single_step (a follow-up task).

        Mutates accumulated_results and execution_summary in place (same objects
        the caller passed in) -- this preserves the exact mutation semantics the
        original inline loop body had, since both are mutable and were read/written
        by reference throughout the original code.

        Args:
            allow_multi_site_handoff: If True (the default -- preserves the
                original behavior for _execute_workflow_inner's call site),
                a successful "resolve_sites" step that discovers more than one
                site hands off the rest of `steps` to
                `_execute_multi_site_steps`. If False (used by
                `run_single_step`, which is explicitly single-site-scoped),
                that hand-off is refused instead of executed -- see the
                "resolve_sites" branch below.
        """
        self._current_step_name = step.name
        LOGGER.info(f"Executing step {step.index + 1}/{len(steps)}: [{step.step_type}] {step.name}")

        # Create execution record for this step
        step_record = StepExecutionRecord(
            step_name=step.name,
            step_type=step.step_type,
            description=step.description,
            status=StepStatus.IN_PROGRESS,
            started_at=time.time(),
        )

        if on_progress:
            try:
                await on_progress(f"Step {step.index + 1}/{len(steps)}: {step.description}")
            except Exception:
                pass  # Don't fail workflow on progress callback errors

        # Persist execution_summary with the current step marked as
        # in_progress BEFORE executing it.  This way, if the step
        # handler calls send_progress_to_user() (which attaches a
        # View State button), the mini app already has step data to
        # display — otherwise the first View State open shows nothing.
        execution_summary.add_record(step_record)
        await self.packet_service.update_state(
            packet["packet_id"],
            {"execution_summary": execution_summary.to_dict()},
            context.session_id,
        )

        step_final_response: Optional[str] = None

        if step.step_type == "function":
            # Check for parameter confirmation before function steps
            # Shows current state values, allows user to override any of them
            confirmation_result = await self._handle_function_step_confirmation(
                step, context, len(steps), packet, accumulated_results
            )
            if confirmation_result is not None:
                # Need to pause for user confirmation (record already in summary)
                step_record.status = StepStatus.PENDING
                step_record.result_summary = "awaiting parameter confirmation"

                state_to_persist = {
                    **(confirmation_result.state_updates or {}),
                    "accumulated_results": accumulated_results,
                    "execution_summary": execution_summary.to_dict(),
                }
                await self.packet_service.update_state(
                    packet["packet_id"],
                    state_to_persist,
                    context.session_id,
                )
                if confirmation_result.state_updates:
                    context.packet_state.update(confirmation_result.state_updates)

                await self.packet_service.set_awaiting_input(
                    packet["packet_id"],
                    confirmation_result.user_prompt or "Please confirm parameters",
                    context.session_id,
                )

                # Attach Mini App form button if configured
                reply_markup = None
                form_url = None
                if confirmation_result.mini_app_form:
                    form_url = build_mini_app_url(
                        packet["packet_id"], confirmation_result.mini_app_form
                    )
                state_url = build_view_state_url(packet["packet_id"])

                if form_url and state_url:
                    reply_markup = build_multi_webapp_keyboard(
                        [("Edit Parameters", form_url), ("View State", state_url)],
                        chat_id=wf_chat_id,
                    )
                elif form_url:
                    reply_markup = build_webapp_keyboard(
                        "Edit Parameters", form_url, chat_id=wf_chat_id
                    )
                elif state_url:
                    reply_markup = build_webapp_keyboard(
                        "View State", state_url, chat_id=wf_chat_id
                    )

                return StepLoopSignal(
                    action="return",
                    return_value=(
                        confirmation_result.user_prompt or "Please confirm parameters",
                        {
                            "needs_user_input": True,
                            "awaiting_param_confirmation": True,
                            "reply_markup": reply_markup,
                        },
                    ),
                )

            # Persist confirmation state (snapshot, cleared flags) to DB
            # so it survives workflow resume across pause/resume cycles.
            confirmation_state = {}
            for key in (
                "confirmed_editable_snapshot",
                "awaiting_param_confirmation",
                "param_confirmation_context",
                "auto_continue_enabled",
            ):
                if key in context.packet_state:
                    confirmation_state[key] = context.packet_state[key]
            if confirmation_state:
                await self.packet_service.update_state(
                    packet["packet_id"],
                    confirmation_state,
                    context.session_id,
                )

            # Execute function handler (with heartbeat to prevent stale-packet timeout)
            result = await self._run_with_heartbeat(
                self._execute_function_step(step, context, accumulated_results),
                packet["packet_id"],
                context.session_id,
            )

            # Clear any confirmation state after successful execution
            if context.get_state("awaiting_param_confirmation"):
                context.packet_state["awaiting_param_confirmation"] = False
                context.clear_parameter_overrides()

            # Record timing
            step_record.ended_at = time.time()
            step_record.duration_ms = int(
                (step_record.ended_at - (step_record.started_at or 0)) * 1000
            )

            if result.error:
                step_record.status = StepStatus.FAILED
                step_record.error = result.error
                execution_summary.failed_steps += 1
                execution_summary.final_status = "failed"
                execution_summary.failure_reason = f"Step '{step.name}' failed: {result.error}"

                # Mark remaining steps as skipped
                for remaining_step in steps[step.index + 1 :]:
                    skipped_record = StepExecutionRecord(
                        step_name=remaining_step.name,
                        step_type=remaining_step.step_type,
                        description=remaining_step.description,
                        status=StepStatus.SKIPPED,
                        result_summary="skipped due to earlier failure",
                    )
                    execution_summary.add_record(skipped_record)

                LOGGER.error(f"Step {step.name} failed: {result.error}")

                # Log detailed execution summary to database
                packet_uuid = packet.get("id") or packet.get("packet_id")
                if packet_uuid:
                    await self._log_execution_summary(
                        packet_uuid, execution_summary, context.session_id
                    )

                await self.packet_service.fail_packet(
                    packet["packet_id"],
                    f"Step {step.name} failed: {result.error}",
                    context.session_id,
                    error_state={
                        "last_error": result.error,
                        "error_step": step.name,
                        "execution_summary": execution_summary.to_dict(),
                    },
                )

                # Build user-friendly message with context about what happened
                user_message = self._build_failure_message(execution_summary)
                return StepLoopSignal(
                    action="return",
                    return_value=(
                        user_message,
                        {
                            "error": result.error,
                            "execution_summary": execution_summary.to_dict(),
                        },
                    ),
                )

            if result.needs_user_input:
                # Headless mode: try to resolve input via callback then re-run step
                if self._input_resolver:
                    resolved = self._input_resolver(step.name, result.user_prompt or "")
                    if resolved is not None:
                        # Guard against infinite re-execution of the same step
                        _headless_retries = getattr(self, "_headless_step_retries", {})
                        _headless_retries[step.name] = _headless_retries.get(step.name, 0) + 1
                        self._headless_step_retries = _headless_retries
                        if _headless_retries[step.name] > 3:
                            step_record.status = StepStatus.FAILED
                            step_record.error = (
                                f"Headless: step {step.name} still requesting input "
                                f"after 3 resolve attempts"
                            )
                            execution_summary.final_status = "failed"
                            LOGGER.error(f"Headless step {step.name} stuck in input loop, aborting")
                            return StepLoopSignal(
                                action="return",
                                return_value=(
                                    f"Step {step.name} stuck requesting input",
                                    {
                                        "error": f"Headless: input loop in step {step.name}",
                                        "execution_summary": execution_summary.to_dict(),
                                    },
                                ),
                            )

                        LOGGER.info(
                            f"Input resolver provided response for step "
                            f"{step.name} (attempt {_headless_retries[step.name]}): "
                            f"{resolved[:50]}..."
                        )
                        context.user_input = resolved
                        # Re-execute THIS step by jumping back to step execution.
                        # We use a flag + break to exit this result-handling block,
                        # then the outer _should_retry_step check re-runs the step.
                        #
                        # NOTE (verified during Phase C Task 3a extraction): the
                        # original `break` here has no enclosing for/while other
                        # than the single-site step-execution while loop, so it
                        # exits that loop directly -- it never reaches the
                        # `if _retry_current_step: continue` check that lived
                        # further down in the same loop body (that check was
                        # unreachable dead code in the original, since a break
                        # always targets the nearest enclosing loop). In other
                        # words, this path does not actually retry the step; it
                        # ends step processing early, same as the
                        # `skip_remaining` break below. Translated 1:1 as
                        # action="break" to preserve that exact (pre-existing,
                        # untested) behavior.
                        return StepLoopSignal(action="break")
                    else:
                        # No prefilled input — fail gracefully
                        step_record.status = StepStatus.FAILED
                        step_record.error = f"Headless: no input for step {step.name}"
                        execution_summary.final_status = "failed"
                        LOGGER.warning(f"Headless execution failed: no input for step {step.name}")
                        return StepLoopSignal(
                            action="return",
                            return_value=(
                                f"Missing required input for step: {step.name}",
                                {
                                    "error": f"Headless: no prefilled input for step {step.name}",
                                    "execution_summary": execution_summary.to_dict(),
                                },
                            ),
                        )

                step_record.status = StepStatus.PENDING
                step_record.result_summary = "awaiting user input"
                execution_summary.final_status = "paused"

                # Apply state updates BEFORE pausing (so they're available on resume)
                # Always persist accumulated_results so step data survives resume
                pause_state = dict(result.state_updates) if result.state_updates else {}
                pause_state["accumulated_results"] = accumulated_results
                pause_state["execution_summary"] = execution_summary.to_dict()
                await self.packet_service.update_state(
                    packet["packet_id"],
                    pause_state,
                    context.session_id,
                )
                if result.state_updates:
                    # Also update context in memory for consistency
                    context.packet_state.update(result.state_updates)

                # Build inline keyboard for Telegram buttons
                reply_markup = None
                form_url = None
                if result.mini_app_form:
                    # Mini App popup form button
                    form_url = build_mini_app_url(packet["packet_id"], result.mini_app_form)
                state_url = build_view_state_url(packet["packet_id"])

                if form_url and state_url:
                    reply_markup = build_multi_webapp_keyboard(
                        [("Edit Parameters", form_url), ("View State", state_url)],
                        chat_id=wf_chat_id,
                    )
                elif form_url:
                    reply_markup = build_webapp_keyboard(
                        "Edit Parameters", form_url, chat_id=wf_chat_id
                    )
                elif is_inline_buttons_enabled():
                    if result.inline_options:
                        reply_markup = build_step_input_keyboard(result.inline_options)
                    else:
                        # Auto-detect numbered options from user_prompt
                        detected = parse_numbered_options(result.user_prompt or "")
                        if detected:
                            reply_markup = build_step_input_keyboard(detected)
                elif state_url:
                    reply_markup = build_webapp_keyboard(
                        "View State", state_url, chat_id=wf_chat_id
                    )

                # Pause workflow, wait for user input
                await self.packet_service.set_awaiting_input(
                    packet["packet_id"],
                    result.user_prompt or "Please provide more information",
                    context.session_id,
                )
                return StepLoopSignal(
                    action="return",
                    return_value=(
                        result.user_prompt or "Please provide more information",
                        {
                            "needs_user_input": True,
                            "execution_summary": execution_summary.to_dict(),
                            "reply_markup": reply_markup,
                        },
                    ),
                )

            if result.redirect_to_main_llm:
                # User input doesn't belong to this step - pause and redirect
                step_record.status = StepStatus.PENDING
                step_record.result_summary = "redirecting to main LLM"
                execution_summary.final_status = "paused"

                LOGGER.info(
                    f"Step {step.name} detected unrelated input, "
                    f"redirecting to main LLM: {result.progress_message}"
                )

                # Keep workflow paused but signal redirect
                await self.packet_service.set_awaiting_input(
                    packet["packet_id"],
                    "Workflow paused - processing new request",
                    context.session_id,
                )
                return StepLoopSignal(
                    action="return",
                    return_value=(
                        None,
                        {
                            "redirect_to_main_llm": True,
                            "redirect_reason": result.progress_message,
                            "execution_summary": execution_summary.to_dict(),
                        },
                    ),
                )

            # Success - update the already-added record in place
            step_record.status = StepStatus.SUCCESS
            step_record.result_summary = self._summarize_result(result.data)
            execution_summary.completed_steps += 1

            # Store result in local dict AND sync back to context so that
            # any code reading context.accumulated_results (e.g. confirmation
            # handler fallback) sees up-to-date results.
            accumulated_results[step.name] = result.data
            context.accumulated_results = accumulated_results

            # Apply state updates
            if result.state_updates:
                await self.packet_service.update_state(
                    packet["packet_id"],
                    result.state_updates,
                    context.session_id,
                )
                # Also update context in memory so subsequent steps see the new state
                context.packet_state.update(result.state_updates)

                # Best-effort: attach any newly-produced Drive artifacts to the
                # design's artifact history now that a design_id exists.
                design_id = context.packet_state.get("design_id")
                if design_id:
                    await asyncio.to_thread(
                        sweep_state_for_artifacts,
                        design_id,
                        result.state_updates,
                        packet_id=packet["packet_id"],
                    )

            # Persist execution_summary after each step so the mini app
            # can show real-time progress (otherwise it stays blank until
            # the workflow pauses or completes).
            await self.packet_service.update_state(
                packet["packet_id"],
                {"execution_summary": execution_summary.to_dict()},
                context.session_id,
            )

            # Check for early completion
            if result.skip_remaining:
                LOGGER.info(f"Step {step.name} requested skip_remaining")
                # Mark remaining steps as skipped
                for remaining_step in steps[step.index + 1 :]:
                    skipped_record = StepExecutionRecord(
                        step_name=remaining_step.name,
                        step_type=remaining_step.step_type,
                        description=remaining_step.description,
                        status=StepStatus.SKIPPED,
                        result_summary="skipped (early completion)",
                    )
                    execution_summary.add_record(skipped_record)
                return StepLoopSignal(action="break")

            # Multi-site: after resolve_sites completes with >1 site,
            # run remaining steps once per site. This avoids modifying
            # individual handlers — they keep using site_name/site_id
            # from state, which we update before each iteration.
            sites_to_process = context.get_state("sites_to_process", [])
            if step.name == "resolve_sites" and len(sites_to_process) > 1:
                if not allow_multi_site_handoff:
                    # run_single_step is explicitly single-site-scoped (see its
                    # own Step 0 guard). With `steps=[step]` there is no real
                    # per-site step list to hand off to -- executing the
                    # hand-off here would run zero per-site work yet still
                    # let _execute_multi_site_steps mark the whole packet
                    # "completed". Refuse cleanly instead.
                    return StepLoopSignal(
                        action="return",
                        return_value=(
                            f"Step '{step.name}' discovered {len(sites_to_process)} sites; "
                            "run_single_step does not support multi-site fan-out.",
                            {
                                "error": "unsupported_multi_site_discovered",
                                "refused": True,
                                "sites_to_process": sites_to_process,
                            },
                        ),
                    )
                per_site_steps = steps[step.index + 1 :]
                return StepLoopSignal(
                    action="return",
                    return_value=await self._execute_multi_site_steps(
                        sites_to_process=sites_to_process,
                        per_site_steps=per_site_steps,
                        expert_config=expert_config,
                        packet=packet,
                        context=context,
                        accumulated_results=accumulated_results,
                        execution_summary=execution_summary,
                    ),
                )

        else:  # LLM step
            try:
                result = await self._run_with_heartbeat(
                    self._execute_llm_step(
                        step,
                        expert_config,
                        packet,
                        context,
                        accumulated_results,
                        execution_summary,
                    ),
                    packet["packet_id"],
                    context.session_id,
                )
                step_record.ended_at = time.time()
                step_record.duration_ms = int(
                    (step_record.ended_at - (step_record.started_at or 0)) * 1000
                )
                step_record.status = StepStatus.SUCCESS
                step_record.result_summary = f"Generated {len(result)} chars"
                execution_summary.completed_steps += 1

                accumulated_results[step.name] = {"response": result}
                context.accumulated_results = accumulated_results
                step_final_response = result  # Last LLM response is the final response

                # Persist execution_summary for mini app real-time progress
                await self.packet_service.update_state(
                    packet["packet_id"],
                    {"execution_summary": execution_summary.to_dict()},
                    context.session_id,
                )

            except Exception as e:
                step_record.ended_at = time.time()
                step_record.duration_ms = int(
                    (step_record.ended_at - (step_record.started_at or 0)) * 1000
                )
                step_record.status = StepStatus.FAILED
                step_record.error = str(e)
                execution_summary.failed_steps += 1
                execution_summary.final_status = "failed"
                execution_summary.failure_reason = f"LLM step '{step.name}' failed: {e}"

                LOGGER.error(f"LLM step {step.name} failed: {e}")

                packet_uuid = packet.get("id") or packet.get("packet_id")
                if packet_uuid:
                    await self._log_execution_summary(
                        packet_uuid, execution_summary, context.session_id
                    )

                await self.packet_service.fail_packet(
                    packet["packet_id"],
                    f"LLM step {step.name} failed: {e}",
                    context.session_id,
                    error_state={
                        "last_error": str(e),
                        "error_step": step.name,
                        "execution_summary": execution_summary.to_dict(),
                    },
                )

                user_message = self._build_failure_message(execution_summary)
                return StepLoopSignal(
                    action="return",
                    return_value=(
                        user_message,
                        {
                            "error": str(e),
                            "execution_summary": execution_summary.to_dict(),
                        },
                    ),
                )

        # Fall-through: function step succeeded (no skip_remaining / multi-site
        # handoff above) or LLM step succeeded. The original inline loop
        # reached its "Headless retry" check and then the "Advance to next
        # step" section at this exact point; both now live in the caller
        # (_execute_workflow_inner), keyed off this "advance" signal.
        return StepLoopSignal(action="advance", final_response=step_final_response)

    def _inject_lpp_entry_steps(
        self, steps: List["ParsedStep"], geo_source: Optional[str]
    ) -> List["ParsedStep"]:
        """Inject the route-specific entry step for the LPP workflow.

        Community route -> resolve_community_site (first function step), no resolve_sites.
        Submission route -> resolve_sites (existing behavior).
        """
        first_fn_idx = next(
            (i for i, s in enumerate(steps) if s.step_type == "function"), len(steps)
        )

        if geo_source == "community":
            if not any(s.name == "resolve_community_site" for s in steps):
                steps.insert(
                    first_fn_idx,
                    ParsedStep(
                        first_fn_idx,
                        "function",
                        "resolve_community_site",
                        "Detect community boundary and footprints from GPS anchor",
                    ),
                )
        else:
            if not any(s.name == "resolve_sites" for s in steps):
                steps.insert(
                    first_fn_idx,
                    ParsedStep(
                        first_fn_idx,
                        "function",
                        "resolve_sites",
                        "Validate and resolve site names",
                    ),
                )

        for i, s in enumerate(steps):
            s.index = i
        return steps

    def _summarize_result(self, data: Dict[str, Any]) -> str:
        """Create a brief summary of step result data."""
        if not data:
            return "no data"

        # Common keys to highlight
        summary_parts = []
        for key in ["url", "doc_url", "sheet_url", "message", "count", "status"]:
            if key in data:
                value = data[key]
                if isinstance(value, str) and len(value) > 50:
                    value = value[:47] + "..."
                summary_parts.append(f"{key}={value}")

        if summary_parts:
            return ", ".join(summary_parts[:3])  # Max 3 items

        # Fallback: show keys
        return f"keys: {', '.join(list(data.keys())[:5])}"

    async def _execute_multi_site_steps(
        self,
        sites_to_process: List[Dict[str, Any]],
        per_site_steps: List["ParsedStep"],
        expert_config: "ExpertConfig",
        packet: Dict[str, Any],
        context: StepContext,
        accumulated_results: Dict[str, Any],
        execution_summary: ExecutionSummary,
    ) -> Tuple[str, Dict[str, Any]]:
        """Execute per-site workflow steps in parallel across sites.

        Uses step-by-step parallel execution: all sites complete step N before
        any site starts step N+1. Steps marked [serial] run one site at a time.

        Each site gets an independent StepContext clone (deep-copied state)
        to prevent state bleed between sites.

        Returns:
            Tuple of (response, state) — always returns a result.
        """
        total_sites = len(sites_to_process)
        per_site_results: Dict[str, Dict[str, Any]] = {}
        site_doc_urls: List[str] = []
        failed_sites: set = set()

        # Deep-copy state snapshot so nested dicts aren't shared between sites
        state_snapshot = copy.deepcopy(context.packet_state)

        # Preserved results carry forward from pre-site steps (resolve_sites, etc.)
        _PRESERVE_KEYS = {"resolve_sites", "expert_raw_sections"}
        preserved_results = {k: v for k, v in accumulated_results.items() if k in _PRESERVE_KEYS}

        # Concurrency controls
        max_concurrency = int(os.getenv("MULTI_SITE_MAX_CONCURRENCY", "5"))
        semaphore = asyncio.Semaphore(max_concurrency)
        state_lock = asyncio.Lock()

        # Create per-site context clones and accumulated results
        site_contexts: Dict[str, StepContext] = {}
        site_accumulated: Dict[str, Dict[str, Any]] = {}

        for site in sites_to_process:
            site_name = site["name"]
            site_id = site.get("id")
            site_contexts[site_name] = context.clone_for_site(
                site_name=site_name,
                site_id=site_id,
                state_snapshot=state_snapshot,
                preserved_results=preserved_results,
            )
            site_accumulated[site_name] = copy.deepcopy(preserved_results)

        LOGGER.info(
            f"Multi-site execution: {total_sites} sites, "
            f"{len(per_site_steps)} steps per site, max_concurrency={max_concurrency}"
        )

        await context.send_progress_to_user(f"Processing {total_sites} sites in parallel...")

        # Step-by-step execution: all sites complete step N before step N+1
        for step in per_site_steps:
            if step.step_type != "function":
                # LLM steps are not supported in multi-site batch mode
                for site in sites_to_process:
                    site_name = site["name"]
                    if site_name in failed_sites:
                        continue
                    step_record = StepExecutionRecord(
                        step_name=f"{step.name}[{site_name}]",
                        step_type=step.step_type,
                        description=f"{step.description} ({site_name})",
                        status=StepStatus.SKIPPED,
                        result_summary="skipped (LLM step not supported in batch)",
                    )
                    execution_summary.add_record(step_record)
                continue

            active_sites = [s for s in sites_to_process if s["name"] not in failed_sites]
            if not active_sites:
                break

            LOGGER.info(
                f"Step [{step.step_type}] {step.name}: "
                f"{len(active_sites)} active sites "
                f"({'serial' if step.serial else 'parallel'})"
            )

            if step.serial:
                # Serial execution: one site at a time
                for site in active_sites:
                    await self._execute_site_step(
                        step=step,
                        site=site,
                        site_contexts=site_contexts,
                        site_accumulated=site_accumulated,
                        per_site_results=per_site_results,
                        failed_sites=failed_sites,
                        execution_summary=execution_summary,
                        packet=packet,
                        context=context,
                        state_lock=state_lock,
                    )
            else:
                # Parallel execution with semaphore
                tasks = []
                for site in active_sites:

                    async def _run_with_semaphore(s: Dict[str, Any] = site) -> None:
                        async with semaphore:
                            await self._execute_site_step(
                                step=step,
                                site=s,
                                site_contexts=site_contexts,
                                site_accumulated=site_accumulated,
                                per_site_results=per_site_results,
                                failed_sites=failed_sites,
                                execution_summary=execution_summary,
                                packet=packet,
                                context=context,
                                state_lock=state_lock,
                            )

                    tasks.append(_run_with_semaphore())

                await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results from per-site contexts
        for site in sites_to_process:
            site_name = site["name"]
            if site_name not in per_site_results:
                # Site completed all steps successfully
                site_ctx = site_contexts[site_name]
                doc_url = site_ctx.get_state("document_url")
                per_site_results[site_name] = {
                    "status": "success",
                    "document_url": doc_url,
                }
                if doc_url:
                    site_doc_urls.append(f"{site_name}: {doc_url}")
                LOGGER.info(f"  [{site_name}] Completed successfully")

        # Build multi-site summary with per-site details
        succeeded = sum(1 for r in per_site_results.values() if r["status"] == "success")
        failed = total_sites - succeeded

        summary_lines = [f"Processed {succeeded}/{total_sites} sites successfully."]

        # Per-site summaries using the same formatter as single-site completions
        for site in sites_to_process:
            site_name = site["name"]
            result = per_site_results.get(site_name, {})
            summary_lines.append(f"\n---\n**{site_name}**")

            if result.get("status") == "success":
                site_ctx = site_contexts[site_name]
                site_acc = site_accumulated[site_name]
                site_summary = self._format_lpp_summary(site_acc, site_ctx)
                if site_summary:
                    summary_lines.extend(site_summary)
                else:
                    doc_url = result.get("document_url")
                    if doc_url:
                        summary_lines.append(f"**Document:** {doc_url}")
            else:
                error = result.get("error", "unknown error")
                failed_step = result.get("failed_step", "")
                summary_lines.append(f"Failed: {error}")
                if failed_step:
                    summary_lines.append(f"_Failed at step: {failed_step}_")

        # Store multi-site results in state
        async with state_lock:
            await self.packet_service.update_state(
                packet["packet_id"],
                {"multi_site_results": per_site_results},
                context.session_id,
            )

        accumulated_results["multi_site_results"] = per_site_results

        # Mark all per-site steps as completed
        for step_idx, step in enumerate(per_site_steps):
            next_step = None
            if step_idx + 1 < len(per_site_steps):
                next_step = per_site_steps[step_idx + 1].name
            await self.packet_service.complete_step(
                packet["packet_id"],
                step.name,
                next_step=next_step,
                session_id=context.session_id,
            )

        # Log execution summary
        execution_summary.final_status = "completed" if failed == 0 else "completed_with_errors"
        packet_uuid = packet.get("id") or packet.get("packet_id")
        if packet_uuid:
            await self._log_execution_summary(packet_uuid, execution_summary, context.session_id)

        try:
            await self.packet_service.complete_packet(
                packet["packet_id"],
                outputs={
                    "multi_site_results": per_site_results,
                    "sites_processed": total_sites,
                    "sites_succeeded": succeeded,
                },
                session_id=context.session_id,
            )
        except Exception as e:
            LOGGER.warning(f"Could not mark packet as completed: {e}")

        final_response = "\n".join(summary_lines)
        return final_response, {
            "accumulated_results": accumulated_results,
            "execution_summary": execution_summary.to_dict(),
            "multi_site_results": per_site_results,
        }

    async def _execute_site_step(
        self,
        step: ParsedStep,
        site: Dict[str, Any],
        site_contexts: Dict[str, StepContext],
        site_accumulated: Dict[str, Dict[str, Any]],
        per_site_results: Dict[str, Dict[str, Any]],
        failed_sites: set,
        execution_summary: ExecutionSummary,
        packet: Dict[str, Any],
        context: StepContext,
        state_lock: asyncio.Lock,
    ) -> None:
        """Execute a single step for a single site.

        Handles error recording, state updates (serialized via lock),
        and failed-site tracking.

        Args:
            step: The workflow step to execute
            site: Site dict with 'name' and optional 'id'
            site_contexts: Per-site StepContext clones
            site_accumulated: Per-site accumulated results
            per_site_results: Shared dict for collecting final per-site outcomes
            failed_sites: Set of site names that have failed (skip these)
            execution_summary: Execution tracking
            packet: Current packet data
            context: Original context (for session_id, send_progress_to_user)
            state_lock: Lock for serializing DB state writes
        """
        site_name = site["name"]

        if site_name in failed_sites:
            return

        site_ctx = site_contexts[site_name]
        site_ctx.current_step = step.name
        acc = site_accumulated[site_name]

        step_record = StepExecutionRecord(
            step_name=f"{step.name}[{site_name}]",
            step_type=step.step_type,
            description=f"{step.description} ({site_name})",
            status=StepStatus.IN_PROGRESS,
            started_at=time.time(),
        )

        LOGGER.info(f"  [{site_name}] Step {step.index + 1}: [{step.step_type}] {step.name}")

        try:
            result = await self._execute_function_step(step, site_ctx, acc)
        except Exception as e:
            LOGGER.exception(f"  [{site_name}] Step {step.name} raised exception: {e}")
            result = StepResult.failure(str(e))

        step_record.ended_at = time.time()
        step_record.duration_ms = int((step_record.ended_at - (step_record.started_at or 0)) * 1000)

        if result.error:
            step_record.status = StepStatus.FAILED
            step_record.error = result.error
            execution_summary.add_record(step_record)

            LOGGER.error(f"  [{site_name}] Step {step.name} failed: {result.error}")
            per_site_results[site_name] = {
                "status": "failed",
                "error": result.error,
                "failed_step": step.name,
            }
            failed_sites.add(site_name)
            return

        if result.needs_user_input:
            step_record.status = StepStatus.SKIPPED
            step_record.result_summary = "skipped (batch mode, no user input)"
            execution_summary.add_record(step_record)

            LOGGER.warning(
                f"  [{site_name}] Step {step.name} needs user input — skipping site in batch mode"
            )
            per_site_results[site_name] = {
                "status": "failed",
                "error": f"Step {step.name} requires user input (not supported in batch)",
            }
            failed_sites.add(site_name)
            return

        step_record.status = StepStatus.SUCCESS
        step_record.result_summary = self._summarize_result(result.data)
        execution_summary.add_record(step_record)

        acc[step.name] = result.data
        site_ctx.accumulated_results = acc

        if result.state_updates:
            site_ctx.packet_state.update(result.state_updates)
            async with state_lock:
                await self.packet_service.update_state(
                    packet["packet_id"],
                    result.state_updates,
                    context.session_id,
                )

            # Best-effort: attach any newly-produced Drive artifacts to the
            # design's artifact history now that a design_id exists. Not part
            # of the packet_state write above, so it stays outside the lock.
            design_id = site_ctx.packet_state.get("design_id")
            if design_id:
                await asyncio.to_thread(
                    sweep_state_for_artifacts,
                    design_id,
                    result.state_updates,
                    packet_id=packet["packet_id"],
                )

        if result.skip_remaining:
            LOGGER.info(f"  [{site_name}] Step {step.name} requested skip_remaining")
            # Mark as succeeded early — don't add to failed_sites
            site_ctx_ref = site_contexts[site_name]
            doc_url = site_ctx_ref.get_state("document_url")
            per_site_results[site_name] = {
                "status": "success",
                "document_url": doc_url,
            }
            failed_sites.add(site_name)  # Prevent further step execution

    def _build_failure_message(self, summary: ExecutionSummary) -> str:
        """Build a user-friendly failure message with execution context."""
        lines = ["⚠️ The workflow encountered an issue.\n"]

        if summary.completed_steps > 0:
            lines.append(f"✓ Completed {summary.completed_steps} of {summary.total_steps} steps")

        if summary.failure_reason:
            # Sanitize the error for user display
            safe_error = sanitize_error_for_user(summary.failure_reason, context="processing")
            lines.append(f"\n{safe_error}")

        # Show which steps succeeded and which failed
        step_summary = []
        for record in summary.step_records:
            if record.status == StepStatus.SUCCESS:
                step_summary.append(f"✓ {record.step_name}")
            elif record.status == StepStatus.FAILED:
                step_summary.append(f"✗ {record.step_name}")

        if step_summary:
            lines.append("\nProgress:")
            lines.extend(step_summary[:5])  # Show up to 5 steps

        lines.append("\nYou can try the command again or contact support if this persists.")

        return "\n".join(lines)

    def _build_completion_summary(
        self,
        packet_type: str,
        accumulated_results: Dict[str, Any],
        execution_summary: ExecutionSummary,
        context: StepContext,
    ) -> str:
        """Build a user-friendly completion summary when workflow ends with function steps.

        Extracts key metrics from accumulated_results and formats them for the user.
        Different packet types get customized summaries highlighting relevant data.

        Args:
            packet_type: Type of work packet (e.g., 'document_ingestion')
            accumulated_results: Results from all completed steps
            execution_summary: Execution tracking summary
            context: Step context with packet state

        Returns:
            Formatted completion message for the user
        """
        lines = ["✅ **Workflow completed successfully**\n"]

        # Add timing info
        total_time_ms = sum(
            r.duration_ms or 0 for r in execution_summary.step_records if r.duration_ms
        )
        if total_time_ms > 0:
            if total_time_ms >= 1000:
                lines.append(f"_Completed in {total_time_ms / 1000:.1f}s_\n")

        # Extract metrics based on packet type
        if packet_type in ("document_ingestion", "rag_ingestion"):
            lines.extend(self._format_ingestion_summary(accumulated_results, context))
        elif packet_type in ("kpi_report", "grid_analysis"):
            lines.extend(self._format_report_summary(accumulated_results, context))
        elif packet_type in ("light_preliminary_package", "lpp"):
            lines.extend(self._format_lpp_summary(accumulated_results, context))
        else:
            # Generic summary for unknown packet types
            lines.extend(self._format_generic_summary(accumulated_results, execution_summary))

        return "\n".join(lines)

    def _format_ingestion_summary(
        self,
        results: Dict[str, Any],
        context: StepContext,
    ) -> List[str]:
        """Format summary for document ingestion workflows."""
        lines = []

        # Document title
        title = context.get_state("document_title") or "Untitled Document"
        lines.append(f"**Title:** {title}")

        # Document type from classify_document
        classify_result = results.get("classify_document", {})
        doc_type = classify_result.get("doc_type") or context.get_state("detected_doc_type")
        if doc_type:
            doc_type_display = {
                "sop": "Standard Operating Procedure",
                "faq": "FAQ / Q&A",
                "support_example": "Support Conversation",
                "technical": "Technical Documentation",
                "policy": "Policy / Guidelines",
            }.get(doc_type, doc_type.replace("_", " ").title())
            confidence = classify_result.get("confidence") or context.get_state(
                "classification_confidence"
            )
            conf_str = f" ({confidence * 100:.0f}%)" if confidence else ""
            lines.append(f"**Type:** {doc_type_display}{conf_str}")

        # Matched procedure - now handled per-chunk in embed_and_store for support_example
        # Still read from state for backwards compatibility and summary display
        procedure_title = context.get_state("matched_procedure_title")
        if procedure_title:
            lines.append(f"**Procedure:** {procedure_title}")

        # Storage metrics from embed_and_store
        storage_result = results.get("embed_and_store", {})
        chunk_count = storage_result.get("chunk_count") or context.get_state("stored_chunk_count")
        chunks_skipped = (
            storage_result.get("chunks_skipped") or context.get_state("stored_chunks_skipped") or 0
        )
        entity_count = storage_result.get("entity_count", 0)
        doc_id = storage_result.get("stored_document_id") or context.get_state("stored_document_id")
        duplicate_mode = storage_result.get("duplicate_mode") or context.get_state("duplicate_mode")

        # Show deduplication mode if not a fresh ingestion
        if duplicate_mode == "incorporate":
            lines.append("**Mode:** Incorporated new content")
        elif duplicate_mode == "replace":
            lines.append("**Mode:** Replaced existing document")

        if chunk_count:
            chunk_line = f"**Chunks stored:** {chunk_count}"
            if chunks_skipped > 0:
                chunk_line += f" ({chunks_skipped} duplicates skipped)"
            lines.append(chunk_line)
        if entity_count:
            lines.append(f"**Entities extracted:** {entity_count}")
        relationship_count = storage_result.get("relationship_count", 0)
        if relationship_count:
            lines.append(f"**Relationships stored:** {relationship_count}")

        # Entity summary from extract_entities
        extract_result = results.get("extract_entities", {})
        entities = extract_result.get("entities") or context.get_state("extracted_entities") or []
        if entities and len(entities) > 0:
            # Show top 3 entity names
            entity_names = [e.get("name", "?") for e in entities[:3]]
            if len(entities) > 3:
                entity_names.append(f"...and {len(entities) - 3} more")
            lines.append(f"**Key entities:** {', '.join(entity_names)}")

        if doc_id:
            lines.append(f"\n_Document ID: {doc_id[:12]}..._")

        return lines

    def _format_report_summary(
        self,
        results: Dict[str, Any],
        context: StepContext,
    ) -> List[str]:
        """Format summary for KPI report and grid analysis workflows."""
        lines = []

        # Look for document URL in results
        for step_name, step_result in results.items():
            if isinstance(step_result, dict):
                doc_url = step_result.get("document_url") or step_result.get("sheet_url")
                if doc_url:
                    lines.append(f"**Report:** {doc_url}")
                    break

        # Grid info
        grid_name = context.get_state("grid_name") or context.get_state("site_name")
        if grid_name:
            lines.append(f"**Grid:** {grid_name}")

        # Metrics count if available
        for step_name, step_result in results.items():
            if isinstance(step_result, dict):
                if "metrics_count" in step_result:
                    lines.append(f"**Metrics analyzed:** {step_result['metrics_count']}")
                if "alerts_count" in step_result:
                    lines.append(f"**Alerts found:** {step_result['alerts_count']}")

        return lines

    def _format_lpp_summary(
        self,
        results: Dict[str, Any],
        context: StepContext,
    ) -> List[str]:
        """Format summary for Light Preliminary Package workflows."""
        lines = []

        # Site name
        site_name = context.get_state("site_name") or context.get_state("grid_name")
        if site_name:
            lines.append(f"**Site:** {site_name}")

        # Footprint provenance (community route) — harmonization visibility
        if context.get_state("geo_source") == "community":
            fp_count = context.get_state("footprint_count")
            fp_source = context.get_state("footprint_source") or "unknown"
            grid3 = context.get_state("grid3_building_count")
            if fp_count is not None:
                line = f"**Footprints:** {fp_count} ({fp_source})"
                if grid3:
                    line += f" — GRID3 estimate {grid3}"
                lines.append(line)

        # Document URL from copy_lpp_template
        template_result = results.get("copy_lpp_template", {})
        doc_url = template_result.get("document_url")
        if doc_url:
            lines.append(f"**Document:** {doc_url}")

        # Map generated
        map_result = results.get("generate_distribution_map", {})
        if map_result:
            statistics = map_result.get("statistics", {})
            if map_result.get("map_image_b64"):
                lines.append("**Map:** Generated and embedded")
            if statistics:
                buildings = statistics.get("total_buildings", 0)
                served = statistics.get("served_buildings", 0)
                poles = statistics.get("poles", 0)
                parts = []
                if buildings:
                    parts.append(f"{buildings} buildings ({served} served)")
                if poles:
                    parts.append(f"{poles} poles")
                cable_m = statistics.get("cable_length_m")
                if cable_m:
                    parts.append(f"{cable_m:,.0f}m cable")
                if parts:
                    lines.append(f"**Layout:** {', '.join(parts)}")

        # Design / BOM from AppSheet — energy & financials
        design_result = results.get("generate_powerplant_design", {})
        if design_result:
            energy_specs = design_result.get("energy_specs", {})
            cost_summary = design_result.get("cost_summary", {})

            # Energy specs
            energy_parts = []
            if energy_specs.get("total_kwp"):
                energy_parts.append(f"{energy_specs['total_kwp']} kWp")
            if energy_specs.get("total_kwh"):
                energy_parts.append(f"{energy_specs['total_kwh']} kWh")
            if energy_specs.get("total_kva"):
                energy_parts.append(f"{energy_specs['total_kva']} kVA")
            if energy_parts:
                lines.append(f"**Energy:** {', '.join(energy_parts)}")

            # Cost breakdown
            total_cost = cost_summary.get("total_cost")
            if total_cost:
                lines.append(f"**Total cost:** ${total_cost:,.2f}")
                # Sub-costs if available
                sub_parts = []
                mea = cost_summary.get("main_energy_asset_cost")
                if mea:
                    sub_parts.append(f"Energy assets ${mea:,.0f}")
                metering = cost_summary.get("metering_cost")
                if metering:
                    sub_parts.append(f"Metering ${metering:,.0f}")
                bos = cost_summary.get("bos_cost")
                if bos:
                    sub_parts.append(f"BoS ${bos:,.0f}")
                if sub_parts:
                    lines.append(f"  _{' | '.join(sub_parts)}_")

            bom_count = design_result.get("bom_item_count") or len(
                design_result.get("bom_items", [])
            )
            if bom_count:
                lines.append(f"**BOM:** {bom_count} items")

        # Cells populated
        populate_result = results.get("populate_lpp_cells", {})
        if populate_result:
            cells_count = populate_result.get("cells_populated")
            if cells_count:
                lines.append(f"**Cells populated:** {cells_count}")

        # Map sent to Telegram
        map_sent = results.get("send_lpp_map_to_telegram", {})
        if map_sent.get("map_sent"):
            lines.append("**Map sent to chat**")

        return lines

    def _format_generic_summary(
        self,
        results: Dict[str, Any],
        summary: ExecutionSummary,
    ) -> List[str]:
        """Format generic summary for unknown packet types."""
        lines = []

        lines.append(f"**Steps completed:** {summary.completed_steps}/{summary.total_steps}")

        # List completed steps
        completed = [r.step_name for r in summary.step_records if r.status == StepStatus.SUCCESS]
        if completed:
            lines.append("**Executed:**")
            for step in completed[:5]:
                lines.append(f"  • {step.replace('_', ' ').title()}")
            if len(completed) > 5:
                lines.append(f"  • ...and {len(completed) - 5} more")

        # Look for any URLs in results
        for step_name, step_result in results.items():
            if isinstance(step_result, dict):
                for key in ["url", "document_url", "sheet_url", "external_url"]:
                    if step_result.get(key):
                        lines.append(f"\n**Output:** {step_result[key]}")
                        break

        return lines

    async def _log_execution_summary(
        self,
        packet_uuid: str,
        summary: ExecutionSummary,
        session_id: Optional[str],
    ) -> None:
        """Log execution summary to database for auditing."""
        try:
            await self.packet_service._log_event(
                packet_uuid,
                "execution_summary",
                None,
                f"Workflow {summary.final_status}: {summary.completed_steps}/{summary.total_steps} steps",
                output_data=summary.to_dict(),
                session_id=session_id,
            )
        except Exception as e:
            LOGGER.warning(f"Failed to log execution summary: {e}")

    async def _run_with_heartbeat(
        self,
        coro: "Coroutine[Any, Any, Any]",
        packet_id: str,
        session_id: Optional[str],
        interval_seconds: int = 60,
    ) -> Any:
        """Run a coroutine while periodically touching the packet's updated_at.

        Long-running steps (e.g. AppSheet design_and_bom with wait_for_completion)
        can exceed the stale-packet timeout.  A lightweight heartbeat prevents
        the packet from being auto-failed while it is genuinely working.
        """

        async def _heartbeat():
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    import datetime as _dt

                    await self.packet_service.update_state(
                        packet_id,
                        {
                            "_heartbeat": time.time(),
                            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        },
                        session_id,
                    )
                except Exception:
                    pass  # Best-effort; don't kill the step

        task = asyncio.create_task(_heartbeat())
        try:
            return await coro
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # Packet types that have their own interactive flows and shouldn't show parameter confirmation
    INTERACTIVE_PACKET_TYPES = {
        "document_ingestion",  # Ingestion expert has step-level user input flows
        "grids_technical_review",  # GTR analysis mode has conversational user input flows
        "code_investigation",  # Code investigator - LLM manages clarification flow
        "light_preliminary_package",  # LPP runs end-to-end with progress messages
        "community_sizing",  # Parse step handles input extraction; no parameter confirmation needed
    }

    def _is_confirmation_enabled(self, packet_type: str = "") -> bool:
        """Check if parameter confirmation is enabled.

        Confirmation is disabled if:
        1. Globally disabled via env var WORKFLOW_PARAMETER_CONFIRMATION=false
        2. Packet type is in the interactive exclusion list (has its own user input flows)

        Args:
            packet_type: The current packet type (e.g., "document_ingestion")

        Returns:
            True if confirmation flow is enabled for this packet
        """
        # Check global setting
        env_value = os.getenv("WORKFLOW_PARAMETER_CONFIRMATION", "true")
        if env_value.lower() not in ("true", "1", "yes", "on"):
            return False

        # Check packet-type exclusion
        if packet_type in self.INTERACTIVE_PACKET_TYPES:
            LOGGER.debug(
                f"Skipping parameter confirmation for interactive packet type: {packet_type}"
            )
            return False

        return True

    async def _handle_function_step_confirmation(
        self,
        step: ParsedStep,
        context: StepContext,
        total_steps: int,
        packet: Dict[str, Any],
        accumulated_results: Optional[Dict[str, Any]] = None,
    ) -> Optional[StepResult]:
        """Handle parameter confirmation before a function step.

        This is the fully general confirmation feature. It shows ALL current
        packet_state values and lets users override any of them. No schema
        definition needed anywhere - just show state and allow overrides.

        Args:
            step: The function step about to execute
            context: Step execution context with packet_state
            total_steps: Total number of steps in workflow
            packet: Current packet data
            accumulated_results: Results from previous steps (local copy from execute_workflow).
                Must be passed so DB persistence saves the correct results, not the
                stale context.accumulated_results which may not include results from
                the current execution run.

        Returns:
            StepResult to pause for input, or None to continue
        """
        # Check if confirmation is enabled for this packet type
        packet_type = packet.get("packet_type", "")
        if not self._is_confirmation_enabled(packet_type):
            return None

        # Check if auto-continue is enabled (user chose 'a' previously)
        if context.get_state("auto_continue_enabled"):
            return None

        # Check if we're already in confirmation flow for this step
        conf_ctx = context.get_state("param_confirmation_context", {})

        # Get schema from current packet state (fully general - no definition needed)
        resolver = get_parameter_resolver()
        schema, resolved_params = resolver.resolve_from_packet_state(context, step.name)

        if not resolved_params:
            # No state values to confirm - skip
            return None

        # Check if we're handling a user response for this step
        if conf_ctx.get("step_name") == step.name and context.get_state(
            "awaiting_param_confirmation"
        ):
            return await self._handle_confirmation_response(
                schema, resolved_params, context, total_steps
            )

        # Only show confirmation when editable params have changed since last confirmation.
        # This prevents re-prompting for the same values before every step. A snapshot of
        # confirmed values is stored after the user confirms; subsequent steps skip
        # confirmation unless a step has updated an editable_ value.
        confirmed_snapshot = context.get_state("confirmed_editable_snapshot") or {}
        current_snapshot = {p.name: str(p.current_value) for p in resolved_params}
        if current_snapshot == confirmed_snapshot:
            return None

        # Start new confirmation flow
        prompt = format_confirmation_prompt(
            step_name=step.name,
            step_index=step.index,
            total_steps=total_steps,
            description=step.description,
            parameters=resolved_params,
        )

        # CRITICAL: Save accumulated_results so they persist when workflow resumes.
        # Must use the local accumulated_results (from execute_workflow), NOT
        # context.accumulated_results, because context.accumulated_results is stale —
        # it's only synced back from the local variable in _execute_function_step
        # (which runs AFTER this confirmation check). Using context.accumulated_results
        # would save an empty/incomplete dict, losing all step results from this run.
        results_to_save = (
            accumulated_results if accumulated_results is not None else context.accumulated_results
        )
        state_updates = {
            "awaiting_param_confirmation": True,
            "param_confirmation_context": {
                "step_name": step.name,
                "selecting_param": None,
            },
            "accumulated_results": results_to_save,
        }

        # Attach Mini App form type if the step has a matching form definition
        mini_app_form = None
        if os.getenv("MINI_APP_FORMS_ENABLED", "false").lower() == "true":
            from orchestrator.mini_app.schemas import FORM_SCHEMAS

            param_names = {p.name for p in resolved_params}
            for form_type, fields in FORM_SCHEMAS.items():
                form_keys = {f["key"] for f in fields}
                if form_keys & param_names:
                    mini_app_form = form_type
                    break

        return StepResult(
            state_updates=state_updates,
            needs_user_input=True,
            user_prompt=prompt,
            mini_app_form=mini_app_form,
        )

    async def _handle_confirmation_response(
        self,
        schema: PacketParameterSchema,
        resolved_params: List[ResolvedParameter],
        context: StepContext,
        total_steps: int,
    ) -> Optional[StepResult]:
        """Process user's response to parameter confirmation.

        Args:
            schema: Packet parameter schema (packet_type contains step name)
            resolved_params: List of resolved parameters
            context: Step execution context
            total_steps: Total number of steps

        Returns:
            StepResult to continue confirmation or None to proceed with step
        """
        user_input = context.user_input.strip()
        conf_ctx = context.get_state("param_confirmation_context", {})
        selecting_param = conf_ctx.get("selecting_param")
        step_name = conf_ctx.get("step_name", schema.packet_type)

        resolver = get_parameter_resolver()

        # Check if user is entering a value for a selected parameter
        if selecting_param is not None:
            param = resolved_params[selecting_param]
            response = parse_confirmation_response(
                user_input,
                num_parameters=len(resolved_params),
                is_editing_param=True,
                editing_param_type=param.param_type,
            )

            if response.action == ConfirmationAction.SET_VALUE:
                # Store the new value in state (not just override)
                # This makes it available to the handler via get_state()
                old_value = param.current_value
                context.set_parameter_override(param.name, response.new_value)
                # Also update packet_state directly so handler can use get_state()
                context.packet_state[param.name] = response.new_value

                # Clear selecting state and show updated prompt
                conf_ctx["selecting_param"] = None

                # Re-resolve to show updated values
                schema, resolved_params = resolver.resolve_from_packet_state(context, step_name)
                change_msg = format_value_change_confirmation(
                    param.name, old_value, response.new_value
                )

                prompt = (
                    change_msg
                    + "\n\n"
                    + format_confirmation_prompt(
                        step_name=step_name,
                        step_index=conf_ctx.get("step_index", 0),
                        total_steps=total_steps,
                        description=schema.description,
                        parameters=resolved_params,
                    )
                )

                return StepResult(
                    state_updates={
                        "param_confirmation_context": conf_ctx,
                        param.name: response.new_value,  # Persist the override
                    },
                    needs_user_input=True,
                    user_prompt=prompt,
                )

            elif response.action == ConfirmationAction.CONTINUE:
                # User cancelled edit - show prompt again
                conf_ctx["selecting_param"] = None
                prompt = format_confirmation_prompt(
                    step_name=step_name,
                    step_index=conf_ctx.get("step_index", 0),
                    total_steps=total_steps,
                    description=schema.description,
                    parameters=resolved_params,
                )
                return StepResult(
                    state_updates={"param_confirmation_context": conf_ctx},
                    needs_user_input=True,
                    user_prompt=prompt,
                )

            elif response.action == ConfirmationAction.INVALID:
                # Invalid input - re-prompt for value
                prompt = f"{response.error_message}\n\n{format_param_edit_prompt(param)}"
                return StepResult(
                    needs_user_input=True,
                    user_prompt=prompt,
                )

        # Parse as confirmation command
        response = parse_confirmation_response(user_input, num_parameters=len(resolved_params))

        if response.action == ConfirmationAction.CONTINUE:
            # Proceed with step - clear confirmation state
            context.packet_state["awaiting_param_confirmation"] = False
            context.packet_state["param_confirmation_context"] = {}
            # Snapshot confirmed values so confirmation won't re-trigger for same values
            context.packet_state["confirmed_editable_snapshot"] = {
                p.name: str(p.current_value) for p in resolved_params
            }
            return None  # Signal to proceed

        if response.action == ConfirmationAction.AUTO:
            # Enable auto mode and proceed
            context.packet_state["auto_continue_enabled"] = True
            context.packet_state["awaiting_param_confirmation"] = False
            context.packet_state["param_confirmation_context"] = {}
            # Snapshot confirmed values
            context.packet_state["confirmed_editable_snapshot"] = {
                p.name: str(p.current_value) for p in resolved_params
            }
            return None

        if response.action == ConfirmationAction.CANCEL:
            # User wants to abort the workflow
            context.packet_state["awaiting_param_confirmation"] = False
            context.packet_state["param_confirmation_context"] = {}
            return StepResult.failure("Workflow cancelled by user.")

        if response.action == ConfirmationAction.NEW_COMMAND:
            # User entered a new slash command - abort current workflow
            # The command will be re-processed by the orchestrator
            context.packet_state["awaiting_param_confirmation"] = False
            context.packet_state["param_confirmation_context"] = {}
            context.packet_state["new_command_to_process"] = response.new_value
            return StepResult.failure(
                f"Starting new command: {response.new_value}",
            )

        if response.action == ConfirmationAction.SELECT_PARAM:
            param_idx = response.param_index
            param = resolved_params[param_idx]

            if not param.editable:
                prompt = (
                    f"**{param.name}** cannot be modified (read-only).\n\n"
                    + format_confirmation_prompt(
                        step_name=step_name,
                        step_index=conf_ctx.get("step_index", 0),
                        total_steps=total_steps,
                        description=schema.description,
                        parameters=resolved_params,
                    )
                )
                return StepResult(
                    needs_user_input=True,
                    user_prompt=prompt,
                )

            # Enter edit mode
            conf_ctx["selecting_param"] = param_idx
            prompt = format_param_edit_prompt(param)

            return StepResult(
                state_updates={"param_confirmation_context": conf_ctx},
                needs_user_input=True,
                user_prompt=prompt,
            )

        # Invalid input
        prompt = f"{response.error_message}\n\n" + format_confirmation_prompt(
            step_name=step_name,
            step_index=conf_ctx.get("step_index", 0),
            total_steps=total_steps,
            description=schema.description,
            parameters=resolved_params,
        )
        return StepResult(
            needs_user_input=True,
            user_prompt=prompt,
        )

    async def _execute_function_step(
        self,
        step: ParsedStep,
        context: StepContext,
        accumulated_results: Dict[str, Any],
    ) -> StepResult:
        """Execute a function step handler.

        Args:
            step: Parsed step definition
            context: Step execution context
            accumulated_results: Results from previous steps

        Returns:
            StepResult from handler
        """
        handler = get_step_handler(step.name)

        if not handler:
            LOGGER.error(f"No handler registered for step: {step.name}")
            return StepResult.failure(f"No handler for step: {step.name}")

        # Update context with accumulated results
        context.accumulated_results = accumulated_results

        try:
            return await handler(context)
        except Exception as e:
            LOGGER.exception(f"Handler {step.name} raised exception: {e}")
            return StepResult.failure(str(e))

    async def _execute_llm_step(
        self,
        step: ParsedStep,
        expert_config: "ExpertConfig",
        packet: Dict[str, Any],
        context: StepContext,
        accumulated_results: Dict[str, Any],
        execution_summary: Optional[ExecutionSummary] = None,
    ) -> str:
        """Execute an LLM reasoning step.

        For parsing steps (step names containing 'parse'), the LLM is asked to
        include structured JSON which is then extracted and stored in accumulated_results.

        Args:
            step: Parsed step definition
            expert_config: Expert configuration
            packet: Current packet data
            context: Step execution context
            accumulated_results: Results from previous steps
            execution_summary: Optional execution summary for context

        Returns:
            LLM response text
        """
        # Check if this is a parsing step that should output structured data
        is_parsing_step = "parse" in step.name.lower()

        # Build prompt with full context
        prompt = self._build_llm_step_prompt(
            step,
            expert_config,
            packet,
            context,
            accumulated_results,
            is_parsing_step,
            execution_summary,
        )

        try:
            # Build payload in Gemini API format
            payload: Dict[str, Any] = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            }

            if expert_config.system_instructions:
                payload["systemInstruction"] = {
                    "parts": [{"text": expert_config.system_instructions}]
                }

            # Note: tools aren't typically needed for LLM-only steps
            # The expert uses tools via function steps, not LLM steps

            response = await self.gemini.generate_content(payload)

            # Check finishReason for blocked content
            finish_reason = self._extract_finish_reason(response)
            if finish_reason and finish_reason in (
                "SAFETY",
                "PROHIBITED_CONTENT",
                "RECITATION",
                "BLOCKLIST",
                "SPII",
            ):
                LOGGER.warning(
                    f"LLM step {step.name} blocked by Gemini (finishReason={finish_reason})"
                )
                return f"I can't help with that request. (blocked: {finish_reason})"

            # Extract text from response
            candidates = response.get("candidates", [])
            text = ""
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    text = str(parts[0].get("text", "")) if parts[0].get("text") else ""

            # If parsing step, try to extract structured JSON
            if is_parsing_step and text:
                structured_data = self._extract_structured_data(text)
                if structured_data:
                    LOGGER.info(
                        f"Extracted structured data from {step.name}: {list(structured_data.keys())}"
                    )
                    # Store in accumulated_results for function steps to use
                    accumulated_results[f"{step.name}_data"] = structured_data
                    # Also update packet_state for persistence
                    await self.packet_service.update_state(
                        packet["packet_id"],
                        {"parsed_inputs": structured_data},
                        context.session_id,
                    )

            return text

        except Exception as e:
            LOGGER.error(f"LLM step {step.name} failed: {e}")
            return sanitize_error_for_user(str(e), context="processing")

    def _extract_finish_reason(self, response: Dict[str, Any]) -> Optional[str]:
        """Extract finishReason from Gemini response.

        Handles cases where candidates array may be empty or missing.

        Args:
            response: Raw Gemini API response

        Returns:
            finishReason string if found, None otherwise
        """
        candidates = response.get("candidates", [])
        if not candidates:
            # Check promptFeedback for block reason (prompt blocked)
            prompt_feedback = response.get("promptFeedback", {})
            block_reason: Optional[str] = prompt_feedback.get("blockReason")
            if block_reason:
                LOGGER.warning(f"Expert prompt blocked with reason: {block_reason}")
                return block_reason
            return None

        finish_reason: Optional[str] = candidates[0].get("finishReason")
        return finish_reason

    def _extract_structured_data(self, text: str) -> Optional[Dict[str, Any]]:
        """Extract JSON from LLM response text.

        Looks for JSON blocks in markdown code fences or inline JSON.

        Args:
            text: LLM response text

        Returns:
            Parsed JSON dict or None if not found
        """
        import json
        import re

        # Try to find JSON in code fence
        json_match = re.search(r"```(?:json)?\s*\n?({[\s\S]*?})\s*\n?```", text)
        if json_match:
            try:
                return json.loads(json_match.group(1))  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                pass

        # Try to find inline JSON object
        json_match = re.search(r"({[^{}]*(?:{[^{}]*}[^{}]*)*})", text)
        if json_match:
            try:
                return json.loads(json_match.group(1))  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                pass

        return None

    def _build_llm_step_prompt(
        self,
        step: ParsedStep,
        expert_config: "ExpertConfig",
        packet: Dict[str, Any],
        context: StepContext,
        accumulated_results: Dict[str, Any],
        is_parsing_step: bool = False,
        execution_summary: Optional[ExecutionSummary] = None,
    ) -> str:
        """Build prompt for LLM step with full workflow context.

        Args:
            step: Current step
            expert_config: Expert configuration
            packet: Current packet data
            context: Step execution context
            accumulated_results: Results from previous steps
            is_parsing_step: If True, add JSON format instructions
            execution_summary: Optional execution summary for additional context

        Returns:
            Formatted prompt string
        """
        # Get workflow steps
        workflow = expert_config.get_workflow(packet["packet_type"])
        completed = set(packet.get("steps_completed", []) or [])

        # Show the full workflow plan with checkmarks and timing from execution summary
        workflow_display_lines = []
        for i, w_line in enumerate(workflow):
            parsed = self._parse_step_line(w_line, i)
            if parsed:
                if parsed.name in completed:
                    mark = "✓"
                    # Add timing info from execution summary if available
                    timing = ""
                    if execution_summary:
                        for record in execution_summary.step_records:
                            if record.step_name == parsed.name and record.duration_ms:
                                timing = f" ({record.duration_ms}ms)"
                                break
                    workflow_display_lines.append(
                        f"{mark} {i + 1}. [{parsed.step_type}] {parsed.name}{timing}"
                    )
                elif parsed.name == step.name:
                    mark = "▶"
                    workflow_display_lines.append(
                        f"{mark} {i + 1}. [{parsed.step_type}] {parsed.name}"
                    )
                else:
                    mark = "○"
                    workflow_display_lines.append(
                        f"{mark} {i + 1}. [{parsed.step_type}] {parsed.name}"
                    )

        workflow_display = "\n".join(workflow_display_lines)

        # Format accumulated results
        results_parts = []
        for k, v in accumulated_results.items():
            formatted = self._format_result(v)
            results_parts.append(f"### {k}\n{formatted}")
        results_display = "\n\n".join(results_parts) if results_parts else "(No previous results)"

        # Add JSON format instructions for parsing steps
        json_instructions = ""
        if is_parsing_step:
            packet_type = packet.get("packet_type", "")
            if "kpi" in packet_type or "report" in packet_type:
                json_instructions = """
## Output Format
You MUST include a JSON block with the extracted data. Format:
```json
{
  "grids": [{"grid_name": "GridName1"}, {"grid_name": "GridName2"}],
  "time_range": {
    "start_date": "2026-01-01T00:00:00Z",
    "end_date": "2026-01-20T00:00:00Z",
    "period": "monthly"
  },
  "report_type": "kpi_report"
}
```
Extract grid names from the user request. For time range, interpret phrases like:
- "monthly" or "last month" → last 30 days
- "weekly" or "last week" → last 7 days
- "last N days" → exactly N days
"""
            elif "analysis" in packet_type or "grid" in packet_type:
                json_instructions = """
## Output Format
You MUST include a JSON block with the extracted data. Format:
```json
{
  "grid": {"grid_name": "GridName"},
  "time_range": {
    "start_date": "2026-01-01T00:00:00Z",
    "end_date": "2026-01-20T00:00:00Z"
  },
  "analysis_focus": "all"
}
```
Extract the grid name from the user request. For time range, default to last 30 days if not specified.
"""

        # Build execution context if available
        execution_context = ""
        if execution_summary and execution_summary.step_records:
            exec_lines = ["\n## Execution Status"]
            for record in execution_summary.step_records:
                status_icon = {
                    StepStatus.SUCCESS: "✓",
                    StepStatus.FAILED: "✗",
                    StepStatus.SKIPPED: "○",
                    StepStatus.PENDING: "·",
                    StepStatus.IN_PROGRESS: "▶",
                }.get(record.status, "?")

                line = f"{status_icon} {record.step_name}"
                if record.duration_ms:
                    line += f" ({record.duration_ms}ms)"
                if record.error:
                    line += f" - ERROR: {record.error}"
                elif record.result_summary:
                    line += f" - {record.result_summary}"
                exec_lines.append(line)
            execution_context = "\n".join(exec_lines)

        # Include enrichment context (grid names, date, etc.) if available
        enrichment_section = ""
        if context.context_message:
            enrichment_section = f"\n## Context & Enrichment\n{context.context_message}\n"

        return f"""You are the {expert_config.display_name} expert.

## Work Packet
- ID: {packet["packet_id"]}
- Goal: {packet["packet_goal"]}
- Type: {packet["packet_type"]}

## Workflow Plan
{workflow_display}
{execution_context}

## Current Step: {step.name}
{step.description}

## Results from Previous Steps
{results_display}
{enrichment_section}
## User's Original Request
{context.packet_goal}

## User's Latest Message
{context.user_input}
{json_instructions}{self._get_automated_facts(packet.get("packet_type", ""))}
Execute the current step ({step.name}) and provide your response.
Be specific and actionable. Reference data from previous steps as needed.
"""

    # Facts about automated workflow steps, keyed by packet type.
    # These override outdated instructions in Google Doc expert definitions.
    AUTOMATED_FACTS: Dict[str, str] = {
        "light_preliminary_package": """
## Important: Automated Workflow Facts
- The site map image is automatically inserted into the spreadsheet by the workflow.
  Do NOT tell the user to manually copy/paste the site map image.
- The BOM (Bill of Materials) is already generated as part of this workflow.
  Do NOT mention generating BOM as a next step — it is already done.
- Do NOT suggest next steps that don't exist (e.g. "Notify Engineering Team").
- Do NOT include a [BUTTONS] block. Buttons are not supported in expert workflows.
""",
    }

    def _get_automated_facts(self, packet_type: str) -> str:
        """Return automated facts section for the given packet type, or empty string."""
        return self.AUTOMATED_FACTS.get(packet_type, "")

    async def validate_step_prerequisites(
        self, packet: Dict[str, Any], step_name: str
    ) -> PrereqReport:
        """Read-only report: what does `step_name` need that isn't available right now?

        Does NOT mutate `packet`, packet_state, or any DB row -- this is pure
        reporting for a later task that will run steps out of normal recipe
        order and needs to know, up front, whether that's safe.

        Checks the step's `StepContract` (see `step_contracts.py`) against
        three tiers of availability, in order:
        1. The packet's own current `packet_state`.
        2. A prior similar-completed packet's `packet_state` (best-effort via
           `packet_service.find_similar_completed`; non-fatal on failure).
        3. For `consumes_state`/param keys ending in `_drive_id` only: the
           Phase B artifact jsonb on the design row (`gd_designs.artifacts`),
           keyed by the artifact type derived from stripping the suffix;
           only consulted when a `design_id` is available from tier 1/2
           (non-fatal on failure).

        `consumes_results` is checked literally against
        `packet.steps_completed` -- it describes this packet's own execution
        history, so tiers 2/3 (which are about a *different* packet's state)
        don't apply.

        `contract.optional_consumes_state` keys are checked through the same
        three-tier `_available()` resolution as `consumes_state`, but any
        missing entries are reported informationally on
        `PrereqReport.missing_optional_state` only -- they never affect
        `satisfied` and are never looked up in `producer_chain` (there is
        nothing to auto-run for a key the step already has fallback logic
        for).

        Args:
            packet: Current packet dict (as returned by WorkPacketService).
            step_name: Name of the step to validate (must be registered).

        Returns:
            A `PrereqReport` describing what's missing (if anything) and,
            for any missing item, which other registered step(s) could
            produce it.

        Raises:
            ValueError: if `step_name` has no registered handler at all --
                that's a caller error, not a data-availability question.
        """
        if get_step_handler(step_name) is None:
            raise ValueError(f"No step handler registered with name: {step_name!r}")

        contract = get_step_contract(step_name)
        if contract is None:
            # No contract attached (e.g. an LLM step, or a function step that
            # predates contracts) -- nothing to validate against, so don't block.
            LOGGER.debug(
                "validate_step_prerequisites: step %r has no StepContract attached; "
                "nothing to validate, treating prerequisites as satisfied",
                step_name,
            )
            return PrereqReport(step_name=step_name, satisfied=True)

        packet_state: Dict[str, Any] = packet.get("packet_state") or {}

        # Tier 2: a prior similar-completed packet's state (best-effort, non-fatal).
        # Uses the same key_entity resolution convention as existing callers of
        # find_similar_completed (see work_packet_service.cancel_stale_packets_for_entity,
        # expert_router._extract_key_entity call sites): packet_inputs["key_entity"],
        # falling back to packet_inputs/packet_state["site_name"].
        similar_state: Dict[str, Any] = {}
        packet_inputs = packet.get("packet_inputs") or {}
        key_entity = (
            packet_inputs.get("key_entity")
            or packet_inputs.get("site_name")
            or packet_state.get("site_name")
        )
        if key_entity:
            try:
                similar_packets = await self.packet_service.find_similar_completed(
                    packet_type=packet.get("packet_type"),
                    key_entity=key_entity,
                    organization_id=packet.get("organization_id"),
                )
                if similar_packets:
                    similar_state = similar_packets[0].get("packet_state") or {}
            except Exception:
                LOGGER.warning(
                    "validate_step_prerequisites: find_similar_completed lookup failed "
                    "for step %r; continuing without Tier 2 data",
                    step_name,
                    exc_info=True,
                )

        # Tier 3: Phase B design-artifact jsonb, cached per design_id so repeat
        # *_drive_id lookups against the same design don't refetch.
        design_row_cache: Dict[str, Optional[Dict[str, Any]]] = {}

        async def _design_row(design_id: str) -> Optional[Dict[str, Any]]:
            if design_id not in design_row_cache:
                try:
                    repo = Repository("designs")
                    design_row_cache[design_id] = await asyncio.to_thread(repo.get, design_id)
                except Exception:
                    LOGGER.warning(
                        "validate_step_prerequisites: design artifact lookup failed for "
                        "design_id=%s; continuing without Tier 3 data",
                        design_id,
                        exc_info=True,
                    )
                    design_row_cache[design_id] = None
            return design_row_cache[design_id]

        async def _available(key: str) -> bool:
            # Presence check (`in`), not truthiness: a legitimately-set falsy
            # value (0, "", False) is genuinely present. This matches
            # StepContext.get_state()'s own semantics -- `packet_state.get(key,
            # default)` returns the real falsy value as-is rather than treating
            # it as absent, so a truthiness check here would be *less*
            # consistent with actual runtime behavior, not more.
            if key in packet_state:
                return True
            if key in similar_state:
                return True
            if key.endswith(_DRIVE_ID_SUFFIX):
                design_id = packet_state.get("design_id") or similar_state.get("design_id")
                if design_id:
                    design = await _design_row(design_id)
                    if design is not None:
                        artifact_type = key[: -len(_DRIVE_ID_SUFFIX)]
                        # `artifacts` is a nullable jsonb column -- backfilled/
                        # pre-existing design rows can have it as NULL, not `{}`.
                        # `.get("artifacts", {})` only substitutes the default
                        # when the KEY is absent, not when its value is None, so
                        # use `or {}` to guard against the None case too.
                        if (design.get("artifacts") or {}).get(artifact_type):
                            return True
            return False

        missing_state = [key for key in contract.consumes_state if not await _available(key)]
        missing_optional_state = [
            key for key in contract.optional_consumes_state if not await _available(key)
        ]

        completed = set(packet.get("steps_completed", []) or [])
        missing_results = [name for name in contract.consumes_results if name not in completed]

        missing_params = []
        for param in contract.params:
            if not param.required:
                continue
            if param.default is not None:
                continue
            # get_parameter_value()'s real resolution order is:
            # pending_param_overrides -> packet_inputs -> parsed_inputs ->
            # packet_state. `packet_inputs` is where params supplied at packet
            # creation time live (e.g. site_name), before they're copied into
            # packet_state -- _available() only sees packet_state-derived
            # sources, so consult packet_inputs here too or an early step would
            # be falsely reported as missing a param get_parameter_value()
            # would resolve fine at runtime. (get_state() never falls back to
            # packet_inputs, so this is deliberately params-only -- the
            # consumes_state check via _available() above is unaffected.)
            if param.name in packet_inputs:
                continue
            if await _available(param.name):
                continue
            missing_params.append(param.name)

        # Producer chain: for missing_state items, search every registered step's
        # contract for one that claims to produce that key. For missing_results
        # items, the "producer" is trivially the step itself -- no search needed.
        producer_chain: Dict[str, Tuple[str, ...]] = {}
        if missing_state:
            registry = get_step_registry()
            all_names = registry.list_handlers()
            for item in missing_state:
                producers = tuple(
                    name
                    for name in all_names
                    if name != step_name
                    and (other_contract := registry.get_contract(name)) is not None
                    and item in other_contract.produces_state
                )
                if producers:
                    producer_chain[item] = producers
        for item in missing_results:
            producer_chain[item] = (item,)

        satisfied = not missing_state and not missing_results and not missing_params

        return PrereqReport(
            step_name=step_name,
            satisfied=satisfied,
            missing_state=tuple(missing_state),
            missing_results=tuple(missing_results),
            missing_params=tuple(missing_params),
            missing_optional_state=tuple(missing_optional_state),
            producer_chain=producer_chain,
        )

    async def run_single_step(
        self,
        packet: Dict[str, Any],
        step_name: str,
        context: StepContext,
        expert_config: "ExpertConfig",
        param_overrides: Optional[Dict[str, Any]] = None,
        force: bool = False,
        run_missing_prerequisites: bool = False,
        _producer_visited: Optional[Set[str]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Execute exactly one named step out of the normal workflow order.

        Unlike `_execute_workflow_inner`'s while-loop (which walks a parsed
        workflow sequence), this runs ONE step directly by name, using
        `validate_step_prerequisites` to decide whether it's safe to run, and
        optionally auto-running producer steps first.

        v1 scope: single-site packets only; only steps registered as function
        handlers (via `@register_step`) are supported -- there's no stable
        standalone identity for an LLM step outside a parsed workflow sequence.

        This method never raises for "normal" bad-input outcomes (unknown
        step, multi-site packet, missing prerequisites) -- those are all
        regular `(message, dict)` return-tuple outcomes, matching this file's
        established convention (see `_execute_one_step`'s `action="return"`
        cases). A genuinely unexpected internal exception (e.g. a DB
        connection failure) is allowed to propagate uncaught.

        Args:
            packet: Current packet dict (as returned by WorkPacketService).
            step_name: Name of the registered function step to run.
            context: Step execution context for this packet.
            expert_config: Expert configuration (workflow/system instructions).
            param_overrides: Optional parameter overrides applied via
                `context.set_parameter_override` before execution.
            force: If True, re-run the step even if already in
                `steps_completed`, clearing its contract's `guard_keys` first
                (via `mark_step_incomplete`) so the step's own idempotency
                guard doesn't immediately no-op the re-run.
            run_missing_prerequisites: If True, attempt to auto-run producer
                steps for any missing state/result prerequisite (one pass
                only -- see `validate_step_prerequisites`'s `producer_chain`).
            _producer_visited: Internal recursion-cycle guard. Do not pass
                explicitly.

        Returns:
            A `(message, dict)` tuple describing the outcome. Notable dict
            shapes: `{"refused": True, "error": ...}` for v1-scope refusals,
            `{"already_completed": True}`, `{"needs_user_input": True, ...}`
            for unmet prerequisites, or `{"success": True, ...}` /
            whatever `_execute_one_step` itself returned for `action="return"`.
        """
        # --- Step 0: v1 guards -------------------------------------------------
        # Mirror _execute_workflow_inner's own multi-site detection exactly
        # (see the "resolve_sites" handoff in _execute_one_step).
        sites_to_process = context.get_state("sites_to_process", [])
        if len(sites_to_process) > 1:
            return (
                "run_single_step does not yet support multi-site packets.",
                {"error": "unsupported_multi_site", "refused": True},
            )

        if get_step_handler(step_name) is None:
            return (f"No such step: {step_name}", {"error": "unknown_step", "refused": True})

        # Refuse if the packet looks like it's actively being driven by a live
        # full-workflow run right now. Unlike claim_signing's 10-minute
        # process-death recovery threshold (which exists to reclaim a packet
        # stranded by a crashed process), this threshold's job is to detect a
        # workflow that is genuinely still executing: a live run touches
        # `updated_at` roughly once per step, so a short 2-minute window is
        # enough to catch an in-flight run while not misfiring on a packet
        # that merely happens to still carry `in_progress` status from a
        # normal, unremarkable earlier step. Running a step out-of-order
        # against a packet another process is actively mutating risks the
        # exact lost-update race update_state's optimistic concurrency
        # guards against, so refuse cleanly up front instead.
        if packet.get("packet_status") == "in_progress":
            from datetime import datetime, timezone

            updated_at_str = packet.get("updated_at")
            if updated_at_str:
                try:
                    updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                    age_minutes = (datetime.now(timezone.utc) - updated_at).total_seconds() / 60
                except (ValueError, TypeError):
                    age_minutes = None
                # Fail open on missing/unparseable updated_at -- a parsing
                # quirk shouldn't block all out-of-order execution.
                if age_minutes is not None and age_minutes < 2:
                    return (
                        f"Packet {packet['packet_id']} appears to be actively running "
                        f"(updated {age_minutes:.1f} min ago); refusing to run a step "
                        "out-of-order to avoid a state race.",
                        {"error": "packet_actively_running", "refused": True},
                    )

        # --- Step 1: already-completed check ------------------------------------
        steps_completed = list(packet.get("steps_completed") or [])
        if step_name in steps_completed and not force:
            return (f"Step '{step_name}' already completed.", {"already_completed": True})

        if force:
            contract = get_step_contract(step_name)
            guard_keys = list(contract.guard_keys) if contract else []
            await self.packet_service.mark_step_incomplete(
                packet["packet_id"],
                step_name,
                clear_state_keys=guard_keys,
                session_id=context.session_id,
            )
            # Refresh our view of the packet -- mark_step_incomplete just
            # mutated steps_completed/packet_state in the DB out from under us.
            packet = await self.packet_service.get_packet(packet["packet_id"])
            context.packet_state = packet.get("packet_state") or {}
            context.steps_completed = list(packet.get("steps_completed") or [])

        # --- Step 2: prerequisite check ------------------------------------------
        report = await self.validate_step_prerequisites(packet, step_name)

        if not report.satisfied:
            if run_missing_prerequisites:
                visited = (_producer_visited or set()) | {step_name}
                # Tracks producer steps already recursed into during THIS pass,
                # so that when two or more missing items resolve to the SAME
                # producer step, that producer only runs once. Without this,
                # each iteration recurses using the same stale `packet` (only
                # re-fetched once, after the whole loop) and doesn't see the
                # first iteration's completion, so the producer's handler
                # would otherwise be invoked once per missing item it satisfies.
                already_attempted_producers: Set[str] = set()
                for item in list(report.missing_state) + list(report.missing_results):
                    producers = report.producer_chain.get(item)
                    if not producers:
                        continue
                    producer_step_name = producers[0]
                    if producer_step_name in visited:
                        # Cycle guard: two steps' contracts claim to mutually
                        # produce each other's missing dependency (or a
                        # producer_chain entry maps a missing_results item
                        # back to step_name itself).
                        continue
                    if producer_step_name in already_attempted_producers:
                        # Already recursed into this producer earlier in this
                        # same pass -- skip to avoid running its handler twice.
                        continue
                    already_attempted_producers.add(producer_step_name)
                    await self.run_single_step(
                        packet,
                        producer_step_name,
                        context,
                        expert_config,
                        force=False,
                        run_missing_prerequisites=True,
                        _producer_visited=visited,
                    )

                # Re-fetch and re-validate after one pass over the producer chain.
                packet = await self.packet_service.get_packet(packet["packet_id"])
                context.packet_state = packet.get("packet_state") or {}
                context.steps_completed = list(packet.get("steps_completed") or [])
                report = await self.validate_step_prerequisites(packet, step_name)

            if not report.satisfied:
                return (
                    f"Cannot run '{step_name}': missing prerequisites.",
                    {
                        "needs_user_input": True,
                        "missing_state": list(report.missing_state),
                        "missing_results": list(report.missing_results),
                        "missing_params": list(report.missing_params),
                        "producer_chain": {k: list(v) for k, v in report.producer_chain.items()},
                    },
                )

        # --- Step 3: apply overrides and execute ---------------------------------
        for key, value in (param_overrides or {}).items():
            context.set_parameter_override(key, value)

        contract = get_step_contract(step_name)
        step = ParsedStep(
            index=0,
            step_type="function",
            name=step_name,
            description=contract.description if contract else step_name,
        )
        execution_summary = ExecutionSummary(
            packet_id=packet["packet_id"],
            packet_type=packet["packet_type"],
            total_steps=1,
            all_steps=[step],
        )

        accumulated_results = context.accumulated_results.copy()
        signal = await self._execute_one_step(
            step,
            [step],
            expert_config,
            packet,
            context,
            accumulated_results,
            execution_summary=execution_summary,
            on_progress=None,
            wf_chat_id=None,
            allow_multi_site_handoff=False,
        )

        if signal.action in ("advance", "break"):
            await self.packet_service.complete_step(
                packet["packet_id"],
                step_name,
                next_step=None,
                session_id=context.session_id,
            )
            return (
                f"Step '{step_name}' completed.",
                {
                    "success": True,
                    "step_name": step_name,
                    "final_response": signal.final_response,
                },
            )

        if signal.action == "return":
            return signal.return_value

        # signal.action == "retry": no real code path in _execute_one_step
        # currently produces this (see StepLoopSignal's own docstring / Task
        # 3a's finding) -- treat defensively rather than looping or crashing.
        return (
            f"Step '{step_name}' requested a retry, which run_single_step does not "
            "support (headless mode only).",
            {"error": "unsupported_retry", "refused": True},
        )

    def _format_result(self, result: Any) -> str:
        """Format a step result for display in prompt.

        Args:
            result: Result data from a step

        Returns:
            Formatted string (truncated if too long)
        """
        max_length = 3000

        if isinstance(result, dict):
            if "response" in result:
                text = str(result["response"])
            else:
                text = str(result)
        else:
            text = str(result)

        if len(text) > max_length:
            return text[:max_length] + "... (truncated)"
        return text


__all__ = [
    "WorkflowExecutor",
    "ParsedStep",
    "StepStatus",
    "StepExecutionRecord",
    "ExecutionSummary",
    "PrereqReport",
]
