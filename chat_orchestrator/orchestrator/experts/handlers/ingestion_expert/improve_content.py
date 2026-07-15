"""Improve content step handler for Document Ingestion Expert.

For manual text input (interactive / inline_text modes), this handler:
1. Evaluates content quality via LLM
2. Offers an iterative improvement loop (accept / modify / use original)
3. Auto-generates a document title including the uploader's name

Passthrough for Google Drive documents (quality is assumed from the source).
"""

import json
import os
from typing import Optional

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.llm import GeminiGateway, GenerationOptions, LLMMessage
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

CANCEL_WORDS = {"cancel", "skip", "abort", "quit", "exit", "stop", "no"}

MAX_QUALITY_ITERATIONS = 3

# --- LLM Prompts ---

QUALITY_EVAL_PROMPT = """You are a content quality evaluator for a **{doc_type}** knowledge base document.

Evaluate whether this content is well-structured and ready for storage.

Guidelines by type:
- **support_example**: Should have clear user question and agent response. Conversation flow should be easy to follow.
- **sop**: Should have numbered steps or a checklist. Steps should be actionable.
- **faq**: Should have clear question-answer pairs. Questions should be distinct.
- **technical**: Should have clear sections, consistent formatting, and accurate terminology.
- **policy**: Should have clear rules/guidelines with scope and applicability stated.

General quality checks:
- Grammar and spelling are acceptable
- Content is organized with headers or clear sections where appropriate
- No excessive repetition or filler text
- Key information is easy to find

Document content:
---
{content}
---

Return a JSON object:
{{
  "is_good": true/false,
  "reasoning": "Brief explanation of your assessment",
  "suggested_version": "If is_good is false, provide an improved version preserving all factual content. Fix only structure, grammar, and formatting. If is_good is true, set to empty string."
}}"""

MODIFICATION_PROMPT = """You have the original content and a previous suggested version for a **{doc_type}** knowledge base document.

Original content:
---
{original}
---

Previous suggested version:
---
{suggestion}
---

The user wants these changes: {user_instructions}

Produce a new version incorporating the user's feedback. Preserve all factual content.
Return ONLY the improved document text, no JSON wrapping or explanation."""

NAMING_PROMPT = """Generate a descriptive title for this {doc_type} knowledge base document.

The title MUST be between 5 and 14 words. It should summarize the document's topic and purpose.

Content preview:
---
{content_preview}
---

Uploaded by: {uploader_name}

Return ONLY the title text. Include the uploader's name at the end, like "... by [Name]".
Do NOT return a title shorter than 5 words."""


async def _call_gemini(
    prompt: str, json_output: bool = False, max_tokens: int = 2048
) -> Optional[str]:
    """Call Gemini Flash for content improvement tasks."""
    try:
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        gateway = GeminiGateway(api_key=os.getenv("GOOGLE_API_KEY"), default_model=model)

        response = await gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(
                model=model,
                temperature=0.3,
                max_output_tokens=max_tokens,
                response_format="json" if json_output else None,
            ),
        )

        if not response.text:
            LOGGER.warning("Gemini returned empty response for content improvement")
            return None

        result: str = response.text.strip()
        return result

    except Exception as e:
        LOGGER.exception(f"Gemini call failed in improve_content: {e}")
        return None


async def _lookup_uploader_name(email: Optional[str]) -> str:
    """Look up the uploader's full name from the auth database.

    Falls back to email prefix if DB lookup fails.
    """
    if not email:
        return "Unknown"

    fallback = email.split("@")[0].replace(".", " ").title()

    try:
        import asyncpg

        conn = await asyncpg.connect(
            host=os.getenv("AUTH_DB_HOST"),
            port=int(os.getenv("AUTH_DB_PORT", "6543")),
            database=os.getenv("AUTH_DB_NAME", "postgres"),
            user=os.getenv("AUTH_DB_USER"),
            password=os.getenv("AUTH_DB_PASSWORD"),
            ssl="require",
            statement_cache_size=0,
        )
        try:
            row = await conn.fetchrow(
                "SELECT full_name FROM public.accounts WHERE email = $1 AND deleted_at IS NULL",
                email,
            )
            if row and row["full_name"]:
                name: str = row["full_name"]
                return name
        finally:
            await conn.close()

    except Exception as e:
        LOGGER.warning(f"Auth DB lookup failed for uploader name: {e}")

    return fallback


