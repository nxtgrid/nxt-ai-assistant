"""Hybrid retrieval: argument contract and fallback behaviour."""

import pytest

from orchestrator.services.rag_provider import (
    HYBRID_RPC_ARGUMENTS,
    RAGProvider,
    build_hybrid_arguments,
)


def test_hybrid_argument_names_are_pinned():
    assert HYBRID_RPC_ARGUMENTS == {
        "query_embedding",
        "query_text",
        "p_org_ids",
        "match_count",
        "rrf_k",
    }


def test_build_hybrid_arguments_emits_only_declared_names():
    args = build_hybrid_arguments(
        embedding=[0.1] * 768, query="E-402", organization_ids=["7"], limit=5
    )
    assert set(args) == HYBRID_RPC_ARGUMENTS


def test_the_raw_query_text_is_passed_for_exact_matching():
    """The whole point: 'E-402' must reach the sparse ranker unmodified."""
    args = build_hybrid_arguments(
        embedding=[0.1] * 768, query="E-402", organization_ids=["7"], limit=5
    )
    assert args["query_text"] == "E-402"


def test_org_ids_are_passed_as_an_array_of_integers_not_a_scalar():
    """search_chunks_hybrid takes integer[], unlike the single-org legacy RPC --
    and integer, not uuid: every org id in this system is an integer (see the
    real-permission-model memory for why this isn't uuid[])."""
    args = build_hybrid_arguments(
        embedding=[0.1] * 768, query="q", organization_ids=["7", "9"], limit=5
    )
    assert args["p_org_ids"] == [7, 9]


def test_staff_pass_null_org_ids():
    args = build_hybrid_arguments(
        embedding=[0.1] * 768, query="q", organization_ids=[], limit=5, is_staff=True
    )
    assert args["p_org_ids"] is None


def test_a_non_staff_caller_with_no_orgs_is_refused():
    with pytest.raises(ValueError, match="no organizations"):
        build_hybrid_arguments(
            embedding=[0.1] * 768, query="q", organization_ids=[], limit=5, is_staff=False
        )


def test_a_non_integer_organization_id_is_refused_not_sent_raw():
    with pytest.raises(ValueError, match="integer-valued"):
        build_hybrid_arguments(
            embedding=[0.1] * 768, query="q", organization_ids=["not-a-number"], limit=5
        )


@pytest.mark.asyncio
async def test_hybrid_failure_returns_empty_rather_than_widening(monkeypatch):
    from orchestrator.services import rag_provider as rag_provider_module

    monkeypatch.setattr(rag_provider_module, "get_auth_service", lambda: object())

    class _Client:
        def rpc(self, name, _args):
            raise RuntimeError(f"{name} does not exist")

    provider = RAGProvider()
    provider._rag_client = _Client()

    class _Perms:
        organization_ids = ["7"]
        roles = []
        is_staff = False

    assert await provider.retrieve("q", "t@example.com", user_permissions=_Perms()) == []
