"""Rendering for prompt bodies: variables, partials, and section splitting."""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from shared.prompts.types import PromptRenderError

_PARTIAL = re.compile(r"\{\{>\s*([A-Za-z0-9_.]+)\s*\}\}")
_VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_HEADING = re.compile(r"^#\s+(.+)$", re.MULTILINE)

MAX_PARTIAL_DEPTH = 3

PartialResolver = Callable[[str], str]


def _inline_partials(body: str, resolve: PartialResolver, seen: List[str], depth: int) -> str:
    if depth > MAX_PARTIAL_DEPTH:
        raise PromptRenderError(
            f"partial include depth exceeded {MAX_PARTIAL_DEPTH} (chain: {' -> '.join(seen)})"
        )

    def replace(match: re.Match) -> str:
        target = match.group(1)
        if not target.startswith("partials."):
            raise PromptRenderError(
                f"'{target}' is not includable: only ids under 'partials.' may be included"
            )
        if target in seen:
            raise PromptRenderError(f"partial cycle detected: {' -> '.join(seen + [target])}")
        return _inline_partials(resolve(target), resolve, seen + [target], depth + 1)

    return _PARTIAL.sub(replace, body)


def render_body(
    body: str,
    variables: Dict[str, object],
    declared: List[str],
    resolve_partial: PartialResolver,
) -> str:
    """Inline partials, then substitute variables.

    Every placeholder must appear in ``declared`` and have a non-None value.
    Silent empty substitution is never correct for a prompt.
    """
    expanded = _inline_partials(body, resolve_partial, [], 1)

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in declared:
            raise PromptRenderError(f"'{{{{{name}}}}}' is used but not declared in 'variables'")
        if variables.get(name) is None:
            raise PromptRenderError(f"'{{{{{name}}}}}' is declared but no value was supplied")
        return str(variables[name])

    return _VARIABLE.sub(replace, expanded)


def _heading_key(title: str) -> str:
    return title.strip().lower().replace(" ", "_")


def split_sections(body: str, sections: List[str]) -> Tuple[str, Optional[str]]:
    """Split a body into (system channel, context channel).

    ``sections[0]`` names the ``# `` heading whose content is the system
    instruction. Everything else becomes the context message. With no declared
    sections the whole body is the system instruction.
    """
    body = body.strip()
    if not sections:
        return body, None

    system_key = sections[0]
    matches = list(_HEADING.finditer(body))
    if not matches:
        raise PromptRenderError(f"body has no '# ' headings but declares sections {sections}")

    chunks: Dict[str, str] = {}
    order: List[str] = []
    for index, match in enumerate(matches):
        key = _heading_key(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        chunks[key] = body[match.end() : end].strip()
        order.append(key)

    if system_key not in chunks:
        wanted = system_key.replace("_", " ").title()
        raise PromptRenderError(f"body is missing the '{wanted}' section")

    system_text = chunks[system_key]
    context_parts = [
        f"# {key.replace('_', ' ').title()}\n\n{chunks[key]}"
        for key in order
        if key != system_key and chunks[key]
    ]
    return system_text, "\n\n".join(context_parts) or None
