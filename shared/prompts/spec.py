"""Parsing for ``.prompt`` files — YAML frontmatter plus a markdown body."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from shared.prompts.components import UNCATEGORIZED

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class AccessSpec:
    """Default group bindings for the three verbs."""

    view: List[str] = field(default_factory=list)
    edit: List[str] = field(default_factory=list)
    publish: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PromptSpec:
    """A parsed ``.prompt`` file. Frontmatter here is authoritative.

    Overrides supply body text only, so a UI edit can never change
    ``overridable``, ``output``, ``schema`` or ``access``.
    """

    id: str
    description: str
    body: str
    checksum: str
    owner: str = "eng"
    component: str = UNCATEGORIZED
    overridable: bool = False
    output: str = "text"
    schema: Optional[Dict[str, Any]] = None
    model: str = "fast"
    variables: List[str] = field(default_factory=list)
    sections: List[str] = field(default_factory=list)
    access: AccessSpec = field(default_factory=AccessSpec)


def body_checksum(body: str) -> str:
    """Content address for a prompt body. Body only — frontmatter excluded."""
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def parse_prompt_file(text: str, *, path: str) -> PromptSpec:
    """Parse ``.prompt`` file contents. ``path`` is used only in error messages."""
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"{path}: missing YAML frontmatter (expected a leading '---' block)")

    raw = yaml.safe_load(match.group(1)) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")

    body = match.group(2)

    prompt_id = raw.get("id")
    if not prompt_id:
        raise ValueError(f"{path}: frontmatter is missing required field 'id'")
    description = raw.get("description")
    if not description:
        raise ValueError(f"{path}: frontmatter is missing required field 'description'")

    output = raw.get("output", "text")
    schema = raw.get("schema")
    if output == "json" and not schema:
        raise ValueError(f"{path}: output 'json' requires a 'schema' field")

    model = raw.get("model", "fast")
    if model not in ("thinking", "fast", "lite"):
        raise ValueError(
            f"{path}: frontmatter 'model' must be one of thinking/fast/lite, got {model!r}"
        )

    access_raw = raw.get("access") or {}
    access = AccessSpec(
        view=list(access_raw.get("view") or []),
        edit=list(access_raw.get("edit") or []),
        publish=list(access_raw.get("publish") or []),
    )

    return PromptSpec(
        id=str(prompt_id),
        description=str(description),
        body=body,
        checksum=body_checksum(body),
        owner=str(raw.get("owner", "eng")),
        component=str(raw.get("component", UNCATEGORIZED)),
        overridable=bool(raw.get("overridable", False)),
        output=str(output),
        schema=schema,
        model=model,
        variables=list(raw.get("variables") or []),
        sections=list(raw.get("sections") or []),
        access=access,
    )
