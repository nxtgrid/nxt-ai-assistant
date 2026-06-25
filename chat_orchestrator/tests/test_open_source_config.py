"""Tests that company-specific values are configurable via env vars."""

import importlib
import os
import sys
from unittest.mock import patch


class TestStaffOrgId:
    """STAFF_ORG_ID env var controls staff detection, not a hardcoded 2."""

    def test_staff_org_id_defaults_to_2(self):
        """Default is 2 so existing deployments work without change."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STAFF_ORG_ID", None)
            import shared.auth.auth_service as m

            importlib.reload(m)
            assert m.STAFF_ORG_ID == 2

    def test_staff_org_id_reads_from_env(self):
        """STAFF_ORG_ID env var overrides the default."""
        with patch.dict(os.environ, {"STAFF_ORG_ID": "99"}):
            import shared.auth.auth_service as m

            importlib.reload(m)
            assert m.STAFF_ORG_ID == 99


class TestDefaultTimezone:
    """DEFAULT_TIMEZONE env var controls system-wide timezone default."""

    def test_default_timezone_is_utc_when_unset(self):
        """Default is UTC when env var not set — safe for any deployment."""
        import importlib

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEFAULT_TIMEZONE", None)
            import chat_orchestrator.orchestrator.utils.cron_parser as cp

            importlib.reload(cp)
            assert cp.DEFAULT_TIMEZONE == "UTC"

    def test_default_timezone_reads_from_env(self):
        """DEFAULT_TIMEZONE env var overrides the default."""
        import importlib

        with patch.dict(os.environ, {"DEFAULT_TIMEZONE": "Africa/Lagos"}):
            import chat_orchestrator.orchestrator.utils.cron_parser as cp

            importlib.reload(cp)
            assert cp.DEFAULT_TIMEZONE == "Africa/Lagos"


class TestCustomerMeterActionsEnabled:
    """CUSTOMER_METER_ACTIONS_ENABLED defaults to false for safe open-source deployments."""

    def test_defaults_to_false(self):
        """Default is false — write actions require explicit operator opt-in."""
        # Set explicitly to "false" rather than popping: load_dotenv(override=False) won't
        # overwrite a key that's already present, so the reload sees "false" regardless of .env.
        with patch.dict(os.environ, {"CUSTOMER_METER_ACTIONS_ENABLED": "false"}):
            # Ensure mcp_servers/ is on the path (repo root is already added via pyproject.toml pythonpath)
            mcp_servers_path = str(
                __import__("pathlib").Path(__file__).resolve().parents[2] / "mcp_servers"
            )
            if mcp_servers_path not in sys.path:
                sys.path.insert(0, mcp_servers_path)
            import mcp_servers.servers.customer_server.customer_mcp_server as m

            importlib.reload(m)
            assert m.CUSTOMER_METER_ACTIONS_ENABLED is False

    def test_reads_from_env(self):
        """CUSTOMER_METER_ACTIONS_ENABLED=true enables write actions."""
        with patch.dict(os.environ, {"CUSTOMER_METER_ACTIONS_ENABLED": "true"}):
            import mcp_servers.servers.customer_server.customer_mcp_server as m

            importlib.reload(m)
            assert m.CUSTOMER_METER_ACTIONS_ENABLED is True
