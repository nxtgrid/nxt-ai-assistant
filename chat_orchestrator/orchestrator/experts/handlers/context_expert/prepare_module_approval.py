"""Show the operator exactly what will be stored, before it is stored.

Handles both turns itself, like detect_module_duplicates: ask on the first
invocation, interpret the reply on the second (an awaiting_module_approval
flag in packet_state distinguishes them). This is required, not stylistic --
a step that returns needs_user_input=True is re-invoked itself on the next
turn (it never gets added to steps_completed, so the workflow executor's
resume logic lands back on the same step index), so store_module would never
see an approval reply if this step just asked unconditionally every time.
"""

from typing import List

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

APPROVE_WORDS = {"save it", "save", "yes", "approve", "approved", "ok", "1"}
CANCEL_WORDS = {"cancel", "no", "skip", "abort", "quit", "exit", "stop", "2"}


def build_approval_text(
    slug: str, title: str, summary: str, body: str, prompt_ids: List[str]
) -> str:
    """Approval summary for a proposed context module.

    No mode line: every attached module is inlined in full, so there is no
    longer a choice to report here.
    """
    lines = [
        "**Ready to save this context module**",
        "",
        f"**Title:** {title}",
        f"**Slug:** `{slug}`",
        f"**Summary:** {summary}",
        f"**Size:** {len(body)} chars",
        "",
    ]
    if prompt_ids:
        lines.append(
            f"**Used by:** {', '.join(prompt_ids)} — inlined in full into each."
        )
    else:
        lines.append(
            "⚠️ This module is **not attached to any prompt**, so the bot will not see it "
            "until you attach it on the Context page."
        )
    return "\n".join(lines)


@register_step("prepare_module_approval")
async def prepare_module_approval(context: StepContext) -> StepResult:
    """Present the proposed module; on the reply turn, gate whether store_module runs."""
    if context.get_state("awaiting_module_approval") and context.user_input:
        response = context.user_input.strip().lower()

        if response in CANCEL_WORDS:
            LOGGER.info("User cancelled the context module before saving")
            return StepResult(
                state_updates={"awaiting_module_approval": False},
                skip_remaining=True,
                progress_message="Cancelled — nothing was saved.",
            )

        if response in APPROVE_WORDS:
            # No needs_user_input here: the workflow must advance to store_module.
            return StepResult(state_updates={"awaiting_module_approval": False})

        return StepResult(
            needs_user_input=True,
            user_prompt="Reply 'save it' to save, or 'cancel' to discard.",
            inline_options=["Save it", "Cancel"],
        )

    text = build_approval_text(
        slug=context.get_state("module_slug") or "",
        title=context.get_state("module_title") or "",
        summary=context.get_state("module_summary") or "",
        body=context.get_state("module_body") or "",
        prompt_ids=context.get_state("module_prompt_ids") or [],
    )
    return StepResult(
        state_updates={"awaiting_module_approval": True},
        needs_user_input=True,
        user_prompt=text,
        inline_options=["Save it", "Cancel"],
    )
