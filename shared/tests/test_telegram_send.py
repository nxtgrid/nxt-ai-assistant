"""Tests for reply_to_message_id support on send_telegram_message_raw /
send_telegram_message_with_fallback -- added for alert-correlation amend
replies (see docs/superpowers/plans/2026-07-27-smart-alert-correlation-notify.md).
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from shared.utils import telegram_send


class _FakeResponse:
    def __init__(self, status: int, json_data: Any):
        self.status = status
        self._json_data = json_data

    async def json(self) -> Any:
        return self._json_data

    async def text(self) -> str:
        return str(self._json_data)

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePostCM:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _FakePostCM:
        self.calls.append((url, kwargs))
        return _FakePostCM(self.response)


@pytest.fixture
def fake_session(monkeypatch):
    session = FakeSession(_FakeResponse(200, {"ok": True, "result": {"message_id": 555}}))
    monkeypatch.setattr(telegram_send, "_get_session", lambda: session)
    return session


class TestSendTelegramMessageRawReply:
    @pytest.mark.asyncio
    async def test_includes_reply_to_message_id_and_allow_without_reply(self, fake_session):
        await telegram_send.send_telegram_message_raw(
            "TOKEN", "-100", "hello", reply_to_message_id=123
        )

        _url, kwargs = fake_session.calls[-1]
        payload = kwargs["json"]
        assert payload["reply_to_message_id"] == 123
        assert payload["allow_sending_without_reply"] is True

    @pytest.mark.asyncio
    async def test_omitted_when_not_given(self, fake_session):
        await telegram_send.send_telegram_message_raw("TOKEN", "-100", "hello")

        _url, kwargs = fake_session.calls[-1]
        payload = kwargs["json"]
        assert "reply_to_message_id" not in payload
        assert "allow_sending_without_reply" not in payload

    @pytest.mark.asyncio
    async def test_invalid_reply_id_ignored_not_raised(self, fake_session):
        await telegram_send.send_telegram_message_raw(
            "TOKEN", "-100", "hello", reply_to_message_id="not-a-number"
        )

        _url, kwargs = fake_session.calls[-1]
        assert "reply_to_message_id" not in kwargs["json"]

    @pytest.mark.asyncio
    async def test_blank_reply_id_ignored(self, fake_session):
        await telegram_send.send_telegram_message_raw(
            "TOKEN", "-100", "hello", reply_to_message_id=""
        )

        _url, kwargs = fake_session.calls[-1]
        assert "reply_to_message_id" not in kwargs["json"]


class TestSendTelegramMessageWithFallbackReply:
    @pytest.mark.asyncio
    async def test_forwards_reply_to_message_id(self, fake_session):
        msg_id = await telegram_send.send_telegram_message_with_fallback(
            "TOKEN", "-100", "hello", reply_to_message_id=123
        )

        assert msg_id == 555
        _url, kwargs = fake_session.calls[-1]
        assert kwargs["json"]["reply_to_message_id"] == 123


class TestEditTelegramMessage:
    @pytest.mark.asyncio
    async def test_successful_edit_returns_true_and_calls_edit_message_text(self):
        session = FakeSession(_FakeResponse(200, {"ok": True, "result": {"message_id": 555}}))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(telegram_send, "_get_session", lambda: session)
            result = await telegram_send.edit_telegram_message(
                "TOKEN", "-100", 555, "updated text", parse_mode="Markdown"
            )

        assert result is True
        url, kwargs = session.calls[-1]
        assert url == "https://api.telegram.org/botTOKEN/editMessageText"
        payload = kwargs["json"]
        assert payload["chat_id"] == "-100"
        assert payload["message_id"] == 555
        assert payload["text"] == "updated text"
        assert payload["parse_mode"] == "Markdown"

    @pytest.mark.asyncio
    async def test_message_not_modified_is_treated_as_success(self):
        session = FakeSession(
            _FakeResponse(
                400,
                {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified",
                },
            )
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(telegram_send, "_get_session", lambda: session)
            result = await telegram_send.edit_telegram_message(
                "TOKEN", "-100", 555, "same text"
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_other_non_2xx_response_returns_false_and_does_not_raise(self):
        session = FakeSession(
            _FakeResponse(
                400,
                {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message to edit not found",
                },
            )
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(telegram_send, "_get_session", lambda: session)
            result = await telegram_send.edit_telegram_message(
                "TOKEN", "-100", 555, "new text"
            )

        assert result is False


class _ScriptedSession:
    """Returns queued responses in order, one per post() call."""

    def __init__(self, responses: List[_FakeResponse]) -> None:
        self._responses = list(responses)
        self._last = responses[-1]
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _FakePostCM:
        self.calls.append((url, kwargs))
        if self._responses:
            self._last = self._responses.pop(0)
        return _FakePostCM(self._last)


_PARSE_ERROR = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: can't parse entities: Can't find end of the entity "
    "starting at byte offset 3020",
}


class TestMarkdownIsBalancedBeforeSending:
    """A stray delimiter must not cost the whole message its formatting.

    Telegram fails a Markdown message outright when one delimiter opens an
    entity that never closes, and text assembled from other systems carries
    them routinely -- a ticket summary truncated mid-sentence keeps its
    opening "[" and loses the closing "]".
    """

    @pytest.mark.asyncio
    async def test_unmatched_bracket_is_escaped_before_the_send(self, fake_session):
        await telegram_send.send_telegram_message_raw(
            "TOKEN", "-100", '- *OPS-1001*: [In reply to the bot: "I have loc...'
        )

        payload = fake_session.calls[-1][1]["json"]
        assert "\\[" in payload["text"]
        # The bold ticket key still renders.
        assert "*OPS-1001*" in payload["text"]

    @pytest.mark.asyncio
    async def test_every_chunk_of_a_split_message_is_balanced(self, fake_session):
        """The split lands on a character budget, not on entity boundaries.

        Each chunk is parsed by Telegram independently, so a chunk that
        inherits a half-open entity loses its formatting on its own -- which
        is what makes a long digest render formatted at the top and raw
        further down.
        """
        # Two chunks: the unmatched bracket sits well past the 4096 boundary.
        text = ("- *OPS-1000*: fine\n" * 300) + '- *OPS-1001*: [truncated summ...\n'
        await telegram_send.send_telegram_message_with_fallback("TOKEN", "-100", text)

        assert len(fake_session.calls) > 1, "expected the message to be split"
        for _url, kwargs in fake_session.calls:
            sent = kwargs["json"]["text"]
            assert sent.count("[") == sent.count("\\["), "a bare [ reached Telegram"
            assert sent.count("*") % 2 == 0

    @pytest.mark.asyncio
    async def test_html_mode_is_not_touched(self, fake_session):
        """The balancer implements Markdown v1 rules only."""
        await telegram_send.send_telegram_message_raw(
            "TOKEN", "-100", "<b>a [ b</b>", parse_mode="HTML"
        )

        assert fake_session.calls[-1][1]["json"]["text"] == "<b>a [ b</b>"


class TestPlainTextFallbackStripsMarkers:
    @pytest.mark.asyncio
    async def test_retry_drops_the_markers_with_the_parse_mode(self, monkeypatch):
        """Resending the marked-up text verbatim is what shows raw "*OPS-1234*"."""
        session = _ScriptedSession(
            [
                _FakeResponse(400, _PARSE_ERROR),
                _FakeResponse(200, {"ok": True, "result": {"message_id": 7}}),
            ]
        )
        monkeypatch.setattr(telegram_send, "_get_session", lambda: session)

        result = await telegram_send.send_telegram_message_raw(
            "TOKEN", "-100", "- *OPS-1001*: a summary"
        )

        assert result["ok"] is True
        retry_payload = session.calls[-1][1]["json"]
        assert "parse_mode" not in retry_payload
        assert "*" not in retry_payload["text"]
        assert "OPS-1001" in retry_payload["text"]

    @pytest.mark.asyncio
    async def test_edit_retries_as_stripped_plain_text(self, monkeypatch):
        session = _ScriptedSession(
            [
                _FakeResponse(400, _PARSE_ERROR),
                _FakeResponse(200, {"ok": True, "result": {"message_id": 7}}),
            ]
        )
        monkeypatch.setattr(telegram_send, "_get_session", lambda: session)

        ok = await telegram_send.edit_telegram_message(
            "TOKEN", "-100", 7, "- *OPS-1001*: a summary", parse_mode="Markdown"
        )

        assert ok is True
        retry_payload = session.calls[-1][1]["json"]
        assert "parse_mode" not in retry_payload
        assert "*" not in retry_payload["text"]
