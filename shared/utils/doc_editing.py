"""Shared Google Doc editing utilities.

Provides reusable functions for scanning @anansibot comments and editing
Google Doc sections via the Apps Script bridge. Used by both MCP tool
handlers and the doc_editor expert step.
"""

import asyncio
import json
import logging
from typing import Any

from shared.prompts import PROMPTS
from shared.utils.apps_script_client import write_doc_markdown

LOGGER = logging.getLogger(__name__)

# URL patterns consolidated in drive_resolver.py — import from there
from shared.utils.drive_resolver import (
    AmbiguousDocumentMatch,
    extract_document_references,
    resolve_document,
)

# The file-type-agnostic half (scanning, reply+resolve, revision pinning)
# moved to file_annotations.py so sheet_editing.py can reuse it. BOT_MENTION,
# pin_revision and strip_bot_mention have no internal caller left in this
# file -- they are re-exported (the redundant "as X" alias is ruff/mypy's
# recognized marker for an intentional re-export, exempt from F401) because
# knowledge_mcp_server.py and process_doc_edits.py import them from here.
from shared.utils.file_annotations import (
    BOT_MENTION as BOT_MENTION,
)
from shared.utils.file_annotations import (
    _get_drive_service,
    build_thread_instruction,
    reply_and_resolve,
    scan_annotations,
)
from shared.utils.file_annotations import (
    pin_revision as pin_revision,
)
from shared.utils.file_annotations import (
    strip_bot_mention as strip_bot_mention,
)

# Keys from packet_state that are safe to pass to the LLM for context
_ALLOWED_STATE_KEYS = {
    "site_name",
    "grid_name",
    "grid_id",
    "organization_name",
    "total_buildings",
    "served_building_count",
    "total_kwp",
    "total_kwh",
    "editable_total_buildings",
    "editable_served_building_count",
    "editable_total_kwp",
    "editable_total_kwh",
    "detected_doc_type",
    "classification_confidence",
}


async def scan_comments(doc_id: str) -> list[dict]:
    """Docs-shaped view of scan_annotations, kept for existing callers.

    Returns a list of comment dicts with:
        comment_id, instruction, highlighted_text, author_email, created_time
    """
    return [
        {
            "comment_id": a.comment_id,
            "instruction": a.instruction,
            "highlighted_text": a.quoted_text,
            "author_email": a.author_email,
            "created_time": a.created_time,
        }
        for a in await scan_annotations(doc_id)
    ]


async def edit_section(
    doc_id: str,
    target_text: str,
    replacement_markdown: str,
    comment_id: str | None = None,
) -> dict:
    """Edit a section of a Google Doc with formatted markdown via Apps Script.

    Args:
        doc_id: Google Doc file ID
        target_text: Exact text to find and replace
        replacement_markdown: Markdown-formatted replacement content
        comment_id: If provided, resolve this comment after successful edit

    Returns:
        Dict with 'success', 'error' (if failed), 'elements_written'

    Note: Callers should call pin_revision() once before a batch of edits,
    not per-edit. This function does NOT pin automatically.
    """
    # Debug logging for end-to-end tracing
    LOGGER.info(
        f"edit_section: doc={doc_id}, target_text={target_text[:80]!r}..., "
        f"markdown_len={len(replacement_markdown)}, comment_id={comment_id}"
    )

    if not target_text:
        LOGGER.error("edit_section called with empty target_text — cannot locate section")
        return {
            "success": False,
            "error": "No target text provided — cannot identify which section to edit.",
        }

    # Call Apps Script to write formatted content
    result = await write_doc_markdown(doc_id, target_text, replacement_markdown)

    LOGGER.info(
        f"edit_section: Apps Script result success={result.success}, "
        f"data={result.data}, error={result.error_message}"
    )

    if not result.success:
        error_msg = result.error_message or "Unknown error from Apps Script"
        LOGGER.error(f"Apps Script write_doc_markdown failed for {doc_id}: {error_msg}")
        return {
            "success": False,
            "error": "Could not write formatted content to the document. "
            "Please try again or edit the document manually.",
        }

    elements_written = (result.data or {}).get("elements_written", 0)
    LOGGER.info(f"Wrote {elements_written} elements to doc {doc_id}")

    if elements_written == 0:
        LOGGER.warning(f"Apps Script reported success but wrote 0 elements for doc {doc_id}")

    # Resolve comment only AFTER confirming elements were written
    if comment_id and elements_written > 0:
        await reply_and_resolve(doc_id, comment_id, f"Done: {replacement_markdown[:200]}")
    elif comment_id:
        LOGGER.warning(f"Skipping comment resolution — 0 elements written for comment {comment_id}")

    return {"success": True, "elements_written": elements_written}


async def get_comment_by_id(doc_id: str, comment_id: str) -> dict | None:
    """Fetch a specific comment's details including reply thread.

    Returns dict with 'highlighted_text', 'instruction', 'author_email'
    or None if not found. The instruction includes all replies in the thread.
    """
    drive_service = _get_drive_service()

    try:
        comment = await asyncio.to_thread(
            lambda: drive_service.comments()
            .get(
                fileId=doc_id,
                commentId=comment_id,
                fields="id,content,quotedFileContent,"
                "author(emailAddress,displayName),"
                "replies(content,author(emailAddress,displayName))",
                includeDeleted=False,
            )
            .execute()
        )

        highlighted = comment.get("quotedFileContent", {}).get("value", "")
        author_name = comment.get("author", {}).get("displayName", "")
        instruction = build_thread_instruction(comment, author_name)

        return {
            "highlighted_text": highlighted,
            "instruction": instruction,
            "author_email": comment.get("author", {}).get("emailAddress", ""),
        }
    except Exception as e:
        LOGGER.warning(f"Could not fetch comment {comment_id} from doc {doc_id}: {e}")
        return None


