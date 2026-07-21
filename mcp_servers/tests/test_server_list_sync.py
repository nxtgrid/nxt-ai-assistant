"""The MCP server list must stay in sync across its three consumers.

server_registry.SERVER_METADATA is what actually launches servers.
shared.config.flag_registry.MCP_SERVER_NAMES drives the {NAME}_ENABLED flags and
the generated env example. action_flags.CONFIGURABLE_SERVERS gates them at
runtime.

These drifted before: all three still listed "codebase" after that server was
removed, so the settings UI offered a CODEBASE_ENABLED toggle for a server that
no longer existed. CONFIGURABLE_SERVERS is now derived from MCP_SERVER_NAMES;
this test covers the remaining gap between that list and the real registry.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[1]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from server_registry import SERVER_METADATA  # noqa: E402

from shared.config.flag_registry import FLAGS, MCP_SERVER_NAMES  # noqa: E402
from shared_code.config.action_flags import CONFIGURABLE_SERVERS  # noqa: E402


def test_configurable_servers_is_derived_not_copied():
    assert CONFIGURABLE_SERVERS == list(MCP_SERVER_NAMES)


def test_every_flagged_server_actually_exists():
    """A {NAME}_ENABLED flag for a server that cannot be launched is dead config."""
    missing = sorted(set(MCP_SERVER_NAMES) - set(SERVER_METADATA))
    assert not missing, (
        f"flag_registry lists servers absent from server_registry: {missing}. "
        "Remove them from MCP_SERVER_NAMES, or add them to SERVER_METADATA."
    )


def test_every_real_server_is_toggleable():
    """A server with no flag cannot be disabled by an operator."""
    missing = sorted(set(SERVER_METADATA) - set(MCP_SERVER_NAMES))
    assert not missing, (
        f"server_registry has servers with no *_ENABLED flag: {missing}. "
        "Add them to MCP_SERVER_NAMES in shared/config/flag_registry.py."
    )


def test_enable_flag_generated_for_each_server():
    for name in MCP_SERVER_NAMES:
        flag = f"{name.upper()}_ENABLED"
        assert flag in FLAGS, f"{flag} missing from the flag registry"
        assert FLAGS[flag].default is True


def test_removed_codebase_server_is_gone_everywhere():
    """Regression guard for the drift that motivated this test."""
    assert "codebase" not in MCP_SERVER_NAMES
    assert "codebase" not in CONFIGURABLE_SERVERS
    assert "codebase" not in SERVER_METADATA
    assert "CODEBASE_ENABLED" not in FLAGS
