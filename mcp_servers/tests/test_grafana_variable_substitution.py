"""Regression tests for SQL-safe Grafana dashboard-variable substitution.

_substitute_variables() is_sql=True substitution feeds panel rawSql sent to
Grafana's /api/ds/query, which executes it against the backing Postgres/
TimescaleDB datasource. Variable values originate from MCP tool arguments
(effectively LLM/chat-controlled), so every non-opt-out substitution form
must be SQL-quoted and escaped, not string-replaced verbatim.
"""

import os

import pytest

os.environ.setdefault("GRAFANA_URL", "http://localhost")
os.environ.setdefault("GRAFANA_API_KEY", "test-key")  # pragma: allowlist secret

import mcp_servers.servers.grafana_server.grafana_mcp_server as m

INJECTION_PAYLOAD = "x' OR '1'='1"


def _substitute(text, variables, is_sql=True):
    client = m.GrafanaDataClient.__new__(m.GrafanaDataClient)
    return m.GrafanaDataClient._substitute_variables(client, text, variables, is_sql=is_sql)


def test_default_form_quotes_and_escapes_sql_injection_payload():
    result = _substitute(
        "SELECT * FROM readings WHERE grid_name = ${Grid}", {"Grid": INJECTION_PAYLOAD}
    )
    assert "OR '1'='1'" not in result.replace("''", "")  # no unescaped break-out
    assert result == "SELECT * FROM readings WHERE grid_name = 'x'' OR ''1''=''1'"


def test_bare_dollar_form_quotes_and_escapes_sql_injection_payload():
    result = _substitute(
        "SELECT * FROM readings WHERE grid_name = $Grid", {"Grid": INJECTION_PAYLOAD}
    )
    assert result == "SELECT * FROM readings WHERE grid_name = 'x'' OR ''1''=''1'"


def test_in_clause_escapes_embedded_quotes():
    result = _substitute(
        "SELECT * FROM readings WHERE grid_name IN (${Grid})", {"Grid": INJECTION_PAYLOAD}
    )
    assert result == "SELECT * FROM readings WHERE grid_name IN ('x'' OR ''1''=''1')"


def test_singlequote_form_escapes_embedded_quotes():
    result = _substitute("ORDER BY ${Grid:singlequote}", {"Grid": INJECTION_PAYLOAD})
    assert result == "ORDER BY 'x'' OR ''1''=''1'"


def test_legit_value_substitutes_normally():
    result = _substitute(
        "SELECT * FROM readings WHERE grid_name = ${Grid}", {"Grid": "ExampleGrid"}
    )
    assert result == "SELECT * FROM readings WHERE grid_name = 'ExampleGrid'"


def test_multi_value_in_clause():
    result = _substitute("WHERE grid_name IN (${Grid})", {"Grid": "A,B,C"})
    assert result == "WHERE grid_name IN ('A', 'B', 'C')"


def test_raw_format_still_bypasses_escaping():
    """:raw is Grafana's documented no-escaping opt-out (e.g. for column names) - unaffected by this fix."""
    result = _substitute("ORDER BY ${Sort:raw}", {"Sort": "grid_name DESC"})
    assert result == "ORDER BY grid_name DESC"


def test_non_sql_usage_is_not_quoted():
    """Non-SQL callers (panel titles, URLs) must not get SQL quoting added."""
    result = _substitute("Panel for ${Grid}", {"Grid": "ExampleGrid"}, is_sql=False)
    assert result == "Panel for ExampleGrid"


@pytest.mark.parametrize("payload", ["a\\1", "a\\g<0>b"])
def test_backslash_values_are_not_misread_as_regex_backreferences(payload):
    result = _substitute("WHERE grid_name = $Grid", {"Grid": payload})
    assert result == f"WHERE grid_name = '{payload}'"
