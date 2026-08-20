"""Bridge from a saved skill to a runnable expert workflow.

Phase 5 of docs/superpowers/plans/2026-08-06-user-designed-skills.md.

expert_router.py routes any request carrying metadata.skill_id +
metadata.scheduled_execution straight to expert_handler.py with
matched_expert_id="skill:<uuid>", bypassing NL/command matching entirely --
a scheduled/triggered run already knows exactly which skill to execute.
expert_handler.py delegates to run_skill_packet() here as soon as it sees
that marker, before it would otherwise call
ExpertInstructionsProvider.get_expert_config(), which has no notion of
skills at all.

Deliberately does NOT reuse expert_handler.py's _create_new_packet (built
for Google-Doc experts: LPP-specific packet_inputs, auto-cancelling
superseded packets, slash-command parsing) or its resume/parameter-
confirmation/cancellation paths -- a skill run is always a single, linear,
unattended pass with nobody present to resume or confirm anything mid-run.
It DOES reuse expert_handler.py's _build_step_context (generic enough to
need no changes) and mirrors its tool_executor construction.

Every request that reaches run_skill_packet() is, by construction, a
scheduled/triggered run (the only path into it requires
metadata.scheduled_execution=true -- see expert_router.py) -- builder-mode
interactive chat never sets metadata.skill_id and never reaches this
module. So the run-mode delivery buffer (_ResponseBuffer) is always active
here; there is no "builder mode inside skill_runner.py" case to gate around.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from orchestrator.clients.factory import create_chat_llm_client
from orchestrator.config.settings import get_settings
from orchestrator.experts.workflow_executor import (
    ParsedStep,
    StepExecutionRecord,
    WorkflowExecutor,
)
from orchestrator.graphs.state import ConversationState
from orchestrator.services.supabase_client import get_supabase_client
from orchestrator.services.work_packet_service import WorkPacketService
from shared.utils.logging import get_logger
from shared.utils.telegram_send import send_telegram_message

LOGGER = get_logger(__name__)

SKILL_EXPERT_PREFIX = "skill:"
SKILL_PACKET_TYPE = "skill_run"


def is_skill_expert_id(expert_id: Optional[str]) -> bool:
    """Whether matched_expert_id names a skill run rather than a real
    Google-Doc expert_id -- see expert_router.py's skill-run branch."""
    return bool(expert_id) and expert_id.startswith(SKILL_EXPERT_PREFIX)


def _skill_id_from_expert_id(expert_id: str) -> str:
    return expert_id[len(SKILL_EXPERT_PREFIX) :]


@dataclass
class _SyntheticExpertConfig:
    """Minimal ExpertConfig stand-in for a skill run.

    execute_workflow only reads .system_instructions (the LLM step's system
    prompt -- skill steps carry their own instructions inline via
    is_skill_step, so this stays empty) and .display_name/.model. .tools
    and .get_workflow are never consulted for a skill run (pre_parsed_steps
    bypasses get_workflow entirely; per-step tool filtering is
    skill_step_bindings.filter_tools_for_step, driven by each ParsedStep's
    own allow_write, not this object) -- both are still implemented so
    anything that duck-types a real ExpertConfig doesn't break.
    """

    expert_id: str
    display_name: str
    system_instructions: str = ""
    tools: List[str] = field(default_factory=list)
    model: Optional[str] = None

    def get_workflow(self, packet_type: str) -> List[str]:
        return []


def _step_mock_override(step: Dict[str, Any]) -> Optional[bool]:
    """`step["mock"]` (Phase 5 of docs/superpowers/plans/2026-08-20-expert-
    steps-as-skill-tools.md), preserving the three-way None/True/False
    distinction -- unlike `allow_write`'s `bool(step.get(..., False))`
    coercion, absent here must stay `None` ("no per-step override, defer to
    this run's StepContext.dry_run baseline"), not collapse to `False`
    ("explicitly always run for real"). See `ParsedStep.mock`'s docstring.
    """
    value = step.get("mock")
    return bool(value) if value is not None else None


