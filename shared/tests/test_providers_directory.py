"""Directory provider: grids, organizations and users, permission-filtered."""

import pytest

from shared.prompts import providers_directory as directory_provider
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.prompts.providers_directory import (
    DirectoryProvider,
    render_directory,
)
from shared.prompts.types import RequestScope


@pytest.fixture(autouse=True)
def _clear_directory_cache():
    """_CACHE is deliberately module-level (production perf; keyed on the
    permission set, not the module -- see the module docstring). That makes
    it process-lifetime state, which leaks between tests in this file: e.g.
    a staff-scoped grid fetch cached by one test silently short-circuits a
    later test that expects the (fake, failing) auth service to be called
    at all. Clear it before every test rather than relaxing the cache
    itself, which is a real permission-safety property in production.
    """
    directory_provider._CACHE.clear()
    yield
    directory_provider._CACHE.clear()


def _module():
    return KnowledgeModule(
        id="d", slug="directory", title="Directory", summary="Known entities.",
        body=None, source="directory",
    )


def test_render_includes_every_populated_section():
    text = render_directory(
        grids=["Alpha", "Beta"], organizations=["Org A"], users=["Ada L."]
    )
    assert "Alpha, Beta" in text
    assert "Org A" in text
    assert "Ada L." in text


def test_render_omits_empty_sections():
    text = render_directory(grids=["Alpha"], organizations=[], users=[])
    assert "Alpha" in text
    assert "organizations" not in text.lower()


def test_render_returns_none_when_everything_is_empty():
    assert render_directory(grids=[], organizations=[], users=[]) is None


def test_render_includes_the_disambiguation_hint():
    text = render_directory(grids=["Alpha"], organizations=[], users=[])
    assert "matches a grid" in text


@pytest.mark.asyncio
async def test_customer_sees_only_their_own_org_grids():
    class _Auth:
        async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
            if include_all:
                return ["Alpha", "Beta", "Gamma"]
            return {"7": ["Alpha"], "9": ["Gamma"]}[organization_id]

    provider = DirectoryProvider(auth_service=_Auth(), jira_fetcher=None)
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Alpha" in text
    assert "Beta" not in text
    assert "Gamma" not in text


@pytest.mark.asyncio
async def test_staff_sees_every_grid():
    class _Auth:
        async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
            return ["Alpha", "Beta", "Gamma"] if include_all else ["Alpha"]

    provider = DirectoryProvider(auth_service=_Auth(), jira_fetcher=None)
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=("7",), is_staff=True)

    text = await provider.resolve(_module(), ctx)

    assert "Alpha" in text and "Beta" in text and "Gamma" in text


@pytest.mark.asyncio
async def test_customer_never_sees_jira_users_or_organizations():
    """Jira data is staff-only -- the pre-existing rule in ContextEnrichmentProvider."""

    class _Auth:
        async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
            return ["Alpha"]

    class _Jira:
        async def participants(self):
            return ["Ada L."]

        async def organizations(self):
            return ["Org A"]

    provider = DirectoryProvider(auth_service=_Auth(), jira_fetcher=_Jira())
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Ada L." not in text
    assert "Org A" not in text


@pytest.mark.asyncio
async def test_a_failing_auth_service_yields_no_grids_not_an_exception():
    class _Auth:
        async def get_grid_names_for_organization(self, **_k):
            raise RuntimeError("auth down")

    provider = DirectoryProvider(auth_service=_Auth(), jira_fetcher=None)
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=("7",), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_source_is_directory():
    # A placeholder, not None: None makes the constructor eagerly build a
    # real AuthService() (a live postgres connection needing AUTH_DB_*),
    # which this test -- just checking a class attribute -- has no business
    # requiring.
    assert DirectoryProvider(auth_service=object(), jira_fetcher=None).source == "directory"


def test_render_directory_matches_the_hardcoded_formatter():
    """Parity with ContextEnrichmentProvider._format_enrichment_text.

    Guards the deletion in Task 14 (deferred pending production seeding --
    see the plan). The only intended difference is label wording: 'Jira Ops
    team members' becomes 'Team members' and 'Available JIRA organizations'
    becomes 'Available organizations', because the module is no longer
    Jira-specific by definition.
    """
    from orchestrator.services.context_enrichment import ContextEnrichmentProvider

    grids = ["Alpha", "Beta"]
    users = ["Ada L."]
    orgs = ["Org A"]

    old = ContextEnrichmentProvider._format_enrichment_text(
        ContextEnrichmentProvider.__new__(ContextEnrichmentProvider), grids, users, orgs
    )
    new = render_directory(grids=grids, organizations=orgs, users=users)

    for name in grids + users + orgs:
        assert name in old and name in new
    assert "matches a grid" in old and "matches a grid" in new
