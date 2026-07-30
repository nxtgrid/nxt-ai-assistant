"""Loads the bundled ``.prompt`` files that ship with the application.

This store is the floor of every resolution: it is always available, needs no
network or database, and is the authority for prompt frontmatter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from shared.prompts.spec import PromptSpec, parse_prompt_file
from shared.prompts.types import PromptNotFound

DEFAULT_DIRECTORY = Path(__file__).parent / "library"


class BundledStore:
    """Parses and caches ``<id>.prompt`` files from a directory."""

    def __init__(self, directory: Optional[Path] = None) -> None:
        self._directory = Path(directory) if directory else DEFAULT_DIRECTORY
        self._specs: Optional[Dict[str, PromptSpec]] = None

    def _load(self) -> Dict[str, PromptSpec]:
        if self._specs is not None:
            return self._specs
        specs: Dict[str, PromptSpec] = {}
        for path in sorted(self._directory.glob("*.prompt")):
            spec = parse_prompt_file(path.read_text(), path=str(path))
            expected = path.name[: -len(".prompt")]
            if spec.id != expected:
                raise ValueError(
                    f"{path}: declared id '{spec.id}' does not match filename '{expected}'"
                )
            specs[spec.id] = spec
        self._specs = specs
        return specs

    def reload(self) -> None:
        """Drop the parse cache. Used by tests and the admin 'reload' action."""
        self._specs = None

    def ids(self) -> List[str]:
        return list(self._load().keys())

    def get(self, prompt_id: str) -> PromptSpec:
        specs = self._load()
        if prompt_id not in specs:
            raise PromptNotFound(f"no bundled prompt with id '{prompt_id}'")
        return specs[prompt_id]
