"""The ``update_*`` / ``score_*`` helpers must no-op when no span is active.

`shared/llm/gemini.py._update_langfuse` (and the OpenRouter equivalent) calls
``update_generation`` after *every* model call. Most callers -- alert judgment,
conversation summarisers, verification sub-calls, the MCP servers, and the
anansi_app poller daemons -- are not wrapped in a ``@langfuse_observe`` span, so
there is no active OpenTelemetry span. Without a guard the Langfuse SDK logs
``Context error: No active span in current context`` on every one of those calls
(thousands per hour in prod) and forces the global Langfuse client -- with its
background exporter threads and ingestion queues -- into existence in processes
that never legitimately produce a trace. Gating on an active span keeps the SDK
completely dormant on those paths.
"""

from __future__ import annotations

import types

import pytest

from shared.utils import langfuse_utils

HELPERS = [
    ("update_generation", "update_current_generation"),
    ("update_span", "update_current_span"),
    ("update_trace", "update_current_trace"),
    ("score_trace", "score_current_trace"),
]


@pytest.fixture
def fake_langfuse_client(monkeypatch):
    """Install a fake ``langfuse.get_client`` and record whether it was called."""
    client = types.SimpleNamespace()
    calls: dict[str, dict] = {}
    for _, sdk_method in HELPERS:
        client.__dict__[sdk_method] = (
            lambda _m=sdk_method, **kw: calls.__setitem__(_m, kw)
        )

    got_client = {"count": 0}

    def _get_client():
        got_client["count"] += 1
        return client

    fake_module = types.ModuleType("langfuse")
    fake_module.get_client = _get_client
    monkeypatch.setitem(__import__("sys").modules, "langfuse", fake_module)
    return got_client, calls


@pytest.mark.parametrize("helper_name,sdk_method", HELPERS)
def test_helper_noops_without_active_span(
    monkeypatch, fake_langfuse_client, helper_name, sdk_method
):
    got_client, calls = fake_langfuse_client
    monkeypatch.setattr(langfuse_utils, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(langfuse_utils, "_has_active_span", lambda: False)

    getattr(langfuse_utils, helper_name)(foo="bar")

    assert got_client["count"] == 0, "must not even fetch the Langfuse client"
    assert calls == {}


@pytest.mark.parametrize("helper_name,sdk_method", HELPERS)
def test_helper_calls_sdk_with_active_span(
    monkeypatch, fake_langfuse_client, helper_name, sdk_method
):
    got_client, calls = fake_langfuse_client
    monkeypatch.setattr(langfuse_utils, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(langfuse_utils, "_has_active_span", lambda: True)

    getattr(langfuse_utils, helper_name)(foo="bar")

    assert got_client["count"] == 1
    assert calls[sdk_method] == {"foo": "bar"}


@pytest.mark.parametrize("helper_name,sdk_method", HELPERS)
def test_helper_noops_when_disabled_even_with_active_span(
    monkeypatch, fake_langfuse_client, helper_name, sdk_method
):
    got_client, _ = fake_langfuse_client
    monkeypatch.setattr(langfuse_utils, "LANGFUSE_ENABLED", False)
    monkeypatch.setattr(langfuse_utils, "_has_active_span", lambda: True)

    getattr(langfuse_utils, helper_name)(foo="bar")

    assert got_client["count"] == 0


def test_helper_swallows_sdk_errors(monkeypatch):
    monkeypatch.setattr(langfuse_utils, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(langfuse_utils, "_has_active_span", lambda: True)

    fake_module = types.ModuleType("langfuse")

    def _boom():
        raise RuntimeError("langfuse backend unreachable")

    fake_module.get_client = _boom
    monkeypatch.setitem(__import__("sys").modules, "langfuse", fake_module)

    # Must not propagate -- observability is best-effort.
    langfuse_utils.update_generation(model="x")


# ── _has_active_span ─────────────────────────────────────────────────────────


def test_has_active_span_false_when_nothing_active():
    pytest.importorskip("opentelemetry")
    assert langfuse_utils._has_active_span() is False


def test_has_active_span_true_for_a_valid_span_context(monkeypatch):
    otel_trace = pytest.importorskip("opentelemetry.trace")

    fake_span = types.SimpleNamespace(
        get_span_context=lambda: types.SimpleNamespace(is_valid=True)
    )
    monkeypatch.setattr(otel_trace, "get_current_span", lambda: fake_span)

    assert langfuse_utils._has_active_span() is True


def test_has_active_span_false_on_any_error(monkeypatch):
    otel_trace = pytest.importorskip("opentelemetry.trace")

    def _boom():
        raise RuntimeError("no otel context")

    monkeypatch.setattr(otel_trace, "get_current_span", _boom)

    assert langfuse_utils._has_active_span() is False
