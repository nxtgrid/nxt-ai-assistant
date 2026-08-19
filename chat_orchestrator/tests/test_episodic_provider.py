"""Episodic provider: per-grid / per-org distillation lookup."""

import pytest

from orchestrator.services.providers.episodic_provider import EpisodicProvider
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.prompts.types import RequestScope


def _module():
    return KnowledgeModule(
        id="e", slug="episodic", title="Prior History", summary="What happened before.",
        body=None, source="episodic",
    )


# grid_access is async (awaited by resolve()): the default implementation
# calls the real, async AuthService.get_grid_names_for_organization, and a
# sync stand-in would either need its own event-loop juggling (broken when
# called from inside resolve()'s already-running loop) or would silently
# diverge from what production actually does. These two cover the fakes
# every test below needs.
async def _allow(*_args):
    return True


async def _deny(*_args):
    return False


class _Client:
    def __init__(self, rows, permitted_grids=None):
        self._rows = rows
        self._permitted = permitted_grids
        self.filters = {}

    def table(self, _name):
        return self

    def select(self, _cols):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def limit(self, _n):
        return self

    def execute(self):
        class _R:
            pass

        r = _R()
        r.data = [
            row
            for row in self._rows
            if all(row.get(k) == v for k, v in self.filters.items())
        ]
        return r


@pytest.mark.asyncio
async def test_returns_the_distillation_for_the_scoped_grid():
    client = _Client([
        {"anchor_type": "grid", "anchor_id": "Alpha", "anchor_name": "Alpha",
         "summary": "Recurring inverter faults since June."},
    ])
    provider = EpisodicProvider(client=client, grid_access=_allow)
    ctx = ResolutionContext(scope=RequestScope(grid="Alpha"), is_staff=True)

    text = await provider.resolve(_module(), ctx)

    assert "Recurring inverter faults since June." in text


@pytest.mark.asyncio
async def test_returns_nothing_when_scope_names_no_anchor():
    client = _Client([{"anchor_type": "grid", "anchor_id": "Alpha", "summary": "x"}])
    provider = EpisodicProvider(client=client, grid_access=_allow)
    ctx = ResolutionContext(scope=RequestScope(), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_returns_nothing_when_no_distillation_exists_yet():
    provider = EpisodicProvider(client=_Client([]), grid_access=_allow)
    ctx = ResolutionContext(scope=RequestScope(grid="Alpha"), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_a_caller_without_grid_access_gets_nothing():
    client = _Client([
        {"anchor_type": "grid", "anchor_id": "Alpha", "summary": "secret history"},
    ])
    provider = EpisodicProvider(client=client, grid_access=_deny)
    ctx = ResolutionContext(
        scope=RequestScope(grid="Alpha"), organization_ids=("9",), is_staff=False
    )

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_organization_scope_is_used_when_no_grid_is_named():
    client = _Client([
        {"anchor_type": "organization", "anchor_id": "7", "summary": "Org-wide billing issues."},
    ])
    provider = EpisodicProvider(client=client, grid_access=_allow)
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7",), is_staff=False
    )

    text = await provider.resolve(_module(), ctx)

    assert "Org-wide billing issues." in text


@pytest.mark.asyncio
async def test_a_failing_query_resolves_to_none():
    class _Boom:
        def table(self, _n):
            raise RuntimeError("relation does not exist")

    provider = EpisodicProvider(client=_Boom(), grid_access=_allow)
    ctx = ResolutionContext(scope=RequestScope(grid="Alpha"), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None
