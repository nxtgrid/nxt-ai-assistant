"""resolve_grid_name -- org-based and text-mention grid fallback for ticket
creation, used when escalation_service.py's exact (chat_id, topic_id) match
finds nothing (e.g. a customer DM, which has no topic id to match on).
"""

from __future__ import annotations

from typing import List, Optional

from orchestrator.services.ticketing.grid_resolution import GridResolution, resolve_grid_name


class _FakeAuthService:
    def __init__(
        self, grid_names: Optional[List[str]] = None, error: Optional[Exception] = None
    ) -> None:
        self._grid_names = grid_names or []
        self._error = error
        self.calls: List[str] = []

    async def get_grid_names_for_organization(self, organization_id: str) -> List[str]:
        self.calls.append(organization_id)
        if self._error is not None:
            raise self._error
        return self._grid_names


def _user_message(content: str) -> dict:
    return {"role": "user", "content": content}


def _bot_message(content: str) -> dict:
    return {"role": "model", "content": content}


async def test_resolve_grid_name_returns_empty_when_organization_id_is_missing():
    result = await resolve_grid_name(organization_id=None, messages=[])

    assert result == GridResolution()


async def test_resolve_grid_name_uses_the_only_grid_when_org_has_exactly_one(monkeypatch):
    fake = _FakeAuthService(grid_names=["Kudi"])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)

    result = await resolve_grid_name(organization_id=42, messages=[])

    assert result == GridResolution(grid_name="Kudi")
    assert fake.calls == ["42"]


async def test_resolve_grid_name_returns_empty_when_org_has_zero_grids(monkeypatch):
    fake = _FakeAuthService(grid_names=[])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)

    result = await resolve_grid_name(organization_id=42, messages=[])

    assert result == GridResolution()


async def test_resolve_grid_name_matches_a_grid_mentioned_in_user_messages(monkeypatch):
    fake = _FakeAuthService(grid_names=["Kudi", "Site Alpha"])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)
    messages = [_user_message("the grid in kudi is down")]

    result = await resolve_grid_name(organization_id=42, messages=messages)

    assert result == GridResolution(grid_name="Kudi")


async def test_resolve_grid_name_ignores_mentions_in_non_user_messages(monkeypatch):
    fake = _FakeAuthService(grid_names=["Kudi", "Site Alpha"])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)
    messages = [_bot_message("I'll check on Kudi for you"), _user_message("ok thanks")]

    result = await resolve_grid_name(organization_id=42, messages=messages)

    assert result == GridResolution(candidates=["Kudi", "Site Alpha"])


async def test_resolve_grid_name_returns_candidates_when_multi_grid_org_has_no_text_match(
    monkeypatch,
):
    fake = _FakeAuthService(grid_names=["Kudi", "Site Alpha"])
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)
    messages = [_user_message("my meter is broken")]

    result = await resolve_grid_name(organization_id=42, messages=messages)

    assert result == GridResolution(candidates=["Kudi", "Site Alpha"])


async def test_resolve_grid_name_degrades_to_empty_when_auth_service_raises(monkeypatch):
    fake = _FakeAuthService(error=RuntimeError("db unreachable"))
    monkeypatch.setattr("shared.auth.get_auth_service", lambda: fake)

    result = await resolve_grid_name(organization_id=42, messages=[])

    assert result == GridResolution()
