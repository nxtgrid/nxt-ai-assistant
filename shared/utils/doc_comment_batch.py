"""Apply every pending @anansi-chatbot comment in one ordered batch.

The sequencing is the point. Comments arrive in Drive's creation order,
which has nothing to do with where they sit in the document or what they
depend on -- "summarise the sections above" is created whenever its author
happened to write it. Left to itself a model calling a one-section-at-a-time
edit tool will work through them in whatever order it received them.

So this does three things the agent cannot be relied on to do:

1. Classifies which comments need the *finished* document (an LLM pass, see
   doc_edit_ordering.classify_deferred) and holds those back to a second
   pass, re-scanning in between so they see the real post-edit text.
2. Sorts each pass bottom-to-top by document position, so writing one
   section never shifts the anchor text of a later one.
3. Pins a revision once for the whole batch, not once per edit.

Mirrors process_doc_edits' MODE 1. The two are kept parallel rather than
merged: the expert step also streams progress and returns a StepResult,
and unifying those shapes buys nothing a reader can see.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from shared.utils import doc_edit_ordering as ordering
from shared.utils.doc_edit_ordering import DOC_CONTEXT_CHAR_LIMIT
from shared.utils.doc_editing import (
    edit_section,
    fetch_doc_markdown,
    generate_replacement_markdown,
    pin_revision,
    scan_comments,
)

LOGGER = logging.getLogger(__name__)

# Cost/rate-limit protection, matching process_doc_edits.MAX_EDITS_PER_RUN.
MAX_EDITS_PER_RUN = 10


async def _apply(
    doc_id: str,
    comments: List[Dict[str, Any]],
    markdown: str,
    user_email: Optional[str],
    tool_runner,
) -> List[Dict[str, Any]]:
    """Generate and write one replacement per comment, in the order given."""
    from shared.utils.error_messages import sanitize_error_for_user

    results: List[Dict[str, Any]] = []
    for comment in comments:
        highlighted = comment.get("highlighted_text") or ""
        comment_id = comment["comment_id"]

        if not highlighted:
            results.append({"comment_id": comment_id, "status": "skipped"})
            continue

        try:
            replacement = await generate_replacement_markdown(
                instruction=comment["instruction"],
                highlighted_text=highlighted,
                section_context=markdown,
                context_limit=DOC_CONTEXT_CHAR_LIMIT,
                user_email=user_email,
                tool_runner=tool_runner,
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
                    {
                        "comment_id": comment_id,
                        "status": "failed",
                        "error": result.get("error"),
                    }
                )
        except Exception as e:
            LOGGER.error(f"Edit failed for comment {comment_id}: {e}", exc_info=True)
            results.append(
                {
                    "comment_id": comment_id,
                    "status": "failed",
                    "error": sanitize_error_for_user(str(e)),
                }
            )
    return results


async def _refresh(doc_id: str, second_pass: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Re-scan so the second pass matches text as it stands after pass one.

    One comments.list call rather than one comments.get per comment. A
    comment pass one resolved, or a human resolved mid-run, simply stops
    coming back and is dropped -- the correct outcome either way.
    """
    wanted = {c["comment_id"] for c in second_pass}
    return [c for c in await scan_comments(doc_id) if c["comment_id"] in wanted]


async def process_comments(doc_id: str, user_email: Optional[str] = None) -> Dict[str, Any]:
    """Apply every pending comment. Returns a summary dict, never raises."""
    from shared.utils.doc_edit_tools import default_tool_runner

    comments = await scan_comments(doc_id)
    if not comments:
        return {"edits": 0, "succeeded": 0, "failed": 0, "deferred": 0, "edit_results": []}

    if len(comments) > MAX_EDITS_PER_RUN:
        LOGGER.warning(f"Capping edits from {len(comments)} to {MAX_EDITS_PER_RUN}")
        comments = comments[:MAX_EDITS_PER_RUN]

    markdown = await fetch_doc_markdown(doc_id)
    tool_runner = default_tool_runner()

    # Classify against the scan-order list, because the prompt numbers the
    # comments by their position in it. Only then sort into document order.
    deferred = await ordering.classify_deferred(comments, markdown) if markdown else set()
    first_pass, second_pass = ordering.partition_by_pass(comments, deferred)
    first_pass = ordering.order_by_position(first_pass, markdown)

    LOGGER.info(
        f"Doc comment batch on {doc_id}: {len(first_pass)} now, "
        f"{len(second_pass)} after the rest"
    )

    await pin_revision(doc_id)
    results = await _apply(doc_id, first_pass, markdown, user_email, tool_runner)

    deferred_count = len(second_pass)
    if second_pass:
        fresh_markdown = await fetch_doc_markdown(doc_id) or markdown
        second_pass = await _refresh(doc_id, second_pass)
        second_pass = ordering.order_by_position(second_pass, fresh_markdown)
        results += await _apply(doc_id, second_pass, fresh_markdown, user_email, tool_runner)

    succeeded = sum(1 for r in results if r["status"] == "done")
    failed = sum(1 for r in results if r["status"] == "failed")
    return {
        "edits": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "deferred": deferred_count,
        "edit_results": results,
    }
