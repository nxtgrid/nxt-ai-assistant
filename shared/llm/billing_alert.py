"""Ops alert for the LLM provider refusing requests over billing (HTTP 402).

A billing block fails every LLM-backed path at once, but each failure is only a
generic error for whoever triggered it, so nothing tells the team until a
customer retries. ``report_billing_block`` sends one message per cooldown
window to the escalation chat -- the same destination the other team alerts
use -- and never raises into the caller, which is already handling a failure.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# One alert per window. Every failing request would otherwise post its own, and
# an outage is when the chat is already busiest.
COOLDOWN_SECONDS = 30 * 60

_TELEGRAM_TIMEOUT_SECONDS = 5


@dataclass
class _AlertState:
    last_alert_at: float | None = None
    suppressed: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_STATE = _AlertState()


def _now() -> float:
    return time.monotonic()


def _clock_label() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _alert_text(model: str, suppressed: int) -> str:
    lines = [
        "⚠️ Gemini API is rejecting requests (HTTP 402). Some or all bot replies and "
        "LLM-backed jobs fail until this clears.",
        f"Model: {model}",
        f"Seen: {_clock_label()}",
    ]
    if suppressed:
        lines.append(f"Further failures since the last alert: {suppressed}")
    lines.append(
        "Usually the prepaid credit balance behind the Gemini API key is depleted. That "
        "balance is only shown in AI Studio → Billing (plan, balance, auto-reload), not in "
        "the Google Cloud console. It can flap, with some calls passing and others refused. "
        "If the balance looks fine, Google's billing check may be misfiring."
    )
    return "\n".join(lines)


def _post_telegram(token: str, chat_id: str, text: str) -> None:
    """Send ``text`` through the Bot API. Never raises.

    urllib errors can embed the request URL, and the URL carries the bot token,
    so only the exception type is logged.
    """
    try:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(
                {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=_TELEGRAM_TIMEOUT_SECONDS):
            pass
    except Exception as exc:
        LOGGER.warning("LLM billing-block alert could not be delivered ({})", type(exc).__name__)


def _dispatch(token: str, chat_id: str, text: str) -> None:
    """Post off the caller's thread: the caller is usually a request handler."""
    threading.Thread(
        target=_post_telegram, args=(token, chat_id, text), name="llm-billing-alert"
    ).start()


def report_billing_block(model: str) -> bool:
    """Alert ops that ``model`` was refused for billing. True when one was sent.

    False when throttled by the cooldown, when no destination is configured, or
    when sending failed. Never raises.
    """
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            # Not counted against the cooldown: nothing was sent, so the first
            # failure after this is configured must still alert.
            LOGGER.warning(
                "LLM billing block on {} but no ops alert destination is configured "
                "(TELEGRAM_BOT_TOKEN / ESCALATION_TELEGRAM_CHAT_ID)",
                model,
            )
            return False

        with _STATE.lock:
            now = _now()
            if _STATE.last_alert_at is not None and now - _STATE.last_alert_at < COOLDOWN_SECONDS:
                _STATE.suppressed += 1
                return False
            suppressed, _STATE.suppressed = _STATE.suppressed, 0
            _STATE.last_alert_at = now

        _dispatch(token, chat_id, _alert_text(model, suppressed))
        return True
    except Exception:
        LOGGER.opt(exception=True).warning("Could not raise the LLM billing-block alert")
        return False
