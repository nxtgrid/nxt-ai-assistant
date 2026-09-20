"""_fetch_grid_context_feeds: direct-fetched O&M messages (staff-only) and
episodic distillation (staff and customer) appended to chat context -- see
orchestrator/services/grid_context_feeds.py for why this is a direct fetch
rather than relying on the model to call a tool."""

from __future__ import annotations

import pytest

import orchestrator.services.grid_context_feeds as grid_context_feeds
from orchestrator.graphs.nodes.prepare_context import _fetch_grid_context_feeds
from orchestrator.models.schemas import UserContext


class _OMMessage:
    def __init__(self, created_at: str, role: str, content: str):
        self.created_at = created_at
        self.role = role
        self.content = content


def _user(*, is_staff: bool) -> UserContext:
    return UserContext(user_id="u1", user_email="a@b.com", is_staff=is_staff)


@pytest.mark.asyncio
async def test_no_grid_in_scope_returns_empty(monkeypatch):
    assert await _fetch_grid_context_feeds(_user(is_staff=True), None) == ""


@pytest.mark.asyncio
async def test_staff_gets_both_om_messages_and_episodic(monkeypatch):
    async def _fake_om(grid, *, since, **_kwargs):
        return [_OMMessage("2026-08-21T10:00:00+00:00", "user", "leak reported")]

    async def _fake_episodic(*, grid, **_kwargs):
        return "history summary"

    monkeypatch.setattr(grid_context_feeds, "fetch_om_messages_for_grid", _fake_om)
    monkeypatch.setattr(grid_context_feeds, "fetch_episodic_summary_for_scope", _fake_episodic)

    result = await _fetch_grid_context_feeds(_user(is_staff=True), "Kudi")

    assert "Recent O&M Messages" in result
    assert "leak reported" in result
    assert "Prior History Summary" in result
    assert "history summary" in result


@pytest.mark.asyncio
async def test_customer_gets_episodic_but_never_om_messages(monkeypatch):
    om_calls = []

    async def _fake_om(grid, *, since, **_kwargs):
        om_calls.append(grid)
        return [_OMMessage("2026-08-21T10:00:00+00:00", "user", "internal staff chat")]

    async def _fake_episodic(*, grid, **_kwargs):
        return "customer-safe summary"

    monkeypatch.setattr(grid_context_feeds, "fetch_om_messages_for_grid", _fake_om)
    monkeypatch.setattr(grid_context_feeds, "fetch_episodic_summary_for_scope", _fake_episodic)

    result = await _fetch_grid_context_feeds(_user(is_staff=False), "Kudi")

    assert om_calls == []
    assert "internal staff chat" not in result
    assert "Recent O&M Messages" not in result
    assert "customer-safe summary" in result


@pytest.mark.asyncio
async def test_fails_open_when_both_feeds_error(monkeypatch):
    async def _boom_om(grid, *, since, **_kwargs):
        raise RuntimeError("db unavailable")

    async def _boom_episodic(*, grid, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(grid_context_feeds, "fetch_om_messages_for_grid", _boom_om)
    monkeypatch.setattr(grid_context_feeds, "fetch_episodic_summary_for_scope", _boom_episodic)

    result = await _fetch_grid_context_feeds(_user(is_staff=True), "Kudi")

    assert result == ""
