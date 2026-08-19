"""The prompt library facade — the one entry point for rendering a prompt.

Named ``core`` rather than ``library`` because ``shared/prompts/library/`` is
the directory of bundled ``.prompt`` files; Python cannot have both a module
and a same-named subpackage directly inside one package. The public API is
unaffected: everything outside this package imports ``from shared.prompts
import PROMPTS``.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.prompts.bundled import BundledStore
from shared.prompts.knowledge import (
    budget_pinned,
    render_catalog,
    render_pinned,
    select_for_prompt,
)
from shared.prompts.render import render_body, split_sections
from shared.prompts.spec import PromptSpec, body_checksum
from shared.prompts.types import PromptSource, RenderedPrompt, RequestScope
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DbBodyFor = Callable[[str], Optional[Tuple[str, int]]]
GDocBodyFor = Callable[[str], Optional[str]]
DocOverrideFor = Callable[[str], bool]


class PromptLibrary:
    """Resolves, renders and reports provenance for every prompt in Anansi.

    Resolution order per id: DB override, then attached Google Doc, then the
    bundled file -- unless the prompt's doc binding has its override flag set,
    in which case the doc goes first instead (see _resolve_body). Frontmatter
    always comes from the bundled file, so a body override can supply text
    but can never change a prompt's overridability, output schema or access
    lists. Model tier is the one deliberate exception -- see spec() -- since
    a tier choice is meant to be admin-changeable without a PR.
    """

    def __init__(
        self,
        bundled: Optional[BundledStore] = None,
        db_body_for: Optional[DbBodyFor] = None,
        gdoc_body_for: Optional[GDocBodyFor] = None,
        invalidate_gdoc: Optional[Callable[[], None]] = None,
        overrides: Optional[Any] = None,
        knowledge: Optional[Any] = None,
        doc_override_for: Optional[DocOverrideFor] = None,
        gdoc_module_provider: Optional[Any] = None,
    ) -> None:
        self._bundled = bundled or BundledStore()
        # `overrides` (an OverrideStore, or any object with the same duck-typed
        # shape) supersedes a bare `db_body_for` callable: it's required for
        # propose()/publish() below, which need more than just a body lookup.
        self._overrides = overrides
        self._db_body_for = overrides.body_for if overrides is not None else db_body_for
        self._gdoc_body_for = gdoc_body_for
        self._invalidate_gdoc = invalidate_gdoc
        self._knowledge = knowledge
        self._gdoc_modules = gdoc_module_provider
        # getattr, not direct attribute access: existing tests pass minimal
        # duck-typed `overrides` fakes (e.g. shared/tests/test_prompt_write_api.py's
        # RecordingStore) that predate this method and have no reason to grow
        # it just to keep constructing. None here means "no toggle wired up",
        # which _resolve_body treats as always the pre-toggle DB-first order.
        self._doc_override_for = (
            getattr(overrides, "doc_override_for", doc_override_for)
            if overrides is not None
            else doc_override_for
        )

    # ── introspection ────────────────────────────────────────────────────────
    def ids(self) -> List[str]:
        return self._bundled.ids()

    def spec(self, prompt_id: str) -> PromptSpec:
        """This prompt's frontmatter, with a live model-tier override merged
        in if one exists.

        The only merge point: overrides never change overridability, output
        schema or access (see the class docstring) -- model tier is the one
        exception, deliberately, because a tier choice is meant to be
        admin-changeable without a PR. getattr-with-default, not a direct
        attribute access: existing minimal overrides fakes that predate
        model_tier_for (e.g. shared/tests/test_prompt_write_api.py's
        RecordingStore) have no reason to grow it just to keep constructing,
        same rationale as doc_override_for above.
        """
        base = self._bundled.get(prompt_id)
        if self._overrides is None:
            return base
        model_tier_for = getattr(self._overrides, "model_tier_for", None)
        tier = model_tier_for(prompt_id) if model_tier_for else None
        if tier is None:
            return base
        return dataclasses.replace(base, model=tier)

    def reload(self) -> None:
        self._bundled.reload()

    def invalidate_doc_cache(self) -> None:
        """Force the next render to re-fetch from Google Docs.

        For callers that know a doc's content just changed (e.g. after an
        automated doc edit) and don't want to wait out the TTL cache.
        """
        if self._invalidate_gdoc is not None:
            self._invalidate_gdoc()

    # ── resolution ───────────────────────────────────────────────────────────
    def _try_db(self, prompt_id: str) -> Optional[Tuple[str, PromptSource, Optional[int]]]:
        if self._db_body_for is None:
            return None
        try:
            found = self._db_body_for(prompt_id)
            if found:
                return found[0], PromptSource.DB, found[1]
        except Exception:
            LOGGER.warning(
                f"Prompt override lookup failed for '{prompt_id}'; continuing to the next source",
                exc_info=True,
            )
        return None

    def _try_gdoc(self, prompt_id: str) -> Optional[Tuple[str, PromptSource, Optional[int]]]:
        if self._gdoc_body_for is None:
            return None
        try:
            body = self._gdoc_body_for(prompt_id)
            if body:
                return body, PromptSource.GDOC, None
        except Exception:
            LOGGER.warning(
                f"Prompt Google Doc lookup failed for '{prompt_id}'; "
                f"continuing to the next source",
                exc_info=True,
            )
        return None

    def _resolve_body(self, spec: PromptSpec) -> Tuple[str, PromptSource, Optional[int]]:
        if not spec.overridable:
            return spec.body, PromptSource.BUNDLED, None

        # Default order: DB, then doc, then bundled. A prompt whose doc
        # binding has is_override=True flips DB and doc -- the doc wins even
        # when a DB version exists, instead of a saved-but-inactive draft
        # silently losing to it. No override_for wired up (None) always means
        # the default order: byte-identical to before this toggle existed.
        is_override = bool(self._doc_override_for(spec.id)) if self._doc_override_for else False
        order = (self._try_gdoc, self._try_db) if is_override else (self._try_db, self._try_gdoc)

        for attempt in order:
            result = attempt(spec.id)
            if result is not None:
                return result

        return spec.body, PromptSource.BUNDLED, None

    def _partial(self, prompt_id: str) -> str:
        """Partials always come from the bundled store.

        A partial is shared infrastructure; letting it be overridden would make
        one operator's edit silently change every prompt that includes it.
        """
        return self._bundled.get(prompt_id).body

    def _with_resolved_body(self, module):
        """Fill in a gdoc module's body. Other sources pass through."""
        if module.source != "gdoc" or self._gdoc_modules is None:
            return module
        return dataclasses.replace(module, body=self._gdoc_modules.body_for(module))

    def _compose_knowledge(
        self, spec: PromptSpec, scope: RequestScope
    ) -> Tuple[Optional[str], List[str]]:
        """Resolve, budget and render this prompt's knowledge. Never raises."""
        if self._knowledge is None:
            return None, []
        try:
            modules = self._knowledge.all_modules()
            pins = self._knowledge.overrides_for(spec.id)
        except Exception:
            LOGGER.warning(
                f"Knowledge lookup failed for '{spec.id}'; rendering without it", exc_info=True
            )
            return None, []

        chosen = select_for_prompt(modules, pins, scope)
        # A gdoc module has no stored body; resolve it here, synchronously,
        # the same way prompt-level doc overrides already resolve. JIT
        # sources (graph/directory/episodic) are handled by
        # JitContextResolver instead and are skipped entirely.
        chosen = [m for m in chosen if not m.is_jit]
        chosen = [self._with_resolved_body(m) for m in chosen]
        chosen = [m for m in chosen if m.body]

        pinned, _dropped = budget_pinned([m for m in chosen if m.mode == "pinned"])
        on_demand = [m for m in chosen if m.mode == "on_demand"]

        blocks = [b for b in (render_pinned(pinned), render_catalog(on_demand)) if b]
        used = [m.slug for m in pinned] + [m.slug for m in on_demand]
        return ("\n\n".join(blocks) or None), used

    def resolve(self, prompt_id: str) -> Tuple[str, PromptSource, Optional[int]]:
        """The current body exactly as stored (DB, then Doc, then bundled).

        Unlike ``render``, this never substitutes ``{{var}}`` placeholders or
        inlines partials -- for editors and diff tools that need the raw
        template, where an edit can be saved straight back through
        ``propose`` unchanged. ``render`` requires real variable values,
        which callers here (an admin page listing every prompt) don't have.
        """
        spec = self._bundled.get(prompt_id)
        return self._resolve_body(spec)

    def render(
        self,
        prompt_id: str,
        vars: Optional[Dict[str, object]] = None,
        scope: Optional[RequestScope] = None,
    ) -> RenderedPrompt:
        """Resolve, render and return a prompt with full provenance."""
        spec = self._bundled.get(prompt_id)
        body, source, version = self._resolve_body(spec)

        rendered = render_body(body, vars or {}, spec.variables, self._partial)
        system_text, context_text = split_sections(rendered, spec.sections)

        knowledge_text, knowledge_used = self._compose_knowledge(spec, scope or RequestScope())
        if knowledge_text:
            context_text = f"{context_text}\n\n{knowledge_text}" if context_text else knowledge_text

        result = RenderedPrompt(
            prompt_id=prompt_id,
            system_text=system_text,
            context_text=context_text,
            source=source,
            version=version,
            checksum=body_checksum(body),
            knowledge_used=knowledge_used,
        )
        LOGGER.debug(f"Rendered prompt {result.provenance()}")
        return result

    def text(self, prompt_id: str, **vars: object) -> str:
        """Convenience for single-channel prompts: the full rendered body."""
        rendered = self.render(prompt_id, vars=vars)
        if rendered.context_text:
            return f"{rendered.system_text}\n\n{rendered.context_text}"
        return rendered.system_text

    # ── write API ────────────────────────────────────────────────────────────
    def propose(
        self,
        prompt_id: str,
        body: str,
        note: str,
        actor: str,
        via: str = "ui",
        enforce_access: bool = True,
    ) -> int:
        """Append a new version. Never makes it live.

        ``enforce_access=False`` is for trusted backend callers, which have no
        email identity. They may propose; ``publish`` still refuses them.
        """
        from shared.prompts.access import can_edit_prompt

        spec = self._bundled.get(prompt_id)
        if enforce_access and not can_edit_prompt(spec, actor):
            raise PermissionError(f"{actor} may not edit prompt '{prompt_id}'")
        if not enforce_access and not spec.overridable:
            raise PermissionError(f"prompt '{prompt_id}' is not overridable")
        if self._overrides is None:
            raise RuntimeError("prompt override store is not configured")
        return self._overrides.propose(prompt_id, body, note=note, actor=actor, via=via)

    def publish(self, prompt_id: str, version: int, actor: str, via: str = "ui") -> None:
        """Make a version live. Humans only."""
        from shared.prompts.access import can_publish_prompt

        if via != "ui":
            raise PermissionError(
                "Automated callers may propose a prompt version but never publish one; "
                "a human with the publish verb must promote it"
            )
        spec = self._bundled.get(prompt_id)
        if not can_publish_prompt(spec, actor):
            raise PermissionError(f"{actor} may not publish prompt '{prompt_id}'")
        if self._overrides is None:
            raise RuntimeError("prompt override store is not configured")
        self._overrides.publish(prompt_id, version, actor=actor)


def _build_default_library() -> PromptLibrary:
    from shared.prompts.gdoc import GDocStore
    from shared.prompts.knowledge import KnowledgeStore
    from shared.prompts.overrides import OverrideStore
    from shared.prompts.providers_gdoc import GDocProvider

    overrides = OverrideStore.from_env()
    gdoc_store = GDocStore(doc_id_for=overrides.doc_id_for)
    return PromptLibrary(
        overrides=overrides,
        gdoc_body_for=gdoc_store.body_for,
        invalidate_gdoc=gdoc_store.invalidate,
        knowledge=KnowledgeStore.from_env(),
        gdoc_module_provider=GDocProvider(),
    )


PROMPTS = _build_default_library()
