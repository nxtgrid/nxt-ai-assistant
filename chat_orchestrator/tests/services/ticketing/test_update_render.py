"""Rendering and summarisation for ticket update cards.

The card must be a complete statement of current state: the same text is
used both to edit the original message in place and to post a fresh reply,
so it can never depend on what a previous message said.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from orchestrator.services.ticketing.update_render import (
    NOISE_FLOOR_CHARS,
    classify_significance,
    fallback_summary,
    is_probably_noise,
    render_update_card,
    summarize_activity,
)


def test_render_card_states_ticket_status_and_summary():
    text = render_update_card(
        ticket_ref="ANS-42",
        summary="Inverter 3 offline at Kasoa",
        status="done",
        activity="Field team replaced the DC isolator; output confirmed at 4.1 kW.",
        url=None,
    )
    assert "ANS-42" in text
    assert "Inverter 3 offline at Kasoa" in text
    assert "Field team replaced the DC isolator" in text
    assert "closed" in text.lower()


def test_render_card_links_jira_tickets_when_a_url_is_known():
    text = render_update_card(
        ticket_ref="OPS-7",
        summary="Meter comms lost",
        status="in_progress",
        activity="Awaiting SIM replacement.",
        url="https://example.atlassian.net/browse/OPS-7",
    )
    assert "[OPS-7](https://example.atlassian.net/browse/OPS-7)" in text


def test_render_card_omits_activity_line_when_there_is_none():
    text = render_update_card(
        ticket_ref="ANS-1", summary="Test", status="open",
        activity="", url=None,
    )
    assert "ANS-1" in text
    assert text.count("\n\n") <= 1


def test_fallback_summary_uses_the_latest_comment_truncated():
    comments: List[dict[str, Any]] = [
        {"body": "first", "author": "a"},
        {"body": "x" * 400, "author": "b"},
    ]
    out = fallback_summary(comments)
    assert out.startswith("x")
    assert len(out) <= 303  # 300 + the ellipsis


def test_fallback_summary_is_empty_without_comments():
    assert fallback_summary([]) == ""


def test_short_comments_are_noise():
    assert is_probably_noise("ok") is True
    assert is_probably_noise("x" * (NOISE_FLOOR_CHARS + 1)) is False


def test_whitespace_only_comment_is_noise():
    assert is_probably_noise("   \n  ") is True


class _FakeGateway:
    """Stands in for shared.llm's GenerationGateway."""

    def __init__(self, text: str = "", raises: bool = False) -> None:
        self._text = text
        self._raises = raises
        self.calls: List[Any] = []

    async def generate(self, messages, options):
        self.calls.append((messages, options))
        if self._raises:
            raise RuntimeError("llm down")

        class _Result:
            text = self._text

        return _Result()


@pytest.mark.asyncio
async def test_summarize_activity_returns_the_llm_text():
    gateway = _FakeGateway(text="Field team replaced the isolator.")
    comments = [{"author": "a", "body": "Found a blown fuse."}]

    out = await summarize_activity(gateway, "fake-model", comments)

    assert out == "Field team replaced the isolator."
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_summarize_activity_falls_back_when_the_llm_fails():
    gateway = _FakeGateway(raises=True)
    comments = [{"author": "a", "body": "Found a blown fuse, replacing now."}]

    out = await summarize_activity(gateway, "fake-model", comments)

    assert out == "Found a blown fuse, replacing now."


@pytest.mark.asyncio
async def test_summarize_activity_is_empty_without_comments():
    gateway = _FakeGateway(text="should not be used")
    assert await summarize_activity(gateway, "fake-model", []) == ""


@pytest.mark.asyncio
async def test_classify_significance_parses_the_llm_json_verdict():
    gateway = _FakeGateway(text='{"significant": true, "summary": "root cause found"}')

    result = await classify_significance(gateway, "fake-model", "Root cause: blown fuse on inverter 3.")

    assert result is True


@pytest.mark.asyncio
async def test_classify_significance_fails_closed_on_llm_error():
    gateway = _FakeGateway(raises=True)

    result = await classify_significance(gateway, "fake-model", "Root cause: blown fuse on inverter 3.")

    assert result is False


@pytest.mark.asyncio
async def test_classify_significance_fails_closed_on_unparseable_json():
    gateway = _FakeGateway(text="not json")

    result = await classify_significance(gateway, "fake-model", "Root cause: blown fuse on inverter 3.")

    assert result is False


@pytest.mark.asyncio
async def test_classify_significance_skips_the_llm_for_noise():
    gateway = _FakeGateway(text='{"significant": true, "summary": "x"}')

    result = await classify_significance(gateway, "fake-model", "ok")

    assert result is False
    assert gateway.calls == []
