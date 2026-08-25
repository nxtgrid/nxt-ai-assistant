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


class _Auth:
    """Every lookup DirectoryProvider makes, all from the Auth DB.

    There is no jira_fetcher any more: the `participants()`/`organizations()`
    interface it expected had no implementation anywhere in the repo, and the
    two MCP tool names the legacy path called don't exist either. See the
    provider's module docstring.
    """

    def __init__(self, grids=None, all_grids=None, orgs=None, all_orgs=None, staff=None):
        self._grids = grids or {}
        self._all_grids = all_grids or []
        self._orgs = orgs or {}
        self._all_orgs = all_orgs or []
        self._staff = staff or []
        self.staff_calls = 0

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        return list(self._all_grids) if include_all else list(self._grids.get(organization_id, []))

    async def get_organization_names(self, organization_ids=None, include_all=False):
        if include_all:
            return list(self._all_orgs)
        return [n for o in (organization_ids or []) for n in self._orgs.get(o, [])]

    async def get_staff_member_names(self):
        self.staff_calls += 1
        return list(self._staff)


@pytest.mark.asyncio
async def test_customer_sees_only_their_own_org_grids():
    auth = _Auth(grids={"7": ["Alpha"], "9": ["Gamma"]}, all_grids=["Alpha", "Beta", "Gamma"])
    provider = DirectoryProvider(auth_service=auth)
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Alpha" in text
    assert "Beta" not in text
    assert "Gamma" not in text


@pytest.mark.asyncio
async def test_staff_sees_every_grid():
    auth = _Auth(grids={"7": ["Alpha"]}, all_grids=["Alpha", "Beta", "Gamma"])
    provider = DirectoryProvider(auth_service=auth)
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=("7",), is_staff=True)

    text = await provider.resolve(_module(), ctx)

    assert "Alpha" in text and "Beta" in text and "Gamma" in text


@pytest.mark.asyncio
async def test_a_customer_sees_their_own_organizations():
    """The line that was permanently empty before: orgs now come from the Auth DB."""
    auth = _Auth(
        grids={"7": ["Alpha"]},
        orgs={"7": ["Org Seven"], "9": ["Org Nine"]},
        all_orgs=["Org Seven", "Org Nine", "Staff Co"],
    )
    provider = DirectoryProvider(auth_service=auth)
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Org Seven" in text
    assert "Org Nine" not in text
    assert "Staff Co" not in text


@pytest.mark.asyncio
async def test_a_customer_in_two_orgs_sees_both():
    """Not organization_ids[0] -- the grid lookup's shortcut is not a pattern."""
    auth = _Auth(orgs={"7": ["Org Seven"], "9": ["Org Nine"]})
    provider = DirectoryProvider(auth_service=auth)
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7", "9"), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Org Seven" in text and "Org Nine" in text


@pytest.mark.asyncio
async def test_staff_see_every_organization_and_the_team_roster():
    auth = _Auth(all_orgs=["Org Seven", "Staff Co"], staff=["Ada L.", "Grace H."])
    provider = DirectoryProvider(auth_service=auth)
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=("2",), is_staff=True)

    text = await provider.resolve(_module(), ctx)

    assert "Org Seven" in text and "Staff Co" in text
    assert "Ada L." in text and "Grace H." in text


@pytest.mark.asyncio
async def test_a_customer_never_sees_the_team_roster():
    """Staff-only, the pre-existing rule -- and never even queried for."""
    auth = _Auth(grids={"7": ["Alpha"]}, orgs={"7": ["Org Seven"]}, staff=["Ada L."])
    provider = DirectoryProvider(auth_service=auth)
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Ada L." not in text
    assert "Team members" not in text
    assert auth.staff_calls == 0


@pytest.mark.asyncio
async def test_a_caller_with_no_orgs_gets_no_organizations():
    auth = _Auth(orgs={"7": ["Org Seven"]})
    provider = DirectoryProvider(auth_service=auth)
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=(), is_staff=False)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_a_failing_auth_service_yields_no_grids_not_an_exception():
    class _Broken:
        async def get_grid_names_for_organization(self, **_k):
            raise RuntimeError("auth down")

        async def get_organization_names(self, **_k):
            raise RuntimeError("auth down")

        async def get_staff_member_names(self):
            raise RuntimeError("auth down")

    provider = DirectoryProvider(auth_service=_Broken())
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=("7",), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_one_failing_lookup_does_not_lose_the_others():
    """A dead org query must not cost the grid list its line."""

    class _PartlyBroken(_Auth):
        async def get_organization_names(self, **_k):
            raise RuntimeError("orgs down")

    auth = _PartlyBroken(grids={"7": ["Alpha"]})
    provider = DirectoryProvider(auth_service=auth)
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Alpha" in text


@pytest.mark.asyncio
async def test_source_is_directory():
    # A placeholder, not None: None makes the constructor eagerly build a
    # real AuthService() (a live postgres connection needing AUTH_DB_*),
    # which this test -- just checking a class attribute -- has no business
    # requiring.
    assert DirectoryProvider(auth_service=object()).source == "directory"


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
