"""Every tool the doc editor may call must exist in the served manifest.

DOC_EDIT_TOOLS is a list of string literals in shared/. tool_definitions.json
is what production actually serves. Nothing else connects them, so a rename
on either side would quietly remove a capability -- build_tool_specs logs a
warning and carries on, which is the right runtime behaviour and the wrong
CI behaviour.
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_ROOT))

from shared.utils.doc_edit_tools import DOC_EDIT_TOOLS, split_tool_name  # noqa: E402

_MANIFEST = json.loads((_ROOT / "mcp_servers" / "tool_definitions.json").read_text())


def _served_names() -> set[str]:
    names = set()
    for server, entries in _MANIFEST["tools"].items():
        tools = entries if isinstance(entries, list) else entries.get("tools", [])
        for tool in tools:
            name = tool["name"] if isinstance(tool, dict) else str(tool)
            names.add(f"{server}_{name}")
    return names


def test_every_whitelisted_tool_is_served():
    missing = sorted(set(DOC_EDIT_TOOLS) - _served_names())
    assert not missing, (
        f"DOC_EDIT_TOOLS names tools that production does not serve: {missing}. "
        "Either the tool was renamed, or tool_definitions.json needs regenerating "
        "with mcp_servers/scripts/export_tools.py."
    )


def test_every_whitelisted_name_splits_into_a_real_server():
    for name in DOC_EDIT_TOOLS:
        server, _tool = split_tool_name(name)
        assert server in _MANIFEST["tools"], f"{name} names unknown server '{server}'"
