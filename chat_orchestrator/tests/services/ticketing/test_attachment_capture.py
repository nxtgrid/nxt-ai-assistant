"""Tests for capture_escalation_media: Telegram download -> Storage upload -> DB record."""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.ticketing.attachment_capture import (
    capture_escalation_media,
    extract_media_file_ids,
)
from orchestrator.services.ticketing.attachment_repository import AttachmentRepository


class TestExtractMediaFileIds:
    def test_single_photo(self) -> None:
        result = extract_media_file_ids({"photo_file_id": "abc"})
        assert result == [{"type": "image", "file_id": "abc"}]

    def test_photo_album(self) -> None:
        result = extract_media_file_ids({"photo_file_ids": ["a", "b"]})
        assert result == [
            {"type": "image", "file_id": "a"},
            {"type": "image", "file_id": "b"},
        ]

    def test_video(self) -> None:
        result = extract_media_file_ids({"video_file_id": "vid1"})
        assert result == [{"type": "video", "file_id": "vid1"}]

    def test_voice_and_audio_map_to_audio_type(self) -> None:
        assert extract_media_file_ids({"voice_file_id": "v1"}) == [
            {"type": "audio", "file_id": "v1"}
        ]
        assert extract_media_file_ids({"audio_file_id": "a1"}) == [
            {"type": "audio", "file_id": "a1"}
        ]

    def test_no_media_returns_empty_list(self) -> None:
        assert extract_media_file_ids({}) == []
        assert extract_media_file_ids({"topic_id": 5}) == []


class _FakeStorageBucket:
    def __init__(self) -> None:
        self.uploaded: List[Dict[str, Any]] = []

    def upload(self, path: str, file: bytes, file_options: Dict[str, Any]) -> None:
        self.uploaded.append({"path": path, "bytes": file, "options": file_options})


class _FakeStorage:
    def __init__(self, bucket: _FakeStorageBucket) -> None:
        self._bucket = bucket

    def from_(self, name: str) -> _FakeStorageBucket:
        assert name == "escalation-media"
        return self._bucket


class _FakeSupabaseClient:
    def __init__(self, bucket: _FakeStorageBucket) -> None:
        self.storage = _FakeStorage(bucket)


@pytest.fixture
def bucket() -> _FakeStorageBucket:
    return _FakeStorageBucket()


@pytest.fixture
def repo() -> AttachmentRepository:
    repo = MagicMock(spec=AttachmentRepository)
    repo.insert = AsyncMock()
    return repo


@pytest.mark.asyncio
async def test_downloads_uploads_and_records_each_media_item(
    bucket: _FakeStorageBucket, repo: AttachmentRepository
) -> None:
    async def fake_download(file_id: str, bot_token: str, max_size_bytes: int):
        return "ZmFrZS1ieXRlcw==", "image/jpeg"  # base64("fake-bytes")

    await capture_escalation_media(
        escalation_id="esc-1",
        media_file_ids=[{"type": "image", "file_id": "file123"}],
        bot_token="token",
        get_client=lambda: _FakeSupabaseClient(bucket),
        attachment_repository=repo,
        download_fn=fake_download,
    )

    assert len(bucket.uploaded) == 1
    assert bucket.uploaded[0]["path"].startswith("esc-1/")
    assert bucket.uploaded[0]["bytes"] == b"fake-bytes"
    assert bucket.uploaded[0]["options"]["content-type"] == "image/jpeg"

    repo.insert.assert_awaited_once()
    call_kwargs = repo.insert.await_args.kwargs
    assert call_kwargs["escalation_id"] == "esc-1"
    assert call_kwargs["media_type"] == "image"
    assert call_kwargs["mime_type"] == "image/jpeg"
    assert call_kwargs["size_bytes"] == len(b"fake-bytes")


@pytest.mark.asyncio
async def test_skips_a_file_that_fails_to_download_without_raising(
    bucket: _FakeStorageBucket, repo: AttachmentRepository
) -> None:
    async def failing_download(file_id: str, bot_token: str, max_size_bytes: int):
        return None, None

    await capture_escalation_media(
        escalation_id="esc-1",
        media_file_ids=[{"type": "image", "file_id": "file123"}],
        bot_token="token",
        get_client=lambda: _FakeSupabaseClient(bucket),
        attachment_repository=repo,
        download_fn=failing_download,
    )

    assert bucket.uploaded == []
    repo.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_bad_file_does_not_block_the_rest(
    bucket: _FakeStorageBucket, repo: AttachmentRepository
) -> None:
    calls = {"n": 0}

    async def flaky_download(file_id: str, bot_token: str, max_size_bytes: int):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network blip")
        return "ZmFrZS1ieXRlcw==", "image/jpeg"

    await capture_escalation_media(
        escalation_id="esc-1",
        media_file_ids=[
            {"type": "image", "file_id": "bad"},
            {"type": "image", "file_id": "good"},
        ],
        bot_token="token",
        get_client=lambda: _FakeSupabaseClient(bucket),
        attachment_repository=repo,
        download_fn=flaky_download,
    )

    assert len(bucket.uploaded) == 1
    repo.insert.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_op_when_media_file_ids_is_empty(
    bucket: _FakeStorageBucket, repo: AttachmentRepository
) -> None:
    await capture_escalation_media(
        escalation_id="esc-1",
        media_file_ids=[],
        bot_token="token",
        get_client=lambda: _FakeSupabaseClient(bucket),
        attachment_repository=repo,
        download_fn=AsyncMock(),
    )
    assert bucket.uploaded == []
    repo.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_op_when_no_supabase_client_available(repo: AttachmentRepository) -> None:
    await capture_escalation_media(
        escalation_id="esc-1",
        media_file_ids=[{"type": "image", "file_id": "file123"}],
        bot_token="token",
        get_client=lambda: None,
        attachment_repository=repo,
        download_fn=AsyncMock(),
    )
    repo.insert.assert_not_awaited()
