"""Ask which prompts should use the new context module.

A module with no prompt pins renders nowhere, so this step is what makes
/learn actually take effect. Selection writes prompt_knowledge_overrides in
store_module.
"""

from typing import List, Tuple

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

NONE_WORDS = {"none", "no", "skip", "later", "nothing"}


def format_prompt_choices(choices: List[Tuple[str, str]]) -> str:
    """Numbered list of ``(prompt_id, description)`` for the user to pick from."""
    return "\n".join(f"{i}. {pid} — {desc}" for i, (pid, desc) in enumerate(choices, 1))


def parse_prompt_selection(reply: str, prompt_ids: List[str]) -> List[str]:
    """Resolve a reply of numbers and/or prompt ids to a list of prompt ids."""
    text = (reply or "").strip().lower()
    if not text or text in NONE_WORDS:
        return []
    picked: List[str] = []
    for token in (t.strip() for t in text.replace(";", ",").split(",")):
        if not token:
            continue
        if token.isdigit():
            index = int(token) - 1
            if 0 <= index < len(prompt_ids):
                picked.append(prompt_ids[index])
            continue
        for pid in prompt_ids:
            if token == pid.lower() and pid not in picked:
                picked.append(pid)
    return picked


@register_step("select_prompts")
async def select_prompts(context: StepContext) -> StepResult:
    """Present the prompt list, then record the user's picks on the reply turn."""
    if context.get_state("awaiting_prompt_selection") and context.user_input:
        known = context.get_state("prompt_choice_ids") or []
        selected = parse_prompt_selection(context.user_input, known)
        LOGGER.info(f"Context module will be pinned to: {selected or '(none)'}")
        return StepResult(
            data={"prompt_ids": selected},
            state_updates={
                "awaiting_prompt_selection": False,
                "module_prompt_ids": selected,
            },
        )

    from shared.prompts import PROMPTS

    choices = [(pid, PROMPTS.spec(pid).description) for pid in sorted(PROMPTS.ids())]
    prompt_ids = [pid for pid, _ in choices]

    return StepResult(
        state_updates={
            "prompt_choice_ids": prompt_ids,
            "awaiting_prompt_selection": True,
        },
        needs_user_input=True,
        user_prompt=(
            "Which prompts should use this context module?\n\n"
            f"{format_prompt_choices(choices)}\n\n"
            "Reply with numbers (e.g. `1, 3`), prompt ids, or `none` to decide later."
        ),
    )
