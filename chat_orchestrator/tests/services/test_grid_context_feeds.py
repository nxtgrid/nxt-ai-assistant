"""grid_context_feeds: bounded O&M-message and episodic-distillation context
shared by the notify/alert-judgment flow and interactive chat."""

from __future__ import annotations

import pytest

from orchestrator.services import grid_context_feeds
from shared.auth.auth_service import GridNotificationTarget


class _FakeAuthService:
    def __init__(self, target: GridNotificationTarget | None):
        self._target = target

    async def resolve_grid_notification_target(self, grid_name: str):
        return self._target


class _FakeRepo:
    def __init__(self, messages):
        self._messages = messages
        self.calls: list[dict] = []

    async def recent_om_messages(self, *, chat_id, topic_id, since, limit):
        self.calls.append(
            {"chat_id": chat_id, "topic_id": topic_id, "since": since, "limit": limit}
        )
        return self._messages


@pytest.mark.asyncio
async def test_fetch_om_messages_resolves_grid_then_reads_its_om_topic(monkeypatch):
    target = GridNotificationTarget(grid_name="Kudi", chat_id="-1001", topic_id="42")
    fake_repo = _FakeRepo(["m1", "m2"])
    monkeypatch.setattr(
        grid_context_feeds, "get_auth_service", lambda: _FakeAuthService(target)
    )
    monkeypatch.setattr(
        grid_context_feeds,
        "NotifyAlertDeliveryRepository",
        lambda **_kwargs: fake_repo,
    )

    result = await grid_context_feeds.fetch_om_messages_for_grid(
        "Kudi", since="2026-08-21T00:00:00+00:00"
    )

    assert result == ["m1", "m2"]
    assert fake_repo.calls == [
        {
            "chat_id": "-1001",
            "topic_id": "42",
            "since": "2026-08-21T00:00:00+00:00",
            "limit": grid_context_feeds.OM_MESSAGE_LIMIT,
        }
    ]


@pytest.mark.asyncio
async def test_fetch_om_messages_returns_empty_for_an_unmatched_grid(monkeypatch):
    monkeypatch.setattr(
        grid_context_feeds, "get_auth_service", lambda: _FakeAuthService(None)
    )

    result = await grid_context_feeds.fetch_om_messages_for_grid(
        "Nonexistent Grid", since="2026-08-21T00:00:00+00:00"
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_om_messages_fails_open_on_resolution_error(monkeypatch):
    class _Boom:
        async def resolve_grid_notification_target(self, grid_name):
            raise RuntimeError("db unavailable")

    monkeypatch.setattr(grid_context_feeds, "get_auth_service", lambda: _Boom())

    result = await grid_context_feeds.fetch_om_messages_for_grid(
        "Kudi", since="2026-08-21T00:00:00+00:00"
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_episodic_summary_for_scope_prefers_grid_over_org(monkeypatch):
    captured = {}

    async def _fake_fetch(ctx, **_kwargs):
        captured["scope"] = ctx.scope
        captured["is_staff"] = ctx.is_staff
        captured["organization_ids"] = ctx.organization_ids
        return "s" * 1_200

    monkeypatch.setattr(grid_context_feeds, "fetch_episodic_summary", _fake_fetch)

    result = await grid_context_feeds.fetch_episodic_summary_for_scope(
        grid="Kudi", organization_id="7", organization_ids=["7"], is_staff=True
    )

    assert len(result) == grid_context_feeds.EPISODIC_SUMMARY_CHARS
    assert captured["scope"].grid == "Kudi"
    assert captured["scope"].organization_id == "7"
    assert captured["is_staff"] is True
    assert captured["organization_ids"] == ("7",)


@pytest.mark.asyncio
async def test_fetch_episodic_summary_for_scope_none_stays_none(monkeypatch):
    async def _fake_fetch(ctx, **_kwargs):
        return None

    monkeypatch.setattr(grid_context_feeds, "fetch_episodic_summary", _fake_fetch)

    result = await grid_context_feeds.fetch_episodic_summary_for_scope(grid="Kudi")

    assert result is None
