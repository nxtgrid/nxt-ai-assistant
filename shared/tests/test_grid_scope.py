"""Resolving which grid a conversation is about.

RequestScope.grid gated every site-scoped context module and the episodic
module's grid anchor, and nothing ever set it -- prepare_context.py called
_fetch_jit_context without a grid, so it was always None. These tests pin the
two signals and, just as importantly, every case that must stay None: a wrong
grid attaches one site's material to another site's conversation, which is
worse than attaching none.
"""

import pytest

from shared import grid_scope
from shared.grid_scope import (
    build_channel_map,
    grid_from_channel,
    resolve_scope_grid,
    resolve_scope_grid_from_user_context,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    grid_scope.invalidate()
    yield
    grid_scope.invalidate()


def _grid(name, chat_id=None, thread_id=None):
    return {
        "name": name,
        "internal_telegram_group_chat_id": chat_id,
        "internal_telegram_group_thread_id": thread_id,
    }


# ── build_channel_map ────────────────────────────────────────────────────────


def test_a_grid_with_a_thread_is_keyed_both_ways():
    """A forum grid must match its own topic and its group's General topic."""
    m = build_channel_map([_grid("Alpha", "-100123", "7")])
    assert m[("-100123", "7")] == "Alpha"
    assert m[("-100123", "")] == "Alpha"


def test_a_grid_without_a_thread_owns_the_whole_chat():
    m = build_channel_map([_grid("Alpha", "-100123")])
    assert m[("-100123", "")] == "Alpha"


def test_a_grid_with_no_chat_id_is_skipped():
    assert build_channel_map([_grid("Alpha")]) == {}


def test_a_grid_with_no_name_is_skipped():
    assert build_channel_map([_grid("", "-100123")]) == {}


def test_ids_are_normalised_to_strings():
    """asyncpg returns these as ints; the UserContext carries strings."""
    m = build_channel_map([_grid("Alpha", -100123, 7)])
    assert m[("-100123", "7")] == "Alpha"


def test_a_forum_sibling_never_steals_a_chat_wide_claim():
    """A grid that owns the whole group keeps the bare-chat key.

    Two grids sharing one chat id, one of them thread-less: the thread-less
    one owns the group, and the forum sibling must not overwrite that
    fallback with itself.
    """
    m = build_channel_map([_grid("Owner", "-100123"), _grid("Sibling", "-100123", "9")])
    assert m[("-100123", "")] == "Owner"
    assert m[("-100123", "9")] == "Sibling"


# ── grid_from_channel ────────────────────────────────────────────────────────


def test_the_exact_topic_wins_over_the_chat_wide_fallback():
    m = build_channel_map([_grid("Owner", "-100123"), _grid("Sibling", "-100123", "9")])
    assert grid_from_channel(m, "-100123", "9") == "Sibling"


def test_an_unknown_topic_falls_back_to_the_chat():
    m = build_channel_map([_grid("Owner", "-100123")])
    assert grid_from_channel(m, "-100123", "999") == "Owner"


def test_an_unknown_chat_resolves_to_nothing():
    m = build_channel_map([_grid("Alpha", "-100123")])
    assert grid_from_channel(m, "-100999", None) is None


def test_no_chat_id_resolves_to_nothing():
    m = build_channel_map([_grid("Alpha", "-100123")])
    assert grid_from_channel(m, None, None) is None
    assert grid_from_channel(m, "", "7") is None


# ── resolve_scope_grid ───────────────────────────────────────────────────────


def _fake_entities(monkeypatch, grids, calls=None):
    async def _get(entity_type):
        if calls is not None:
            calls.append(entity_type)
        return grids

    monkeypatch.setattr("shared.entity_eligibility.get_eligible_entities", _get)


def _fake_auth(monkeypatch, names, raises=None):
    class _Auth:
        async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
            if raises:
                raise raises
            return names

    monkeypatch.setattr("shared.auth.get_auth_service", lambda: _Auth())


@pytest.mark.asyncio
async def test_the_conversations_own_channel_wins(monkeypatch):
    """The strongest signal, and the only one that works for staff."""
    _fake_entities(monkeypatch, [_grid("Alpha", "-100123", "7")])
    _fake_auth(monkeypatch, ["Zulu"])

    got = await resolve_scope_grid(
        chat_id="-100123", topic_id="7", organization_ids=["9"], is_staff=True
    )
    assert got == "Alpha"


@pytest.mark.asyncio
async def test_a_customer_with_exactly_one_grid_gets_it(monkeypatch):
    _fake_entities(monkeypatch, [])
    _fake_auth(monkeypatch, ["Alpha"])

    got = await resolve_scope_grid(chat_id=None, organization_ids=["9"], is_staff=False)
    assert got == "Alpha"


@pytest.mark.asyncio
async def test_a_customer_with_several_grids_gets_none(monkeypatch):
    """Ambiguous. Picking one would attach the wrong site's material."""
    _fake_entities(monkeypatch, [])
    _fake_auth(monkeypatch, ["Alpha", "Beta"])

    got = await resolve_scope_grid(chat_id=None, organization_ids=["9"], is_staff=False)
    assert got is None


@pytest.mark.asyncio
async def test_staff_off_channel_get_none_without_querying_grids(monkeypatch):
    """Staff see every grid, so 'exactly one' can never be meaningful."""
    _fake_entities(monkeypatch, [])

    def _boom():
        raise AssertionError("staff must not trigger a grid-name query")

    monkeypatch.setattr("shared.auth.get_auth_service", _boom)

    got = await resolve_scope_grid(chat_id="-100999", organization_ids=["2"], is_staff=True)
    assert got is None


@pytest.mark.asyncio
async def test_a_caller_with_no_orgs_gets_none(monkeypatch):
    _fake_entities(monkeypatch, [])
    _fake_auth(monkeypatch, ["Alpha"])

    assert await resolve_scope_grid(organization_ids=[], is_staff=False) is None


@pytest.mark.asyncio
async def test_a_failing_entity_lookup_degrades_to_none(monkeypatch):
    async def _boom(_entity_type):
        raise RuntimeError("auth db down")

    monkeypatch.setattr("shared.entity_eligibility.get_eligible_entities", _boom)
    _fake_auth(monkeypatch, ["Alpha", "Beta"])

    assert await resolve_scope_grid(chat_id="-100123", organization_ids=["9"]) is None


@pytest.mark.asyncio
async def test_a_failing_grid_name_lookup_degrades_to_none(monkeypatch):
    _fake_entities(monkeypatch, [])
    _fake_auth(monkeypatch, [], raises=RuntimeError("auth db down"))

    assert await resolve_scope_grid(organization_ids=["9"], is_staff=False) is None


@pytest.mark.asyncio
async def test_the_channel_map_is_cached_across_calls(monkeypatch):
    calls: list = []
    _fake_entities(monkeypatch, [_grid("Alpha", "-100123")], calls=calls)

    assert await resolve_scope_grid(chat_id="-100123") == "Alpha"
    assert await resolve_scope_grid(chat_id="-100123") == "Alpha"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_empty_enumeration_is_not_cached(monkeypatch):
    """An outage must not suppress grid scope for the next five minutes."""
    calls: list = []
    _fake_entities(monkeypatch, [], calls=calls)

    await resolve_scope_grid(chat_id="-100123")
    await resolve_scope_grid(chat_id="-100123")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_resolve_scope_grid_from_user_context_returns_none_for_no_context():
    assert await resolve_scope_grid_from_user_context(None) is None


@pytest.mark.asyncio
async def test_resolve_scope_grid_from_user_context_unpacks_the_right_fields(monkeypatch):
    from types import SimpleNamespace

    captured = {}

    async def fake_resolve_scope_grid(chat_id, topic_id, organization_ids, is_staff):
        captured.update(
            chat_id=chat_id, topic_id=topic_id,
            organization_ids=organization_ids, is_staff=is_staff,
        )
        return "grid-a"

    monkeypatch.setattr("shared.grid_scope.resolve_scope_grid", fake_resolve_scope_grid)

    user_context = SimpleNamespace(
        chat_id="-100999", topic_id="42", organization_ids=["7"], is_staff=False,
    )
    result = await resolve_scope_grid_from_user_context(user_context)

    assert result == "grid-a"
    assert captured == {
        "chat_id": "-100999", "topic_id": "42",
        "organization_ids": ["7"], "is_staff": False,
    }
