"""Transport-neutral response envelope.

Phase 1 of docs/superpowers/plans/2026-08-06-user-designed-skills.md.

``webhook_processor.process_webhook_with_graph`` still returns its original
``(text, tool_results, reply_markup, tokens)`` tuple -- it does NOT return a
``ResponseEnvelope`` directly. This is a deliberate deviation from that
plan's literal wording, made after reading the actual Telegram-sending code
(``handler.py``'s ``_process_telegram_async`` / ``_process_and_respond_async``):
that code depends on the FULL ``ToolCallResult`` objects -- ``.raw_response``
for image extraction, ``.output``/``.error``/``.success`` for escalation
formatting, ``.name`` for a tool-triggered button special-case -- not just
the narrower ``Attachment`` projection below. Replacing the tuple with an
envelope-only return would force rewriting all of that in the same change,
which is a materially bigger, riskier refactor than "byte-identical, this is
a refactor not a redesign" calls for.

So: the tuple stays canonical. ``build_response_envelope()`` here is a
derived VIEW over that same tuple, built only where the transport-neutral
shape is actually needed -- today, that's the direct (non-Telegram) API
response path in ``handler.py``. The Telegram paths are untouched beyond
widening their unpacking by one (now-ignored) ``tokens`` element.

A consequence worth flagging for whoever builds Phase 4's builder UI: the
production Telegram send path does NOT go through ``choices_to_reply_markup``
below -- that function exists so the envelope's shape is provably
round-trippable (see test_response_envelope.py's Telegram-adapter-round-trip
case), not because anything in production calls it yet. Wiring Telegram
sending through the envelope for real is a separate, later piece of work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from orchestrator.models.schemas import ToolCallResult


@dataclass
class Attachment:
    """One piece of media produced by a tool call during this turn."""

    kind: str  # "image" | "document"
    url: Optional[str]  # Drive URL or proxy URL, when the data lives elsewhere
    data_b64: Optional[str]  # Inline base64 payload, when there's no URL
    mime_type: str
    caption: str = ""


@dataclass
class Choice:
    """One selectable option, e.g. from an inline-keyboard decision prompt.

    ``value`` is the callback_data equivalent for a callback-style button.
    For a web_app button (no callback_data), ``value`` holds the raw URL
    instead -- Choice has no separate "this is a link, not an action" field
    in v1, so a caller receiving one has to know from context which it got.
    """

    label: str
    value: str


@dataclass
class ResponseEnvelope:
    """Transport-neutral view of one turn's response.

    Built via ``build_response_envelope()`` from the same
    ``(text, tool_results, reply_markup, tokens)`` tuple
    ``process_webhook_with_graph`` already returns -- see module docstring.
    """

    text: str
    attachments: List[Attachment] = field(default_factory=list)
    choices: List[Choice] = field(default_factory=list)
    tool_calls: List[str] = field(default_factory=list)  # tool names invoked, for the builder UI
    tokens: Dict[str, int] = field(default_factory=dict)
    session_id: str = ""


def attachments_from_tool_results(
    tool_results: Optional[List["ToolCallResult"]],
) -> List[Attachment]:
    """Extract image attachments from tool results.

    Mirrors ``telegram_transport._send_tool_images_to_telegram``'s MCP-content
    extraction exactly (same ``raw_response["result"][].type == "image"``
    shape, same caption format) so an API caller sees the same images a
    Telegram caller would have received as separate ``sendPhoto`` calls --
    just returned in the response body instead of sent as separate messages.
    Keep these two extractions in sync if the MCP image-content shape changes.
    """
    attachments: List[Attachment] = []
    for result in tool_results or []:
        raw = getattr(result, "raw_response", None)
        if not raw:
            continue
        mcp_result = raw.get("result", [])
        if not isinstance(mcp_result, list):
            continue
        for item in mcp_result:
            if not isinstance(item, dict) or item.get("type") != "image":
                continue
            data = item.get("data")
            if not data:
                continue
            tool_name = (getattr(result, "name", "") or "").replace("_", " ").title()
            attachments.append(
                Attachment(
                    kind="image",
                    url=None,
                    data_b64=data,
                    mime_type=item.get("mimeType", "image/png"),
                    caption=f"📊 {tool_name}" if tool_name else "",
                )
            )
    return attachments


def choices_from_reply_markup(reply_markup: Optional[Dict[str, Any]]) -> List[Choice]:
    """Extract Choice objects from a Telegram-shaped reply_markup.

    Best-effort across both button shapes seen in this codebase: a
    ``callback_data`` button (decision prompts, procedure buttons) maps
    cleanly; a ``web_app`` button (e.g. "View Agent State") has no
    callback_data, so its URL becomes the value instead. See Choice's
    docstring for the ambiguity this leaves an API caller with.
    """
    choices: List[Choice] = []
    if not reply_markup:
        return choices
    for row in reply_markup.get("inline_keyboard", []) or []:
        for button in row:
            if not isinstance(button, dict):
                continue
            label = button.get("text", "")
            value = button.get("callback_data") or button.get("web_app", {}).get("url", "")
            choices.append(Choice(label=label, value=value))
    return choices


def choices_to_reply_markup(choices: List[Choice]) -> Optional[Dict[str, Any]]:
    """Inverse of choices_from_reply_markup: one button per row, callback_data-style.

    NOT used by the production Telegram send path today -- see this module's
    docstring. Exists so the envelope's shape is provably round-trippable.
    """
    if not choices:
        return None
    return {
        "inline_keyboard": [
            [{"text": choice.label, "callback_data": choice.value}] for choice in choices
        ]
    }


def tool_names_from_tool_results(tool_results: Optional[List["ToolCallResult"]]) -> List[str]:
    """Tool names invoked this turn, in call order, for the builder UI to show."""
    return [name for tr in (tool_results or []) if (name := getattr(tr, "name", None))]


def build_response_envelope(
    text: str,
    tool_results: Optional[List["ToolCallResult"]],
    reply_markup: Optional[Dict[str, Any]],
    tokens: Optional[Dict[str, int]],
    session_id: str,
) -> ResponseEnvelope:
    """Build the transport-neutral view of one turn.

    Inputs are exactly what ``process_webhook_with_graph`` returns -- see
    this module's docstring for why that function still returns a tuple
    rather than a ``ResponseEnvelope`` directly.
    """
    return ResponseEnvelope(
        text=text or "",
        attachments=attachments_from_tool_results(tool_results),
        choices=choices_from_reply_markup(reply_markup),
        tool_calls=tool_names_from_tool_results(tool_results),
        tokens=dict(tokens or {}),
        session_id=session_id,
    )


__all__ = [
    "Attachment",
    "Choice",
    "ResponseEnvelope",
    "attachments_from_tool_results",
    "build_response_envelope",
    "choices_from_reply_markup",
    "choices_to_reply_markup",
    "tool_names_from_tool_results",
]
