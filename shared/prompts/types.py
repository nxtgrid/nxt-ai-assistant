"""Value types for the prompt library."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class PromptSource(str, Enum):
    """Where a rendered prompt's body came from."""

    DB = "db"
    GDOC = "gdoc"
    BUNDLED = "bundled"


class PromptError(Exception):
    """Base class for prompt library errors."""


class PromptNotFound(PromptError):
    """No prompt with this id exists in the bundled library."""


class PromptRenderError(PromptError):
    """A prompt could not be rendered (bad variable, bad partial)."""


@dataclass(frozen=True)
class RequestScope:
    """The entity context a prompt is being rendered for.

    Used to decide which scoped knowledge modules apply.
    """

    grid: Optional[str] = None
    organization_id: Optional[str] = None

    def matches(self, scope: str) -> bool:
        """Whether a module declaring ``scope`` applies to this request.

        'sector' is the pre-0018 spelling of 'global' and stays accepted
        permanently: this method fails closed on an unknown value, so a row
        the rename missed would stop contributing with no error anywhere.
        """
        if scope in ("global", "sector"):
            return True
        if scope.startswith("site:"):
            return bool(self.grid) and scope[5:].lower() == (self.grid or "").lower()
        if scope.startswith("org:"):
            return bool(self.organization_id) and scope[4:] == self.organization_id
        return False


@dataclass
class RenderedPrompt:
    """A prompt resolved, rendered, and ready to send, with provenance."""

    prompt_id: str
    system_text: str
    context_text: Optional[str]
    source: PromptSource
    version: Optional[int]
    checksum: str
    knowledge_used: List[str] = field(default_factory=list)

    def provenance(self) -> str:
        """Compact identity for logs and traces."""
        version = f"v{self.version}" if self.version is not None else "default"
        return f"{self.prompt_id}@{self.source.value}:{version}:{self.checksum[:8]}"
