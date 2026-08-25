"""Google Drive-backed context module bodies.

Async and permission-gated: a module whose ``doc_audience`` is 'acl_mirror'
resolves only for a caller who can read the underlying file in Drive. This is
the reason `gdoc` sits on the JitContextResolver path rather than inside
PromptLibrary's synchronous render -- only the async path carries the caller's
identity (see shared/prompts/providers.py's ResolutionContext).

Every failure mode resolves to None, which the resolver treats as "this module
contributes nothing" rather than an error. That includes a denied access check,
a missing source_ref, a Drive outage and an empty document -- all fail closed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_TTL_SECONDS = 300
# Shorter than the content TTL on purpose: this is the revocation window.
# Someone who loses Drive access keeps resolving the body for up to this long.
DEFAULT_ACCESS_TTL_SECONDS = 60

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


class GDocProvider:
    """Resolves the `gdoc` source by Drive file id, gated on the caller."""

    source = "gdoc"

    def __init__(
        self,
        fetch: Optional[Callable[[str], Optional[str]]] = None,
        fetch_sheet: Optional[Callable[[str, Optional[str]], Optional[str]]] = None,
        mime_for: Optional[Callable[[str], Optional[str]]] = None,
        can_access: Optional[Callable[..., Any]] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        access_ttl_seconds: int = DEFAULT_ACCESS_TTL_SECONDS,
    ) -> None:
        self._fetch = fetch or _default_fetch
        self._fetch_sheet = fetch_sheet or _default_fetch_sheet
        self._mime_for = mime_for or _default_mime_for
        self._can_access = can_access or _default_can_access
        self._ttl = ttl_seconds
        self._access_ttl = access_ttl_seconds
        self._cache: Dict[Tuple[str, Optional[str]], Tuple[float, Optional[str]]] = {}
        self._access: Dict[Tuple[str, str], Tuple[float, bool]] = {}

    def invalidate(self) -> None:
        self._cache.clear()
        self._access.clear()

    async def visible_to(self, module: KnowledgeModule, ctx: ResolutionContext) -> bool:
        """Whether this caller may see the module at all. Never raises.

        Separate from resolve() so a caller can ask "may they see this?"
        without paying to fetch the body. It is not the gate on its own:
        resolve() calls this itself and returns None when denied, which is
        what actually keeps a denied document out of a prompt.
        """
        if module.doc_audience == "published":
            return True

        file_id = module.source_ref
        if not file_id or not ctx.user_email:
            return False

        key = (file_id, ctx.user_email)
        hit = self._access.get(key)
        if hit and hit[0] > time.time():
            return hit[1]

        try:
            allowed = await self._can_access(file_id, ctx.user_email, strict=True)
        except Exception:
            LOGGER.opt(exception=True).warning(
                f"Drive access check failed for module '{module.slug}'; withholding",
            )
            return False

        self._access[key] = (time.time() + self._access_ttl, bool(allowed))
        return bool(allowed)

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]:
        """The document's text for this caller, or None. Never raises."""
        if not module.source_ref:
            LOGGER.warning(f"Module '{module.slug}' is gdoc-sourced but has no source_ref")
            return None

        if not await self.visible_to(module, ctx):
            LOGGER.info(
                f"Module '{module.slug}' withheld from {ctx.user_email}: no Drive access"
            )
            return None

        return await self._body(module.source_ref, module.source_tab, module.slug)

    async def _body(
        self, file_id: str, tab: Optional[str], slug: str
    ) -> Optional[str]:
        key = (file_id, tab)
        hit = self._cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]

        try:
            # One mime lookup per cache miss, not per request -- the type of a
            # file is stable, and this sits behind the content cache.
            mime = await asyncio.to_thread(self._mime_for, file_id)
            if mime == SPREADSHEET_MIME:
                body = await asyncio.to_thread(self._fetch_sheet, file_id, tab)
            else:
                body = await asyncio.to_thread(self._fetch, file_id)
        except Exception:
            LOGGER.opt(exception=True).warning(f"Drive fetch failed for module '{slug}'")
            return None

        body = body.strip() if body else None
        self._cache[key] = (time.time() + self._ttl, body or None)
        return body or None


def _default_fetch(file_id: str) -> Optional[str]:
    from shared.prompts.gdoc import fetch_doc_text

    return fetch_doc_text(file_id)


def _default_fetch_sheet(file_id: str, tab: Optional[str]) -> Optional[str]:
    from shared.utils.gdrive_doc_fetcher import fetch_google_sheet_markdown

    return fetch_google_sheet_markdown(file_id, tab)


def _default_mime_for(file_id: str) -> Optional[str]:
    from shared.utils.gdrive_doc_fetcher import GoogleDriveDocFetcher

    meta = GoogleDriveDocFetcher().get_file_metadata(file_id) or {}
    return meta.get("mimeType")


async def _default_can_access(file_id: str, user_email: str, strict: bool = True) -> bool:
    from shared.utils.drive_permissions import user_can_access

    return await user_can_access(file_id, user_email, strict=strict)


__all__ = ["GDocProvider"]
