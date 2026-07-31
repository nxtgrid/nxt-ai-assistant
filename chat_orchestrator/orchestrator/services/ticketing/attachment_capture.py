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

    try:
        client = get_client()
    except Exception:
        LOGGER.warning(
            "capture_escalation_media: get_client() raised -- skipping capture "
            "for escalation {}",
            escalation_id,
            exc_info=True,
        )
        return

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
        file_id = item.get("file_id")
        media_type = item.get("type")
        if not file_id or not media_type:
            LOGGER.warning(
                "capture_escalation_media: malformed media item {} -- skipping "
                "(escalation {})",
                item,
                escalation_id,
            )
            continue
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
