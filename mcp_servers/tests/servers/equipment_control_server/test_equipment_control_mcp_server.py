"""Behavior tests for equipment_control_mcp_server."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure mcp_servers root and repo root are on PYTHONPATH (mirrors dev.sh)
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../"))
_MCP_ROOT = os.path.join(_REPO_ROOT, "mcp_servers")
for _p in (_MCP_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MODULE_PATH = "servers.equipment_control_server.equipment_control_mcp_server"
_BASE_ENV = {
    "EQUIPMENT_CONTROL_ALLOWED_USERS": "staff@example.com",
    "EQUIPMENT_CONTROL_ACTIONS_ENABLED": "true",
    "VRM_MQTT_USER": "mqtt@example.com",
    # MQTT auth reads VRM_MQTT_PASSWORD (a VRM personal access token formatted as
    # "Token <token>"); VRM_TOKEN is the separate REST-API credential used by the
    # site-online pre-checks. Both are needed — they are not interchangeable.
    "VRM_MQTT_PASSWORD": "Token test-token",  # pragma: allowlist secret
    "VRM_TOKEN": "test-token",  # pragma: allowlist secret
}


def _load(extra_env=None):
    env = {**_BASE_ENV, **(extra_env or {})}
    with patch.dict(os.environ, env, clear=False):
        import importlib as _il

        import servers.equipment_control_server.equipment_control_mcp_server as m

        _il.reload(m)
        return m


@pytest.fixture()
def mod():
    m = _load()
    # Clear per-gateway lock state between tests
    m._gateway_locks.clear()
    return m


# ---------------------------------------------------------------------------
# send_mqtt_command
# ---------------------------------------------------------------------------


class TestSendMqttCommand:
    def _make_mqtt_mock(self, connected=True, is_published=True):
        """Return a mock paho Client that simulates connect + publish."""
        mock_client = MagicMock()

        def fake_loop_start():
            # Immediately invoke on_connect callback to simulate broker accepting
            if connected:
                mock_client.on_connect(mock_client, None, None, 0)

        mock_client.loop_start.side_effect = fake_loop_start

        pub_result = MagicMock()
        pub_result.rc = 0  # MQTT_ERR_SUCCESS
        pub_result.is_published.return_value = is_published
        mock_client.publish.return_value = pub_result

        return mock_client

    def test_raises_on_missing_credentials(self):
        """Raises immediately when VRM credentials are absent."""
        m = _load({"VRM_MQTT_PASSWORD": "", "VRM_MQTT_USER": ""})
        with pytest.raises(
            Exception, match="VRM_MQTT_USER and VRM_MQTT_PASSWORD must be configured"
        ):
            m.send_mqtt_command("gw1", "platform", "0", "Device/Reboot", 1)

    def test_unique_client_id_per_call(self, mod):
        """Each call generates a distinct MQTT client_id."""
        created_ids = []

        def capture_client(*args, **kwargs):
            created_ids.append(kwargs.get("client_id", ""))
            # Raise early to avoid real network I/O
            raise ConnectionRefusedError("test stop")

        with patch("paho.mqtt.client.Client", side_effect=capture_client):
            for _ in range(3):
                with pytest.raises(Exception):
                    mod.send_mqtt_command("gw1", "platform", "0", "Device/Reboot", 1)

        assert len(created_ids) == 3, "Expected 3 Client() calls"
        assert len(set(created_ids)) == 3, "Expected 3 distinct client IDs"

    def test_raises_on_publish_not_acknowledged(self, mod):
        """Raises when wait_for_publish times out (is_published() returns False)."""
        mock_client = self._make_mqtt_mock(connected=True, is_published=False)

        with patch("paho.mqtt.client.Client", return_value=mock_client):
            with pytest.raises(Exception, match="publish confirmation timeout"):
                mod.send_mqtt_command("gw1", "vebus", "276", "SystemReset", 1)

    def test_success_path(self, mod):
        """Returns success dict when broker connects and acknowledges publish."""
        mock_client = self._make_mqtt_mock(connected=True, is_published=True)

        with patch("paho.mqtt.client.Client", return_value=mock_client):
            result = mod.send_mqtt_command("gw1", "vebus", "276", "SystemReset", 1)

        assert result["status"] == "success"

    def test_disconnect_called_before_loop_stop(self, mod):
        """disconnect() precedes loop_stop() so DISCONNECT packet drains before thread join."""
        call_order = []
        mock_client = self._make_mqtt_mock(connected=True, is_published=True)
        mock_client.disconnect.side_effect = lambda: call_order.append("disconnect")
        mock_client.loop_stop.side_effect = lambda: call_order.append("loop_stop")

        with patch("paho.mqtt.client.Client", return_value=mock_client):
            mod.send_mqtt_command("gw1", "platform", "0", "Device/Reboot", 1)

        assert call_order == ["disconnect", "loop_stop"]


# ---------------------------------------------------------------------------
# restart_inverter / restart_comms_chain
# ---------------------------------------------------------------------------


class TestRestartHandlers:
    def _patch_deps(self, mod):
        """Context manager stack for common handler dependencies."""
        return (
            patch.object(mod, "check_rate_limit", AsyncMock(return_value=(True, None))),
            patch.object(mod, "log_equipment_action", AsyncMock()),
            patch.object(mod, "schedule_followup_check", AsyncMock(return_value=None)),
        )

    @pytest.mark.asyncio
    async def test_restart_inverter_uses_to_thread(self, mod):
        """restart_inverter dispatches send_mqtt_command via asyncio.to_thread."""
        dispatched = []

        async def fake_to_thread(fn, **kwargs):
            dispatched.append(fn)
            return {"status": "success", "message": "ok"}

        rate, log, sched = self._patch_deps(mod)
        with rate, log, sched, patch("asyncio.to_thread", fake_to_thread):
            await mod.restart_inverter("gw1", "TestGrid", "staff@example.com")

        assert mod.send_mqtt_command in dispatched

    @pytest.mark.asyncio
    async def test_restart_comms_chain_uses_to_thread(self, mod):
        """restart_comms_chain dispatches send_mqtt_command via asyncio.to_thread."""
        dispatched = []

        async def fake_to_thread(fn, **kwargs):
            dispatched.append(fn)
            return {"status": "success", "message": "ok"}

        rate, log, sched = self._patch_deps(mod)
        with rate, log, sched, patch("asyncio.to_thread", fake_to_thread):
            await mod.restart_comms_chain("gw1", "TestGrid", "staff@example.com")

        assert mod.send_mqtt_command in dispatched

    @pytest.mark.asyncio
    async def test_cancelled_error_writes_audit_log_restart_inverter(self, mod):
        """CancelledError during MQTT still writes the audit log before re-raising."""
        log_calls = []

        async def capturing_log(*args, **kwargs):
            log_calls.append(kwargs)

        async def raising_to_thread(fn, **kwargs):
            raise asyncio.CancelledError()

        rate, _, sched = self._patch_deps(mod)
        with (
            rate,
            patch.object(mod, "log_equipment_action", side_effect=capturing_log),
            sched,
            patch("asyncio.to_thread", raising_to_thread),
        ):
            with pytest.raises(asyncio.CancelledError):
                await mod.restart_inverter("gw1", "TestGrid", "staff@example.com")

        assert len(log_calls) == 1
        assert log_calls[0]["success"] is False
        assert "cancelled" in log_calls[0]["error_message"].lower()

    @pytest.mark.asyncio
    async def test_cancelled_error_writes_audit_log_restart_comms_chain(self, mod):
        """CancelledError during MQTT still writes the audit log before re-raising."""
        log_calls = []

        async def capturing_log(*args, **kwargs):
            log_calls.append(kwargs)

        async def raising_to_thread(fn, **kwargs):
            raise asyncio.CancelledError()

        rate, _, sched = self._patch_deps(mod)
        with (
            rate,
            patch.object(mod, "log_equipment_action", side_effect=capturing_log),
            sched,
            patch("asyncio.to_thread", raising_to_thread),
        ):
            with pytest.raises(asyncio.CancelledError):
                await mod.restart_comms_chain("gw1", "TestGrid", "staff@example.com")

        assert len(log_calls) == 1
        assert log_calls[0]["success"] is False

    @pytest.mark.asyncio
    async def test_concurrent_calls_same_gateway_serialised(self, mod):
        """Two concurrent calls for the same gateway are serialised by the per-gateway lock."""
        execution_log = []

        async def slow_to_thread(fn, **kwargs):
            execution_log.append("start")
            await asyncio.sleep(0.05)
            execution_log.append("end")
            return {"status": "success", "message": "ok"}

        rate, log, sched = self._patch_deps(mod)
        with rate, log, sched, patch("asyncio.to_thread", slow_to_thread):
            await asyncio.gather(
                mod.restart_comms_chain("gw_same", "Grid", "staff@example.com"),
                mod.restart_comms_chain("gw_same", "Grid", "staff@example.com"),
            )

        # Serialised means start/end/start/end, not start/start/end/end
        assert execution_log == ["start", "end", "start", "end"]
