"""Shared Google Doc editing utilities.

Provides reusable functions for scanning @anansibot comments and editing
Google Doc sections via the Apps Script bridge. Used by both MCP tool
handlers and the doc_editor expert step.
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from shared.prompts import PROMPTS
from shared.utils.apps_script_client import write_doc_markdown

if TYPE_CHECKING:
    from shared.prompts.types import RequestScope
    from shared.utils.doc_edit_tools import ToolRunner

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


_TOOL_GUIDANCE = (
    "\nYou may call the listed tools to get real figures before writing. "
    "Do this whenever the instruction asks for live data (power, battery, "
    "generation totals) — never estimate or invent a number that a tool can "
    "give you. If a tool fails, say plainly in the replacement text that the "
    "figure was unavailable rather than guessing one."
)

_NO_IMAGES = "Do NOT use images."

_IMAGE_GUIDANCE = (
    "To place a chart you fetched with generate_power_chart, write "
    "![Short caption](anansi-chart:N) on its own line, where N is 1 for the "
    "first chart you fetched, 2 for the second, and so on. Do not paste image "
    "data — the reference is replaced with the real chart after you answer. "
    "Never write an anansi-chart reference for a chart you did not actually "
    "fetch."
)


def _tool_manifest() -> dict[str, dict]:
    """The served tool manifest, flattened to server-prefixed names.

    Read from mcp_servers/tool_definitions.json -- the same file production
    serves -- so a tool's schema here is the schema the model sees everywhere
    else. Returns {} if mcp_servers is not on the path, which degrades the
    editor to its untooled behaviour rather than failing the edit.
    """
    try:
        import json as _json
        from pathlib import Path

        import mcp_servers

        path = Path(mcp_servers.__file__).parent / "tool_definitions.json"
        manifest = _json.loads(path.read_text())
    except Exception:
        LOGGER.warning("Tool manifest unavailable; editing without tools", exc_info=True)
        return {}

    flattened: dict[str, dict] = {}
    for server, entries in (manifest.get("tools") or {}).items():
        tools = entries if isinstance(entries, list) else entries.get("tools", [])
        for tool in tools:
            if isinstance(tool, dict) and "name" in tool:
                flattened[f"{server}_{tool['name']}"] = tool
    return flattened


def _generation_gateway():
    """The configured gateway. A function so tests can replace it."""
    from orchestrator.config.settings import get_settings
    from shared.llm import get_default_generation_gateway

    return get_default_generation_gateway(default_model=get_settings().gemini.model)


def _jit_resolver():
    """The JIT context resolver, or None where orchestrator is absent."""
    try:
        from orchestrator.services.jit_context_resolver import JitContextResolver
    except ImportError:
        return None
    return JitContextResolver()


async def _live_knowledge(user_email: str | None, scope) -> str:
    """Provider-backed modules pinned to this prompt, resolved for this caller.

    PromptLibrary.render() drops every JIT module -- gdoc, graph, directory,
    episodic -- because it is synchronous and carries no identity. A Google
    Doc attached to the editor as a knowledge module would otherwise resolve
    to nothing, which is the opposite of what attaching it means.
    """
    resolver = _jit_resolver()
    if resolver is None:
        return ""
    try:
        from shared.prompts.providers import ResolutionContext
        from shared.prompts.types import RequestScope

        ctx = ResolutionContext(scope=scope or RequestScope(), user_email=user_email)
        text, _slugs = await resolver.resolve_for_prompt("doc_editing.edit_highlighted", ctx)
        return f"\n\n{text}" if text else ""
    except Exception:
        LOGGER.warning("Live context resolution failed; editing without it", exc_info=True)
        return ""


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
    tool_runner: "ToolRunner | None" = None,
    scope: "RequestScope | None" = None,
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
        user_email: Requesting user's email (for permission checks on reference docs,
            and for resolving Google-Doc-backed knowledge modules attached to
            this prompt)
        context_limit: How much of section_context to include. The default
            suits a single section edit; the comment-driven batch raises it
            so an instruction can refer to the whole document.
        tool_runner: When supplied, the model may call the read-only tools in
            DOC_EDIT_TOOLS (grid status, historical power, charts) instead of
            answering from nothing. Omit it for a purely editorial rewrite --
            the untooled single call is both cheaper and the long-standing
            behaviour for Sheets.
        scope: The entity context (grid/org) this edit is running in. Lets a
            site-scoped knowledge module attached to this prompt apply. Global
            modules apply regardless of scope.
    """
    from orchestrator.config.settings import get_settings
    from shared.llm import GenerationOptions, LLMMessage, ToolResult
    from shared.utils.doc_edit_tools import DOC_EDIT_TOOLS, MAX_TOOL_ROUNDS, build_tool_specs

    settings = get_settings()
    gateway = _generation_gateway()

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

    tools = build_tool_specs(_tool_manifest()) if tool_runner else None
    rendered = PROMPTS.render(
        "doc_editing.edit_highlighted",
        vars={
            "instruction": instruction,
            "highlighted_text": highlighted_text,
            "context_block": context_block,
            "context_summary": context_summary,
            "reference_block": reference_block,
            "tool_guidance": _TOOL_GUIDANCE if tools else "",
            "image_guidance": _IMAGE_GUIDANCE if tools else _NO_IMAGES,
        },
        scope=scope,
    )
    prompt = rendered.system_text
    if rendered.context_text:
        prompt = f"{prompt}\n\n{rendered.context_text}"
    prompt += await _live_knowledge(user_email, scope)

    options = GenerationOptions(
        model=settings.gemini.model,
        temperature=0.3,
        max_output_tokens=2000,
    )
    messages = [LLMMessage(role="user", text=prompt)]
    response = await gateway.generate(messages, options, tools=tools)

    allowed = set(DOC_EDIT_TOOLS)
    images: list[str] = []
    for _ in range(MAX_TOOL_ROUNDS):
        if not response.tool_calls:
            break
        results = []
        for call in response.tool_calls:
            if call.name not in allowed:
                LOGGER.warning(f"Doc editor requested a tool outside its whitelist: {call.name}")
                results.append(
                    ToolResult(
                        call_id=call.id,
                        name=call.name,
                        result=json.dumps({"error": f"Tool {call.name} is not available here"}),
                        is_error=True,
                    )
                )
                continue
            outcome = await tool_runner(call.name, dict(call.args or {}))
            images.extend(outcome.images)
            results.append(
                ToolResult(
                    call_id=call.id,
                    name=call.name,
                    result=outcome.text,
                    is_error=outcome.is_error,
                )
            )
        response = await gateway.generate(
            messages,
            options,
            tools=tools,
            tool_results=results,
            conversation_state=response.conversation_state,
        )

    from shared.utils.doc_edit_images import substitute_chart_refs

    return substitute_chart_refs(str(response.text or "").strip(), images)
