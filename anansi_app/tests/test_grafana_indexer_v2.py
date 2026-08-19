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


class TestLastError:
    """last_error is the mechanism that lets a caller show *why* generation
    failed, not just that it did -- see index_grafana_panels' docstring for
    "first_generation_error". Without it, a 16/16 failure could only ever be
    reported as a count plus a generic guessed cause ("check GOOGLE_API_KEY /
    quota"), which is not always even the right guess."""

    def test_none_before_any_call(self):
        gen = _generator()

        assert gen.last_error is None

    def test_set_to_the_real_exception_on_failure(self):
        gen = _generator()
        gen.gateway.generate_sync.side_effect = RuntimeError("503 Service Unavailable")

        gen.generate_description(
            panel_title="Battery SOC",
            panel_description="",
            panel_query="SELECT soc FROM battery",
            dashboard_variables=[],
        )

        assert gen.last_error is not None
        assert "RuntimeError" in gen.last_error
        assert "503 Service Unavailable" in gen.last_error
        assert "Battery SOC" in gen.last_error

    def test_set_on_empty_response_text_too(self):
        gen = _generator()
        gen.gateway.generate_sync.return_value = MagicMock(text="")

        gen.generate_description(
            panel_title="Battery SOC",
            panel_description="",
            panel_query="SELECT soc FROM battery",
            dashboard_variables=[],
        )

        assert gen.last_error is not None
        assert "empty text" in gen.last_error
        assert "Battery SOC" in gen.last_error

    def test_stays_none_after_a_successful_call(self):
        gen = _generator()
        gen.gateway.generate_sync.return_value = MagicMock(text="Battery state of charge")

        gen.generate_description(
            panel_title="Battery SOC",
            panel_description="",
            panel_query="SELECT soc FROM battery",
            dashboard_variables=[],
        )

        assert gen.last_error is None

    def test_reflects_only_the_most_recent_call(self):
        """Callers (index_grafana_panels) read last_error immediately after
        each generate_description() call and copy it into
        stats["first_generation_error"] on the first failure -- last_error
        itself is a per-call snapshot, not an accumulator, since the caller
        owns "keep only the first one" semantics."""
        gen = _generator()
        gen.gateway.generate_sync.side_effect = RuntimeError("first failure")
        gen.generate_description(
            panel_title="Panel A",
            panel_description="",
            panel_query="",
            dashboard_variables=[],
        )
        assert gen.last_error is not None and "first failure" in gen.last_error

        gen.gateway.generate_sync.side_effect = None
        gen.gateway.generate_sync.return_value = MagicMock(text="ok")
        gen.generate_description(
            panel_title="Panel B",
            panel_description="",
            panel_query="",
            dashboard_variables=[],
        )

        assert gen.last_error is None
