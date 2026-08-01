# Ticket Media Attachments (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Media (image/video/audio) present on the message that triggers an escalation flows through to the internal ticket record, and to Jira as an issue attachment when the ticket backend is Jira.

**Architecture:** A new `escalation_attachments` table (keyed by `escalation_id`, since a ticket may not exist yet) plus a new private Supabase Storage bucket `escalation-media` are the durable store. Capture happens synchronously inside `escalate_to_support()` (the only point in the request lifecycle with access to the triggering turn's raw Telegram file_ids). Attachment-to-ticket happens inside `TicketService.create_ticket()`, after the ticket itself is created — linking is backend-agnostic (just stamping `ticket_id`), while pushing bytes to Jira is a new `TicketBackend.add_attachments()` method only `JiraTicketBackend` does real work in.

**Tech Stack:** Python (chat_orchestrator FastAPI service), Supabase (Postgres + Storage, via the `supabase-py` sync client already used everywhere in this service), aiohttp (Jira REST calls), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-31-ticket-media-attachments-design.md`

---

## Before you start

Work happens in this worktree, already on branch `feat/ticket-media-attachments` (based on `fix/canonical-escalation-dual-write-phase1`). Run tests with:

```bash
cd chat_orchestrator && python -m pytest tests/ -q
```

Remember (`CLAUDE.md`): any new file under a `tests/` directory must be `git add -f`'d — this repo's `.gitignore` denies `tests/` by default, and a plain `git add` silently drops new test files from the commit. Run `pre-commit run --all-files` before the final task is considered done.

---

### Task 1: Migration — `escalation_attachments` table + Storage bucket

**Files:**
- Create: `db/migrations/0008_escalation_attachments.sql`

- [ ] **Step 1: Write the migration**

```sql
BEGIN;

-- Media attachments captured from the chat turn that triggers an escalation.
-- Keyed by escalation_id (not ticket_id) because an escalation can sit
-- unfiled for a while, or never get filed -- the media is captured at
-- escalation time, well before any ticket exists. ticket_id is filled in by
-- TicketService.create_ticket() once the ticket is created.
CREATE TABLE IF NOT EXISTS escalation_attachments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    escalation_id uuid NOT NULL REFERENCES escalations(id) ON DELETE CASCADE,
    ticket_id uuid REFERENCES tickets(id),
    storage_path text NOT NULL,
    media_type text NOT NULL,
    mime_type text NOT NULL,
    size_bytes integer NOT NULL,
    jira_attachment_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_escalation_attachments_escalation_id
    ON escalation_attachments(escalation_id);
CREATE INDEX IF NOT EXISTS idx_escalation_attachments_ticket_id
    ON escalation_attachments(ticket_id);

-- Private bucket -- objects are only ever read/written server-side via the
-- service-role client (same client every other table in this migration is
-- accessed through), so no storage.objects RLS policies are needed.
INSERT INTO storage.buckets (id, name, public)
VALUES ('escalation-media', 'escalation-media', false)
ON CONFLICT (id) DO NOTHING;

COMMIT;
```

- [ ] **Step 2: Apply the migration**

This repo's migrations are applied by hand in the Supabase SQL editor (see the header comment convention in `db/migrations/0007_backfill_ticket_correlations_ticket_id.sql`). Run the SQL above against `chat_db` in the Supabase SQL editor for your dev/staging project before running any test that hits a real database. Tests in this plan all use a fake/mocked client, so this step does not block Tasks 2+.

- [ ] **Step 3: Commit**

```bash
git add db/migrations/0008_escalation_attachments.sql
git commit -m "db: add escalation_attachments table and escalation-media storage bucket"
```

---

### Task 2: `AttachmentRepository`

**Files:**
- Create: `chat_orchestrator/orchestrator/services/ticketing/attachment_repository.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_attachment_repository.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for AttachmentRepository, the sole writer/reader for escalation_attachments."""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from orchestrator.services.ticketing.attachment_repository import (
    AttachmentRepository,
    EscalationAttachment,
)


class _FakeResult:
    def __init__(self, data: List[Dict[str, Any]]) -> None:
        self.data = data


class _FakeTable:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self._filters: Dict[str, Any] = {}
        self._insert_payload: Dict[str, Any] | None = None
        self._update_payload: Dict[str, Any] | None = None

    def insert(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._insert_payload = payload
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._update_payload = payload
        return self

    def select(self, *_args: Any, **_kwargs: Any) -> "_FakeTable":
        return self

    def eq(self, field: str, value: Any) -> "_FakeTable":
        self._filters[field] = value
        return self

    def execute(self) -> _FakeResult:
        if self._insert_payload is not None:
            row = {"id": "attachment-1", **self._insert_payload}
            self._rows.append(row)
            return _FakeResult([row])
        if self._update_payload is not None:
            matched = [
                {**row, **self._update_payload}
                for row in self._rows
                if all(row.get(k) == v for k, v in self._filters.items())
            ]
            return _FakeResult(matched)
        matched = [
            row for row in self._rows if all(row.get(k) == v for k, v in self._filters.items())
        ]
        return _FakeResult(matched)


class _FakeClient:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def table(self, name: str) -> _FakeTable:
        assert name == "escalation_attachments"
        return _FakeTable(self._rows)


@pytest.fixture
def rows() -> List[Dict[str, Any]]:
    return []


@pytest.fixture
def repo(rows: List[Dict[str, Any]]) -> AttachmentRepository:
    return AttachmentRepository(client=_FakeClient(rows))


class TestInsert:
    @pytest.mark.asyncio
    async def test_inserts_and_returns_the_new_row(self, repo: AttachmentRepository) -> None:
        attachment = await repo.insert(
            escalation_id="esc-1",
            storage_path="esc-1/attachment-1.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=1234,
        )
        assert attachment.escalation_id == "esc-1"
        assert attachment.storage_path == "esc-1/attachment-1.jpg"
        assert attachment.media_type == "image"
        assert attachment.mime_type == "image/jpeg"
        assert attachment.size_bytes == 1234
        assert attachment.ticket_id is None
        assert attachment.jira_attachment_id is None


class TestListByEscalation:
    @pytest.mark.asyncio
    async def test_returns_only_rows_for_the_given_escalation(
        self, repo: AttachmentRepository, rows: List[Dict[str, Any]]
    ) -> None:
        await repo.insert(
            escalation_id="esc-1",
            storage_path="esc-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        await repo.insert(
            escalation_id="esc-2",
            storage_path="esc-2/b.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=20,
        )
        result = await repo.list_by_escalation("esc-1")
        assert len(result) == 1
        assert result[0].storage_path == "esc-1/a.jpg"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_none_found(self, repo: AttachmentRepository) -> None:
        result = await repo.list_by_escalation("no-such-escalation")
        assert result == []


class TestLinkTicket:
    @pytest.mark.asyncio
    async def test_stamps_ticket_id_on_every_row_for_the_escalation(
        self, repo: AttachmentRepository
    ) -> None:
        await repo.insert(
            escalation_id="esc-1",
            storage_path="esc-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        await repo.link_ticket("esc-1", "ticket-99")
        result = await repo.list_by_escalation("esc-1")
        assert result[0].ticket_id == "ticket-99"


class TestMarkSynced:
    @pytest.mark.asyncio
    async def test_stamps_jira_attachment_id(self, repo: AttachmentRepository) -> None:
        attachment = await repo.insert(
            escalation_id="esc-1",
            storage_path="esc-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        await repo.mark_synced(attachment.id, "10001")
        result = await repo.list_by_escalation("esc-1")
        assert result[0].jira_attachment_id == "10001"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_attachment_repository.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.ticketing.attachment_repository'`.

- [ ] **Step 3: Write the implementation**

```python
"""Persistence boundary for escalation-triggering media attachments.

Keyed by escalation_id, not ticket_id, because media is captured at
escalation time (see EscalationService.escalate_to_support) which always
happens before any ticket exists -- an escalation may sit unfiled for a
while, or never get filed at all. ticket_id is stamped on later, once
TicketService.create_ticket() actually creates a ticket for the escalation.
"""

from __future__ import annotations

from typing import Any, Callable, List, Literal, Optional

from pydantic import BaseModel

BUCKET_NAME = "escalation-media"


class EscalationAttachment(BaseModel):
    id: str
    escalation_id: str
    ticket_id: Optional[str] = None
    storage_path: str
    media_type: Literal["image", "video", "audio", "document"]
    mime_type: str
    size_bytes: int
    jira_attachment_id: Optional[str] = None


class AttachmentRepositoryError(RuntimeError):
    """Raised when an escalation_attachments operation cannot be completed."""


class AttachmentRepository:
    """The sole writer/reader for ``escalation_attachments``."""

    def __init__(
        self,
        client: Optional[Any] = None,
        get_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        if client is None and get_client is None:
            raise ValueError("AttachmentRepository requires either `client` or `get_client`")
        self._client_instance = client
        self._get_client = get_client

    def _raw_client(self) -> Any:
        client = self._client_instance
        if client is None and self._get_client is not None:
            client = self._get_client()
        if client is None:
            raise AttachmentRepositoryError("attachment repository has no database client")
        return client

    async def insert(
        self,
        *,
        escalation_id: str,
        storage_path: str,
        media_type: Literal["image", "video", "audio", "document"],
        mime_type: str,
        size_bytes: int,
    ) -> EscalationAttachment:
        payload = {
            "escalation_id": escalation_id,
            "storage_path": storage_path,
            "media_type": media_type,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }
        try:
            response = self._raw_client().table("escalation_attachments").insert(payload).execute()
        except AttachmentRepositoryError:
            raise
        except Exception as exc:
            raise AttachmentRepositoryError(f"failed to insert escalation attachment: {exc}") from exc
        rows = getattr(response, "data", None) or []
        if not rows:
            raise AttachmentRepositoryError("escalation attachment insert returned no row")
        return EscalationAttachment.model_validate(rows[0])

    async def list_by_escalation(self, escalation_id: str) -> List[EscalationAttachment]:
        try:
            response = (
                self._raw_client()
                .table("escalation_attachments")
                .select("*")
                .eq("escalation_id", escalation_id)
                .execute()
            )
        except AttachmentRepositoryError:
            raise
        except Exception as exc:
            raise AttachmentRepositoryError(f"failed to list escalation attachments: {exc}") from exc
        rows = getattr(response, "data", None) or []
        return [EscalationAttachment.model_validate(row) for row in rows]

    async def link_ticket(self, escalation_id: str, ticket_id: str) -> None:
        try:
            self._raw_client().table("escalation_attachments").update(
                {"ticket_id": ticket_id}
            ).eq("escalation_id", escalation_id).execute()
        except Exception as exc:
            raise AttachmentRepositoryError(
                f"failed to link attachments for escalation {escalation_id} to ticket {ticket_id}: {exc}"
            ) from exc

    async def mark_synced(self, attachment_id: str, jira_attachment_id: str) -> None:
        try:
            self._raw_client().table("escalation_attachments").update(
                {"jira_attachment_id": jira_attachment_id}
            ).eq("id", attachment_id).execute()
        except Exception as exc:
            raise AttachmentRepositoryError(
                f"failed to mark attachment {attachment_id} synced: {exc}"
            ) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_attachment_repository.py -v
```

Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add -f chat_orchestrator/tests/services/ticketing/test_attachment_repository.py
git add chat_orchestrator/orchestrator/services/ticketing/attachment_repository.py
git commit -m "feat(tickets): add AttachmentRepository for escalation_attachments"
```

---

### Task 3: `download_telegram_photo` gains a configurable size cap

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/telegram_transport.py:24,452-460,486-487`
- Test: `chat_orchestrator/tests/services/test_telegram_transport_media_cap.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd chat_orchestrator && python -m pytest tests/services/test_telegram_transport_media_cap.py -v
```

Expected: FAIL — `TypeError: download_telegram_photo() got an unexpected keyword argument 'max_size_bytes'`.

- [ ] **Step 3: Modify `download_telegram_photo`**

In `chat_orchestrator/orchestrator/services/telegram_transport.py`, change the signature at line 452 and the size check at line 486-487:

```python
async def download_telegram_photo(
    file_id: str, bot_token: str, max_size_bytes: int = MAX_MEDIA_SIZE_BYTES
) -> tuple:
    """
    Download a photo from Telegram using the Bot API.

    Args:
        file_id: Telegram file_id of the photo
        bot_token: Telegram bot token
        max_size_bytes: Reject files larger than this. Defaults to
            MAX_MEDIA_SIZE_BYTES (the LLM-vision path's cap). Callers
            downloading for ticket-attachment purposes should pass a
            higher, separate limit -- see MAX_TICKET_ATTACHMENT_SIZE_BYTES.

    Returns:
        Tuple of (base64_data, mime_type) or (None, None) on failure
    """
```

And the size check:

```python
                # Check file size
                if file_size > max_size_bytes:
                    LOGGER.warning(f"File too large: {file_size} bytes > {max_size_bytes}")
                    return None, None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd chat_orchestrator && python -m pytest tests/services/test_telegram_transport_media_cap.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Run the full existing telegram_transport test suite to confirm no regression**

```bash
cd chat_orchestrator && python -m pytest tests/services/ -k telegram_transport -v
```

Expected: PASS, all existing tests still green (default `max_size_bytes` preserves old behavior for every existing caller).

- [ ] **Step 6: Commit**

```bash
git add -f chat_orchestrator/tests/services/test_telegram_transport_media_cap.py
git add chat_orchestrator/orchestrator/services/telegram_transport.py
git commit -m "feat(media): make download_telegram_photo's size cap configurable"
```

---

### Task 4: `capture_escalation_media` — download + upload + record

**Files:**
- Create: `chat_orchestrator/orchestrator/services/ticketing/attachment_capture.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_attachment_capture.py`

**Design notes:**
- `extract_media_file_ids(metadata)` mirrors the exact field names `prepare_media.py` reads (`photo_file_ids`, `photo_file_id`, `video_file_id`, `voice_file_id`, `audio_file_id`), so any caller that already has a `metadata` dict (all four escalation call sites do) can build the list with one call.
- `MAX_TICKET_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024` — matches the Jira Cloud standard per-file attachment limit. If your Jira instance's actual limit differs, adjust this constant.
- Every failure (download or upload) is caught, logged, and skipped — this function must never raise, since escalation creation must never be blocked by a media problem.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_attachment_capture.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.services.ticketing.attachment_capture'`.

- [ ] **Step 3: Write the implementation**

```python
"""Downloads Telegram media for the escalation-triggering turn and durably
records it as an escalation attachment (Storage upload + DB row).

Called synchronously from EscalationService.escalate_to_support() -- the
only point in the request lifecycle with access to the triggering turn's
raw Telegram file_ids (see attachment_repository.py's module docstring for
why capture must happen here, not at ticket-filing time).
"""

from __future__ import annotations

import mimetypes
import uuid
from base64 import b64decode
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple

from shared.utils.logging import get_logger

from .attachment_repository import BUCKET_NAME, AttachmentRepository

LOGGER = get_logger(__name__)

# Jira Cloud's standard per-file attachment limit. Adjust if your Jira
# instance's actual limit differs. Deliberately separate from
# telegram_transport.MAX_MEDIA_SIZE_BYTES (5MB), which caps the unrelated
# LLM-vision download path and must not change for this feature.
MAX_TICKET_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024

DownloadFn = Callable[[str, str, int], Awaitable[Tuple[Optional[str], Optional[str]]]]

_MEDIA_FIELD_TO_TYPE: Dict[str, Literal["image", "video", "audio"]] = {
    "video_file_id": "video",
    "voice_file_id": "audio",
    "audio_file_id": "audio",
}


def extract_media_file_ids(metadata: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pull {type, file_id} pairs out of a chat turn's metadata dict.

    Mirrors exactly the fields orchestrator.graphs.nodes.prepare_media reads
    (photo_file_ids/photo_file_id/video_file_id/voice_file_id/audio_file_id)
    so callers that already have `metadata` (every escalate_to_support call
    site does) don't need to know Telegram's webhook shape themselves.
    """
    result: List[Dict[str, str]] = []

    photo_file_ids = metadata.get("photo_file_ids") or []
    if not photo_file_ids and metadata.get("photo_file_id"):
        photo_file_ids = [metadata["photo_file_id"]]
    for file_id in photo_file_ids:
        result.append({"type": "image", "file_id": file_id})

    for field, media_type in _MEDIA_FIELD_TO_TYPE.items():
        file_id = metadata.get(field)
        if file_id:
            result.append({"type": media_type, "file_id": file_id})

    return result


async def capture_escalation_media(
    *,
    escalation_id: str,
    media_file_ids: List[Dict[str, str]],
    bot_token: str,
    get_client: Callable[[], Optional[Any]],
    attachment_repository: AttachmentRepository,
    download_fn: Optional[DownloadFn] = None,
) -> None:
    """Download, upload, and record each media item. Never raises.

    Per-item failures (download or upload) are logged and skipped -- a
    problem with one attachment must never block escalation creation or
    lose the other attachments in the same turn.
    """
    if not media_file_ids:
        return

    client = get_client()
    if client is None:
        LOGGER.warning(
            "capture_escalation_media: no Supabase client available -- skipping capture "
            "for escalation {}",
            escalation_id,
        )
        return

    if download_fn is None:
        from orchestrator.services.telegram_transport import download_telegram_photo

        download_fn = download_telegram_photo

    for item in media_file_ids:
        file_id = item["file_id"]
        media_type = item["type"]
        try:
            base64_data, mime_type = await download_fn(
                file_id, bot_token, MAX_TICKET_ATTACHMENT_SIZE_BYTES
            )
            if not base64_data:
                LOGGER.warning(
                    "capture_escalation_media: download failed for file_id={} (escalation {})",
                    file_id,
                    escalation_id,
                )
                continue

            file_bytes = b64decode(base64_data)
            extension = mimetypes.guess_extension(mime_type or "") or ""
            storage_path = f"{escalation_id}/{uuid.uuid4()}{extension}"

            client.storage.from_(BUCKET_NAME).upload(
                storage_path, file_bytes, {"content-type": mime_type or "application/octet-stream"}
            )

            await attachment_repository.insert(
                escalation_id=escalation_id,
                storage_path=storage_path,
                media_type=media_type,
                mime_type=mime_type or "application/octet-stream",
                size_bytes=len(file_bytes),
            )
        except Exception:
            LOGGER.warning(
                "capture_escalation_media: failed to capture file_id={} for escalation {}",
                file_id,
                escalation_id,
                exc_info=True,
            )
            continue
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_attachment_capture.py -v
```

Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add -f chat_orchestrator/tests/services/ticketing/test_attachment_capture.py
git add chat_orchestrator/orchestrator/services/ticketing/attachment_capture.py
git commit -m "feat(tickets): add capture_escalation_media for the initial-escalation media path"
```

---

### Task 5: Wire `EscalationService` to capture media at escalation time

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/escalation_service.py:91-133 (constructor), 213-291 (escalate_to_support signature), 327-341 (_escalate_to_telegram signature), 636-651 (post-record_canonical_escalation hook)`
- Test: `chat_orchestrator/tests/services/test_escalation_service_media_capture.py`

**Design note:** only the *initial* escalation branch of `_escalate_to_telegram` (the one that runs `_record_canonical_escalation` at line 641, reached when there is no existing active escalation) gets wired. The follow-up branch (lines 357-514, active-escalation replies) is explicitly out of scope for phase 1 — that's the `forward_customer_message()`/Telegram-forwarding path, unchanged by this plan.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for EscalationService's media-capture wiring at escalation time."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.escalation_service import EscalationService


def _make_service() -> EscalationService:
    service = EscalationService(
        escalation_chat_id="-100123",
        bot_token="test-token",
        supabase_url="https://example.supabase.co",
        supabase_key="test-key",
    )
    service._send_telegram_message = AsyncMock(
        return_value={"ok": True, "result": {"message_id": 555}}
    )
    service._get_supabase_client = MagicMock(return_value=MagicMock())
    service._get_raw_client = MagicMock(return_value=MagicMock())
    service.get_escalation_info = AsyncMock(return_value=None)
    service._get_or_create_escalation_topic = AsyncMock(return_value=None)
    return service


@pytest.mark.asyncio
async def test_escalate_to_support_captures_media_when_present() -> None:
    service = _make_service()
    service._get_supabase_client.return_value.save_escalation_mapping = AsyncMock(
        return_value="mapping-1"
    )
    service._record_canonical_escalation = AsyncMock()
    with patch(
        "orchestrator.services.escalation_service.capture_escalation_media",
        new=AsyncMock(),
    ) as fake_capture:
        result = await service.escalate_to_support(
            question_summary="Meter is sparking, see photo",
            session_id="telegram_abc",
            customer_chat_id="123",
            media_file_ids=[{"type": "image", "file_id": "photo1"}],
        )

    assert result["success"] is True
    fake_capture.assert_awaited_once()
    call_kwargs = fake_capture.await_args.kwargs
    assert call_kwargs["escalation_id"] == "mapping-1"
    assert call_kwargs["media_file_ids"] == [{"type": "image", "file_id": "photo1"}]


@pytest.mark.asyncio
async def test_escalate_to_support_skips_capture_when_no_media() -> None:
    service = _make_service()
    service._get_supabase_client.return_value.save_escalation_mapping = AsyncMock(
        return_value="mapping-1"
    )
    service._record_canonical_escalation = AsyncMock()
    with patch(
        "orchestrator.services.escalation_service.capture_escalation_media",
        new=AsyncMock(),
    ) as fake_capture:
        await service.escalate_to_support(
            question_summary="Just a question",
            session_id="telegram_abc",
            customer_chat_id="123",
        )
    fake_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalation_still_succeeds_when_capture_raises() -> None:
    service = _make_service()
    service._get_supabase_client.return_value.save_escalation_mapping = AsyncMock(
        return_value="mapping-1"
    )
    service._record_canonical_escalation = AsyncMock()
    with patch(
        "orchestrator.services.escalation_service.capture_escalation_media",
        new=AsyncMock(side_effect=RuntimeError("storage down")),
    ):
        result = await service.escalate_to_support(
            question_summary="Meter is sparking, see photo",
            session_id="telegram_abc",
            customer_chat_id="123",
            media_file_ids=[{"type": "image", "file_id": "photo1"}],
        )
    assert result["success"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd chat_orchestrator && python -m pytest tests/services/test_escalation_service_media_capture.py -v
```

Expected: FAIL — `TypeError: escalate_to_support() got an unexpected keyword argument 'media_file_ids'`.

- [ ] **Step 3: Add the `AttachmentRepository` to the constructor**

In `chat_orchestrator/orchestrator/services/escalation_service.py`, add the import near the other ticketing imports (line 33):

```python
from orchestrator.services.ticketing.attachment_capture import capture_escalation_media
from orchestrator.services.ticketing.attachment_repository import AttachmentRepository
```

Then in `__init__` (after line 133's `self._deliveries = DeliveryRepository(get_client=self._get_raw_client)`):

```python
        self._attachments = AttachmentRepository(get_client=self._get_raw_client)
```

- [ ] **Step 4: Thread `media_file_ids` through `escalate_to_support` and `_escalate_to_telegram`**

Add the parameter to both signatures (`escalate_to_support` at line 213, `_escalate_to_telegram` at line 327), and forward it in the one call from the former to the latter:

```python
    async def escalate_to_support(
        self,
        question_summary: str,
        session_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        organization_short_name: Optional[str] = None,
        customer_chat_id: Optional[str] = None,
        customer_topic_id: Optional[str] = None,
        customer_username: Optional[str] = None,
        customer_email: Optional[str] = None,
        conversation_context: Optional[str] = None,
        grid_name: Optional[str] = None,
        reason: Optional[str] = None,
        action_type: Optional[str] = None,
        thread_id: Optional[str] = None,
        media_file_ids: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
```

(add `media_file_ids` to the docstring's Args too: `media_file_ids: Optional [{type, file_id}] pairs from the triggering turn's Telegram metadata (see attachment_capture.extract_media_file_ids) -- captured to the new ticket-attachment pipeline, independent of the LLM-vision media path.`)

In the body's `return await self._escalate_to_telegram(...)` call, add:

```python
            media_file_ids=media_file_ids,
```

And on `_escalate_to_telegram`'s signature, add the same parameter:

```python
    async def _escalate_to_telegram(
        self,
        question_summary: str,
        session_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        organization_short_name: Optional[str] = None,
        customer_chat_id: Optional[str] = None,
        customer_topic_id: Optional[str] = None,
        customer_username: Optional[str] = None,
        customer_email: Optional[str] = None,
        conversation_context: Optional[str] = None,
        reason: Optional[str] = None,
        action_type: Optional[str] = None,
        thread_id: Optional[str] = None,
        media_file_ids: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
```

- [ ] **Step 5: Capture media after the initial escalation's canonical record succeeds**

In `_escalate_to_telegram`, right after the existing block that calls `_record_canonical_escalation` for the *initial* (non-follow-up) escalation (around line 640-646):

```python
                        if saved_mapping_id:
                            await self._record_canonical_escalation(
                                saved_mapping_id,
                                session_id,
                                message_id=escalation_message_id,
                                topic_id=escalation_topic_id,
                            )
```

add immediately after it:

```python
                        if saved_mapping_id and media_file_ids:
                            try:
                                await capture_escalation_media(
                                    escalation_id=saved_mapping_id,
                                    media_file_ids=media_file_ids,
                                    bot_token=self._bot_token,
                                    get_client=self._get_supabase_client,
                                    attachment_repository=self._attachments,
                                )
                            except Exception:
                                LOGGER.warning(
                                    "Failed to capture escalation media for mapping {}",
                                    saved_mapping_id,
                                    exc_info=True,
                                )
```

Note: `get_client=self._get_supabase_client` passes the *wrapper* (`SupabaseClient`), not the raw postgrest client — `capture_escalation_media` needs `.storage`, which lives on the wrapper's underlying `supabase-py` client. Since `self._get_supabase_client()` returns the `SupabaseClient` wrapper (not the raw client), and `capture_escalation_media`'s `get_client` callback is expected to return something with `.storage`, use `self._get_raw_client` instead — the raw client is the actual `supabase-py` `Client` object with both `.table()` and `.storage`:

```python
                                    get_client=self._get_raw_client,
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd chat_orchestrator && python -m pytest tests/services/test_escalation_service_media_capture.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 7: Run the full existing escalation_service test suite to confirm no regression**

```bash
cd chat_orchestrator && python -m pytest tests/services/test_escalation_service*.py -q
```

Expected: PASS, all existing tests green.

- [ ] **Step 8: Commit**

```bash
git add -f chat_orchestrator/tests/services/test_escalation_service_media_capture.py
git add chat_orchestrator/orchestrator/services/escalation_service.py
git commit -m "feat(escalation): capture triggering-turn media when an escalation is first created"
```

---

### Task 6: Pass `media_file_ids` from the four `escalate_to_support` call sites

**Files:**
- Modify: `chat_orchestrator/orchestrator/graphs/conversation_graph.py:975 area (_escalate_for_blocked_content), 1042 area (_escalate_for_loop), 1460 area (_execute_tool_calls direct-call path)`
- Modify: `chat_orchestrator/orchestrator/graphs/nodes/safety_check.py:190 area`
- Test: `chat_orchestrator/tests/graphs/test_escalate_to_support_media_wiring.py`

**Design note:** all four call sites already have a `metadata` dict in scope (`state.get("metadata", {})` or a local copy of it) that carries the same raw `photo_file_id`/`video_file_id`/etc. fields `prepare_media.py` reads — `extract_media_file_ids` (Task 4) turns that into the `media_file_ids` list `escalate_to_support` now accepts.

- [ ] **Step 1: Write the failing tests**

```python
"""Verifies every escalate_to_support call site forwards media_file_ids
extracted from the turn's metadata."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.graphs.conversation_graph import ConversationGraphBuilder


def _escalation_service_mock() -> MagicMock:
    mock = MagicMock()
    mock.is_enabled.return_value = True
    mock.escalate_to_support = AsyncMock(return_value={"success": True})
    return mock


@pytest.mark.asyncio
async def test_direct_tool_call_path_forwards_media_file_ids() -> None:
    graph = ConversationGraphBuilder.__new__(ConversationGraphBuilder)
    graph._escalation_handler = None  # forces the direct-EscalationService path
    from orchestrator.models.schemas import FunctionCall

    call = FunctionCall(
        name="escalate_to_support",
        arguments={"question_summary": "Help, see photo"},
    )
    metadata = {"photo_file_id": "abc123", "session_id": "telegram_1"}

    with patch(
        "orchestrator.services.escalation_service.EscalationService",
        return_value=_escalation_service_mock(),
    ) as service_cls:
        await graph._execute_tool_calls([call], metadata)

    escalation_service = service_cls.return_value
    escalation_service.escalate_to_support.assert_awaited_once()
    kwargs = escalation_service.escalate_to_support.await_args.kwargs
    assert kwargs["media_file_ids"] == [{"type": "image", "file_id": "abc123"}]


@pytest.mark.asyncio
async def test_content_block_escalation_forwards_media_file_ids() -> None:
    graph = ConversationGraphBuilder.__new__(ConversationGraphBuilder)
    graph._extract_org_id = MagicMock(return_value=None)
    state: Dict[str, Any] = {
        "session_id": "telegram_1",
        "user_context": None,
        "metadata": {"video_file_id": "vid123"},
        "user_input": "hello",
    }

    with patch(
        "orchestrator.services.escalation_service.EscalationService",
        return_value=_escalation_service_mock(),
    ) as service_cls:
        await graph._escalate_for_blocked_content(state, "SAFETY", "hello")

    kwargs = service_cls.return_value.escalate_to_support.await_args.kwargs
    assert kwargs["media_file_ids"] == [{"type": "video", "file_id": "vid123"}]


@pytest.mark.asyncio
async def test_loop_escalation_forwards_media_file_ids() -> None:
    graph = ConversationGraphBuilder.__new__(ConversationGraphBuilder)
    graph._extract_org_id = MagicMock(return_value=None)
    state: Dict[str, Any] = {
        "session_id": "telegram_1",
        "user_context": None,
        "metadata": {"audio_file_id": "aud123"},
        "user_input": "hello",
    }
    loop_result = MagicMock(consecutive_similar_turns=3)

    with patch(
        "orchestrator.services.escalation_service.EscalationService",
        return_value=_escalation_service_mock(),
    ) as service_cls:
        await graph._escalate_for_loop(state, loop_result)

    kwargs = service_cls.return_value.escalate_to_support.await_args.kwargs
    assert kwargs["media_file_ids"] == [{"type": "audio", "file_id": "aud123"}]


```

The `safety_check.py` call site is covered separately in Step 1b below, added
directly to the existing `chat_orchestrator/tests/test_safety_check.py` (that
file already has the exact fixture this call site needs — `EscalationService`
and `get_auth_service` stubbed via `monkeypatch`, plus a `_make_state` helper
and a real leaked-tool-call fixture string. Reusing it, rather than
reconstructing a parallel fixture, is what makes this test accurate instead
of guessing at `safety_check.py`'s internal control flow).

- [ ] **Step 1b: Add the media-forwarding test to the existing `test_safety_check.py`**

Append to `chat_orchestrator/tests/test_safety_check.py` (reusing its
existing `_make_state`, `LEAKED_TEXT`, and `fake_escalation_service` fixture
already defined at the top of that file):

```python
@pytest.mark.asyncio
async def test_raw_tool_call_leak_forwards_media_file_ids(fake_escalation_service):
    state = _make_state(final_response=LEAKED_TEXT, metadata={"photo_file_id": "photo1"})

    await safety_check(state)

    _, kwargs = fake_escalation_service.escalate_to_support.await_args
    assert kwargs["media_file_ids"] == [{"type": "image", "file_id": "photo1"}]
```

Note this also requires `_make_state`'s `base` dict (line ~23) to include a
`"metadata": {}` default, since `safety_check.py` will now read
`state.get("metadata", {})` — check whether `_make_state` already has a
`metadata` key; if not, add `"metadata": {}` to its `base` dict so every
other pre-existing test in the file keeps passing an empty-but-present
metadata rather than relying on `.get(..., {})`'s fallback (harmless either
way, but explicit is clearer here since this task is what introduces the
first read of it).

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd chat_orchestrator && python -m pytest tests/graphs/test_escalate_to_support_media_wiring.py tests/test_safety_check.py -v -k media
```

Expected: FAIL — each `escalate_to_support.await_args.kwargs` will not contain `media_file_ids` (call sites don't pass it yet).

- [ ] **Step 3: Wire the direct-call site (`conversation_graph.py` ~1460)**

Add the import near the top of `conversation_graph.py` (with the other `orchestrator.services.ticketing` imports, or inline within the function like the existing `from orchestrator.services.escalation_service import EscalationService` at line ~1458):

```python
                    from orchestrator.services.ticketing.attachment_capture import (
                        extract_media_file_ids,
                    )

                    esc_service = EscalationService()
                    args = call.arguments or {}
                    esc_result = await esc_service.escalate_to_support(
                        question_summary=args.get("question_summary", "Escalation requested"),
                        session_id=metadata.get("session_id"),
                        organization_id=metadata.get("organization_id"),
                        organization_short_name=metadata.get("organization_name"),
                        customer_chat_id=metadata.get("original_chat_id"),
                        customer_topic_id=metadata.get("topic_id"),
                        customer_username=metadata.get("user_name"),
                        customer_email=metadata.get("user_email"),
                        conversation_context=args.get("conversation_context"),
                        reason=args.get("reason"),
                        action_type=args.get("action_type"),
                        thread_id=metadata.get("thread_id"),
                        media_file_ids=extract_media_file_ids(metadata),
                    )
```

- [ ] **Step 4: Wire `_escalate_for_blocked_content` (~line 990-1010)**

```python
        try:
            escalation_service = EscalationService()

            if escalation_service.is_enabled():
                from orchestrator.services.ticketing.attachment_capture import (
                    extract_media_file_ids,
                )

                result = await escalation_service.escalate_to_support(
                    question_summary=f"[CONTENT BLOCKED - {finish_reason}] User message was blocked by AI safety filters",
                    session_id=session_id or "",
                    organization_id=organization_id,
                    organization_short_name=organization_name,
                    customer_chat_id=customer_chat_id,
                    customer_topic_id=customer_topic_id,
                    customer_username=customer_username,
                    customer_email=customer_email,
                    conversation_context=(
                        f"[AUTO-ESCALATION: Gemini blocked with finishReason={finish_reason}]\n\n"
                        f"User message: {user_input[:500]}\n\n"
                        "Please review if this was a legitimate request that was incorrectly blocked."
                    ),
                    reason="content_blocked",
                    thread_id=state.get("thread_id"),
                    media_file_ids=extract_media_file_ids(metadata),
                )
```

- [ ] **Step 5: Wire `_escalate_for_loop` (~line 1064-1085)**

```python
        try:
            escalation_service = EscalationService()

            if escalation_service.is_enabled():
                from orchestrator.services.ticketing.attachment_capture import (
                    extract_media_file_ids,
                )

                result = await escalation_service.escalate_to_support(
                    question_summary=(
                        f"[LOOP DETECTED] Bot stuck in repetitive loop "
                        f"({loop_result.consecutive_similar_turns} identical responses)"
                    ),
                    session_id=session_id or "",
                    organization_id=organization_id,
                    organization_short_name=organization_name,
                    customer_chat_id=customer_chat_id,
                    customer_topic_id=customer_topic_id,
                    customer_username=customer_username,
                    customer_email=customer_email,
                    conversation_context=(
                        f"[AUTO-ESCALATION: Cross-request loop detected]\n\n"
                        f"The bot has repeated the same response "
                        f"{loop_result.consecutive_similar_turns} times in a row.\n"
                        f"Latest user message: {user_input[:500]}\n\n"
                        f"The loop-breaking hint was injected but the model "
                        f"continues repeating. Human intervention needed."
                    ),
                    reason="loop_detected",
                    thread_id=state.get("thread_id"),
                    media_file_ids=extract_media_file_ids(metadata),
                )
```

- [ ] **Step 6: Wire `safety_check.py` (~line 190)**

Add the import near the top of `chat_orchestrator/orchestrator/graphs/nodes/safety_check.py`:

```python
from orchestrator.services.ticketing.attachment_capture import extract_media_file_ids
```

Then, right before the `safety_result = await safety_escalation_service.escalate_to_support(` call, add:

```python
        safety_result = await safety_escalation_service.escalate_to_support(
            question_summary=summary,
            session_id=session_id,
            organization_id=(
                int(user_context.organization_ids[0])
                if user_context and user_context.organization_ids
                else None
            ),
            organization_short_name=org_short_name,
            customer_chat_id=user_context.chat_id if user_context else None,
            customer_topic_id=user_context.topic_id if user_context else None,
            customer_username=user_context.username if user_context else None,
            customer_email=user_context.user_email if user_context else None,
            conversation_context=esc_context,
            reason="safety_escalation",
            media_file_ids=extract_media_file_ids(state.get("metadata", {})),
        )
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd chat_orchestrator && python -m pytest tests/graphs/test_escalate_to_support_media_wiring.py -v
```

Expected: PASS (4 tests: 3 in the new file, 1 appended to `test_safety_check.py`).

- [ ] **Step 8: Run the full existing graphs and safety-check test suites to confirm no regression**

```bash
cd chat_orchestrator && python -m pytest tests/graphs/ tests/test_safety_check.py -q
```

Expected: PASS, all existing tests green.

- [ ] **Step 9: Commit**

```bash
git add -f chat_orchestrator/tests/graphs/test_escalate_to_support_media_wiring.py
git add chat_orchestrator/orchestrator/graphs/conversation_graph.py chat_orchestrator/orchestrator/graphs/nodes/safety_check.py
git add chat_orchestrator/tests/test_safety_check.py
git commit -m "feat(escalation): forward triggering-turn media_file_ids from every escalate_to_support call site"
```

---

### Task 7: `TicketBackend.add_attachments` — protocol, types, and `InternalTicketBackend`'s no-op

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/backend.py`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/internal_backend.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_internal_backend.py` (extend existing file)

- [ ] **Step 1: Write the failing test**

Add to `chat_orchestrator/tests/services/ticketing/test_internal_backend.py` (check the existing file first for its exact fixture/import conventions — mirror them rather than introducing a new style):

```python
from orchestrator.services.ticketing.attachment_repository import EscalationAttachment


class TestAddAttachments:
    @pytest.mark.asyncio
    async def test_returns_empty_list_without_touching_anything(self) -> None:
        backend = InternalTicketBackend(client=MagicMock())
        attachment = EscalationAttachment(
            id="att-1",
            escalation_id="esc-1",
            ticket_id="ticket-1",
            storage_path="esc-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        result = await backend.add_attachments("INT-1", [attachment])
        assert result == []
```

(Use whatever `MagicMock`/fixture imports the existing test file already has at its top — do not duplicate imports it already provides.)

- [ ] **Step 2: Run test to verify it fails**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_internal_backend.py -v -k add_attachments
```

Expected: FAIL — `AttributeError: 'InternalTicketBackend' object has no attribute 'add_attachments'`.

- [ ] **Step 3: Add `EscalationAttachment` re-export and `AttachmentSyncResult` type, plus the Protocol method**

In `chat_orchestrator/orchestrator/services/ticketing/backend.py`, add after the `TicketBackendError` class (line 120) and before the `TicketBackend` Protocol:

```python
class AttachmentSyncResult(BaseModel):
    """One attachment successfully pushed to an external backend (e.g. Jira).

    Returned by TicketBackend.add_attachments() so TicketService can persist
    the external id (escalation_attachments.jira_attachment_id) without the
    backend needing its own AttachmentRepository reference.
    """

    attachment_id: str
    external_id: str
```

Add the import for `EscalationAttachment` at the top of `backend.py`:

```python
from .attachment_repository import EscalationAttachment
```

Then add the method to the `TicketBackend` Protocol (after `find_open_by_grid`):

```python
    async def add_attachments(
        self, ticket_ref: str, attachments: List["EscalationAttachment"]
    ) -> List["AttachmentSyncResult"]:
        """Push attachments to this backend's external system, if any.

        Attachments are already linked to the ticket (escalation_attachments.
        ticket_id is set by TicketService before this is called) and already
        live in Storage -- this method only needs to do backend-specific I/O.
        The internal backend has nothing external to push to and always
        returns []. Never raises -- a failed upload is logged and simply
        omitted from the returned list, leaving that attachment's
        jira_attachment_id NULL for a future retry.
        """
        ...
```

- [ ] **Step 4: Implement the no-op on `InternalTicketBackend`**

In `chat_orchestrator/orchestrator/services/ticketing/internal_backend.py`, add the import:

```python
from .attachment_repository import EscalationAttachment
from .backend import AttachmentSyncResult
```

(alongside the existing `from .backend import (...)` import block) and add the method after `find_open_by_grid`:

```python
    async def add_attachments(
        self, ref: str, attachments: List[EscalationAttachment]
    ) -> List[AttachmentSyncResult]:
        """No-op: internal tickets have no external system to push bytes to.

        Linking (escalation_attachments.ticket_id) already happened in
        TicketService.create_ticket() before this is called -- there is
        nothing backend-specific left to do.
        """
        return []
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_internal_backend.py -v
```

Expected: PASS, including the new test and all pre-existing ones in the file.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/backend.py chat_orchestrator/orchestrator/services/ticketing/internal_backend.py
git add -f chat_orchestrator/tests/services/ticketing/test_internal_backend.py
git commit -m "feat(tickets): add TicketBackend.add_attachments protocol + internal no-op"
```

---

### Task 8: `JiraTicketBackend.add_attachments` — real upload

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/jira_backend.py:159-180 (constructor), end of class (new methods)`
- Test: `chat_orchestrator/tests/services/ticketing/test_jira_backend.py` (extend, reusing `FakeJiraSession`)

- [ ] **Step 1: Write the failing tests**

Add to `chat_orchestrator/tests/services/ticketing/test_jira_backend.py`, reusing the existing `fake_session` fixture and `FakeJiraSession`/`_FakeResponse` helpers already defined at the top of that file:

```python
from orchestrator.services.ticketing.attachment_repository import EscalationAttachment


class _FakeStorageBucket:
    def __init__(self, contents: Dict[str, bytes]) -> None:
        self._contents = contents

    def download(self, path: str) -> bytes:
        return self._contents[path]


class _FakeStorage:
    def __init__(self, bucket: _FakeStorageBucket) -> None:
        self._bucket = bucket

    def from_(self, name: str) -> _FakeStorageBucket:
        assert name == "escalation-media"
        return self._bucket


class _FakeSupabaseClient:
    def __init__(self, contents: Dict[str, bytes]) -> None:
        self.storage = _FakeStorage(_FakeStorageBucket(contents))


def _attachment(**overrides: Any) -> EscalationAttachment:
    defaults = dict(
        id="att-1",
        escalation_id="esc-1",
        ticket_id="ticket-1",
        storage_path="esc-1/photo.jpg",
        media_type="image",
        mime_type="image/jpeg",
        size_bytes=10,
        jira_attachment_id=None,
    )
    defaults.update(overrides)
    return EscalationAttachment(**defaults)


class TestAddAttachments:
    @pytest.mark.asyncio
    async def test_uploads_each_unsynced_attachment_and_returns_sync_results(
        self, fake_session: FakeJiraSession
    ) -> None:
        backend = JiraTicketBackend(
            base_url="https://example.atlassian.net",
            email="bot@example.com",
            api_token="tok",
            get_storage_client=lambda: _FakeSupabaseClient({"esc-1/photo.jpg": b"fake-image-bytes"}),
        )
        fake_session.queue(
            "POST",
            "/issue/OPS-1/attachments",
            _FakeResponse(200, json_data=[{"id": "10050", "filename": "esc-1_photo.jpg"}]),
        )

        results = await backend.add_attachments("OPS-1", [_attachment()])

        assert len(results) == 1
        assert results[0].attachment_id == "att-1"
        assert results[0].external_id == "10050"

        post_calls = [c for c in fake_session.calls if c[0] == "POST"]
        assert len(post_calls) == 1
        _, url, kwargs = post_calls[0]
        assert "/issue/OPS-1/attachments" in url
        assert kwargs["headers"]["X-Atlassian-Token"] == "no-check"
        assert "Content-Type" not in kwargs["headers"]

    @pytest.mark.asyncio
    async def test_skips_attachments_already_synced(self, fake_session: FakeJiraSession) -> None:
        backend = JiraTicketBackend(
            base_url="https://example.atlassian.net",
            email="bot@example.com",
            api_token="tok",
            get_storage_client=lambda: _FakeSupabaseClient({}),
        )
        already_synced = _attachment(jira_attachment_id="99999")

        results = await backend.add_attachments("OPS-1", [already_synced])

        assert results == []
        assert fake_session.calls == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_and_does_not_raise_on_upload_failure(
        self, fake_session: FakeJiraSession
    ) -> None:
        backend = JiraTicketBackend(
            base_url="https://example.atlassian.net",
            email="bot@example.com",
            api_token="tok",
            get_storage_client=lambda: _FakeSupabaseClient({"esc-1/photo.jpg": b"fake-image-bytes"}),
        )
        fake_session.queue(
            "POST", "/issue/OPS-1/attachments", _FakeResponse(500, text_data="server error")
        )

        results = await backend.add_attachments("OPS-1", [_attachment()])

        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_storage_client_configured(self) -> None:
        backend = JiraTicketBackend(
            base_url="https://example.atlassian.net", email="bot@example.com", api_token="tok"
        )
        results = await backend.add_attachments("OPS-1", [_attachment()])
        assert results == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_jira_backend.py -v -k AddAttachments
```

Expected: FAIL — `TypeError: JiraTicketBackend.__init__() got an unexpected keyword argument 'get_storage_client'`.

- [ ] **Step 3: Add `get_storage_client` to the constructor**

Check the exact current constructor signature first (`chat_orchestrator/orchestrator/services/ticketing/jira_backend.py:164-180`), then add one new parameter following the same `Optional[...] = None` style already used there for `base_url`/`email`/`api_token`:

```python
    def __init__(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        get_storage_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
```

(add `Callable` to the existing `typing` import line if not already imported) and store it:

```python
        self._get_storage_client = get_storage_client
```

- [ ] **Step 4: Add `_download_attachment_bytes`, `_upload_jira_attachment`, and `add_attachments`**

Add near the other "Jira REST helpers" section (after `_jira_auth_headers`, ~line 578), and import `EscalationAttachment`/`AttachmentSyncResult`/`BUCKET_NAME`:

```python
from .attachment_repository import BUCKET_NAME, EscalationAttachment
```

(add to the existing `from .backend import (...)` block:)

```python
from .backend import (
    AttachmentSyncResult,
    TicketBackendError,
    TicketCreateRequest,
    TicketResult,
    TicketStatus,
    TicketSummary,
)
```

Then the new methods:

```python
    def _download_attachment_bytes(self, storage_path: str) -> Optional[bytes]:
        if self._get_storage_client is None:
            return None
        client = self._get_storage_client()
        if client is None:
            return None
        try:
            return client.storage.from_(BUCKET_NAME).download(storage_path)
        except Exception:
            LOGGER.warning(
                "Failed to download attachment {} from storage", storage_path, exc_info=True
            )
            return None

    async def _upload_jira_attachment(
        self, issue_key: str, filename: str, content: bytes, mime_type: str
    ) -> Optional[str]:
        """POST one file to Jira's attachment endpoint. Returns the Jira
        attachment id on success, None on any failure (never raises).

        Jira's attachment endpoint requires multipart/form-data and the
        X-Atlassian-Token header -- unlike every other Jira call in this
        class, it must NOT send Content-Type: application/json, so this
        builds its own headers rather than reusing _jira_auth_headers().
        """
        url = f"{self._jira_base_url}/rest/api/3/issue/{issue_key}/attachments"
        headers = {
            "Authorization": self._jira_auth_headers()["Authorization"],
            "X-Atlassian-Token": "no-check",
        }
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename, content_type=mime_type)
        try:
            session = _get_jira_session()
            async with session.post(
                url, data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status not in (200, 201):
                    body = await response.text()
                    LOGGER.warning(
                        "Jira attachment upload failed for {}: HTTP {}: {}",
                        issue_key,
                        response.status,
                        body,
                    )
                    return None
                result = await response.json()
        except Exception:
            LOGGER.warning("Jira attachment upload failed for {}", issue_key, exc_info=True)
            return None

        entries = result if isinstance(result, list) else []
        if not entries or not entries[0].get("id"):
            LOGGER.warning("Jira attachment upload for {} returned no attachment id", issue_key)
            return None
        return str(entries[0]["id"])

    async def add_attachments(
        self, ticket_ref: str, attachments: List[EscalationAttachment]
    ) -> List[AttachmentSyncResult]:
        results: List[AttachmentSyncResult] = []
        for attachment in attachments:
            if attachment.jira_attachment_id:
                continue
            content = self._download_attachment_bytes(attachment.storage_path)
            if content is None:
                continue
            filename = attachment.storage_path.rsplit("/", 1)[-1]
            jira_attachment_id = await self._upload_jira_attachment(
                ticket_ref, filename, content, attachment.mime_type
            )
            if jira_attachment_id:
                results.append(
                    AttachmentSyncResult(
                        attachment_id=attachment.id, external_id=jira_attachment_id
                    )
                )
        return results
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_jira_backend.py -v -k AddAttachments
```

Expected: PASS (4 tests).

- [ ] **Step 6: Run the full existing jira_backend test suite to confirm no regression**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_jira_backend.py -q
```

Expected: PASS, all existing tests green.

- [ ] **Step 7: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/jira_backend.py
git add -f chat_orchestrator/tests/services/ticketing/test_jira_backend.py
git commit -m "feat(tickets): implement JiraTicketBackend.add_attachments"
```

---

### Task 9: Wire `TicketService.create_ticket` to link and sync attachments

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/service.py:43-69 (constructor), 180-195 (create_ticket)`
- Test: `chat_orchestrator/tests/services/ticketing/test_service.py` (extend existing file)

- [ ] **Step 1: Write the failing tests**

Add to `chat_orchestrator/tests/services/ticketing/test_service.py` (check its existing fixtures/imports first and mirror them):

```python
from orchestrator.services.ticketing.attachment_repository import EscalationAttachment
from orchestrator.services.ticketing.backend import AttachmentSyncResult


class TestDefaultJiraBackendGetsStorageGetter:
    def test_default_constructed_jira_backend_can_download_attachments(self) -> None:
        """Regression guard for the exact bug caught in this task's self-review:
        TicketService's default `self._jira = jira_backend or JiraTicketBackend()`
        must pass get_storage_client=self._raw_client, or add_attachments()
        silently no-ops in production (only the DI'd tests in Task 8 supply one)."""
        service = TicketService(supabase_client=MagicMock())
        assert service._jira._get_storage_client is not None
        assert service._jira._get_storage_client == service._raw_client


class TestCreateTicketAttachments:
    @pytest.mark.asyncio
    async def test_links_and_syncs_attachments_when_present_for_the_escalation(self) -> None:
        attachment = EscalationAttachment(
            id="att-1",
            escalation_id="mapping-1",
            storage_path="mapping-1/a.jpg",
            media_type="image",
            mime_type="image/jpeg",
            size_bytes=10,
        )
        internal_backend = MagicMock()
        internal_backend.name = "internal"
        internal_backend.create_ticket = AsyncMock(
            return_value=BackendTicketResult(ref="INT-1", backend="internal")
        )
        internal_backend.add_attachments = AsyncMock(
            return_value=[AttachmentSyncResult(attachment_id="att-1", external_id="ext-1")]
        )

        service = TicketService(supabase_client=MagicMock(), internal_backend=internal_backend)
        service._tickets = MagicMock()
        service._tickets.create_intent = AsyncMock(
            return_value=TicketRecord(
                id="ticket-1",
                created_via="escalation",
                provisioning_state="pending",
                summary="s",
            )
        )
        service._tickets.set_pending_backend = AsyncMock()
        service._tickets.activate = AsyncMock()
        service._attachments = MagicMock()
        service._attachments.list_by_escalation = AsyncMock(return_value=[attachment])
        service._attachments.link_ticket = AsyncMock()
        service._attachments.mark_synced = AsyncMock()

        await service.create_ticket(
            TicketCreateRequest(
                summary="s", escalation_mapping_id="mapping-1", source="escalation"
            ),
            backend_override="internal",
        )

        service._attachments.list_by_escalation.assert_awaited_once_with("mapping-1")
        service._attachments.link_ticket.assert_awaited_once_with("mapping-1", "ticket-1")
        internal_backend.add_attachments.assert_awaited_once_with("INT-1", [attachment])
        service._attachments.mark_synced.assert_awaited_once_with("att-1", "ext-1")

    @pytest.mark.asyncio
    async def test_skips_attachment_work_when_none_exist_for_the_escalation(self) -> None:
        internal_backend = MagicMock()
        internal_backend.name = "internal"
        internal_backend.create_ticket = AsyncMock(
            return_value=BackendTicketResult(ref="INT-1", backend="internal")
        )
        internal_backend.add_attachments = AsyncMock(return_value=[])

        service = TicketService(supabase_client=MagicMock(), internal_backend=internal_backend)
        service._tickets = MagicMock()
        service._tickets.create_intent = AsyncMock(
            return_value=TicketRecord(
                id="ticket-1",
                created_via="escalation",
                provisioning_state="pending",
                summary="s",
            )
        )
        service._tickets.set_pending_backend = AsyncMock()
        service._tickets.activate = AsyncMock()
        service._attachments = MagicMock()
        service._attachments.list_by_escalation = AsyncMock(return_value=[])
        service._attachments.link_ticket = AsyncMock()

        await service.create_ticket(
            TicketCreateRequest(
                summary="s", escalation_mapping_id="mapping-1", source="escalation"
            ),
            backend_override="internal",
        )

        service._attachments.link_ticket.assert_not_awaited()
        internal_backend.add_attachments.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_attachment_lookup_when_no_escalation_mapping_id(self) -> None:
        internal_backend = MagicMock()
        internal_backend.name = "internal"
        internal_backend.create_ticket = AsyncMock(
            return_value=BackendTicketResult(ref="INT-1", backend="internal")
        )

        service = TicketService(supabase_client=MagicMock(), internal_backend=internal_backend)
        service._tickets = MagicMock()
        service._tickets.create_intent = AsyncMock(
            return_value=TicketRecord(
                id="ticket-1",
                created_via="notification",
                provisioning_state="pending",
                summary="s",
            )
        )
        service._tickets.set_pending_backend = AsyncMock()
        service._tickets.activate = AsyncMock()
        service._attachments = MagicMock()
        service._attachments.list_by_escalation = AsyncMock()

        await service.create_ticket(
            TicketCreateRequest(summary="s", source="notify"), backend_override="internal"
        )

        service._attachments.list_by_escalation.assert_not_awaited()
```

(Import `BackendTicketResult`, `TicketRecord`, `TicketCreateRequest`, `AsyncMock`, `MagicMock` at the top if the existing file doesn't already have them — match its existing import style.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_service.py -v -k "CreateTicketAttachments or DefaultJiraBackendGetsStorageGetter"
```

Expected: FAIL — `AttributeError: 'TicketService' object has no attribute '_attachments'` (or similar, since `create_ticket` doesn't call any of the asserted methods yet, and the default `JiraTicketBackend()` isn't given a storage getter yet).

- [ ] **Step 3: Add `AttachmentRepository` to the constructor, and give the default `JiraTicketBackend` its storage getter**

In `chat_orchestrator/orchestrator/services/ticketing/service.py`, add the import:

```python
from .attachment_repository import AttachmentRepository
```

In `__init__`, change the existing default-construction line

```python
        self._jira: TicketBackend = jira_backend or JiraTicketBackend()
```

to pass the storage getter (without this, `JiraTicketBackend.add_attachments` silently no-ops in production — Task 8's `_download_attachment_bytes` returns `None` whenever `self._get_storage_client` is `None`, and only the DI-based tests in Task 8 supply one):

```python
        self._jira: TicketBackend = jira_backend or JiraTicketBackend(
            get_storage_client=self._raw_client
        )
```

Then, after `self._internal = internal_backend or InternalTicketBackend(...)` (line 67-69):

```python
        self._attachments = AttachmentRepository(get_client=self._raw_client)
```

- [ ] **Step 4: Wire `create_ticket` to link and sync**

Modify `create_ticket` (lines 180-195):

```python
    async def create_ticket(
        self, req: TicketCreateRequest, backend_override: Optional[str] = None
    ) -> TicketResult:
        """Create a ticket. ``backend_override`` is forwarded to ``resolve_backend``
        (see its docstring) -- omit to use ``TICKET_BACKEND_OVERRIDE`` as usual."""
        created_via = "notification" if req.source == "notify" else "escalation"
        intent = await self._tickets.create_intent(req, created_via=created_via)
        backend = await self.resolve_backend(override=backend_override)
        await self._tickets.set_pending_backend(intent.id, backend.name)
        result = await backend.create_ticket(req)
        await self._tickets.activate(intent.id, result)
        if req.escalation_mapping_id:
            await self._stamp_escalation_mapping(
                req.escalation_mapping_id, result.ref, result.backend
            )
            await self._sync_attachments(req.escalation_mapping_id, intent.id, result.ref, backend)
        return result.model_copy(update={"ticket_id": intent.id})

    async def _sync_attachments(
        self, escalation_id: str, ticket_id: str, ticket_ref: str, backend: TicketBackend
    ) -> None:
        """Link any escalation-time attachments to the new ticket and push
        them to the backend (a no-op for internal, a real upload for Jira).

        Best-effort: a failure here must not turn an already-created ticket
        into a reported failure -- the ticket exists either way, only the
        attachment sync is incomplete.
        """
        try:
            attachments = await self._attachments.list_by_escalation(escalation_id)
            if not attachments:
                return
            await self._attachments.link_ticket(escalation_id, ticket_id)
            synced = await backend.add_attachments(ticket_ref, attachments)
            for sync_result in synced:
                await self._attachments.mark_synced(
                    sync_result.attachment_id, sync_result.external_id
                )
        except Exception:
            LOGGER.warning(
                "Failed to sync attachments for escalation {} / ticket {}",
                escalation_id,
                ticket_ref,
                exc_info=True,
            )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_service.py -v -k "CreateTicketAttachments or DefaultJiraBackendGetsStorageGetter"
```

Expected: PASS (4 tests).

- [ ] **Step 6: Run the full existing service.py test suite to confirm no regression**

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing/test_service.py tests/services/ticketing/test_service_resolve_backend.py -q
```

Expected: PASS, all existing tests green.

- [ ] **Step 7: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/service.py
git add -f chat_orchestrator/tests/services/ticketing/test_service.py
git commit -m "feat(tickets): link and sync escalation attachments when a ticket is created"
```

---

### Task 10: Ticket UI — surface attachments on the ticket detail view

**Files:**
- Modify: `anansi_app/services/supabase_reader.py:1871-1927 (get_canonical_ticket_detail)`
- Modify: `anansi_app/nicegui_app/pages/tickets.py:329-372 (_render_detail_body), new _attachment_card function`
- Test: `anansi_app/tests/test_supabase_reader_tickets.py` (extend existing file)

- [ ] **Step 1: Write the failing test**

Add to `anansi_app/tests/test_supabase_reader_tickets.py` (check its existing fixtures for how `self.client`/table mocking is set up, and mirror that style):

```python
def test_get_canonical_ticket_detail_includes_attachments(reader_with_client):
    reader, client = reader_with_client
    client.table("tickets").insert(
        {"id": "ticket-1", "summary": "s", "created_via": "escalation", "provisioning_state": "active"}
    ).execute()
    client.table("escalation_attachments").insert(
        {
            "id": "att-1",
            "escalation_id": "esc-1",
            "ticket_id": "ticket-1",
            "storage_path": "esc-1/a.jpg",
            "media_type": "image",
            "mime_type": "image/jpeg",
            "size_bytes": 10,
            "created_at": "2026-07-31T00:00:00Z",
        }
    ).execute()

    detail = reader.get_canonical_ticket_detail("ticket-1")

    assert len(detail["attachments"]) == 1
    assert detail["attachments"][0]["media_type"] == "image"
    assert detail["attachments"][0]["mime_type"] == "image/jpeg"
```

(This test's exact fixture name/shape (`reader_with_client`) is a placeholder for whatever fake-Supabase-client fixture the existing file already uses for `get_canonical_ticket_detail` tests — inspect the file first and match its actual fixture, table-seeding, and assertion style exactly; do not introduce a second, differently-shaped fake client.)

- [ ] **Step 2: Run test to verify it fails**

```bash
cd anansi_app && python -m pytest tests/test_supabase_reader_tickets.py -v -k attachments
```

Expected: FAIL — `KeyError: 'attachments'`.

- [ ] **Step 3: Add the attachments query and a signed-URL helper to `get_canonical_ticket_detail`**

In `anansi_app/services/supabase_reader.py`, inside `get_canonical_ticket_detail` (around line 1891-1898, alongside the existing `deliveries` query):

```python
            attachments = (
                self.client.table("escalation_attachments")
                .select("id, storage_path, media_type, mime_type, size_bytes, created_at")
                .eq("ticket_id", ticket_id)
                .order("created_at", desc=False)
                .limit(50)
                .execute()
            ).data or []
```

And after the existing `ticket["deliveries"] = [...]` block (around line 1926):

```python
        ticket["attachments"] = [
            {
                "media_type": attachment.get("media_type"),
                "mime_type": attachment.get("mime_type"),
                "size_bytes": attachment.get("size_bytes"),
                "created_at": attachment.get("created_at"),
                "signed_url": self._signed_attachment_url(attachment.get("storage_path")),
            }
            for attachment in attachments
        ]
```

Add a small helper method near the other private helpers in the same class (e.g. near `_fetch_correlation`):

```python
    def _signed_attachment_url(self, storage_path: Optional[str]) -> Optional[str]:
        """Return a short-lived signed URL for a private escalation-media object."""
        if not storage_path or not self.client:
            return None
        try:
            response = self.client.storage.from_("escalation-media").create_signed_url(
                storage_path, 3600
            )
            return response.get("signedURL") or response.get("signedUrl")
        except Exception as exc:
            logger.error("Error signing attachment URL for %s: %s", storage_path, exc)
            return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd anansi_app && python -m pytest tests/test_supabase_reader_tickets.py -v -k attachments
```

Expected: PASS.

- [ ] **Step 5: Render attachments in the ticket detail view**

In `anansi_app/nicegui_app/pages/tickets.py`, add a new function near `_comment_card` (around line 448):

```python
def _attachment_card(attachment: dict) -> None:
    with ui.card().classes("w-full q-pa-sm").style("border: 1px solid #e2e8f0"):
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label(f"📎 {attachment.get('media_type', 'file')}").classes("text-caption text-bold")
            if attachment.get("size_bytes"):
                kb = attachment["size_bytes"] / 1024
                ui.label(f"{kb:.0f} KB").classes("text-caption text-grey")
            ui.space()
            ui.label(_format_time_ago(attachment.get("created_at"))).classes(
                "text-caption text-grey"
            )
        if attachment.get("signed_url"):
            if (attachment.get("media_type") or "").startswith("image"):
                ui.image(attachment["signed_url"]).classes("q-mt-xs").style("max-width: 300px")
            else:
                ui.link("↗ View attachment", attachment["signed_url"], new_tab=True)
        else:
            ui.label("(attachment unavailable)").classes("text-caption text-grey")
```

Then in `_render_detail_body`, add a section before the existing `"Comment timeline"` label (i.e. right after the `_correlation_section(detail)` call, around line 366):

```python
        _correlation_section(detail)

        attachments = detail.get("attachments") or []
        if attachments:
            ui.label(f"Attachments ({len(attachments)})").classes("text-bold q-mt-sm")
            with ui.column().classes("gap-1"):
                for attachment in attachments:
                    _attachment_card(attachment)

        ui.label("Comment timeline (read-only)").classes("text-bold q-mt-sm")
```

- [ ] **Step 6: Manually verify in the browser**

Start the anansi_app dev server (check `.claude/launch.json` or the project's existing run instructions for the exact command), open a ticket detail page for a ticket with at least one seeded `escalation_attachments` row, and confirm the attachment section renders with a working image preview or link.

- [ ] **Step 7: Run the full existing anansi_app ticket tests to confirm no regression**

```bash
cd anansi_app && python -m pytest tests/test_supabase_reader_tickets.py -q
```

Expected: PASS, all existing tests green.

- [ ] **Step 8: Commit**

```bash
git add anansi_app/services/supabase_reader.py anansi_app/nicegui_app/pages/tickets.py
git add -f anansi_app/tests/test_supabase_reader_tickets.py
git commit -m "feat(anansi): show escalation attachments on the ticket detail page"
```

---

### Task 11: End-to-end integration test

**Files:**
- Test: `chat_orchestrator/tests/services/test_media_attachment_integration.py`

- [ ] **Step 1: Write the integration test**

```python
"""End-to-end: escalating with media -> capture -> ticket creation -> attachment linked.

Exercises the full chain through EscalationService.escalate_to_support() and
TicketService.create_ticket() (internal backend) with a single fake Supabase
client shared across both, so the test catches any mismatch between how
capture writes escalation_attachments and how ticket creation reads them
back -- something the per-module unit tests in Tasks 2-9 can't catch since
they each mock their own collaborators.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.escalation_service import EscalationService
from orchestrator.services.ticketing.attachment_repository import AttachmentRepository
from orchestrator.services.ticketing.backend import TicketCreateRequest


class _FakeTable:
    def __init__(self, store: Dict[str, List[Dict[str, Any]]], name: str) -> None:
        self._store = store
        self._name = name
        self._filters: Dict[str, Any] = {}
        self._payload: Dict[str, Any] | None = None
        self._mode = "select"

    def insert(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._payload = payload
        self._mode = "insert"
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._payload = payload
        self._mode = "update"
        return self

    def select(self, *_a: Any, **_k: Any) -> "_FakeTable":
        return self

    def eq(self, field: str, value: Any) -> "_FakeTable":
        self._filters[field] = value
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_FakeTable":
        return self

    def order(self, *_a: Any, **_k: Any) -> "_FakeTable":
        return self

    def execute(self) -> MagicMock:
        rows = self._store.setdefault(self._name, [])
        if self._mode == "insert":
            row = {"id": f"{self._name}-{len(rows) + 1}", **self._payload}
            rows.append(row)
            return MagicMock(data=[row])
        if self._mode == "update":
            matched = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
            for row in matched:
                row.update(self._payload)
            return MagicMock(data=matched)
        matched = [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]
        return MagicMock(data=matched)


class _FakeStorageBucket:
    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}

    def upload(self, path: str, file: bytes, _options: Dict[str, Any]) -> None:
        self.objects[path] = file


class _FakeStorage:
    def __init__(self, bucket: _FakeStorageBucket) -> None:
        self._bucket = bucket

    def from_(self, _name: str) -> _FakeStorageBucket:
        return self._bucket


class _FakeSupabaseRawClient:
    def __init__(self) -> None:
        self._store: Dict[str, List[Dict[str, Any]]] = {}
        self.storage = _FakeStorage(_FakeStorageBucket())

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(self._store, name)


@pytest.mark.asyncio
async def test_media_from_escalation_reaches_the_internal_ticket() -> None:
    raw_client = _FakeSupabaseRawClient()

    service = EscalationService(
        escalation_chat_id="-100123",
        bot_token="test-token",
        supabase_url="https://example.supabase.co",
        supabase_key="test-key",
    )
    service._send_telegram_message = AsyncMock(
        return_value={"ok": True, "result": {"message_id": 555}}
    )
    service._get_raw_client = MagicMock(return_value=raw_client)
    service.get_escalation_info = AsyncMock(return_value=None)
    service._get_or_create_escalation_topic = AsyncMock(return_value=None)

    wrapper = MagicMock()
    wrapper.save_escalation_mapping = AsyncMock(return_value="mapping-1")
    wrapper._get_client = MagicMock(return_value=raw_client)
    service._get_supabase_client = MagicMock(return_value=wrapper)
    service._resolve_chat_session_uuid = AsyncMock(return_value="session-uuid-1")

    async def fake_download(file_id: str, bot_token: str, max_size_bytes: int):
        return "ZmFrZS1ieXRlcw==", "image/jpeg"  # base64("fake-bytes")

    with patch(
        "orchestrator.services.telegram_transport.download_telegram_photo",
        new=fake_download,
    ):
        escalate_result = await service.escalate_to_support(
            question_summary="Meter sparking, see photo",
            session_id="telegram_abc",
            customer_chat_id="123",
            media_file_ids=[{"type": "image", "file_id": "photo1"}],
        )
    assert escalate_result["success"] is True

    attachments = await AttachmentRepository(client=raw_client).list_by_escalation("mapping-1")
    assert len(attachments) == 1
    assert attachments[0].mime_type == "image/jpeg"
    assert raw_client.storage.from_("escalation-media").objects[attachments[0].storage_path] == (
        b"fake-bytes"
    )

    ticket_result = await service._tickets.create_ticket(
        TicketCreateRequest(
            summary="Meter sparking",
            escalation_mapping_id="mapping-1",
            source="escalation",
        ),
        backend_override="internal",
    )

    linked = await AttachmentRepository(client=raw_client).list_by_escalation("mapping-1")
    assert linked[0].ticket_id == ticket_result.ticket_id
```

- [ ] **Step 2: Run the test**

```bash
cd chat_orchestrator && python -m pytest tests/services/test_media_attachment_integration.py -v
```

Expected: PASS. If it fails, the failure is telling you about a real integration gap between two modules that were each individually tested in earlier tasks — debug the actual mismatch (e.g. a client-shape assumption that differs between `capture_escalation_media` and `AttachmentRepository`/`TicketService`) rather than loosening the test.

- [ ] **Step 3: Commit**

```bash
git add -f chat_orchestrator/tests/services/test_media_attachment_integration.py
git commit -m "test: add end-to-end integration test for escalation media -> ticket attachment"
```

---

### Task 12: Full verification pass

- [ ] **Step 1: Run the complete chat_orchestrator test suite**

```bash
cd chat_orchestrator && python -m pytest tests/ -q
```

Expected: PASS, no regressions anywhere in the service.

- [ ] **Step 2: Run the complete anansi_app test suite**

```bash
cd anansi_app && python -m pytest tests/ -q
```

Expected: PASS, no regressions.

- [ ] **Step 3: Run `pre-commit run --all-files`**

```bash
pre-commit run --all-files
```

Per this repo's `CLAUDE.md`: this catches two things `pytest`/`git status` alone won't — new test files that a plain `git add` silently dropped (this repo's `.gitignore` denies `tests/` by default; every test file added in Tasks 2-11 was already `git add -f`'d, so this should be clean, but confirm), and `ruff` lint issues on those files that only run once they're tracked.

- [ ] **Step 4: If `pre-commit` reports any untracked files under a `tests/` directory**

```bash
git status
```

Vet each one, then:

```bash
git add -f <path>
pre-commit run --all-files
```

- [ ] **Step 5: Confirm the final diff matches expectations**

```bash
git log --oneline fix/canonical-escalation-dual-write-phase1..HEAD
git diff fix/canonical-escalation-dual-write-phase1..HEAD --stat
```

Review the file list against this plan's Task 1-11 file lists — every modified/created file listed above should appear, nothing else should.