def _build_fallback_title(content: str, doc_type: str, uploader_name: str) -> str:
    """Build a descriptive fallback title from content when LLM fails or returns too short."""
    # Take meaningful words from the content (skip markdown formatting)
    words = []
    for word in content.split():
        clean = word.strip("#*_-|>[]():`")
        if clean and len(clean) > 1:
            words.append(clean)
        if len(words) >= 8:
            break

    doc_type_label = doc_type.replace("_", " ").title()
    if len(words) >= 5:
        return f"{doc_type_label}: {' '.join(words[:8])}... by {uploader_name}"
    return f"{doc_type_label} Document Submitted by {uploader_name}"


async def _auto_generate_title(content: str, doc_type: str, uploader_name: str) -> str:
    """Generate a document title using LLM.

    Ensures the title is at least 5 words. Retries once if too short,
    then falls back to a content-derived title.
    """
    prompt = NAMING_PROMPT.format(
        doc_type=doc_type,
        content_preview=content[:1000],
        uploader_name=uploader_name,
    )

    title = await _call_gemini(prompt, json_output=False, max_tokens=128)

    # Strip quotes that LLMs sometimes wrap titles in
    if title:
        title = title.strip().strip('"').strip("'").strip()

    # Validate minimum word count
    if title and len(title.split()) >= 5:
        return title

    # LLM returned too-short title or failed - use content-derived fallback
    LOGGER.warning(f"Title too short or empty ({title!r}), using fallback")
    return _build_fallback_title(content, doc_type, uploader_name)


