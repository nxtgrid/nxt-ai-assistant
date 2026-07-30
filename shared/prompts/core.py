"""The prompt library facade — the one entry point for rendering a prompt.

Named ``core`` rather than ``library`` because ``shared/prompts/library/`` is
the directory of bundled ``.prompt`` files; Python cannot have both a module
and a same-named subpackage directly inside one package. The public API is
unaffected: everything outside this package imports ``from shared.prompts
import PROMPTS``.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from shared.prompts.bundled import BundledStore
from shared.prompts.render import render_body, split_sections
from shared.prompts.spec import PromptSpec, body_checksum
from shared.prompts.types import PromptSource, RenderedPrompt, RequestScope
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DbBodyFor = Callable[[str], Optional[Tuple[str, int]]]
GDocBodyFor = Callable[[str], Optional[str]]


class PromptLibrary:
    """Resolves, renders and reports provenance for every prompt in Anansi.

    Resolution order per id: DB override, then attached Google Doc, then the
    bundled file. Frontmatter always comes from the bundled file, so an override
    can supply body text but can never change a prompt's overridability, output
    schema or access lists.
    """

    def __init__(
        self,
        bundled: Optional[BundledStore] = None,
        db_body_for: Optional[DbBodyFor] = None,
        gdoc_body_for: Optional[GDocBodyFor] = None,
    ) -> None:
        self._bundled = bundled or BundledStore()
        self._db_body_for = db_body_for
        self._gdoc_body_for = gdoc_body_for

    # ── introspection ────────────────────────────────────────────────────────
    def ids(self) -> List[str]:
        return self._bundled.ids()

    def spec(self, prompt_id: str) -> PromptSpec:
        return self._bundled.get(prompt_id)

    def reload(self) -> None:
        self._bundled.reload()

    # ── resolution ───────────────────────────────────────────────────────────
    def _resolve_body(self, spec: PromptSpec) -> Tuple[str, PromptSource, Optional[int]]:
        if not spec.overridable:
            return spec.body, PromptSource.BUNDLED, None

        if self._db_body_for is not None:
            try:
                found = self._db_body_for(spec.id)
                if found:
                    return found[0], PromptSource.DB, found[1]
            except Exception:
                LOGGER.warning(
                    f"Prompt override lookup failed for '{spec.id}'; "
                    f"falling through to doc/bundled",
                    exc_info=True,
                )

        if self._gdoc_body_for is not None:
            try:
                body = self._gdoc_body_for(spec.id)
                if body:
                    return body, PromptSource.GDOC, None
            except Exception:
                LOGGER.warning(
                    f"Prompt Google Doc lookup failed for '{spec.id}'; using bundled",
                    exc_info=True,
                )

        return spec.body, PromptSource.BUNDLED, None

    def _partial(self, prompt_id: str) -> str:
        """Partials always come from the bundled store.

        A partial is shared infrastructure; letting it be overridden would make
        one operator's edit silently change every prompt that includes it.
        """
        return self._bundled.get(prompt_id).body

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

        result = RenderedPrompt(
            prompt_id=prompt_id,
            system_text=system_text,
            context_text=context_text,
            source=source,
            version=version,
            checksum=body_checksum(body),
        )
        LOGGER.debug(f"Rendered prompt {result.provenance()}")
        return result

    def text(self, prompt_id: str, **vars: object) -> str:
        """Convenience for single-channel prompts: the full rendered body."""
        rendered = self.render(prompt_id, vars=vars)
        if rendered.context_text:
            return f"{rendered.system_text}\n\n{rendered.context_text}"
        return rendered.system_text


def _build_default_library() -> PromptLibrary:
    from shared.prompts.gdoc import GDocStore

    return PromptLibrary(gdoc_body_for=GDocStore().body_for)


PROMPTS = _build_default_library()
