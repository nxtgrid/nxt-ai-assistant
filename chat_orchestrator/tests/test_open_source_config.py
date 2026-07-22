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

    # DEFAULT_TIMEZONE is computed via os.getenv() at import time in
    # shared.scheduling.recurrence; cron_parser only re-exports the name. The
    # env override therefore only takes effect when the *source* module is
    # reloaded — reloading cron_parser just re-binds the already-computed value.
    def test_default_timezone_is_utc_when_unset(self):
        """Default is UTC when env var not set — safe for any deployment."""
        import importlib

        import shared.scheduling.recurrence as rc

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEFAULT_TIMEZONE", None)
            importlib.reload(rc)
            assert rc.DEFAULT_TIMEZONE == "UTC"
        importlib.reload(rc)  # restore to ambient-env value for later tests

    def test_default_timezone_reads_from_env(self):
        """DEFAULT_TIMEZONE env var overrides the default."""
        import importlib

        import shared.scheduling.recurrence as rc

        try:
            with patch.dict(os.environ, {"DEFAULT_TIMEZONE": "Africa/Lagos"}):
                importlib.reload(rc)
                assert rc.DEFAULT_TIMEZONE == "Africa/Lagos"
        finally:
            importlib.reload(rc)  # restore module-level default once env is unpatched


class TestCustomerMeterActionsEnabled:
    """CUSTOMER_METER_ACTIONS_ENABLED defaults to false for safe open-source deployments."""

    # CUSTOMER_METER_ACTIONS_ENABLED is computed via os.getenv() at import time in
    # servers.customer_server.client_base (Phase 4 file split); customer_mcp_server
    # only consumes it indirectly through the mixins. Same reload semantics as
    # TestDefaultTimezone above: the *source* module must be reloaded directly.

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
            import mcp_servers.servers.customer_server.client_base as m

            importlib.reload(m)
            assert m.CUSTOMER_METER_ACTIONS_ENABLED is False

    def test_reads_from_env(self):
        """CUSTOMER_METER_ACTIONS_ENABLED=true enables write actions."""
        with patch.dict(os.environ, {"CUSTOMER_METER_ACTIONS_ENABLED": "true"}):
            import mcp_servers.servers.customer_server.client_base as m

            importlib.reload(m)
            assert m.CUSTOMER_METER_ACTIONS_ENABLED is True
