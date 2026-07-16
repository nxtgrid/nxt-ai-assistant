"""Process Google Doc edits from @anansibot comments or chat instructions.

Two modes:
1. Comment-driven: processes all unresolved @anansibot mentions in the doc
2. Instruction-driven: edits a section identified from a chat instruction
   (uses markdown converter for section identification, errors if < 80% confidence)

Delegates to shared functions in shared.utils.doc_editing for comment scanning,
replacement generation, verification, section editing, and revision pinning.
"""

import json
import logging
from typing import Any, Dict

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step

LOGGER = logging.getLogger(__name__)

# Maximum edits per run (cost/rate limit protection)
MAX_EDITS_PER_RUN = 10


async def _identify_section(markdown: str, instruction: str) -> Dict[str, Any]:
    """Use LLM to identify which section of the document to edit.

    Returns: {"text": "matched section text", "confidence": 0.0-1.0, "reasoning": "..."}
    """
    from orchestrator.config.settings import get_settings
    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

    settings = get_settings()
    gateway = get_default_generation_gateway(
        default_model=settings.gemini.model,
    )

    prompt = f"""Given this document (in markdown) and an edit instruction, identify the EXACT
text section that should be edited. Return JSON only.

INSTRUCTION: {instruction}

DOCUMENT:
{markdown[:8000]}

Return JSON:
{{
    "text": "the exact text from the document that should be edited (copy verbatim)",
    "confidence": 0.0 to 1.0 (how confident you are this is the right section),
    "reasoning": "why this section was selected"
}}

Rules:
- The "text" must be an EXACT substring of the document (character-for-character match)
- If you cannot identify a specific section, set confidence to 0.0
- If the instruction is ambiguous, set confidence below 0.5
- Pick the smallest text range that covers the edit target
"""

    response = await gateway.generate(
        [LLMMessage(role="user", text=prompt)],
        GenerationOptions(
            model=settings.gemini.model,
            temperature=0.1,
            max_output_tokens=1000,
        ),
    )

    try:
        text = response.text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
        return {
            "text": result.get("text", ""),
            "confidence": float(result.get("confidence", 0)),
            "reasoning": result.get("reasoning", ""),
        }
    except (json.JSONDecodeError, ValueError, IndexError) as e:
        LOGGER.warning(f"Could not parse section identification response: {e}")
        return {"text": "", "confidence": 0.0, "reasoning": f"Parse error: {e}"}


@register_step("process_doc_edits")
async def process_doc_edits(context: StepContext) -> StepResult:
    """Process Google Doc edits from comments or instructions.

    Delegates to shared functions in shared.utils.doc_editing.
    """
    from shared.utils.doc_editing import (
        edit_section,
        generate_replacement_markdown,
        pin_revision,
        scan_comments,
    )

    doc_id = context.get_input("document_id")
    if not doc_id:
        return StepResult(error="document_id is required")

    instruction = context.get_input("instruction") or ""

    if instruction:
        # ── MODE 2: Instruction-driven ──
        await context.send_progress_to_user("Analyzing document to find target section...")

        from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown

        markdown = fetch_google_doc_markdown(doc_id)
        if not markdown:
            return StepResult(error="Could not fetch document as markdown.")

        section_match = await _identify_section(markdown, instruction)
        LOGGER.info(
            f"Section identification: confidence={section_match['confidence']:.0%}, "
            f"reasoning={section_match['reasoning'][:100]}"
        )

        if section_match["confidence"] < 0.8:
            return StepResult(
                error=f"Could not identify which section to edit "
                f"(confidence: {section_match['confidence']:.0%}). "
                f"Reason: {section_match['reasoning']}. "
                f"Please be more specific or highlight the section in a Google Doc comment."
            )

        target_text = section_match["text"]

        replacement = await generate_replacement_markdown(
            instruction=instruction,
            highlighted_text=target_text,
            section_context=markdown[:1500],
            expert_context=context.packet_state,
            user_email=context.effective_email,
        )

        await pin_revision(doc_id)

        result = await edit_section(
            doc_id=doc_id,
            target_text=target_text,
            replacement_markdown=replacement,
        )

        if result.get("success"):
            return StepResult(
                data={"edits": 1, "elements_written": result.get("elements_written", 0)},
                progress_message="Edited 1 section",
            )
        else:
            return StepResult(error=result.get("error", "Edit failed"))

    else:
        # ── MODE 1: Comment-driven ──
        await context.send_progress_to_user("Checking for @anansibot comments...")

        comments = await scan_comments(doc_id)
        if not comments:
            return StepResult(
                data={"edits": 0},
                progress_message="No pending @anansibot comments found.",
            )

        if len(comments) > MAX_EDITS_PER_RUN:
            LOGGER.warning(f"Capping edits from {len(comments)} to {MAX_EDITS_PER_RUN}")
            comments = comments[:MAX_EDITS_PER_RUN]

        # Process in reverse order so earlier edits don't shift positions
        # of later target text. Comments are returned in creation order;
        # reversing approximates bottom-to-top document order.
        comments = list(reversed(comments))

        await context.send_progress_to_user(f"Processing {len(comments)} edit(s)...")

        requester = context.effective_email or "unknown"
        LOGGER.info(f"Doc editor: {requester} editing doc {doc_id} ({len(comments)} edits)")

        # Pin revision once before the batch
        await pin_revision(doc_id)

        results = []
        for comment in comments:
            highlighted = comment["highlighted_text"]
            comment_instruction = comment["instruction"]
            comment_id = comment["comment_id"]

            if not highlighted:
                results.append({"comment_id": comment_id, "status": "skipped"})
                continue

            try:
                replacement = await generate_replacement_markdown(
                    instruction=comment_instruction,
                    highlighted_text=highlighted,
                    expert_context=context.packet_state,
                    user_email=context.effective_email,
                )

                result = await edit_section(
                    doc_id=doc_id,
                    target_text=highlighted,
                    replacement_markdown=replacement,
                    comment_id=comment_id,
                )

                if result.get("success"):
                    results.append({"comment_id": comment_id, "status": "done"})
                else:
                    results.append(
                        {"comment_id": comment_id, "status": "failed", "error": result.get("error")}
                    )

            except Exception as e:
                LOGGER.error(f"Edit failed for comment {comment_id}: {e}")
                from shared.utils.error_messages import sanitize_error_for_user

                results.append(
                    {
                        "comment_id": comment_id,
                        "status": "failed",
                        "error": sanitize_error_for_user(str(e)),
                    }
                )

        succeeded = sum(1 for r in results if r["status"] == "done")
        failed = sum(1 for r in results if r["status"] == "failed")

        return StepResult(
            data={"edit_results": results, "succeeded": succeeded, "failed": failed},
            progress_message=f"Edited {succeeded} section(s)"
            + (f", {failed} failed" if failed else ""),
        )
