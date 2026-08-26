"""Deciding what order a document's comment edits run in.

Two separate problems live here and they are solved differently on purpose.

*Position* -- an edit that rewrites text shifts everything below it, so edits
run bottom-to-top. That is arithmetic, not judgement: the quoted text either
appears in the document markdown at some offset or it does not, and `str.find`
answers it for free. It replaced a `reversed(creation order)` approximation
that was only ever right by luck.

*Dependency* -- "add a summary here once the rest is written" cannot be
answered until the other comments have been applied. Nothing about the text
reveals that; it is in what the instruction means. That one needs the model,
and it is the only thing here that costs a call.
"""

import html
import json
import logging
from typing import Any, Dict, List, Set

from shared.prompts import PROMPTS

LOGGER = logging.getLogger(__name__)

# How much of the document the generator and the classifier each get. The
# 1500-char default in generate_replacement_markdown is sized for "here is the
# paragraph around the bit you are rewriting"; an instruction like "summarise
# everything above" is unanswerable from 1500 characters, which is the whole
# reason this plan exists. 12k keeps a full ordinary report in the prompt while
# staying far below the model's window.
DOC_CONTEXT_CHAR_LIMIT = 12000


def document_position(markdown: str, quoted_text: str) -> int:
    """Character offset of a comment's quoted text in the document markdown.

    Returns -1 when the quote cannot be located -- the text was edited since
    the comment was made, or the markdown conversion renders it differently.

    Drive serves `quotedFileContent` as text/html (see `Annotation` in
    shared/utils/file_annotations.py), so it is unescaped before matching.
    """
    if not quoted_text:
        return -1

    needle = html.unescape(quoted_text).strip()
    if not needle:
        return -1

    position = markdown.find(needle)
    if position != -1:
        return position

    # A Docs quote can span a paragraph break that the markdown conversion
    # renders with different whitespace. The first line is enough to place it.
    first_line = needle.splitlines()[0].strip()
    if first_line and first_line != needle:
        return markdown.find(first_line)
    return -1


def order_by_position(comments: List[Dict[str, Any]], markdown: str) -> List[Dict[str, Any]]:
    """Bottom-to-top document order, so an edit never shifts a later target.

    Comments whose quote cannot be located sort last: their write will fail to
    find its target whatever we do, and running them last keeps that failure
    from disturbing the ones that can still succeed.
    """
    located = []
    unlocated = []
    for comment in comments:
        position = document_position(markdown, comment.get("highlighted_text", ""))
        if position < 0:
            unlocated.append(comment)
        else:
            located.append((position, comment))

    located.sort(key=lambda pair: pair[0], reverse=True)
    return [comment for _, comment in located] + unlocated


def parse_deferred(text: str) -> Set[int]:
    """The 1-based request numbers the model marked deferred.

    Deliberately tolerant. An ordering pass is an optimisation on top of work
    the user actually asked for -- a response we cannot read degrades to a
    single-pass run, which is exactly today's behaviour, never to a failed
    edit run.
    """
    body = text.strip()
    if "```" in body:
        body = body.split("```")[1]
        if body.startswith("json"):
            body = body[4:]

    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError, IndexError):
        LOGGER.warning("Could not parse the edit ordering response; running a single pass")
        return set()

    if not isinstance(parsed, list):
        return set()

    return {
        int(item["request"])
        for item in parsed
        if isinstance(item, dict) and item.get("deferred") is True and "request" in item
    }


def build_comments_block(comments: List[Dict[str, Any]]) -> str:
    """The numbered request list the classifier prompt is built around.

    The numbering is positional: `parse_deferred` maps the model's answers
    back through it, so this must be built from the same list, in the same
    order, that `partition_by_pass` is later given.
    """
    return "\n".join(
        f'{i}. instruction: "{comment.get("instruction", "")}"\n'
        f'   quoted text: "{(comment.get("highlighted_text") or "")[:200]}"'
        for i, comment in enumerate(comments, start=1)
    )


async def _classify(comments_block: str, markdown: str) -> str:
    """The raw model response. Split out so tests can fail it deliberately."""
    from orchestrator.config.settings import get_settings
    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

    settings = get_settings()
    gateway = get_default_generation_gateway(default_model=settings.gemini.model)

    prompt = PROMPTS.text(
        "doc_editor.order_edits",
        comments_block=comments_block,
        markdown=markdown[:DOC_CONTEXT_CHAR_LIMIT],
    )
    response = await gateway.generate(
        [LLMMessage(role="user", text=prompt)],
        GenerationOptions(model=settings.gemini.model, temperature=0.1, max_output_tokens=1000),
    )
    return str(response.text)


async def classify_deferred(comments: List[Dict[str, Any]], markdown: str) -> Set[int]:
    """Which comments (1-based, in the given order) belong in the second pass.

    One extra model call per run, skipped entirely below two comments -- there
    is nothing to order, and one comment is the common case. Every failure
    path returns an empty set: ordering must never be the reason an edit the
    user asked for does not happen.
    """
    if len(comments) < 2:
        return set()

    try:
        return parse_deferred(await _classify(build_comments_block(comments), markdown))
    except Exception as e:
        LOGGER.warning(f"Edit ordering pass failed; running every comment in one pass: {e}")
        return set()


def partition_by_pass(
    comments: List[Dict[str, Any]], deferred: Set[int]
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split into (first pass, second pass), preserving the order given.

    `deferred` holds 1-based positions in `comments` exactly as the classifier
    was shown them, so this must be called with the same list, in the same
    order, that `build_comments_block` was given -- before any position sort
    reorders it. A number outside the range is ignored rather than trusted:
    the model does not get to drop a comment the user left.
    """
    first: List[Dict[str, Any]] = []
    second: List[Dict[str, Any]] = []
    for index, comment in enumerate(comments, start=1):
        (second if index in deferred else first).append(comment)
    return first, second