async def _fetch_reference_docs(instruction: str, user_email: str | None = None) -> str:
    """Fetch content of documents referenced in the instruction.

    Detects references by:
    - URLs (Google Docs/Drive links)
    - Doc-codes (e.g., DOC-1234; prefix configured via DOC_CODE_PREFIX env var)
    - Quoted names (e.g., "ExampleSite Visit Plan")

    Uses resolve_document() for unified resolution with permission checks.
    Returns a formatted block for the LLM prompt, or empty string if none found.
    """
    refs = extract_document_references(instruction)
    if not refs:
        return ""

    from shared.utils.gdrive_doc_fetcher import fetch_google_doc

    async def _fetch_one(ref: str) -> str | None:
        """Resolve and fetch a single reference doc."""
        try:
            # resolve_document handles permission checks when user_email is provided
            doc = await resolve_document(ref, user_email=user_email)
            if not doc:
                return None

            content = await asyncio.to_thread(fetch_google_doc, doc["file_id"])
            if content:
                truncated = content[:4000]
                if len(content) > 4000:
                    truncated += "\n... (truncated)"
                doc_label = doc.get("name") or doc["file_id"][:12]
                LOGGER.info(f"Fetched reference doc '{doc_label}' ({len(content)} chars)")
                return f"--- Reference document: {doc_label} ---\n{truncated}"
        except AmbiguousDocumentMatch as e:
            LOGGER.info(f"Ambiguous reference '{ref}' matched {len(e.matches)} docs — skipping")
        except Exception as e:
            LOGGER.warning(f"Could not fetch reference doc '{ref}': {e}")
        return None

    # Fetch in parallel (up to 3 docs) without blocking the event loop
    results = await asyncio.gather(*[_fetch_one(ref) for ref in refs[:3]])
    reference_blocks = [r for r in results if r]

    if not reference_blocks:
        return ""

    return "\n\nREFERENCE DOCUMENTS (use these as examples for style and content):\n" + "\n\n".join(
        reference_blocks
    )


async def fetch_doc_markdown(doc_id: str) -> str:
    """The document as markdown, off the event loop. Never raises.

    fetch_google_doc_markdown is a blocking Drive call, so it goes through a
    thread. A document we cannot read degrades to "no context" -- editing
    blind is worse than editing well, but it is much better than failing the
    whole batch because one Drive call timed out.
    """
    from shared.utils import gdrive_doc_fetcher

    try:
        markdown = await asyncio.to_thread(gdrive_doc_fetcher.fetch_google_doc_markdown, doc_id)
    except Exception:
        LOGGER.warning(
            f"Could not fetch {doc_id} as markdown -- editing without document context",
            exc_info=True,
        )
        return ""
    if not markdown:
        LOGGER.warning(f"Could not fetch {doc_id} as markdown -- editing without document context")
    return markdown or ""


def build_context_block(section_context: str, context_limit: int = 1500) -> str:
    """The SURROUNDING CONTEXT block, truncated to the caller's budget.

    The default is sized for a single instruction-driven edit, where the point
    is "here is the paragraph around the bit you are rewriting". The
    comment-driven batch passes the whole document and a much larger limit --
    an instruction like "summarise the sections above" is unanswerable from
    1500 characters, and answering it from nothing at all is what this
    parameter exists to stop.
    """
    if not section_context:
        return ""
    return f"\nSURROUNDING CONTEXT:\n{section_context[:context_limit]}"


async def generate_replacement_markdown(
    instruction: str,
    highlighted_text: str,
    section_context: str = "",
    expert_context: dict[str, Any] | None = None,
    user_email: str | None = None,
    context_limit: int = 1500,
) -> str:
    """Use LLM to generate markdown replacement text for a doc section.

    If the instruction contains links to Google Docs/Drive files, those
    documents are fetched (with permission check) and included as reference
    context for the LLM to follow in terms of style and content.

    Args:
        instruction: Edit instruction from user or comment
        highlighted_text: The text being replaced
        section_context: Surrounding document context (optional)
        expert_context: Workflow state dict — only allowed keys are passed to LLM
        user_email: Requesting user's email (for permission checks on reference docs)
        context_limit: How much of section_context to include. The default
            suits a single section edit; the comment-driven batch raises it
            so an instruction can refer to the whole document.
    """
    from orchestrator.config.settings import get_settings
    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

    settings = get_settings()
    gateway = get_default_generation_gateway(
        default_model=settings.gemini.model,
    )

    # Fetch any reference documents linked in the instruction (with authz check)
    reference_block = await _fetch_reference_docs(instruction, user_email=user_email)

    # Build context from expert state (allowlist only — no sensitive data to LLM)
    context_summary = ""
    if expert_context:
        relevant = {k: v for k, v in expert_context.items() if k in _ALLOWED_STATE_KEYS}
        if relevant:
            context_summary = (
                f"\n\nAvailable data from the current workflow:\n"
                f"{json.dumps(relevant, indent=2, default=str)[:2000]}"
            )

    context_block = build_context_block(section_context, context_limit)

    prompt = PROMPTS.text(
        "doc_editing.edit_highlighted",
        instruction=instruction,
        highlighted_text=highlighted_text,
        context_block=context_block,
        context_summary=context_summary,
        reference_block=reference_block,
    )

    response = await gateway.generate(
        [LLMMessage(role="user", text=prompt)],
        GenerationOptions(
            model=settings.gemini.model,
            temperature=0.3,
            max_output_tokens=2000,
        ),
    )

    return str(response.text).strip()
