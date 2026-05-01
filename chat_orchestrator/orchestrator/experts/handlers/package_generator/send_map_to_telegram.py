"""Send map image to Telegram step handler.

This handler takes a generated map image (base64) from a previous step
and sends it to the user via Telegram.
"""

import base64
import os
from typing import Any, Dict

import aiohttp

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


async def _send_telegram_photo(
    bot_token: str,
    chat_id: int,
    photo_data: str,
    caption: str | None = None,
    topic_id: int | None = None,
) -> Dict[str, Any]:
    """Send photo to Telegram via Bot API.

    Args:
        bot_token: Telegram bot token
        chat_id: Target chat ID
        photo_data: Base64-encoded photo data
        caption: Optional caption for the photo
        topic_id: Optional topic/thread ID for forum chats

    Returns:
        Dict with success status and message_id if successful
    """
    webhook_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

    # Decode base64 to bytes
    photo_bytes = base64.b64decode(photo_data)

    # Build form data
    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("photo", photo_bytes, filename="map.png", content_type="image/png")

    if caption:
        # Telegram caption limit is 1024 characters
        if len(caption) > 1024:
            caption = caption[:1020] + "..."
        data.add_field("caption", caption)
        data.add_field("parse_mode", "Markdown")

    if topic_id:
        data.add_field("message_thread_id", str(topic_id))

    LOGGER.info(
        f"Sending map photo to Telegram: chat_id={chat_id}, "
        f"topic_id={topic_id}, size={len(photo_bytes)} bytes"
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, data=data) as response:
            if response.status != 200:
                error_text = await response.text()
                LOGGER.error(f"Failed to send photo: status={response.status}, error={error_text}")
                return {"success": False, "error": error_text}

            result = await response.json()
            if result.get("ok"):
                message_id = result.get("result", {}).get("message_id")
                LOGGER.info(f"Successfully sent map photo to Telegram, message_id={message_id}")
                return {"success": True, "message_id": message_id}
            else:
                error = result.get("description", "Unknown error")
                LOGGER.error(f"Telegram API error: {error}")
                return {"success": False, "error": error}


@register_step("send_lpp_map_to_telegram")
async def send_lpp_map_to_telegram(context: StepContext) -> StepResult:
    """Send the generated map image to Telegram.

    Retrieves the map image from previous step results and sends it
    to the user's Telegram chat.

    Args:
        context: Step execution context

    Returns:
        StepResult with send status
    """
    await context.send_progress_to_user("Sending map to chat...")

    # Get map image from previous step results
    map_result = context.get_previous_result("generate_distribution_map")
    if not map_result:
        LOGGER.warning("No map data found from generate_distribution_map step")
        return StepResult(
            data={"map_sent": False, "reason": "no_map_data"},
            progress_message="No map available to send",
        )

    map_image_b64 = map_result.get("map_image_b64")
    if not map_image_b64:
        # Fall back to downloading from Drive via stored file ID
        drive_id = context.get_state("map_image_drive_id")
        if drive_id:
            try:
                from shared.utils.drive_upload import download_drive_file

                image_bytes = await download_drive_file(drive_id)
                map_image_b64 = base64.b64encode(image_bytes).decode()
                LOGGER.info("Retrieved map image from Drive for Telegram send")
            except Exception as e:
                LOGGER.warning(f"Failed to download map image from Drive: {e}")

    if not map_image_b64:
        LOGGER.warning("Map image data not found in step results or Drive")
        return StepResult(
            data={"map_sent": False, "reason": "no_image_data"},
            progress_message="Map image not available",
        )

    # Get site info for caption
    site_name = map_result.get("site_name") or context.get_state("site_name") or "Site"
    statistics = map_result.get("statistics", {})

    # Build caption
    caption_parts = [f"*Site Map: {site_name}*"]
    if statistics:
        buildings = statistics.get("total_buildings", 0)
        served = statistics.get("served_buildings", 0)
        poles = statistics.get("poles", 0)
        cable_length = statistics.get("cable_length_m")
        stats_line = f"\nBuildings: {buildings} ({served} served)\nPoles: {poles}"
        if cable_length:
            stats_line += f"\nCable length: {cable_length:,.0f}m"
        caption_parts.append(stats_line)
    caption = "".join(caption_parts)

    # Get Telegram details from user context
    if not context.user_context:
        LOGGER.warning("No user context available - cannot send to Telegram")
        return StepResult(
            data={"map_sent": False, "reason": "no_user_context"},
            progress_message="Cannot send map - no chat context",
        )

    chat_id = context.user_context.chat_id
    topic_id = context.user_context.topic_id
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    if not chat_id:
        LOGGER.warning("No chat_id in user context")
        return StepResult(
            data={"map_sent": False, "reason": "no_chat_id"},
            progress_message="Cannot send map - no chat ID",
        )

    if not bot_token:
        LOGGER.error("TELEGRAM_BOT_TOKEN not configured")
        return StepResult(
            data={"map_sent": False, "reason": "no_bot_token"},
            progress_message="Bot token not configured",
        )

    # Send the photo
    try:
        result = await _send_telegram_photo(
            bot_token=bot_token,
            chat_id=chat_id,
            photo_data=map_image_b64,
            caption=caption,
            topic_id=topic_id,
        )

        if result.get("success"):
            # Bonus: send site options map if auto-detected plant location
            site_options_b64 = None
            site_options_drive_id = context.get_state("site_options_drive_id")
            if site_options_drive_id:
                try:
                    from shared.utils.drive_upload import download_drive_file

                    options_bytes = await download_drive_file(site_options_drive_id)
                    site_options_b64 = base64.b64encode(options_bytes).decode()
                except Exception as e:
                    LOGGER.warning(f"Failed to download site options map from Drive: {e}")
            if site_options_b64:
                # Build caption with GPS coordinates for each candidate
                candidates = context.get_state("site_candidates") or []
                options_caption = f"*{site_name} \u2014 Potential Power Plant Locations*\n"
                if candidates:
                    for c in candidates:
                        options_caption += (
                            f"\nSite {c['rank']}: "
                            f"{c['lat']:.6f}, {c['lon']:.6f} "
                            f"({c['area_sqm']:.0f}m\u00b2)"
                        )
                else:
                    options_caption += (
                        "Automatically detected candidate sites ranked by proximity to load center."
                    )
                try:
                    options_result = await _send_telegram_photo(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        photo_data=site_options_b64,
                        caption=options_caption,
                        topic_id=topic_id,
                    )
                    if options_result.get("success"):
                        LOGGER.info("Sent site options map to Telegram")
                    else:
                        LOGGER.warning(
                            f"Failed to send site options map: {options_result.get('error')}"
                        )
                except Exception:
                    LOGGER.debug("Failed to send site options map", exc_info=True)

            return StepResult(
                data={
                    "map_sent": True,
                    "message_id": result.get("message_id"),
                    "site_name": site_name,
                },
                progress_message=f"Sent map for {site_name}",
            )
        else:
            error = result.get("error", "Unknown error")
            LOGGER.error(f"Failed to send map to Telegram: {error}")
            return StepResult(
                data={"map_sent": False, "error": error},
                progress_message=f"Could not send map: {error}",
            )

    except Exception as e:
        LOGGER.exception(f"Error sending map to Telegram: {e}")
        return StepResult(
            data={"map_sent": False, "error": str(e)},
            progress_message="Error sending map",
        )
