"""web_search's tool description promises 'Supports country targeting
(default: Nigeria)'. Before this test existed, the code default was an empty
string — no country bias applied unless the caller passed `country`
explicitly. See docs/superpowers/plans/2026-08-19-mcp-tool-description-audit.md
section 1.3.
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
_MCP_ROOT = os.path.join(_REPO_ROOT, "mcp_servers")
for _p in (_MCP_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import servers.knowledge_server.knowledge_mcp_server as m  # noqa: E402


def _run_search(arguments):
    """Call _handle_web_search with a fake Tavily client and return the
    kwargs it was called with (i.e. the actual search_params sent)."""
    fake_client = MagicMock()
    fake_client.search.return_value = {"answer": "", "results": []}
    with patch.object(m, "_get_tavily_client", return_value=fake_client):
        asyncio.run(m._handle_web_search(arguments))
    _, kwargs = fake_client.search.call_args
    return kwargs


class TestWebSearchCountryDefault:
    def test_omitted_country_defaults_to_nigeria_bias(self):
        params = _run_search({"query": "diesel price today"})
        assert params.get("country") == "Nigeria"

    def test_omitted_country_on_news_topic_appends_nigeria_to_query(self):
        """Tavily's 'country' param isn't supported for topic='news', so the
        country bias is applied by appending the country name to the query
        text instead — same default should still apply."""
        params = _run_search({"query": "fuel subsidy news", "topic": "news"})
        assert params.get("query") == "fuel subsidy news Nigeria"
        assert "country" not in params

    def test_explicit_country_overrides_the_default(self):
        params = _run_search({"query": "cocoa prices", "country": "gh"})
        assert params.get("country") == "Ghana"

    def test_explicit_empty_string_opts_out_of_country_bias(self):
        params = _run_search({"query": "solar panel prices", "country": ""})
        assert "country" not in params
