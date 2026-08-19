"""GeminiDescriptionGenerator.generate_description()'s (description, generated)
return contract -- the mechanism that makes a per-panel LLM failure visible
instead of indistinguishable from success.

Before this existed, the method returned only a string: a fallback ("Tool for
viewing X panel") and a real LLM-generated description were the same type,
so every layer above it -- the stats dict, the CLI exit code, the NiceGUI
"Sync Now" button's success/failure toast -- had no way to tell them apart.
That's exactly what happened on 2026-08-19: a force-reindex silently replaced
all 16 enabled panels' descriptions with the fallback string (every Gemini
call failed near-instantly) while the UI reported "Grafana sync complete."
"""

import os
import sys
from unittest.mock import MagicMock

_ANANSI_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_ANANSI_APP_ROOT, os.path.join(_ANANSI_APP_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("MODEL_FAST", "gemini-flash-latest")
os.environ.setdefault("LLM_PROVIDER", "gemini")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")  # pragma: allowlist secret

from grafana_indexer_v2 import GeminiDescriptionGenerator  # noqa: E402


def _generator() -> GeminiDescriptionGenerator:
    gen = GeminiDescriptionGenerator(system_prompt="You write panel descriptions.")
    gen.gateway = MagicMock()
    return gen


class TestGenerateDescriptionReturnContract:
    def test_successful_generation_reports_generated_true(self):
        gen = _generator()
        gen.gateway.generate_sync.return_value = MagicMock(text="Battery state of charge over time")

        description, generated = gen.generate_description(
            panel_title="Battery SOC",
            panel_description="",
            panel_query="SELECT soc FROM battery",
            dashboard_variables=[],
        )

        assert generated is True
        assert description == "Battery state of charge over time"

    def test_exception_falls_back_and_reports_generated_false(self):
        gen = _generator()
        gen.gateway.generate_sync.side_effect = RuntimeError("401 Unauthorized")

        description, generated = gen.generate_description(
            panel_title="Battery SOC",
            panel_description="",
            panel_query="SELECT soc FROM battery",
            dashboard_variables=[],
        )

        assert generated is False
        # The fallback string is what the wrapper's default_description path
        # also produces (grafana_mcp_server.py) -- callers key off `generated`,
        # not the text, to detect failure. Pinning the exact string here too
        # since other code (stats, tests) may still compare against it.
        assert description == "Tool for viewing Battery SOC panel"

    def test_empty_response_text_falls_back_and_reports_generated_false(self):
        gen = _generator()
        gen.gateway.generate_sync.return_value = MagicMock(text="   ")

        description, generated = gen.generate_description(
            panel_title="Battery SOC",
            panel_description="",
            panel_query="SELECT soc FROM battery",
            dashboard_variables=[],
        )

        assert generated is False
        assert description == "Tool for viewing Battery SOC panel"


def test_google_genai_is_importable():
    """Regression test for the actual 2026-08-19 root cause: anansi_app/
    requirements.txt never pinned google-genai, only google-api-python-client
    and google-auth (Docs/Sheets access, unrelated to Gemini generation).
    GeminiGateway.client (shared/llm/gemini.py) does `from google import
    genai` lazily, so the gap didn't fail at import time -- it failed the
    first time any panel actually tried to generate, as "cannot import name
    'genai' from 'google' (unknown location)", indistinguishable at a glance
    from a real Gemini API error. Every test above mocks gen.gateway
    directly, bypassing .client (and this import) entirely, which is exactly
    why none of them caught it. This one doesn't mock anything.

    Note this only regression-protects local/deployed installs of
    anansi_app/requirements.txt, not CI: ci.yml's "Run tests (anansi_app)"
    step installs mcp_servers/requirements.txt (which does pin google-genai)
    for both the mcp_servers and anansi_app test jobs, not anansi_app's own
    requirements.txt -- so this test would stay green in CI even if the pin
    were removed from anansi_app/requirements.txt again. Run
    `pip install -r anansi_app/requirements.txt` in a clean venv before this
    test to actually exercise the gap CI can't see.
    """
    from google import genai  # noqa: F401