def build_parsed_steps(skill_steps: List[Dict[str, Any]]) -> List[ParsedStep]:
    """Convert a skill's stored steps (skills.steps jsonb -- see
    skill_validation.py's module docstring for the shape) into ParsedStep
    objects, preserving is_skill_step/allow_write/is_response_step/mock.
    Going through expert_config.get_workflow() + parse_workflow()'s doc-text
    parser instead cannot do this -- see execute_workflow's pre_parsed_steps
    docstring for why.

    A step's `kind` is "llm" (the default -- every step predating P3's
    function steps omits it, so this must not change their behaviour) or
    "function", naming a handler the builder's step picker exposed (see
    step_registry.py's exposed_to_builder). A function step's own handler
    brings its own tool access, so is_skill_step (which unlocks {{var}}
    binding and read-only tool gating for [llm] steps) stays False for it.
    `mock` (Phase 5) is meaningful for both kinds: it pins a `kind:"function"`
    step's own mocked-ness directly, and pins an `[llm]` step's OWN mocked-
    ness for any function-step tool it invokes mid-loop (see
    WorkflowExecutor._execute_llm_step's mock_override threading).

    The final step is always forced is_response_step=True even if not
    explicitly flagged by its author, per the plan's "Run-mode output"
    section: "The final step is always treated as an implicit response
    step... so a skill with zero flagged steps still delivers exactly one
    message." -- true for both kinds.
    """
    ordered = sorted(skill_steps, key=lambda s: s.get("index", 0))
    parsed: List[ParsedStep] = []
    for i, step in enumerate(ordered):
        is_last = i == len(ordered) - 1
        kind = step.get("kind") or "llm"

        if kind == "function":
            parsed.append(
                ParsedStep(
                    index=i,
                    step_type="function",
                    name=step.get("handler") or f"step_{i + 1}",
                    description=step.get("instruction") or "",
                    is_skill_step=False,
                    is_response_step=is_last or bool(step.get("is_response_step", False)),
                    mock=_step_mock_override(step),
                )
            )
            continue

        parsed.append(
            ParsedStep(
                index=i,
                step_type="llm",
                name=step.get("name") or f"step_{i + 1}",
                description=step.get("instruction") or "",
                is_skill_step=True,
                allow_write=bool(step.get("allow_write", False)),
                is_response_step=is_last or bool(step.get("is_response_step", False)),
                mock=_step_mock_override(step),
            )
        )
    return parsed


