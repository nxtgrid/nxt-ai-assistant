"""Auto-generate a skill's catalog summary from its step list.

Phase 3 of docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 4:
"On save, generate `summary` with a single LLM call from the step list.
Author can edit it. Keep it under 200 chars -- it goes into every request's
context." This module is the generation half; there is no "save a skill"
caller yet (that's Phase 4's builder) -- this is ready for it to call.

Uses shared.llm's gateway directly (LLMMessage/GenerationOptions), the same
pattern gtr_analysis_conversation.py already uses for a standalone LLM call
outside the main conversation graph, rather than routing through
WorkflowExecutor -- there is no workflow step, packet, or tool loop here,
just one summarization call.
"""

from __future__ import annotations

from typing import Any, Dict, List

from orchestrator.config.settings import get_settings
from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

MAX_SUMMARY_CHARS = 200


def _truncate_at_word_boundary(text: str, limit: int) -> str:
    """Cut text to at most `limit` chars, backing up to the last space.

    A safety net for when the model doesn't respect the length instruction
    exactly -- prefer a clean word boundary over a mid-word chop.
    """
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    last_space = clipped.rfind(" ")
    if last_space > limit * 0.6:  # Only back up if it doesn't lose too much
        clipped = clipped[:last_space]
    return clipped.rstrip(" ,.;:-")


def _build_summary_prompt(steps: List[Dict[str, Any]], title: str) -> str:
    lines = []
    for i, step in enumerate(steps):
        # A P3 [function] step has no instruction text (SkillStepPayload's
        # instruction is Optional and empty/None for it) -- fall back to its
        # handler name so the line describes something instead of reading
        # blank or the literal string "None".
        line = f"{i + 1}. {step.get('instruction') or step.get('handler') or ''}"
        # result_preview is the builder's own capture of what the step's
        # tools actually returned (skill_builder.py's _step_response_text,
        # truncated) -- present only for a live-built step, not one loaded
        # from validate-only payloads. Folding it in lets the summary name
        # the kind of data retrieved instead of just paraphrasing intent.
        result_preview = (step.get("result_preview") or "").strip()
        if result_preview:
            line += f"\n   Result: {result_preview}"
        lines.append(line)
    step_lines = "\n".join(lines)
    header = f"Skill title: {title}\n\n" if title else ""
    return (
        f"{header}Summarize what this procedure accomplishes, in ONE sentence "
        f"under {MAX_SUMMARY_CHARS} characters. Describe the outcome, not a "
        f"step-by-step recap. Where a step shows a Result, let it sharpen the "
        f"summary -- e.g. naming the kind of data actually retrieved, not just "
        f"the intent. This will be shown to an AI assistant deciding whether "
        f"the procedure is relevant to a request, not to the person who wrote "
        f"it.\n\n"
        f"Steps:\n{step_lines}\n\n"
        f"Respond with the summary sentence only -- no preamble, no quotes."
    )


async def generate_skill_summary(steps: List[Dict[str, Any]], title: str = "") -> str:
    """Generate a catalog summary for a skill from its step list.

    Single LLM call, truncated to MAX_SUMMARY_CHARS as a safety net. Returns
    "" for an empty step list rather than calling the LLM for nothing. The
    caller (Phase 4's save flow) presents this as an editable starting
    point, not a final value -- never treat this as authoritative without
    the author having seen it.
    """
    if not steps:
        return ""

    prompt = _build_summary_prompt(steps, title)
    settings = get_settings()
    gateway = get_default_generation_gateway()
    options = GenerationOptions(model=settings.gemini.model, max_output_tokens=200)

    try:
        result = await gateway.generate([LLMMessage(role="user", text=prompt)], options)
    except Exception as e:
        LOGGER.warning(f"Skill summary generation failed: {e}")
        return ""

    summary = (result.text or "").strip().strip('"')
    return _truncate_at_word_boundary(summary, MAX_SUMMARY_CHARS)


__all__ = ["MAX_SUMMARY_CHARS", "generate_skill_summary"]
