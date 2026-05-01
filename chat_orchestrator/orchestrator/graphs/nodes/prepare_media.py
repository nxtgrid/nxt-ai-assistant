"""Prepare media node for LangGraph.

This node downloads Telegram media attachments (photos, videos, audio)
and adds them to the state for processing. Supports both single files
and albums (multiple photo_file_ids from media group aggregation).
"""

import asyncio
import os
from typing import Any, Dict, List

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState
from orchestrator.models.schemas import MediaAttachment


async def prepare_media(state: ConversationState) -> Dict[str, Any]:
    """Download and prepare Telegram media attachments.

    This node:
    1. Checks for photo/video/voice/audio file_ids in metadata
    2. Downloads media via Telegram Bot API (concurrently for albums)
    3. Appends MediaAttachment objects to state

    Args:
        state: Current conversation state

    Returns:
        State updates with media attachments list
    """
    metadata = state.get("metadata", {})
    media: List[MediaAttachment] = list(state.get("media", []))

    # Build list of photo file_ids — album (photo_file_ids) or single (photo_file_id)
    photo_file_ids = metadata.get("photo_file_ids") or []
    if not photo_file_ids and metadata.get("photo_file_id"):
        photo_file_ids = [metadata["photo_file_id"]]

    # Other media types (single file only)
    other_file_id = (
        metadata.get("video_file_id")
        or metadata.get("voice_file_id")
        or metadata.get("audio_file_id")
    )

    if not photo_file_ids and not other_file_id:
        LOGGER.debug("No media to download")
        return {"media": media}

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        LOGGER.warning("TELEGRAM_BOT_TOKEN not set, cannot download media")
        return {"media": media}

    # Import the download function from handler (kept as utility)
    from handler import _download_telegram_photo

    # Download photos (concurrently for albums)
    if photo_file_ids:
        results = await asyncio.gather(
            *[_download_telegram_photo(fid, bot_token) for fid in photo_file_ids],
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                LOGGER.warning(f"Failed to download photo {i + 1}/{len(photo_file_ids)}: {result}")
                continue
            base64_data, mime_type = result
            if base64_data:
                media.append(MediaAttachment(type="image", data=base64_data, mime_type=mime_type))
                LOGGER.info(f"Added photo {i + 1}/{len(photo_file_ids)} ({mime_type})")
            else:
                LOGGER.warning(f"Empty data for photo {i + 1}/{len(photo_file_ids)}")

    # Download other media types (video/voice/audio — single file)
    if other_file_id:
        base64_data, mime_type = await _download_telegram_photo(other_file_id, bot_token)
        if base64_data:
            if metadata.get("voice_file_id") or metadata.get("audio_file_id"):
                media_type = "audio"
            elif metadata.get("video_file_id"):
                media_type = "video"
            else:
                media_type = "image"
            media.append(MediaAttachment(type=media_type, data=base64_data, mime_type=mime_type))
            LOGGER.info(f"Added Telegram media to request: {media_type} ({mime_type})")
        else:
            LOGGER.warning(f"Failed to download media file_id={other_file_id}")

    return {"media": media}
