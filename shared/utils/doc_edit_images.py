"""Placing a fetched chart into generated markdown.

A rendered chart is a base64 PNG of a hundred kilobytes or more. The model's
max_output_tokens is 2000, so the payload cannot pass through its context in
either direction. Instead the tool loop holds the images aside, the model
writes ``![Alt](anansi-chart:N)`` naming the Nth image it fetched, and this
module swaps in the payload afterwards.

The two failure modes both have to be handled here, because Apps Script
cannot tell a real reference from a made-up one -- an unsubstituted
placeholder would be written into the document as literal text.
"""

from __future__ import annotations

import logging
import re
from typing import Sequence

LOGGER = logging.getLogger(__name__)

_CHART_REF = re.compile(r"!\[([^\]]*)\]\(anansi-chart:(\d+)\)")


def substitute_chart_refs(markdown: str, images: Sequence[str]) -> str:
    """Replace every ``anansi-chart:N`` reference with its base64 payload.

    N is 1-based, indexing ``images`` in the order the tools returned them.
    A reference with no matching image is removed entirely rather than left
    in place. An image nothing referenced is appended at the end -- the user
    asked for a chart and one was fetched; dropping it silently is worse
    than putting it somewhere slightly wrong.
    """
    used: set[int] = set()

    def _swap(match: re.Match) -> str:
        alt, index = match.group(1), int(match.group(2))
        if not 1 <= index <= len(images):
            LOGGER.warning(
                f"Dropping chart reference {index}; only {len(images)} image(s) were fetched"
            )
            return ""
        used.add(index)
        return f"![{alt}](base64:{images[index - 1]})"

    out = _CHART_REF.sub(_swap, markdown)

    # Tidy the blank line a dropped reference leaves behind.
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    orphans = [img for i, img in enumerate(images, start=1) if i not in used]
    for image in orphans:
        LOGGER.info("Appending a fetched chart the model did not reference")
        out = f"{out}\n\n![Chart](base64:{image})"

    return out
