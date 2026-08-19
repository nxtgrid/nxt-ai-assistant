"""The seam between a knowledge module and whatever produces its body.

A module whose `source` is not 'manual' has no stored body. This module
defines what resolves one: the per-request authorization context a provider
needs, the provider protocol itself, and a registry mapping source -> provider.

Deliberately split from knowledge.py, which stays a pure value/selection
module with no I/O and no async.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, Tuple, runtime_checkable

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.types import RequestScope


@dataclass(frozen=True)
class ResolutionContext:
    """Who is asking, for authorization purposes.

    Deliberately separate from RequestScope, which answers "which modules
    apply to this conversation" (a selection question). This answers "what
    may this caller see" (an authorization question). Conflating them would
    let a visibility rule silently become an access-control rule.

    Tuples, not lists: providers cache on this object, and an unhashable
    field would defeat that silently rather than loudly.
    """

    scope: RequestScope
    user_email: Optional[str] = None
    organization_ids: Tuple[str, ...] = ()
    role_ids: Tuple[str, ...] = ()
    is_staff: bool = False

    @classmethod
    def from_user_context(cls, user_context, grid: Optional[str] = None) -> "ResolutionContext":
        """Build from an orchestrator UserContext (or None).

        Duck-typed rather than importing orchestrator.models.schemas: this
        module lives in `shared`, which must not depend on the orchestrator.
        Mirrors instructions_provider.py's existing convention of taking
        organization_ids[0] as the scope org.
        """
        if user_context is None:
            return cls(scope=RequestScope(grid=grid))
        org_ids = tuple(getattr(user_context, "organization_ids", None) or ())
        return cls(
            scope=RequestScope(grid=grid, organization_id=org_ids[0] if org_ids else None),
            user_email=getattr(user_context, "user_email", None),
            organization_ids=org_ids,
            role_ids=tuple(getattr(user_context, "roles", None) or ()),
            is_staff=bool(getattr(user_context, "is_staff", False)),
        )


@runtime_checkable
class ContextProvider(Protocol):
    """Produces a module's body at render time.

    Returning None means "nothing to contribute" and is normal, not an
    error -- an episodic module for a grid with no distillation yet, for
    instance. Raising is also survivable: the resolver catches it.
    """

    source: str

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]: ...


@dataclass
class ProviderRegistry:
    """Maps a module's `source` to the provider that resolves it."""

    _providers: Dict[str, ContextProvider] = field(default_factory=dict)

    def register(self, provider: ContextProvider) -> None:
        self._providers[provider.source] = provider

    def get(self, source: str) -> Optional[ContextProvider]:
        return self._providers.get(source)

    def sources(self) -> Tuple[str, ...]:
        return tuple(sorted(self._providers))


__all__ = ["ContextProvider", "ProviderRegistry", "ResolutionContext"]
