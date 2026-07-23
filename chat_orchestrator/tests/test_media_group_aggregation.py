"""Tests for media group aggregation (album buffering in handler.py).

Tests the _buffer_media_group_message and _flush_media_group functions
that aggregate Telegram album photos into a single merged request.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest


def _make_telegram_body(
    message_id: int,
    chat_id: int = 123,
    media_group_id: str | None = None,
    photo_file_id: str | None = None,
    caption: str = "",
    text: str = "",
    auth_method: str = "telegram",
) -> dict:
    """Build a minimal Telegram webhook body for testing."""
    msg: dict = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": 999, "is_bot": False, "first_name": "Test"},
        "date": int(time.time()),
    }
    if media_group_id:
        msg["media_group_id"] = media_group_id
    if photo_file_id:
        msg["photo"] = [
            {"file_id": f"{photo_file_id}_small", "width": 90, "height": 90},
            {"file_id": photo_file_id, "width": 800, "height": 600},
        ]
    if caption:
        msg["caption"] = caption
    if text:
        msg["text"] = text

    body: dict = {"message": msg}
    if auth_method:
        body["_auth_method"] = auth_method
    return body


@pytest.fixture(autouse=True)
def _clear_buffers():
    """Clear media group buffers and processed messages before each test."""
    import handler

    handler._MEDIA_GROUP_BUFFERS.clear()
    handler._MEDIA_GROUP_TIMERS.clear()
    handler._PROCESSED_MESSAGES.clear()
    yield
    handler._MEDIA_GROUP_BUFFERS.clear()
    handler._MEDIA_GROUP_TIMERS.clear()
    handler._PROCESSED_MESSAGES.clear()


class TestBufferMediaGroupMessage:
    """Tests for _buffer_media_group_message."""

    @pytest.mark.asyncio
    async def test_single_photo_not_buffered(self):
        """A photo without media_group_id is NOT buffered."""
        from handler import _buffer_media_group_message

        body = _make_telegram_body(message_id=1, photo_file_id="photo_abc")
        result = await _buffer_media_group_message(body)
        assert result is False

    @pytest.mark.asyncio
    async def test_text_message_not_buffered(self):
        """A plain text message is NOT buffered."""
        from handler import _buffer_media_group_message

        body = _make_telegram_body(message_id=1, text="Hello bot")
        result = await _buffer_media_group_message(body)
        assert result is False

    @pytest.mark.asyncio
    async def test_album_photo_buffered(self):
        """A photo with media_group_id IS buffered."""
        from handler import _MEDIA_GROUP_BUFFERS, _buffer_media_group_message

        body = _make_telegram_body(message_id=1, media_group_id="album_1", photo_file_id="photo_1")

        with patch("handler._send_telegram_typing_indicator", new_callable=AsyncMock):
            result = await _buffer_media_group_message(body)

        assert result is True
        assert "album_1" in _MEDIA_GROUP_BUFFERS
        assert len(_MEDIA_GROUP_BUFFERS["album_1"]) == 1

    @pytest.mark.asyncio
    async def test_multiple_album_photos_buffered(self):
        """Multiple photos in same album accumulate in buffer."""
        from handler import _MEDIA_GROUP_BUFFERS, _buffer_media_group_message

        with patch("handler._send_telegram_typing_indicator", new_callable=AsyncMock):
            for i in range(3):
                body = _make_telegram_body(
                    message_id=i + 1,
                    media_group_id="album_2",
                    photo_file_id=f"photo_{i}",
                )
                result = await _buffer_media_group_message(body)
                assert result is True

        assert len(_MEDIA_GROUP_BUFFERS["album_2"]) == 3

    @pytest.mark.asyncio
    async def test_dedup_prevents_double_buffer(self):
        """Same message_id is not buffered twice (Telegram retry)."""
        from handler import _MEDIA_GROUP_BUFFERS, _buffer_media_group_message

        body = _make_telegram_body(message_id=42, media_group_id="album_3", photo_file_id="photo_x")

        with patch("handler._send_telegram_typing_indicator", new_callable=AsyncMock):
            result1 = await _buffer_media_group_message(body)
            result2 = await _buffer_media_group_message(body)

        assert result1 is True
        assert result2 is True  # Acked but not double-buffered
        assert len(_MEDIA_GROUP_BUFFERS["album_3"]) == 1

    @pytest.mark.asyncio
    async def test_re_entry_guard(self):
        """Merged body with _photo_file_ids is NOT re-buffered."""
        from handler import _buffer_media_group_message

        body = _make_telegram_body(message_id=1, media_group_id="album_4", photo_file_id="photo_1")
        # Simulate merged message from _flush_media_group
        body["message"]["_photo_file_ids"] = ["photo_1", "photo_2"]

        result = await _buffer_media_group_message(body)
        assert result is False  # Should pass through, not buffer

    @pytest.mark.asyncio
    async def test_auth_method_preserved_in_buffer(self):
        """The _auth_method from app.py is preserved in the buffered body."""
        from handler import _MEDIA_GROUP_BUFFERS, _buffer_media_group_message

        body = _make_telegram_body(
            message_id=1,
            media_group_id="album_5",
            photo_file_id="photo_1",
            auth_method="telegram",
        )

        with patch("handler._send_telegram_typing_indicator", new_callable=AsyncMock):
            await _buffer_media_group_message(body)

        buffered = _MEDIA_GROUP_BUFFERS["album_5"][0]
        assert buffered.get("_auth_method") == "telegram"

    @pytest.mark.asyncio
    async def test_buffer_rejects_beyond_max_group_size(self):
        """Buffer rejects items once a media group reaches _MAX_MEDIA_GROUP_SIZE."""
        from handler import _MEDIA_GROUP_BUFFERS, _buffer_media_group_message

        with patch("handler._send_telegram_typing_indicator", new_callable=AsyncMock):
            with patch("handler._MAX_MEDIA_GROUP_SIZE", 3):
                for i in range(5):
                    body = _make_telegram_body(
                        message_id=i + 1,
                        media_group_id="album_cap",
                        photo_file_id=f"photo_{i}",
                    )
                    await _buffer_media_group_message(body)

        # Only 3 should be buffered (items 4 and 5 dropped)
        assert len(_MEDIA_GROUP_BUFFERS["album_cap"]) == 3

    @pytest.mark.asyncio
    async def test_buffer_rejects_too_many_concurrent_groups(self):
        """Buffer rejects new media groups beyond _MAX_ACTIVE_MEDIA_GROUPS."""
        from handler import _MEDIA_GROUP_BUFFERS, _buffer_media_group_message

        with patch("handler._send_telegram_typing_indicator", new_callable=AsyncMock):
            with patch("handler._MAX_ACTIVE_MEDIA_GROUPS", 2):
                # Fill up 2 groups
                for i in range(2):
                    body = _make_telegram_body(
                        message_id=i + 1,
                        media_group_id=f"album_{i}",
                        photo_file_id=f"photo_{i}",
                    )
                    await _buffer_media_group_message(body)

                # Third group should be rejected
                body = _make_telegram_body(
                    message_id=100,
                    media_group_id="album_rejected",
                    photo_file_id="photo_x",
                )
                result = await _buffer_media_group_message(body)

        assert result is True  # Acked but dropped
        assert "album_rejected" not in _MEDIA_GROUP_BUFFERS
        assert len(_MEDIA_GROUP_BUFFERS) == 2


class TestFlushMediaGroup:
    """Tests for _flush_media_group."""

    @pytest.mark.asyncio
    async def test_flush_merges_photos(self):
        """Flush collects all photo file_ids into _photo_file_ids."""
        from handler import _MEDIA_GROUP_BUFFERS, _flush_media_group

        bodies = [
            _make_telegram_body(
                message_id=i + 1,
                media_group_id="album_f1",
                photo_file_id=f"photo_{i}",
                caption="Album caption" if i == 0 else "",
            )
            for i in range(3)
        ]
        _MEDIA_GROUP_BUFFERS["album_f1"] = bodies

        with patch("handler.async_main", new_callable=AsyncMock) as mock_main:
            with patch("handler._MEDIA_GROUP_FLUSH_DELAY", 0):
                await _flush_media_group("album_f1")

            mock_main.assert_called_once()
            merged = mock_main.call_args[0][0]
            msg = merged["message"]
            assert msg["_photo_file_ids"] == ["photo_0", "photo_1", "photo_2"]

    @pytest.mark.asyncio
    async def test_flush_replies_to_last_message(self):
        """Merged body uses highest message_id for reply threading."""
        from handler import _MEDIA_GROUP_BUFFERS, _flush_media_group

        bodies = [
            _make_telegram_body(message_id=100, media_group_id="album_f3", photo_file_id="p1"),
            _make_telegram_body(message_id=102, media_group_id="album_f3", photo_file_id="p2"),
        ]
        _MEDIA_GROUP_BUFFERS["album_f3"] = bodies

        with patch("handler.async_main", new_callable=AsyncMock) as mock_main:
            with patch("handler._MEDIA_GROUP_FLUSH_DELAY", 0):
                await _flush_media_group("album_f3")

            msg = mock_main.call_args[0][0]["message"]
            assert msg["message_id"] == 102

    @pytest.mark.asyncio
    async def test_flush_empty_buffer_is_noop(self):
        """Flushing a missing/empty buffer does nothing."""
        from handler import _flush_media_group

        with patch("handler.async_main", new_callable=AsyncMock) as mock_main:
            with patch("handler._MEDIA_GROUP_FLUSH_DELAY", 0):
                await _flush_media_group("nonexistent")

            mock_main.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_deepcopies_base_body(self):
        """Flush does not mutate the original buffered body."""
        from handler import _MEDIA_GROUP_BUFFERS, _flush_media_group

        body = _make_telegram_body(
            message_id=1,
            media_group_id="album_dc",
            photo_file_id="p1",
            auth_method="telegram",
        )
        original_msg_keys = set(body["message"].keys())
        _MEDIA_GROUP_BUFFERS["album_dc"] = [body]

        with patch("handler.async_main", new_callable=AsyncMock):
            with patch("handler._MEDIA_GROUP_FLUSH_DELAY", 0):
                await _flush_media_group("album_dc")

        # Original body should NOT have _photo_file_ids injected
        assert "_photo_file_ids" not in body["message"]
        assert set(body["message"].keys()) == original_msg_keys

    @pytest.mark.asyncio
    async def test_flush_sets_internal_reentry_flag(self):
        """Merged body has _internal_reentry flag to skip field sanitization."""
        from handler import _MEDIA_GROUP_BUFFERS, _flush_media_group

        bodies = [
            _make_telegram_body(message_id=1, media_group_id="album_re", photo_file_id="p1"),
        ]
        _MEDIA_GROUP_BUFFERS["album_re"] = bodies

        with patch("handler.async_main", new_callable=AsyncMock) as mock_main:
            with patch("handler._MEDIA_GROUP_FLUSH_DELAY", 0):
                await _flush_media_group("album_re")

            merged = mock_main.call_args[0][0]
            assert merged.get("_internal_reentry") is True

    @pytest.mark.asyncio
    async def test_flush_sends_error_on_failure(self):
        """Flush sends error message to user when async_main raises."""
        from handler import _MEDIA_GROUP_BUFFERS, _flush_media_group

        bodies = [
            _make_telegram_body(message_id=1, media_group_id="album_err", photo_file_id="p1"),
        ]
        _MEDIA_GROUP_BUFFERS["album_err"] = bodies

        with patch("handler.async_main", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            with patch("handler._MEDIA_GROUP_FLUSH_DELAY", 0):
                with patch("handler._send_telegram_message", new_callable=AsyncMock) as mock_send:
                    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test_token"}):
                        # Should not raise — error is caught and user is notified
                        await _flush_media_group("album_err")

                    mock_send.assert_called_once()
                    # Verify it sent to the right chat
                    assert mock_send.call_args[0][1] == "123"


class TestPrepareMediaMultiPhoto:
    """Tests for prepare_media with multiple photo_file_ids."""

    @pytest.mark.asyncio
    async def test_album_downloads_concurrently(self):
        """Multiple photo_file_ids are downloaded via asyncio.gather."""
        from orchestrator.graphs.nodes.prepare_media import prepare_media

        state = {
            "metadata": {"photo_file_ids": ["p1", "p2", "p3"]},
            "media": [],
        }

        async def mock_download(file_id, token):
            return ("base64data", "image/jpeg")

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test_token"}):
            with patch("orchestrator.services.telegram_transport.download_telegram_photo", side_effect=mock_download):
                result = await prepare_media(state)

        assert len(result["media"]) == 3
        assert all(m.type == "image" for m in result["media"])

    @pytest.mark.asyncio
    async def test_single_photo_fallback(self):
        """Single photo_file_id still works (backward compat)."""
        from orchestrator.graphs.nodes.prepare_media import prepare_media

        state = {
            "metadata": {"photo_file_id": "single_photo"},
            "media": [],
        }

        async def mock_download(file_id, token):
            return ("base64data", "image/jpeg")

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test_token"}):
            with patch("orchestrator.services.telegram_transport.download_telegram_photo", side_effect=mock_download):
                result = await prepare_media(state)

        assert len(result["media"]) == 1

    @pytest.mark.asyncio
    async def test_partial_download_failure(self):
        """One photo fails, others are still processed."""
        from orchestrator.graphs.nodes.prepare_media import prepare_media

        state = {
            "metadata": {"photo_file_ids": ["p1", "p_fail", "p3"]},
            "media": [],
        }

        async def mock_download(file_id, token):
            if file_id == "p_fail":
                raise Exception("403 Forbidden")
            return ("base64data", "image/jpeg")

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test_token"}):
            with patch("orchestrator.services.telegram_transport.download_telegram_photo", side_effect=mock_download):
                result = await prepare_media(state)

        # 2 out of 3 succeed
        assert len(result["media"]) == 2

    @pytest.mark.asyncio
    async def test_no_media_returns_empty(self):
        """No file_ids in metadata returns empty media list."""
        from orchestrator.graphs.nodes.prepare_media import prepare_media

        state = {"metadata": {}, "media": []}
        result = await prepare_media(state)
        assert result["media"] == []
