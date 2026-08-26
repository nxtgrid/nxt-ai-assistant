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
