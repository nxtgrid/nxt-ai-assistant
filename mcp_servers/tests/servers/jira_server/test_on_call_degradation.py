"""Task 7 (Jira-optional ticket backend plan): the on-call tools must degrade
cleanly when Jira/JSM is unreachable or not configured, instead of surfacing
a raw exception through ToolRegistry's generic "Error: ..." fallback.
"""

import json
import os
import sys

import aiohttp
import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.jira_server import jira_mcp_server as jira_module  # noqa: E402

pytestmark = pytest.mark.asyncio


def _parse(result):
    assert len(result) == 1
    return json.loads(result[0].text)


@pytest.fixture(autouse=True)
def _reset_client_config():
    """Isolate tests from each other and from whatever env vars the process has."""
    client = jira_module.client
    original = (client.auth_header, client.ops_cloud_id, client.ops_schedule_id)
    yield client
    client.auth_header, client.ops_cloud_id, client.ops_schedule_id = original


class TestGetOnCallDegradation:
    async def test_no_auth_header_returns_clean_unavailable(self, _reset_client_config):
        client = _reset_client_config
        client.auth_header = None
        client.ops_cloud_id = "cloud-1"
        client.ops_schedule_id = "sched-1"

        result = await jira_module._tool_get_on_call({"start_date": "2026-01-01T00:00:00Z"})

        data = _parse(result)
        assert data == {"available": False, "reason": jira_module._jira_ops_unavailable_reason()}
        assert "reason" in data and data["reason"]

    async def test_no_ops_schedule_configured_returns_clean_unavailable(
        self, _reset_client_config
    ):
        client = _reset_client_config
        client.auth_header = "Basic xxx"
        client.ops_cloud_id = None
        client.ops_schedule_id = None

        result = await jira_module._tool_get_on_call({"start_date": "2026-01-01T00:00:00Z"})

        data = _parse(result)
        assert data["available"] is False
        assert "reason" in data

    async def test_network_failure_returns_clean_unavailable_not_raw_error(
        self, _reset_client_config, monkeypatch
    ):
        """The bug this guards: before Task 7, a connectivity failure would
        propagate as an unhandled exception and surface to the LLM as a raw
        "Error: ..." string via ToolRegistry's generic fallback."""
        client = _reset_client_config
        client.auth_header = "Basic xxx"
        client.ops_cloud_id = "cloud-1"
        client.ops_schedule_id = "sched-1"

        async def _boom(self, start_date, end_date=None):
            raise aiohttp.ClientConnectionError("Connection refused")

        monkeypatch.setattr(jira_module.JiraClient, "get_on_call", _boom)

        result = await jira_module._tool_get_on_call({"start_date": "2026-01-01T00:00:00Z"})

        data = _parse(result)
        assert data == {
            "available": False,
            "reason": "On-call schedule is unavailable because Jira/JSM is offline.",
        }

    async def test_timeout_returns_clean_unavailable(self, _reset_client_config, monkeypatch):
        client = _reset_client_config
        client.auth_header = "Basic xxx"
        client.ops_cloud_id = "cloud-1"
        client.ops_schedule_id = "sched-1"

        async def _timeout(self, start_date, end_date=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(jira_module.JiraClient, "get_on_call", _timeout)

        result = await jira_module._tool_get_on_call({"start_date": "2026-01-01T00:00:00Z"})

        data = _parse(result)
        assert data["available"] is False

    async def test_healthy_path_still_returns_periods(self, _reset_client_config, monkeypatch):
        """Confirm the degradation logic doesn't break the normal success path."""
        client = _reset_client_config
        client.auth_header = "Basic xxx"
        client.ops_cloud_id = "cloud-1"
        client.ops_schedule_id = "sched-1"

        async def _fake_periods(self, start_date, end_date=None):
            return [{"user": "alice"}, {"user": "bob"}]

        monkeypatch.setattr(jira_module.JiraClient, "get_on_call", _fake_periods)

        result = await jira_module._tool_get_on_call({"start_date": "2026-01-01T00:00:00Z"})

        data = _parse(result)
        assert data["available"] is True
        assert data["total_periods"] == 2
        assert data["on_call_periods"] == [{"user": "alice"}, {"user": "bob"}]


class TestAddOnCallOverrideDegradation:
    async def test_no_config_returns_clean_unavailable(self, _reset_client_config, monkeypatch):
        monkeypatch.setattr(jira_module.ActionFlags, "is_actions_enabled", lambda _s: True)
        client = _reset_client_config
        client.auth_header = None

        result = await jira_module._tool_add_on_call_override(
            {"user_name": "Alice", "start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-02T00:00:00Z"}
        )

        data = _parse(result)
        assert data["available"] is False
        assert data["success"] is False

    async def test_network_failure_returns_clean_unavailable(
        self, _reset_client_config, monkeypatch
    ):
        monkeypatch.setattr(jira_module.ActionFlags, "is_actions_enabled", lambda _s: True)
        client = _reset_client_config
        client.auth_header = "Basic xxx"
        client.ops_cloud_id = "cloud-1"
        client.ops_schedule_id = "sched-1"

        async def _boom(self, user_name, start_time, end_time):
            raise aiohttp.ClientConnectionError("Connection refused")

        monkeypatch.setattr(jira_module.JiraClient, "add_on_call_override", _boom)

        result = await jira_module._tool_add_on_call_override(
            {"user_name": "Alice", "start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-02T00:00:00Z"}
        )

        data = _parse(result)
        assert data == {
            "available": False,
            "success": False,
            "reason": "On-call schedule is unavailable because Jira/JSM is offline.",
        }

    async def test_business_logic_error_still_propagates_normally(
        self, _reset_client_config, monkeypatch
    ):
        """A 'user not found' ValueError is NOT a connectivity issue -- it must
        NOT be swallowed into a generic 'unavailable' response; the caller needs
        to see the real problem."""
        monkeypatch.setattr(jira_module.ActionFlags, "is_actions_enabled", lambda _s: True)
        client = _reset_client_config
        client.auth_header = "Basic xxx"
        client.ops_cloud_id = "cloud-1"
        client.ops_schedule_id = "sched-1"

        async def _not_found(self, user_name, start_time, end_time):
            raise ValueError(f"Could not find user with name: {user_name}")

        monkeypatch.setattr(jira_module.JiraClient, "add_on_call_override", _not_found)

        with pytest.raises(ValueError, match="Could not find user"):
            await jira_module._tool_add_on_call_override(
                {
                    "user_name": "Nobody",
                    "start_time": "2026-01-01T00:00:00Z",
                    "end_time": "2026-01-02T00:00:00Z",
                }
            )
