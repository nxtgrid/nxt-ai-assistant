"""Regression coverage for shared Telegram message delivery."""

from __future__ import annotations

from shared.utils import telegram_send


async def test_oversized_message_is_split_without_losing_text(monkeypatch) -> None:
    sent: list[dict] = []

    async def fake_send_raw(_bot_token, _chat_id, text, **kwargs):
        sent.append({"text": text, **kwargs})
        return {"ok": True, "result": {"message_id": len(sent)}}

    monkeypatch.setattr(telegram_send, "send_telegram_message_raw", fake_send_raw)
    text = "A" * 4097

    message_id = await telegram_send.send_telegram_message_with_fallback(
        "token", "chat", text, parse_mode="Markdown"
    )

    assert message_id == 2
    assert len(sent) == 2
    assert all(len(call["text"]) <= 4096 for call in sent)
    assert "".join(call["text"] for call in sent) == text
