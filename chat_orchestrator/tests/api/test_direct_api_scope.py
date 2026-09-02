"""The direct-API response reports the scope the turn actually ran under.

resolve_auth mutates the same UserContext object the caller passed in, so by
the time the graph returns, user_context carries the RESOLVED org and staff
flag -- not what the request asked for. Surfacing that lets a chat UI show
which organization answered, which is what makes a mis-scoped session visible
instead of silent (see resolve_auth's admin_app_auth branch).

Additive: existing consumers (anansi_app, the broadcast scheduler, n8n) read
success/message/session_id and are unaffected.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from handler import _handle_webhook_async


def _args() -> dict:
    return {
        "message": "status?",
        "user_id": "anansi-app:admin@example.com:n1",
        "user_email": "admin@example.com",
        "source": "web",
        "metadata": {"admin_app_auth": True},
        "_auth_method": "api",
        "_identity_trusted": True,
    }


def _graph_returning(*, is_staff: bool, org_name, org_ids):
    """Stand-in for the graph, mutating user_context the way resolve_auth does."""

    async def fake_graph(*, user_input, user_context, entity_context, media, session_id, metadata):
        user_context.is_staff = is_staff
        user_context.organization_name = org_name
        user_context.organization_ids = org_ids
        return ("All grids online.", [], None, {"input_tokens": 10, "output_tokens": 4})

    return fake_graph


async def _run(fake_graph) -> dict:
    fake_auth = AsyncMock()
    fake_auth.get_user_email = AsyncMock(return_value="admin@example.com")
    with (
        patch("handler.get_auth_service", return_value=fake_auth),
        patch("handler._get_settings", return_value=SimpleNamespace(debug=False)),
        patch("handler._process_webhook_with_graph", side_effect=fake_graph),
    ):
        return await _handle_webhook_async(_args())


@pytest.mark.asyncio
async def test_scope_reports_staff_and_the_org_short_name():
    result = await _run(_graph_returning(is_staff=True, org_name="yourorg", org_ids=["2"]))

    assert result["scope"] == {"is_staff": True, "organization": "yourorg"}


@pytest.mark.asyncio
async def test_scope_falls_back_to_the_org_id_when_there_is_no_short_name():
    """_get_user_permissions_direct (the email path) never sets
    organization_short_name, so the id is all there is to show."""
    result = await _run(_graph_returning(is_staff=False, org_name=None, org_ids=["9"]))

    assert result["scope"] == {"is_staff": False, "organization": "9"}


@pytest.mark.asyncio
async def test_scope_is_none_when_nothing_resolved():
    result = await _run(_graph_returning(is_staff=False, org_name=None, org_ids=[]))

    assert result["scope"] == {"is_staff": False, "organization": None}


@pytest.mark.asyncio
async def test_existing_keys_are_untouched():
    result = await _run(_graph_returning(is_staff=True, org_name="yourorg", org_ids=["2"]))

    assert result["success"] is True
    assert result["message"] == "All grids online."
    assert result["session_id"].startswith("web_dm_")
    assert result["tokens"] == {"input_tokens": 10, "output_tokens": 4}
