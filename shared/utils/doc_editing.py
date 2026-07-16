"""Shared Google Doc editing utilities.

Provides reusable functions for scanning @anansibot comments and editing
Google Doc sections via the Apps Script bridge. Used by both MCP tool
handlers and the doc_editor expert step.
"""

import asyncio
import functools
import json
import logging
import re
from typing import Any

from googleapiclient.discovery import build

from shared.utils.apps_script_client import write_doc_markdown
from shared.utils.google_auth import get_drive_write_credentials

LOGGER = logging.getLogger(__name__)

# URL patterns consolidated in drive_resolver.py — import from there
from shared.utils.drive_resolver import (
    AmbiguousDocumentMatch,
    extract_document_references,
    resolve_document,
)


@functools.lru_cache(maxsize=1)
def _get_drive_service():
    """Cached Drive v3 service (write credentials). Built once per process."""
    creds = get_drive_write_credentials()
    return build("drive", "v3", credentials=creds)


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

# Partial match for the service account email in comments
BOT_MENTION = "@anansi-chatbot"


async def scan_comments(doc_id: str) -> list[dict]:
    """Scan a Google Doc for pending @anansibot comments.

    Includes reply threads — if a comment has replies from multiple users,
    they are concatenated into the instruction with author attribution.

    Returns a list of comment dicts with:
        comment_id, instruction, highlighted_text, author_email, created_time
    """
    drive_service = _get_drive_service()

    comments_resp = await asyncio.to_thread(
        lambda: drive_service.comments()
        .list(
            fileId=doc_id,
            fields="comments(id,content,resolved,quotedFileContent,createdTime,"
            "author(emailAddress,displayName),"
            "replies(content,author(emailAddress,displayName)))",
            includeDeleted=False,
        )
        .execute()
    )

    pending = [
        c
        for c in comments_resp.get("comments", [])
        if not c.get("resolved") and BOT_MENTION in (c.get("content", "").lower())
    ]

    results = []
    for c in pending:
        highlighted = c.get("quotedFileContent", {}).get("value", "")
        author_email = c.get("author", {}).get("emailAddress", "")
        author_name = c.get("author", {}).get("displayName", "")

        # Build instruction from the comment + all replies
        instruction = _build_thread_instruction(c, author_name)

        results.append(
            {
                "comment_id": c["id"],
                "instruction": instruction,
                "highlighted_text": highlighted,
                "author_email": author_email,
                "created_time": c.get("createdTime", ""),
            }
        )

    return results


def _strip_bot_mention(text: str) -> str:
    """Remove @anansibot mentions from comment text."""
    text = text.replace(BOT_MENTION, "")
    return re.sub(r"@?anansi-chatbot[-\w.@]*", "", text, flags=re.IGNORECASE).strip()


def _build_thread_instruction(comment: dict, initial_author: str) -> str:
    """Build a single instruction string from a comment and its reply thread.

    If all messages are from the same author, concatenates plainly.
    If multiple authors, prefixes each reply with the author's name.
    """
    initial_text = _strip_bot_mention(comment.get("content", ""))
    replies = comment.get("replies", [])

    if not replies:
        return initial_text

    # Check if all replies are from the same author as the initial comment
    initial_email = comment.get("author", {}).get("emailAddress", "")
    all_same_author = all(
        r.get("author", {}).get("emailAddress", "") == initial_email for r in replies
    )

    if all_same_author:
        # Same person — just concatenate
        parts = [initial_text]
        for r in replies:
            reply_text = _strip_bot_mention(r.get("content", ""))
            if reply_text:
                parts.append(reply_text)
        return "\n".join(parts)
    else:
        # Multiple authors — attribute each message
        parts = [f"[{initial_author or 'Author'}]: {initial_text}"]
        for r in replies:
            reply_text = _strip_bot_mention(r.get("content", ""))
            if reply_text:
                reply_author = r.get("author", {}).get("displayName", "Someone")
                parts.append(f"[{reply_author}]: {reply_text}")
        return "\n".join(parts)


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
        await _resolve_comment(doc_id, comment_id, replacement_markdown)
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
        instruction = _build_thread_instruction(comment, author_name)

        return {
            "highlighted_text": highlighted,
            "instruction": instruction,
            "author_email": comment.get("author", {}).get("emailAddress", ""),
        }
    except Exception as e:
        LOGGER.warning(f"Could not fetch comment {comment_id} from doc {doc_id}: {e}")
        return None


async def pin_revision(doc_id: str) -> bool:
    """Pin the current revision before editing for rollback safety.

    Creates a permanent revision entry in Google Docs version history.
    Note: Google API does not support naming versions on Docs — this
    only pins the revision to prevent auto-deletion.
    """
    try:
        service = _get_drive_service()
        await asyncio.to_thread(
            lambda: service.revisions()
            .update(
                fileId=doc_id,
                revisionId="head",
                body={"keepForever": True},
            )
            .execute()
        )
        LOGGER.info(f"Pinned pre-edit revision for doc {doc_id}")
        return True
    except Exception as e:
        LOGGER.warning(f"Could not pin revision for {doc_id}: {e}")
        return False  # Non-fatal


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


async def generate_replacement_markdown(
    instruction: str,
    highlighted_text: str,
    section_context: str = "",
    expert_context: dict[str, Any] | None = None,
    user_email: str | None = None,
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

    context_block = ""
    if section_context:
        context_block = f"\nSURROUNDING CONTEXT:\n{section_context[:1500]}"

    prompt = f"""Edit the highlighted text in a Google Doc according to the instruction.
Return the replacement as **Markdown-formatted text**.

Supported formatting:
- **bold** and *italic*
- ## Headings (H2-H4 only — do not use H1)
- Bullet lists (- item) and numbered lists (1. item)
- Tables (| col1 | col2 |)
- [Links](url)

Do NOT use: H1 headings, images, footnotes, HTML tags, code blocks.
If the original text is a simple sentence, keep it as a plain paragraph.

INSTRUCTION: {instruction}

HIGHLIGHTED TEXT (to be replaced):
{highlighted_text}
{context_block}
{context_summary}
{reference_block}

Return ONLY the replacement markdown — no explanation, no fences.
If reference documents were provided, follow their style, structure, and tone closely.
"""

    response = await gateway.generate(
        [LLMMessage(role="user", text=prompt)],
        GenerationOptions(
            model=settings.gemini.model,
            temperature=0.3,
            max_output_tokens=2000,
        ),
    )

    return str(response.text).strip()


async def _resolve_comment(doc_id: str, comment_id: str, replacement_preview: str) -> None:
    """Reply to and resolve a Google Doc comment after a successful edit."""
    try:
        drive_service = _get_drive_service()
        preview = replacement_preview[:200]
        await asyncio.to_thread(
            lambda: drive_service.replies()
            .create(
                fileId=doc_id,
                commentId=comment_id,
                fields="id",
                body={"action": "resolve", "content": f"Done: {preview}"},
            )
            .execute()
        )
    except Exception as e:
        LOGGER.warning(f"Could not resolve comment {comment_id}: {e}")
