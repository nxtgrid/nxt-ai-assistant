"""Graph provider: the ontology primer, permission-filtered."""

import pytest

from orchestrator.services.providers.graph_provider import GraphProvider, render_primer
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.prompts.types import RequestScope


def _module():
    return KnowledgeModule(
        id="g", slug="entity-graph", title="Knowledge Graph", summary="Ontology.",
        body=None, source="graph",
    )


def _rows():
    return [
        {"kind": "entity", "type_name": "Meter", "item_count": 120,
         "examples": ["M-001", "M-002", "M-003"]},
        {"kind": "entity", "type_name": "DCU", "item_count": 18,
         "examples": ["DCU-7721"]},
        {"kind": "relationship", "type_name": "connected_to", "item_count": 140,
         "examples": []},
    ]


def test_primer_lists_entity_types_with_counts():
    text = render_primer(_rows())
    assert "Meter" in text and "120" in text
    assert "DCU" in text and "18" in text


def test_primer_lists_relationship_types():
    text = render_primer(_rows())
    assert "connected_to" in text and "140" in text


def test_primer_includes_examples_for_entity_types():
    assert "M-001" in render_primer(_rows())


def test_primer_returns_none_for_no_rows():
    assert render_primer([]) is None


def test_primer_stays_compact():
    """It is pinned into every request that attaches the module."""
    assert len(render_primer(_rows())) < 1500


@pytest.mark.asyncio
async def test_staff_query_passes_null_org_ids():
    seen = {}

    class _Client:
        def rpc(self, name, params):
            seen["name"] = name
            seen["params"] = params
            return self

        def execute(self):
            class _R:
                data = _rows()
            return _R()

    provider = GraphProvider(client=_Client())
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=("7",), is_staff=True)

    await provider.resolve(_module(), ctx)

    assert seen["name"] == "summarize_entity_graph"
    assert seen["params"]["p_org_ids"] is None


@pytest.mark.asyncio
async def test_customer_query_passes_their_org_ids():
    seen = {}

    class _Client:
        def rpc(self, name, params):
            seen["params"] = params
            return self

        def execute(self):
            class _R:
                data = _rows()
            return _R()

    provider = GraphProvider(client=_Client())
    ctx = ResolutionContext(
        scope=RequestScope(organization_id="7"), organization_ids=("7", "9"), is_staff=False
    )

    await provider.resolve(_module(), ctx)

    assert seen["params"]["p_org_ids"] == ["7", "9"]


@pytest.mark.asyncio
async def test_a_customer_with_no_orgs_gets_nothing_not_everything():
    """The fail-safe that matters: no orgs must never mean unrestricted."""

    class _Client:
        def rpc(self, *_a, **_k):
            raise AssertionError("must not query at all")

    provider = GraphProvider(client=_Client())
    ctx = ResolutionContext(scope=RequestScope(), organization_ids=(), is_staff=False)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_a_failing_rpc_resolves_to_none():
    class _Client:
        def rpc(self, *_a, **_k):
            raise RuntimeError("function does not exist")

    provider = GraphProvider(client=_Client())
    ctx = ResolutionContext(scope=RequestScope(), is_staff=True)

    assert await provider.resolve(_module(), ctx) is None


@pytest.mark.asyncio
async def test_no_client_resolves_to_none():
    ctx = ResolutionContext(scope=RequestScope(), is_staff=True)
    assert await GraphProvider(client=None).resolve(_module(), ctx) is None
