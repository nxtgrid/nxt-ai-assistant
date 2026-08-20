"""Google Doc/Sheet-backed context modules and their access gate."""

import pytest

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.prompts.providers_gdoc import GDocProvider
from shared.prompts.types import RequestScope

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _module(source_ref="doc-abc", audience="acl_mirror", tab=None):
    return KnowledgeModule(
        id="d", slug="procedures", title="Procedures", summary="How-tos.",
        body=None, source="gdoc", source_ref=source_ref, source_tab=tab,
        doc_audience=audience,
    )


def _ctx(email="tech@example.com"):
    return ResolutionContext(scope=RequestScope(), user_email=email)


def _provider(allowed=True, doc_text="doc body", sheet_text="| A |", mime=DOC_MIME, **kw):
    async def _can_access(_file_id, _email, strict=False):
        return allowed

    return GDocProvider(
        fetch=lambda _id: doc_text,
        fetch_sheet=lambda _id, _tab: sheet_text,
        mime_for=lambda _id: mime,
        can_access=_can_access,
        **kw,
    )


@pytest.mark.asyncio
async def test_an_allowed_caller_gets_the_doc_body():
    assert await _provider().resolve(_module(), _ctx()) == "doc body"


@pytest.mark.asyncio
async def test_a_denied_caller_gets_nothing():
    assert await _provider(allowed=False).resolve(_module(), _ctx()) is None


@pytest.mark.asyncio
async def test_a_published_module_skips_the_access_check_entirely():
    """The deliberate opt-out: curated doc content meant for customers."""
    checked = []

    async def _can_access(file_id, _email, strict=False):
        checked.append(file_id)
        return False

    provider = GDocProvider(
        fetch=lambda _id: "public body",
        mime_for=lambda _id: DOC_MIME,
        can_access=_can_access,
    )
    result = await provider.resolve(_module(audience="published"), _ctx())

    assert result == "public body"
    assert checked == []


@pytest.mark.asyncio
async def test_a_caller_with_no_email_is_denied():
    assert await _provider().resolve(_module(), _ctx(email=None)) is None


@pytest.mark.asyncio
async def test_a_module_without_a_source_ref_resolves_to_none():
    assert await _provider().resolve(_module(source_ref=None), _ctx()) is None


@pytest.mark.asyncio
async def test_a_sheet_module_uses_the_sheet_fetcher():
    provider = _provider(mime=SHEET_MIME, sheet_text="| Code |\n| --- |\n| E01 |")
    assert "E01" in await provider.resolve(_module(tab="Errors"), _ctx())


@pytest.mark.asyncio
async def test_the_tab_is_passed_through_to_the_sheet_fetcher():
    seen = {}

    def _fetch_sheet(file_id, tab):
        seen["tab"] = tab
        return "| A |"

    async def _ok(*_a, **_kw):
        return True

    provider = GDocProvider(
        fetch_sheet=_fetch_sheet, mime_for=lambda _id: SHEET_MIME, can_access=_ok
    )
    await provider.resolve(_module(tab="Thresholds"), _ctx())

    assert seen["tab"] == "Thresholds"


@pytest.mark.asyncio
async def test_a_failing_fetch_resolves_to_none():
    def _boom(_id):
        raise RuntimeError("403")

    async def _ok(*_a, **_kw):
        return True

    provider = GDocProvider(fetch=_boom, mime_for=lambda _id: DOC_MIME, can_access=_ok)
    assert await provider.resolve(_module(), _ctx()) is None


@pytest.mark.asyncio
async def test_a_failing_access_check_fails_closed():
    async def _boom(*_a, **_kw):
        raise RuntimeError("drive down")

    provider = GDocProvider(
        fetch=lambda _id: "body", mime_for=lambda _id: DOC_MIME, can_access=_boom
    )
    assert await provider.resolve(_module(), _ctx()) is None


@pytest.mark.asyncio
async def test_an_empty_doc_resolves_to_none():
    assert await _provider(doc_text="   ").resolve(_module(), _ctx()) is None


@pytest.mark.asyncio
async def test_content_is_cached_per_file_and_tab():
    calls = []

    def _fetch(file_id):
        calls.append(file_id)
        return "body"

    async def _ok(*_a, **_kw):
        return True

    provider = GDocProvider(fetch=_fetch, mime_for=lambda _id: DOC_MIME, can_access=_ok)
    await provider.resolve(_module(), _ctx())
    await provider.resolve(_module(), _ctx())

    assert calls == ["doc-abc"]


@pytest.mark.asyncio
async def test_access_is_cached_per_file_and_caller():
    calls = []

    async def _can_access(file_id, email, strict=False):
        calls.append((file_id, email))
        return True

    provider = GDocProvider(
        fetch=lambda _id: "body", mime_for=lambda _id: DOC_MIME, can_access=_can_access
    )
    await provider.resolve(_module(), _ctx("a@example.com"))
    await provider.resolve(_module(), _ctx("a@example.com"))
    await provider.resolve(_module(), _ctx("b@example.com"))

    assert calls == [("doc-abc", "a@example.com"), ("doc-abc", "b@example.com")]


@pytest.mark.asyncio
async def test_the_access_check_is_always_strict():
    """Non-strict would grant every caller read on any link-shared file."""
    seen = {}

    async def _can_access(_file_id, _email, strict=False):
        seen["strict"] = strict
        return True

    provider = GDocProvider(
        fetch=lambda _id: "body", mime_for=lambda _id: DOC_MIME, can_access=_can_access
    )
    await provider.resolve(_module(), _ctx())

    assert seen["strict"] is True


@pytest.mark.asyncio
async def test_visible_to_does_not_fetch_content():
    """It gates the on-demand catalog line, where the body is not wanted."""
    fetched = []

    async def _ok(*_a, **_kw):
        return True

    provider = GDocProvider(
        fetch=lambda i: fetched.append(i), mime_for=lambda _id: DOC_MIME, can_access=_ok
    )
    assert await provider.visible_to(_module(), _ctx()) is True
    assert fetched == []


@pytest.mark.asyncio
async def test_invalidate_clears_both_caches():
    calls = []

    async def _can_access(file_id, email, strict=False):
        calls.append("access")
        return True

    def _fetch(file_id):
        calls.append("fetch")
        return "body"

    provider = GDocProvider(
        fetch=_fetch, mime_for=lambda _id: DOC_MIME, can_access=_can_access
    )
    await provider.resolve(_module(), _ctx())
    provider.invalidate()
    await provider.resolve(_module(), _ctx())

    assert calls == ["access", "fetch", "access", "fetch"]