@register_step("improve_content")
async def improve_content(context: StepContext) -> StepResult:
    """Quality check and title generation for manual text input.

    For Google Drive documents, this is a passthrough.
    For manual input (interactive/inline_text), runs quality evaluation,
    optional iterative improvement, and auto-naming.
    """
    input_mode = context.get_state("input_mode")

    # --- Passthrough for Google Drive documents ---
    if input_mode not in ("interactive", "inline_text"):
        return StepResult(
            data={}, progress_message="Skipping content improvement (Google Drive document)"
        )

    # --- Resume: quality decision (accept / modify / use original) ---
    if context.get_state("awaiting_quality_decision") and context.user_input:
        user_response = context.user_input.strip().lower()

        if user_response in CANCEL_WORDS:
            LOGGER.info("User cancelled quality improvement")
            return StepResult(
                skip_remaining=True,
                progress_message="Ingestion cancelled.",
            )

        if user_response == "1":
            # Accept suggested version
            suggested = context.get_state("suggested_content") or ""
            content = suggested if suggested else context.get_state("document_content")
            return await _finalize_content(context, content)

        elif user_response == "2":
            # User wants to modify further
            iteration = context.get_state("quality_iteration_count") or 0
            if iteration >= MAX_QUALITY_ITERATIONS:
                LOGGER.info("Max quality iterations reached, accepting current suggestion")
                suggested = context.get_state("suggested_content") or context.get_state(
                    "document_content"
                )
                return await _finalize_content(context, suggested)

            return StepResult(
                state_updates={
                    "awaiting_quality_decision": False,
                    "awaiting_modification_input": True,
                },
                needs_user_input=True,
                user_prompt=(
                    "What changes would you like?\n\n"
                    "Describe what to fix (e.g., 'add more detail to step 3', "
                    "'make the tone more formal', 'restructure as Q&A').\n\n"
                    "Reply `cancel` to abort."
                ),
            )

        elif user_response == "3":
            # Use original content
            original = context.get_state("original_user_content") or context.get_state(
                "document_content"
            )
            return await _finalize_content(context, original)

        elif user_response == "4":
            # Cancel ingestion
            LOGGER.info("User cancelled ingestion via option 4")
            return StepResult(
                skip_remaining=True,
                progress_message="Ingestion cancelled.",
            )

        else:
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "Please reply with 1, 2, 3, or 4:\n\n"
                    "1. Accept suggestion\n"
                    "2. Modify further\n"
                    "3. Use original\n"
                    "4. Cancel ingestion"
                ),
            )

    # --- Resume: modification input ---
    if context.get_state("awaiting_modification_input") and context.user_input:
        user_response = context.user_input.strip()

        if user_response.lower() in CANCEL_WORDS:
            LOGGER.info("User cancelled modification")
            return StepResult(
                skip_remaining=True,
                progress_message="Ingestion cancelled.",
            )

        iteration = (context.get_state("quality_iteration_count") or 0) + 1
        original = context.get_state("original_user_content") or context.get_state(
            "document_content"
        )
        current_suggestion = context.get_state("suggested_content") or original
        doc_type = context.get_state("detected_doc_type") or "technical"

        await context.send_progress_to_user("Applying your changes...")

        prompt = MODIFICATION_PROMPT.format(
            doc_type=doc_type,
            original=original[:6000],
            suggestion=current_suggestion[:6000],
            user_instructions=user_response,
        )

        new_version = await _call_gemini(prompt, json_output=False, max_tokens=4096)
        if not new_version:
            # LLM failed - accept current suggestion
            LOGGER.warning("LLM modification failed, accepting current suggestion")
            return await _finalize_content(context, current_suggestion)

        # Show new version and ask again
        preview = new_version[:500] + ("..." if len(new_version) > 500 else "")
        remaining = MAX_QUALITY_ITERATIONS - iteration

        return StepResult(
            state_updates={
                "suggested_content": new_version,
                "quality_iteration_count": iteration,
                "awaiting_modification_input": False,
                "awaiting_quality_decision": True,
            },
            needs_user_input=True,
            user_prompt=(
                f"Here's the updated version:\n\n---\n{preview}\n---\n\n"
                f"1. Accept this version\n"
                f"2. Modify further ({remaining} revision{'s' if remaining != 1 else ''} remaining)\n"
                f"3. Use original text\n"
                f"4. Cancel ingestion\n\n"
                f"Reply 1, 2, 3, or 4."
            ),
            inline_options=[
                "Accept this version",
                f"Modify further ({remaining} left)",
                "Use original text",
                "Cancel ingestion",
            ],
        )

    # --- First run: evaluate quality ---
    content = context.get_state("document_content")
    if not content:
        return StepResult.failure("No document content available for quality check")

    doc_type = context.get_state("detected_doc_type") or "technical"

    await context.send_progress_to_user("Checking content quality...")

    prompt = QUALITY_EVAL_PROMPT.format(
        doc_type=doc_type,
        content=content[:6000],
    )

    response_text = await _call_gemini(prompt, json_output=True, max_tokens=4096)
    if not response_text:
        # LLM failed - accept as-is and proceed to naming
        LOGGER.warning("Quality eval failed, accepting content as-is")
        return await _finalize_content(context, content)

    try:
        # Clean markdown wrappers if present
        clean = response_text
        if clean.startswith("```"):
            lines = clean.split("\n")
            if lines[-1].strip() == "```":
                clean = "\n".join(lines[1:-1])
            else:
                clean = "\n".join(lines[1:])
            clean = clean.strip()

        result = json.loads(clean)
    except json.JSONDecodeError:
        LOGGER.warning(f"Could not parse quality eval response: {response_text[:200]}")
        return await _finalize_content(context, content)

    is_good = result.get("is_good", True)
    reasoning = result.get("reasoning", "")
    suggested = result.get("suggested_version", "")

    if is_good or not suggested:
        # Content is good - proceed to naming
        LOGGER.info(f"Content quality: good ({reasoning})")
        return await _finalize_content(context, content)

    # Content needs improvement - present options
    LOGGER.info(f"Content quality: needs improvement ({reasoning})")
    preview = suggested[:500] + ("..." if len(suggested) > 500 else "")

    return StepResult(
        state_updates={
            "original_user_content": content,
            "suggested_content": suggested,
            "quality_iteration_count": 0,
            "awaiting_quality_decision": True,
        },
        needs_user_input=True,
        user_prompt=(
            f"**Quality check:** {reasoning}\n\n"
            f"Here's a suggested improvement:\n\n---\n{preview}\n---\n\n"
            f"1. Accept suggested version\n"
            f"2. Modify further\n"
            f"3. Use original text as-is\n"
            f"4. Cancel ingestion\n\n"
            f"Reply 1, 2, 3, or 4."
        ),
        inline_options=[
            "Accept suggested version",
            "Modify further",
            "Use original text as-is",
            "Cancel ingestion",
        ],
    )


async def _finalize_content(context: StepContext, final_content: str) -> StepResult:
    """Generate title and finalize the content improvement step."""
    doc_type = context.get_state("detected_doc_type") or "technical"
    email = context.effective_email

    await context.send_progress_to_user("Generating document title...")

    uploader_name = await _lookup_uploader_name(email)
    title = await _auto_generate_title(final_content, doc_type, uploader_name)

    LOGGER.info(f"Auto-generated title: {title}")

    return StepResult(
        data={
            "improved_content": final_content,
            "document_title": title,
            "uploader_name": uploader_name,
        },
        state_updates={
            "document_content": final_content,
            "document_title": title,
            "awaiting_quality_decision": False,
            "awaiting_modification_input": False,
        },
        progress_message=f"Content finalized. Title: {title}",
    )
