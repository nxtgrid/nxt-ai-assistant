"""Tests for orchestrator.models.envelope -- the transport-neutral response
envelope (Phase 1 of docs/superpowers/plans/2026-08-06-user-designed-skills.md).

Covers the four scenarios the plan's Phase 1 acceptance criteria name
explicitly: text-only, text+image, text+choices, and the Telegram adapter
round-trip -- plus focused coverage of the extraction helpers themselves.
"""

from __future__ import annotations

from orchestrator.models.envelope import (
    Attachment,
    Choice,
    attachments_from_tool_results,
    build_response_envelope,
    choices_from_reply_markup,
    choices_to_reply_markup,
    tool_names_from_tool_results,
)
from orchestrator.models.schemas import ToolCallResult


def _tool_result(**overrides) -> ToolCallResult:
    defaults = {"name": "get_grid_status", "success": True, "output": {}}
    return ToolCallResult(**{**defaults, **overrides})


def _image_tool_result(
    data: str = "base64imagedata",
    mime_type: str = "image/png",
    name: str = "get_grid_status",
) -> ToolCallResult:
    return _tool_result(
        name=name,
        raw_response={"result": [{"type": "image", "data": data, "mimeType": mime_type}]},
    )


class TestBuildResponseEnvelopeScenarios:
    """The four scenarios named in the plan's Phase 1 acceptance criteria."""

    def test_text_only(self):
        envelope = build_response_envelope(
            text="hello there",
            tool_results=[],
            reply_markup=None,
            tokens={"input_tokens": 10, "output_tokens": 5},
            session_id="session-1",
        )

        assert envelope.text == "hello there"
        assert envelope.attachments == []
        assert envelope.choices == []
        assert envelope.tool_calls == []
        assert envelope.tokens == {"input_tokens": 10, "output_tokens": 5}
        assert envelope.session_id == "session-1"

    def test_text_plus_image(self):
        envelope = build_response_envelope(
            text="here's the chart",
            tool_results=[_image_tool_result()],
            reply_markup=None,
            tokens={},
            session_id="session-2",
        )

        assert len(envelope.attachments) == 1
        attachment = envelope.attachments[0]
        assert attachment.kind == "image"
        assert attachment.data_b64 == "base64imagedata"
        assert attachment.mime_type == "image/png"
        assert attachment.url is None
        # Matches telegram_transport._send_tool_images_to_telegram's caption
        # format exactly -- an API caller should see the same label a
        # Telegram caller would have seen on the sendPhoto message.
        assert attachment.caption == "📊 Get Grid Status"

    def test_text_plus_choices(self):
        reply_markup = {
            "inline_keyboard": [
                [{"text": "Yes", "callback_data": "yes"}],
                [{"text": "No", "callback_data": "no"}],
            ]
        }

        envelope = build_response_envelope(
            text="Proceed?",
            tool_results=[],
            reply_markup=reply_markup,
            tokens={},
            session_id="session-3",
        )

        assert envelope.choices == [Choice(label="Yes", value="yes"), Choice(label="No", value="no")]

    def test_telegram_adapter_round_trip(self):
        """An envelope's choices survive a Telegram-format round trip.

        choices_to_reply_markup is not called by the production Telegram
        send path (see envelope.py's module docstring) -- this proves the
        shape is correct in isolation, so a future phase that does wire it
        in has a known-good starting point.
        """
        original = [Choice(label="Escalate", value="escalate"), Choice(label="Resolve", value="resolve")]

        reply_markup = choices_to_reply_markup(original)
        round_tripped = choices_from_reply_markup(reply_markup)

        assert round_tripped == original


class TestAttachmentsFromToolResults:
    def test_no_tool_results_returns_empty_list(self):
        assert attachments_from_tool_results([]) == []
        assert attachments_from_tool_results(None) == []

    def test_tool_result_without_raw_response_is_skipped(self):
        result = _tool_result(raw_response=None)

        assert attachments_from_tool_results([result]) == []

    def test_non_image_content_items_are_skipped(self):
        result = _tool_result(
            raw_response={"result": [{"type": "text", "text": "just words"}]}
        )

        assert attachments_from_tool_results([result]) == []

    def test_image_without_data_is_skipped(self):
        # Malformed/truncated content item -- must not crash or emit a
        # half-populated Attachment with data_b64=None presented as real.
        result = _tool_result(raw_response={"result": [{"type": "image"}]})

        assert attachments_from_tool_results([result]) == []

    def test_missing_mime_type_defaults_to_png(self):
        result = _tool_result(
            raw_response={"result": [{"type": "image", "data": "xyz"}]}
        )

        [attachment] = attachments_from_tool_results([result])
        assert attachment.mime_type == "image/png"

    def test_multiple_tool_results_each_contribute_images(self):
        results = [
            _image_tool_result(data="img1", name="get_grid_status"),
            _image_tool_result(data="img2", name="fetch_grafana_kpis"),
        ]

        attachments = attachments_from_tool_results(results)

        assert [a.data_b64 for a in attachments] == ["img1", "img2"]

    def test_raw_response_result_not_a_list_is_ignored(self):
        # Defensive: a malformed/unexpected raw_response shape must not raise.
        result = _tool_result(raw_response={"result": "not-a-list"})

        assert attachments_from_tool_results([result]) == []


class TestChoicesFromReplyMarkup:
    def test_none_reply_markup_returns_empty_list(self):
        assert choices_from_reply_markup(None) == []

    def test_empty_inline_keyboard_returns_empty_list(self):
        assert choices_from_reply_markup({"inline_keyboard": []}) == []

    def test_web_app_button_uses_url_as_value(self):
        reply_markup = {
            "inline_keyboard": [
                [{"text": "View Agent State", "web_app": {"url": "https://example.com/state"}}]
            ]
        }

        choices = choices_from_reply_markup(reply_markup)

        assert choices == [Choice(label="View Agent State", value="https://example.com/state")]

    def test_multiple_buttons_in_one_row_are_both_captured(self):
        reply_markup = {
            "inline_keyboard": [[{"text": "A", "callback_data": "a"}, {"text": "B", "callback_data": "b"}]]
        }

        choices = choices_from_reply_markup(reply_markup)

        assert choices == [Choice(label="A", value="a"), Choice(label="B", value="b")]


class TestChoicesToReplyMarkup:
    def test_empty_choices_returns_none(self):
        # None, not {"inline_keyboard": []} -- matches reply_markup's own
        # "no buttons" convention (absent, not an empty keyboard object).
        assert choices_to_reply_markup([]) is None

    def test_one_choice_per_row(self):
        reply_markup = choices_to_reply_markup([Choice(label="Yes", value="yes")])

        assert reply_markup == {"inline_keyboard": [[{"text": "Yes", "callback_data": "yes"}]]}


class TestToolNamesFromToolResults:
    def test_extracts_names_in_call_order(self):
        results = [_tool_result(name="first_tool"), _tool_result(name="second_tool")]

        assert tool_names_from_tool_results(results) == ["first_tool", "second_tool"]

    def test_empty_or_none_returns_empty_list(self):
        assert tool_names_from_tool_results([]) == []
        assert tool_names_from_tool_results(None) == []


class TestAttachmentAndChoiceDataclasses:
    def test_attachment_defaults(self):
        attachment = Attachment(kind="document", url="https://example.com/f.pdf", data_b64=None, mime_type="application/pdf")

        assert attachment.caption == ""
