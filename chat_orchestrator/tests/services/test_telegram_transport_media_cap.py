"""Tests for download_telegram_photo's configurable size cap."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.telegram_transport import (
    MAX_MEDIA_SIZE_BYTES,
    download_telegram_photo,
)


class _FakeResponse:
    def __init__(self, json_data: Dict[str, Any] | None = None, read_data: bytes = b"") -> None:
        self._json_data = json_data
        self._read_data = read_data
        self.status = 200

    async def json(self) -> Dict[str, Any]:
        return self._json_data or {}

    async def read(self) -> bytes:
        return self._read_data

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeSession:
    def __init__(self, get_file_response: _FakeResponse, download_response: _FakeResponse) -> None:
        self._get_file_response = get_file_response
        self._download_response = download_response
        self.calls = 0

    def get(self, url: str, **_kwargs: Any) -> _FakeResponse:
        self.calls += 1
        return self._get_file_response if "getFile" in url else self._download_response

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_default_cap_rejects_file_over_5mb() -> None:
    get_file_response = _FakeResponse(
        json_data={"ok": True, "result": {"file_path": "video.mp4", "file_size": 6 * 1024 * 1024}}
    )
    session = _FakeSession(get_file_response, _FakeResponse())
    with patch("aiohttp.ClientSession", return_value=session):
        data, mime = await download_telegram_photo("file123", "token")
    assert data is None
    assert mime is None


@pytest.mark.asyncio
async def test_custom_cap_accepts_a_file_the_default_cap_would_reject() -> None:
    get_file_response = _FakeResponse(
        json_data={"ok": True, "result": {"file_path": "video.mp4", "file_size": 6 * 1024 * 1024}}
    )
    download_response = _FakeResponse(read_data=b"x" * (6 * 1024 * 1024))
    session = _FakeSession(get_file_response, download_response)
    with patch("aiohttp.ClientSession", return_value=session):
        data, mime = await download_telegram_photo(
            "file123", "token", max_size_bytes=10 * 1024 * 1024
        )
    assert data is not None
    assert mime == "video/mp4"


@pytest.mark.asyncio
async def test_default_max_size_constant_is_unchanged() -> None:
    assert MAX_MEDIA_SIZE_BYTES == 5 * 1024 * 1024
