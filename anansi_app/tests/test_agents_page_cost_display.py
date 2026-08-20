"""Runs page display-contract tests."""

from types import SimpleNamespace

import pytest
from nicegui_app.pages.agents import _cost_display


class _FakeElement:
    def classes(self, _classes):
        return self


@pytest.mark.asyncio
async def test_runs_page_renders_current_icon(monkeypatch):
    from nicegui import run
    from nicegui_app.pages import agents

    labels = []
    element = _FakeElement()
    monkeypatch.setattr(
        agents.ui,
        "label",
        lambda text: labels.append(text) or element,
        raising=False,
    )
    monkeypatch.setattr(
        agents,
        "get_reader",
        lambda: SimpleNamespace(is_configured=lambda: False),
    )

    async def io_bound(function):
        return function()

    monkeypatch.setattr(run, "io_bound", io_bound, raising=False)

    await agents.render()

    assert labels[0] == "🎰 Runs"


def test_none_renders_as_unknown_dash_not_zero():
    # None means "at least one run used an unpriced model" -- must never
    # read as "$0.00", which would imply the runs were free.
    assert _cost_display(None) == "—"


def test_known_cost_renders_with_four_decimal_places():
    # LLM costs are often fractions of a cent; 2dp would round a real,
    # nonzero cost down to "$0.00" and misleadingly imply free.
    assert _cost_display("0.003") == "$0.0030"


def test_zero_cost_renders_as_real_zero_not_dash():
    # A real, known $0 (e.g. a function-only workflow) is different from
    # "cost unknown" -- it must render as an actual amount, not "—".
    assert _cost_display("0") == "$0.0000"