class _ResponseBuffer:
    """Run-mode delivery (Phase 5, item 8): sends one Telegram message each
    time a response step (or the final step) completes, prefixed with a
    templated join of the buffered summaries of every step since the last
    send. See the plan's "Run-mode output: which steps talk to the user".

    Prefers the free, already-computed StepExecutionRecord.result_summary
    for buffered (non-response) steps -- no extra LLM call, matching the
    plan's explicit cost guidance ("prefer free, not another LLM call").
    The response step itself sends its full response text, not just its
    summary.
    """

    def __init__(
        self, bot_token: str, chat_id: str, topic_id: Optional[str], dry_run: bool = False
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._topic_id = topic_id
        self._buffered_summaries: List[str] = []
        self.messages_sent = 0
        # R6 (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md):
        # the chat response surface must say "mocked" too -- a mocked BOM or
        # signature request reading as real in the delivered Telegram
        # message is the worst failure that feature can produce.
        self._dry_run = dry_run

    async def on_step_complete(
        self, step: ParsedStep, record: StepExecutionRecord, final_response: Optional[str]
    ) -> None:
        if not step.is_response_step:
            self._buffered_summaries.append(record.result_summary or step.description)
            return

        response_text = final_response or record.result_summary or step.description
        if self._buffered_summaries:
            summary_block = "\n".join(f"• {s}" for s in self._buffered_summaries)
            text = f"{summary_block}\n\n{response_text}"
        else:
            text = response_text

        if self._dry_run:
            text = f"🧪 MOCK RUN — mutating steps were mocked, not performed for real.\n\n{text}"

        if not self._bot_token:
            LOGGER.warning("Skill run: TELEGRAM_BOT_TOKEN not set, cannot deliver response step")
        else:
            await send_telegram_message(
                self._bot_token, self._chat_id, text, topic_id=self._topic_id
            )
            self.messages_sent += 1
        self._buffered_summaries = []


async def run_skill_packet(
    state: ConversationState,
    expert_id: str,
    packet_service: WorkPacketService,
) -> Dict[str, Any]:
    """Create and execute a skill run packet. The skill-run counterpart to
    expert_handler.py's Google-Doc-expert new-packet-then-execute flow --
    see this module's docstring for why it doesn't reuse that flow directly.

    Returns a dict shaped like expert_handler.py's own return values
    (final_response/active_work_packet/expert_executed/expert_error) so its
    caller needs no special-casing -- but note the *user-facing delivery*
    for a successful run already happened via direct Telegram sends inside
    execute_workflow's on_step_complete hook (see _ResponseBuffer); the
    caller of expert_handler.py (the scheduler/dispatcher) must not re-send
    final_response itself, only use it for logging/run-history.
    """
    skill_id = _skill_id_from_expert_id(expert_id)
    user_context = state.get("user_context")
    session_id = state.get("session_id")

    supabase = get_supabase_client()
    skill = await supabase.get_skill(skill_id)
    if not skill:
        LOGGER.error(f"Skill run: skill {skill_id} not found")
        return {
            "expert_error": f"Skill not found: {skill_id}",
            "final_response": None,
            "expert_executed": False,
        }

    if skill.get("status") != "active":
        LOGGER.warning(f"Skill run: skill {skill_id} is {skill.get('status')}, not active")
        return {
            "expert_error": f"Skill is {skill.get('status')}",
            "final_response": None,
            "expert_executed": False,
        }

    steps = build_parsed_steps(skill.get("steps") or [])
    if not steps:
        LOGGER.error(f"Skill run: skill {skill_id} has no steps")
        return {
            "expert_error": "Skill has no steps",
            "final_response": None,
            "expert_executed": False,
        }

    expert_config = _SyntheticExpertConfig(
        expert_id=expert_id,
        display_name=skill.get("title") or skill_id,
    )

    org_id = None
    if user_context and user_context.organization_ids:
        org_id = int(user_context.organization_ids[0])

    metadata = state.get("metadata") or {}
    skill_inputs = metadata.get("skill_inputs") or {}
    # dry_run: Phase 5 of docs/superpowers/plans/2026-08-20-expert-steps-as-
    # skill-tools.md's run-wide mock-mode baseline (StepContext.dry_run) --
    # read from metadata rather than a new parameter on this function so
    # whatever eventually triggers a "run this skill mocked" request (Phase
    # 11 -- nothing sets this key yet) only needs to add it to the SAME
    # metadata dict skill_inputs already flows through, with no change
    # needed here or at this function's one real caller
    # (expert_handler.py). False (the default -- every request today,
    # scheduled or triggered) preserves current behavior exactly: every
    # mutating step runs for real, same as before this field existed.
    dry_run = bool(metadata.get("dry_run", False))

    title_prefix = "[MOCK RUN] " if dry_run else ""
    packet = await packet_service.create_packet(
        packet_type=SKILL_PACKET_TYPE,
        packet_title=(
            f"{title_prefix}{skill.get('title')}: "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        ),
        packet_goal=skill.get("summary") or skill.get("title") or skill_id,
        assigned_expert=expert_id,
        packet_inputs=skill_inputs,
        session_id=session_id,
        requested_by_email=skill.get("created_by"),
        organization_id=org_id,
    )
    packet = await packet_service.start_packet(
        packet["packet_id"], first_step=steps[0].name, session_id=session_id
    )

    settings = get_settings()
    try:
        gemini = create_chat_llm_client(settings, settings.gemini)
    except Exception as e:
        LOGGER.error(f"Skill run: failed to create LLM client: {e}")
        await packet_service.fail_packet(packet["packet_id"], f"LLM client error: {e}", session_id)
        return {
            "expert_error": f"LLM client error: {e}",
            "final_response": None,
            "active_work_packet": packet,
            "expert_executed": False,
        }

    tool_executor = state.get("tool_executor")
    if not tool_executor:
        try:
            from orchestrator.services.tool_executor import ToolExecutor
            from orchestrator.services.tool_registry import ToolRegistry

            registry = ToolRegistry(settings)
            tool_executor = ToolExecutor(
                registry,
                settings,
                default_metadata=_skill_tool_executor_metadata(state, packet),
            )
        except Exception as e:
            LOGGER.warning(f"Skill run: tool executor unavailable, steps get no tool access: {e}")
            tool_executor = None

    from orchestrator.graphs.nodes.expert_handler import _build_step_context

    step_context = _build_step_context(
        state=state, packet=packet, expert_config=expert_config, tool_executor=tool_executor
    )
    step_context.dry_run = dry_run

    buffer = _ResponseBuffer(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        chat_id=user_context.chat_id if user_context else "",
        topic_id=user_context.topic_id if user_context else None,
        dry_run=dry_run,
    )

    executor = WorkflowExecutor(
        gemini_client=gemini, packet_service=packet_service, mcp_executor=tool_executor
    )

    try:
        final_response, extra_state = await executor.execute_workflow(
            expert_config=expert_config,
            packet=packet,
            context=step_context,
            pre_parsed_steps=steps,
            on_step_complete=buffer.on_step_complete,
        )
    except Exception as e:
        LOGGER.exception(f"Skill run {skill_id} failed: {e}")
        await packet_service.fail_packet(
            packet["packet_id"],
            f"Skill run error: {e}",
            session_id,
            error_state={
                "last_error": str(e),
                "error_step": step_context.current_step,
                "error_time": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {
            "expert_error": str(e),
            "final_response": None,
            "active_work_packet": packet,
            "expert_executed": False,
            "skill_messages_sent": buffer.messages_sent,
        }

    if extra_state.get("error"):
        LOGGER.warning(f"Skill run {skill_id} step failed: {extra_state.get('error')}")
        return {
            "expert_error": extra_state.get("error"),
            "final_response": final_response,
            "active_work_packet": packet,
            "expert_executed": False,
            "skill_messages_sent": buffer.messages_sent,
        }

    return {
        "final_response": final_response,
        "active_work_packet": packet,
        "expert_executed": True,
        "skill_messages_sent": buffer.messages_sent,
    }


def _skill_tool_executor_metadata(
    state: ConversationState, packet: Dict[str, Any]
) -> Dict[str, Any]:
    """Mirrors expert_handler.py's _build_tool_executor_metadata -- kept as
    a separate, smaller copy rather than imported, since that function also
    reads expert-workflow-specific packet fields (packet_id-keyed
    View-State button wiring) a skill run packet doesn't have. Only the
    auth-relevant subset a skill step's tool calls actually need.
    """
    metadata: Dict[str, Any] = dict(state.get("metadata", {}))
    user_context = state.get("user_context")
    if user_context:
        metadata.update(
            {
                "user_email": user_context.user_email,
                "organization_ids": user_context.organization_ids,
                "grid_ids": user_context.grid_ids,
                "meter_ids": user_context.meter_ids,
                "is_admin": user_context.is_admin,
                "is_staff": user_context.is_staff,
            }
        )
    metadata["packet_id"] = packet.get("packet_id")
    return metadata


__all__ = [
    "SKILL_EXPERT_PREFIX",
    "SKILL_PACKET_TYPE",
    "build_parsed_steps",
    "is_skill_expert_id",
    "run_skill_packet",
]
