"""Ops alert for the LLM provider refusing requests over billing (HTTP 402).

Nobody finds out from the logs: the customer sees a generic reply and the first
the team hears of it is a retry. These pin the alert's contract -- one message
per cooldown window to the escalation chat, never an exception in the caller,
and never the bot token in a message or a log line.
"""

import json
import threading
import urllib.request

import pytest
from loguru import logger

from shared.llm import billing_alert


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """The cooldown is process-wide state; give every test its own."""
    monkeypatch.setattr(billing_alert, "_STATE", billing_alert._AlertState())


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(billing_alert, "_now", lambda: now["t"])
    return now


@pytest.fixture
def sent(monkeypatch):
    """What would have gone to Telegram; nothing leaves the process."""
    calls = []
    monkeypatch.setattr(
        billing_alert,
        "_dispatch",
        lambda token, chat_id, text: calls.append((token, chat_id, text)),
    )
    return calls


@pytest.fixture
def destination(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("ESCALATION_TELEGRAM_CHAT_ID", "test-ops-chat")


def test_first_billing_block_alerts_the_escalation_chat(destination, sent):
    assert billing_alert.report_billing_block("model-x") is True

    assert len(sent) == 1
    token, chat_id, text = sent[0]
    assert chat_id == "test-ops-chat"
    assert "402" in text
    assert "model-x" in text
    assert "AI Studio" in text
    # The token authenticates the request; it must never be message content.
    assert "test-bot-token" not in text
    assert "since the last alert" not in text


def test_repeat_inside_the_cooldown_is_suppressed(destination, sent, clock):
    billing_alert.report_billing_block("m")
    clock["t"] += billing_alert.COOLDOWN_SECONDS - 1

    assert billing_alert.report_billing_block("m") is False
    assert len(sent) == 1


def test_alert_repeats_after_the_cooldown_and_counts_what_was_suppressed(
    destination, sent, clock
):
    billing_alert.report_billing_block("m")
    for _ in range(3):
        clock["t"] += 60
        billing_alert.report_billing_block("m")
    clock["t"] += billing_alert.COOLDOWN_SECONDS

    assert billing_alert.report_billing_block("m") is True

    assert len(sent) == 2
    assert "since the last alert: 3" in sent[1][2]


def test_unconfigured_destination_sends_nothing_and_does_not_start_the_cooldown(
    monkeypatch, sent
):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ESCALATION_TELEGRAM_CHAT_ID", raising=False)

    assert billing_alert.report_billing_block("m") is False
    assert sent == []

    # Once it is configured the very next failure must alert, not wait out a
    # cooldown that an alert-less call started.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("ESCALATION_TELEGRAM_CHAT_ID", "test-ops-chat")
    assert billing_alert.report_billing_block("m") is True


def test_alerting_never_raises_into_the_caller(destination, monkeypatch):
    def broken(*_args):
        raise RuntimeError("telegram client exploded")

    monkeypatch.setattr(billing_alert, "_dispatch", broken)

    assert billing_alert.report_billing_block("m") is False


def test_dispatch_posts_off_the_callers_thread(monkeypatch):
    """The caller is a request handler; the network call must not block it."""
    done = threading.Event()
    seen = {}

    def fake_post(token, chat_id, text):
        seen["thread"] = threading.current_thread()
        done.set()

    monkeypatch.setattr(billing_alert, "_post_telegram", fake_post)

    billing_alert._dispatch("tok", "chat", "hi")

    assert done.wait(timeout=5)
    assert seen["thread"] is not threading.current_thread()


def test_post_goes_to_the_bot_api_send_message_endpoint(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    billing_alert._post_telegram("tok", "chat-1", "hello")

    assert captured["url"] == "https://api.telegram.org/bottok/sendMessage"
    assert captured["body"]["chat_id"] == "chat-1"
    assert captured["body"]["text"] == "hello"
    assert captured["timeout"] > 0


def test_delivery_failure_is_swallowed_and_never_logs_the_bot_token(monkeypatch):
    def failing_urlopen(request, timeout):
        # urllib errors can embed the request URL, and the URL carries the token.
        raise OSError(f"cannot reach {request.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    seen = []
    sink_id = logger.add(seen.append, level="DEBUG", format="{message}")
    try:
        billing_alert._post_telegram("secret-token", "chat-1", "hello")
    finally:
        logger.remove(sink_id)

    assert seen, "a delivery failure should leave a log line"
    assert not any("secret-token" in str(message) for message in seen)
