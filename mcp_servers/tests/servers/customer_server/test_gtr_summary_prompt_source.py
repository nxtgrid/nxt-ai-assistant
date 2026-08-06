"""get_last_gtr_summary now sources grid-to-sheet mappings via the shared
prompt library (PROMPTS.resolve("experts.definitions")) instead of a direct
os.getenv("EXPERT_INSTRUCTIONS_DOC_ID") + fetch_google_doc_markdown call.

resolve() never raises and always returns a body -- at minimum the bundled
default -- so "not configured" is no longer a distinct state; a body with no
grid-sheet mappings in it (the bundled default has none, since that mapping
data is real-deployment-specific) falls through to the existing
{"no_gtr": True, ...} branch, which already existed for exactly this case.
"""

import asyncio
import inspect
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.customer_server.customer_mcp_server import get_last_gtr_summary  # noqa: E402


def test_source_has_no_env_var_gate():
    """Belt-and-suspenders on top of the repo-wide guardrail in
    shared/tests/test_prompt_single_resolution.py -- names this exact
    function."""
    source = inspect.getsource(get_last_gtr_summary)
    assert "EXPERT_INSTRUCTIONS_DOC_ID" not in source
    assert "fetch_google_doc_markdown" not in source


def test_falls_through_to_no_gtr_when_bundled_default_has_no_mappings():
    """No CHAT_DB_URL / doc binding configured in the test environment, so
    experts.definitions resolves to the bundled prompt -- which has no
    grid-to-sheet mappings (that data is real-deployment-specific). This
    must produce the existing graceful no_gtr response, not an exception
    and not a "not configured" error that no longer applies."""
    result = asyncio.run(get_last_gtr_summary("Any Grid"))
    assert result.get("no_gtr") is True
    assert "error" not in result
