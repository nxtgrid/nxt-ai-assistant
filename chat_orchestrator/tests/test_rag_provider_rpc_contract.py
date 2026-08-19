"""The RPC contract that broke retrieval silently for weeks.

RAGProvider called search_chunks_with_permissions with argument names the
function does not declare. Both that call and its fallback raised, and
retrieve() returned [] on every request without anyone noticing. These tests
pin the argument names.
"""

import pytest

from orchestrator.services import rag_provider as rag_provider_module
from orchestrator.services.rag_provider import (
    SEARCH_RPC_ARGUMENTS,
    RAGProvider,
    build_search_arguments,
)


def test_the_rpc_argument_names_are_pinned():
    """These must match the SQL function's declared parameters exactly.

    If you change this set, change db/schema/chat_db.sql in the same commit
    and apply the migration -- a mismatch fails silently at runtime.
    """
    assert SEARCH_RPC_ARGUMENTS == {
        "query_embedding",
        "p_organization_id",
        "match_count",
        "similarity_threshold",
    }


def test_build_search_arguments_emits_only_declared_names():
    args = build_search_arguments(
        embedding=[0.1] * 768, organization_ids=["7"], limit=5, threshold=0.3
    )
    assert set(args) == SEARCH_RPC_ARGUMENTS


def test_organization_id_is_the_first_org():
    args = build_search_arguments(
        embedding=[0.1] * 768, organization_ids=["7", "9"], limit=5, threshold=0.3
    )
    assert args["p_organization_id"] == "7"


def test_staff_pass_null_for_unrestricted_access():
    args = build_search_arguments(
        embedding=[0.1] * 768, organization_ids=[], limit=5, threshold=0.3, is_staff=True
    )
    assert args["p_organization_id"] is None


def test_a_non_staff_caller_with_no_orgs_is_refused_not_widened():
    """The failure mode that matters: no orgs must never mean unrestricted."""
    with pytest.raises(ValueError, match="no organizations"):
        build_search_arguments(
            embedding=[0.1] * 768, organization_ids=[], limit=5, threshold=0.3, is_staff=False
        )


def test_match_count_over_fetches_for_reranking():
    args = build_search_arguments(
        embedding=[0.1] * 768, organization_ids=["7"], limit=5, threshold=0.3
    )
    assert args["match_count"] > 5


@pytest.mark.asyncio
async def test_retrieve_returns_empty_and_logs_when_the_rpc_fails(monkeypatch):
    """Fail closed: a broken permission filter must never widen access."""

    class _Client:
        def rpc(self, *_a, **_k):
            raise RuntimeError("function does not exist")

    # get_auth_service() is a real singleton that opens a direct postgres
    # connection and raises without AUTH_DB_* configured. This test supplies
    # user_permissions directly, so retrieve() never touches it -- stub it
    # out so construction doesn't require a live database.
    monkeypatch.setattr(rag_provider_module, "get_auth_service", lambda: object())

    provider = RAGProvider()
    provider._rag_client = _Client()

    class _Perms:
        organization_ids = ["7"]
        roles = []
        is_staff = False

    docs = await provider.retrieve(
        "query", "tech@example.com", limit=5, user_permissions=_Perms()
    )
    assert docs == []
