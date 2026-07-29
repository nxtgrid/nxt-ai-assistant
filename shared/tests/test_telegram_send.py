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
