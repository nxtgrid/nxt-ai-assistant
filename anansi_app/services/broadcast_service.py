"""
Broadcast messaging service for Anansi App.

Enables sending messages to multiple customer organization Telegram groups.
Supports templates, placeholder enrichment, and delivery tracking.
"""

import asyncio
import base64
import json
import logging
import os
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from supabase import Client, create_client

from services.scheduling_service import SchedulingService

logger = logging.getLogger(__name__)


@dataclass
class BroadcastResult:
    """Result of a broadcast send operation."""

    broadcast_id: str
    total: int
    successful: int
    failed: int
    errors: List[str]


@dataclass
class ImageData:
    """Image data for broadcast attachment."""

    filename: str
    content_type: str
    data: bytes

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "data_b64": base64.b64encode(self.data).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ImageData":
        return cls(
            filename=d["filename"],
            content_type=d["content_type"],
            data=base64.b64decode(d["data_b64"]),
        )


class BroadcastService:
    """Service for sending broadcast messages to customer groups."""

    # Rate limiting: 100ms between sends to avoid Telegram rate limits
    RATE_LIMIT_DELAY = 0.1

    # Large broadcast threshold - queue for background processing
    LARGE_BROADCAST_THRESHOLD = 10

    def __init__(self):
        """Initialize broadcast service with database connections."""
        # Supabase client for chat database (broadcasts, logs, templates) - with legacy fallback
        supabase_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY")
        self._supabase: Optional[Client] = None
        if supabase_url and supabase_key:
            self._supabase = create_client(supabase_url, supabase_key)

        # Telegram bot token
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

        # Cache for organization data (populated lazily)
        self._org_cache: Dict[str, Dict[str, Any]] = {}

    def is_configured(self) -> bool:
        """Check if service is properly configured."""
        return self._supabase is not None and self._bot_token is not None

    # =========================================================================
    # Group Selection
    # =========================================================================

    def get_available_groups(self) -> List[Dict[str, Any]]:
        """
        Get customer organizations + escalation group for broadcast selection.

        Returns:
            List of dicts with chat_id, name, org_id, type
        """
        groups: List[Dict[str, Any]] = []

        # Query auth DB for customer organizations
        orgs = self._query_organizations()
        for org in orgs:
            chat_id = org.get("developer_group_telegram_chat_id")
            if chat_id:
                groups.append(
                    {
                        "chat_id": chat_id,
                        "name": f"{org.get('formal_name') or org.get('name')} (Customer)",
                        "org_id": org.get("id"),
                        "formal_name": org.get("formal_name"),
                        "org_name": org.get("name"),
                        "type": "customer",
                    }
                )

        # Add escalation group for internal testing
        escalation_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID")
        if escalation_id:
            groups.append(
                {
                    "chat_id": escalation_id,
                    "name": "Escalation group (internal)",
                    "org_id": None,
                    "formal_name": "Internal Support",
                    "org_name": "Support",
                    "type": "escalation",
                }
            )

        return groups

    def _query_organizations(self) -> List[Dict[str, Any]]:
        """
        Query auth database for organizations with Telegram groups.

        Returns:
            List of organization records
        """
        try:
            import asyncpg

            async def fetch():
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                conn = await asyncpg.connect(
                    host=os.getenv("AUTH_DB_HOST"),
                    port=int(os.getenv("AUTH_DB_PORT", "6543")),
                    database=os.getenv("AUTH_DB_NAME", "postgres"),
                    user=os.getenv("AUTH_DB_USER"),
                    password=os.getenv("AUTH_DB_PASSWORD"),
                    ssl=ssl_context,
                    command_timeout=10,
                )
                try:
                    rows = await conn.fetch(
                        """
                        SELECT id, name, formal_name, developer_group_telegram_chat_id
                        FROM organizations
                        WHERE developer_group_telegram_chat_id IS NOT NULL
                        AND deleted_at IS NULL
                        AND id != 2  -- Exclude internal org
                        ORDER BY name
                        """
                    )
                    return [dict(row) for row in rows]
                finally:
                    await conn.close()

            return asyncio.run(fetch())
        except Exception as e:
            logger.error("Error querying organizations: %s", e)
            return []

    # =========================================================================
    # Template Management
    # =========================================================================

    def get_templates(self) -> List[Dict[str, Any]]:
        """
        Get all broadcast templates.

        Returns:
            List of template records
        """
        if not self._supabase:
            return []

        try:
            result = self._supabase.table("broadcast_templates").select("*").order("name").execute()
            return list(result.data) if result.data else []
        except Exception as e:
            logger.error("Error fetching templates: %s", e)
            return []

    def save_template(
        self,
        name: str,
        content: str,
        created_by: str = "",
        images: Optional[List[ImageData]] = None,
    ) -> Tuple[bool, str]:
        """
        Create a new template.

        Args:
            name: Template name (must be unique)
            content: Template content with placeholders
            created_by: Admin email
            images: Optional list of ImageData attachments

        Returns:
            Tuple of (success, message)
        """
        if not self._supabase:
            return False, "Database not configured"

        try:
            # Check for existing template with same name
            existing = (
                self._supabase.table("broadcast_templates").select("id").eq("name", name).execute()
            )
            if existing.data:
                return False, f"Template '{name}' already exists. Use a different name."

            # Create new template
            data = {"name": name, "content": content, "created_by": created_by}
            if images:
                data["image_attachments"] = json.dumps([img.to_dict() for img in images])
            self._supabase.table("broadcast_templates").insert(data).execute()
            return True, f"Template '{name}' saved successfully"

        except Exception as e:
            logger.error("Error saving template: %s", e)
            return False, f"Error: {str(e)}"

    def update_template(
        self,
        template_id: str,
        content: str,
        images: Optional[List[ImageData]] = None,
    ) -> Tuple[bool, str]:
        """
        Update template content and optionally images.

        Args:
            template_id: Template UUID
            content: New content
            images: None = don't touch images, [] = clear all images, list = replace images

        Returns:
            Tuple of (success, message)
        """
        if not self._supabase:
            return False, "Database not configured"

        try:
            update_data = {"content": content}
            if images is not None:
                update_data["image_attachments"] = json.dumps([img.to_dict() for img in images])
            self._supabase.table("broadcast_templates").update(update_data).eq(
                "id", template_id
            ).execute()
            return True, "Template updated"
        except Exception as e:
            logger.error("Error updating template: %s", e)
            return False, f"Error: {str(e)}"

    def delete_template(self, template_id: str) -> Tuple[bool, str]:
        """
        Delete a template.

        Args:
            template_id: Template UUID

        Returns:
            Tuple of (success, message)
        """
        if not self._supabase:
            return False, "Database not configured"

        try:
            self._supabase.table("broadcast_templates").delete().eq("id", template_id).execute()
            return True, "Template deleted"
        except Exception as e:
            logger.error("Error deleting template: %s", e)
            return False, f"Error: {str(e)}"

    # =========================================================================
    # Placeholder Enrichment
    # =========================================================================

    def enrich_message(self, message: str, chat_id: str) -> str:
        """
        Replace placeholders with actual values for a specific recipient.

        Args:
            message: Message template with placeholders
            chat_id: Target Telegram chat ID

        Returns:
            Enriched message with placeholders replaced
        """
        # Get org data for this chat_id
        org_data = self._get_org_data_for_chat(chat_id)

        # Define placeholder handlers (modular for future expansion)
        placeholders = {
            "<org_name>": org_data.get("name") or "Organization",
            "<org_hashtag>": f"#{(org_data.get('name') or 'customer').lower().replace(' ', '')}",
        }

        # Replace all placeholders
        enriched = message
        for placeholder, value in placeholders.items():
            enriched = enriched.replace(placeholder, value)

        return enriched

    def _get_org_data_for_chat(self, chat_id: str) -> Dict[str, Any]:
        """
        Get organization data for a chat ID (cached).

        Args:
            chat_id: Telegram chat ID

        Returns:
            Dict with org fields or fallback values
        """
        # Check cache first
        if chat_id in self._org_cache:
            return self._org_cache[chat_id]

        # Default fallback for escalation group or unknown chats
        fallback = {"name": "Customer", "formal_name": "Customer"}

        try:
            import asyncpg

            async def fetch():
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                conn = await asyncpg.connect(
                    host=os.getenv("AUTH_DB_HOST"),
                    port=int(os.getenv("AUTH_DB_PORT", "6543")),
                    database=os.getenv("AUTH_DB_NAME", "postgres"),
                    user=os.getenv("AUTH_DB_USER"),
                    password=os.getenv("AUTH_DB_PASSWORD"),
                    ssl=ssl_context,
                    command_timeout=10,
                )
                try:
                    # Try with chat_id as-is first
                    row = await conn.fetchrow(
                        """
                        SELECT id, name, formal_name
                        FROM organizations
                        WHERE developer_group_telegram_chat_id = $1
                        AND deleted_at IS NULL
                        """,
                        chat_id,
                    )
                    # If no result and chat_id is string, try as integer
                    if not row and isinstance(chat_id, str):
                        try:
                            chat_id_int = int(chat_id)
                            row = await conn.fetchrow(
                                """
                                SELECT id, name, formal_name
                                FROM organizations
                                WHERE developer_group_telegram_chat_id = $1
                                AND deleted_at IS NULL
                                """,
                                chat_id_int,
                            )
                            if row:
                                logger.debug("Org lookup succeeded with int cast for %s", chat_id)
                        except ValueError:
                            pass
                    if row:
                        logger.debug("Found org for %s: %s", chat_id, dict(row))
                    else:
                        logger.debug(
                            "No org found for chat_id=%s (type=%s)", chat_id, type(chat_id)
                        )
                    return dict(row) if row else None
                finally:
                    await conn.close()

            result = asyncio.run(fetch())
            org_data = result if result else fallback
            self._org_cache[chat_id] = org_data
            return org_data

        except Exception:
            logger.exception("Error fetching org data for %s", chat_id)
            self._org_cache[chat_id] = fallback
            return fallback

    def preload_org_cache(self, chat_ids: List[str]) -> None:
        """
        Preload organization data for multiple chat IDs.
        Call this before sending to avoid per-recipient DB queries.

        Args:
            chat_ids: List of Telegram chat IDs
        """
        # Filter out already cached IDs
        uncached = [cid for cid in chat_ids if cid not in self._org_cache]
        if not uncached:
            return

        try:
            import asyncpg

            async def fetch_all():
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                conn = await asyncpg.connect(
                    host=os.getenv("AUTH_DB_HOST"),
                    port=int(os.getenv("AUTH_DB_PORT", "6543")),
                    database=os.getenv("AUTH_DB_NAME", "postgres"),
                    user=os.getenv("AUTH_DB_USER"),
                    password=os.getenv("AUTH_DB_PASSWORD"),
                    ssl=ssl_context,
                    command_timeout=10,
                )
                try:
                    rows = await conn.fetch(
                        """
                        SELECT id, name, formal_name, developer_group_telegram_chat_id
                        FROM organizations
                        WHERE developer_group_telegram_chat_id = ANY($1::text[])
                        AND deleted_at IS NULL
                        """,
                        uncached,
                    )
                    return [dict(row) for row in rows]
                finally:
                    await conn.close()

            orgs = asyncio.run(fetch_all())

            # Populate cache
            for org in orgs:
                chat_id = org.get("developer_group_telegram_chat_id")
                if chat_id:
                    self._org_cache[chat_id] = org

            # Set fallback for IDs not found (e.g., escalation group)
            fallback = {"name": "Customer", "formal_name": "Customer"}
            for cid in uncached:
                if cid not in self._org_cache:
                    self._org_cache[cid] = fallback

        except Exception as e:
            logger.error("Error preloading org cache: %s", e)

    # =========================================================================
    # Message Validation
    # =========================================================================

    def validate_message_length(self, message: str, chat_ids: List[str]) -> List[str]:
        """
        Validate that enriched message length is within Telegram limits.

        Args:
            message: Message template
            chat_ids: Target chat IDs

        Returns:
            List of chat_ids where enriched message exceeds 4096 chars
        """
        oversized = []
        for chat_id in chat_ids:
            enriched = self.enrich_message(message, chat_id)
            if len(enriched) > 4096:
                oversized.append(chat_id)
        return oversized

    # =========================================================================
    # Broadcast Sending
    # =========================================================================

    def send_broadcast(
        self,
        message: str,
        group_ids: List[str],
        created_by: str,
        scheduled_for: Optional[datetime] = None,
        verification_passed: Optional[bool] = None,
        verification_feedback: Optional[str] = None,
        images: Optional[List["ImageData"]] = None,
    ) -> BroadcastResult:
        """
        Send broadcast to multiple groups.

        Args:
            message: Message template (with placeholders)
            group_ids: List of target Telegram chat IDs
            created_by: Admin email
            scheduled_for: Optional scheduled time (UTC)
            verification_passed: Whether LLM verification passed
            verification_feedback: Feedback from LLM verification
            images: Optional list of ImageData attachments (sent before text)

        Returns:
            BroadcastResult with delivery status
        """
        if not self._supabase or not self._bot_token:
            return BroadcastResult(
                broadcast_id="",
                total=len(group_ids),
                successful=0,
                failed=len(group_ids),
                errors=["Service not configured"],
            )

        # Preload org cache for all recipients
        self.preload_org_cache(group_ids)

        # Create broadcast record
        try:
            broadcast_data = {
                "message": message,
                "created_by": created_by,
                "target_group_ids": group_ids,
                "total_recipients": len(group_ids),
                "status": "scheduled" if scheduled_for else "sending",
                "verification_passed": verification_passed,
                "verification_feedback": verification_feedback,
            }
            if images:
                broadcast_data["metadata"] = {
                    "images": [img.to_dict() for img in images],
                    "image_count": len(images),
                }
            if scheduled_for:
                broadcast_data["scheduled_for"] = scheduled_for.isoformat()

            broadcast = self._supabase.table("broadcasts").insert(broadcast_data).execute()
            broadcast_id = broadcast.data[0]["id"]
        except Exception as e:
            logger.error("Error creating broadcast record: %s", e)
            return BroadcastResult(
                broadcast_id="",
                total=len(group_ids),
                successful=0,
                failed=len(group_ids),
                errors=[f"Database error: {str(e)}"],
            )

        # If scheduled, create entry in scheduled_messages for the scheduler daemon
        if scheduled_for:
            scheduling_service = SchedulingService()
            payload = {
                "broadcast_id": broadcast_id,
                "message": message,
                "group_ids": group_ids,
                "created_by": created_by,
            }
            if images:
                payload["images"] = [img.to_dict() for img in images]
            success, msg, schedule_id = scheduling_service.schedule_message(
                message_type="broadcast",
                payload=payload,
                scheduled_for=scheduled_for,
                created_by=created_by,
            )
            if not success:
                logger.warning("Failed to create scheduled_message entry: %s", msg)

            return BroadcastResult(
                broadcast_id=broadcast_id,
                total=len(group_ids),
                successful=0,
                failed=0,
                errors=[],
            )

        # Send immediately
        successful = 0
        errors: List[str] = []
        cached_file_ids = None

        for chat_id in group_ids:
            # Enrich message for this recipient
            enriched_message = self.enrich_message(message, chat_id)

            # Get org name for logging
            org_data = self._org_cache.get(chat_id, {})
            chat_name = org_data.get("formal_name") or org_data.get("name") or chat_id

            # Send images FIRST (if any)
            image_ok = True
            image_error = None
            if images:
                image_ok, new_fids, image_error = self._send_broadcast_images(
                    chat_id, images, cached_file_ids
                )
                if image_ok and new_fids:
                    cached_file_ids = new_fids

            # Send text message
            result = self._send_telegram_message(chat_id, enriched_message)

            # Overall success = both images and text
            text_ok = result.get("ok", False)
            overall_ok = text_ok and image_ok
            error_msg = (
                image_error
                if not image_ok
                else (result.get("description") if not text_ok else None)
            )

            # Log delivery result
            log_entry = {
                "broadcast_id": broadcast_id,
                "chat_id": chat_id,
                "chat_name": chat_name,
                "enriched_message": enriched_message,
                "success": overall_ok,
                "telegram_message_id": result.get("result", {}).get("message_id"),
                "error_message": error_msg,
            }
            try:
                self._supabase.table("broadcast_logs").insert(log_entry).execute()
            except Exception as e:
                logger.error("Error logging broadcast delivery: %s", e)

            if overall_ok:
                successful += 1
                # Save broadcast message to chat_messages for conversation history
                self._save_broadcast_to_chat_history(
                    chat_id=chat_id,
                    enriched_message=enriched_message,
                    created_by=created_by,
                )
            else:
                errors.append(f"{chat_name}: {error_msg or 'Unknown error'}")

            # Rate limiting delay
            time.sleep(self.RATE_LIMIT_DELAY)

        # Update broadcast status
        try:
            self._supabase.table("broadcasts").update(
                {
                    "status": "completed",
                    "successful_sends": successful,
                    "failed_sends": len(group_ids) - successful,
                }
            ).eq("id", broadcast_id).execute()
        except Exception as e:
            logger.error("Error updating broadcast status: %s", e)

        return BroadcastResult(
            broadcast_id=broadcast_id,
            total=len(group_ids),
            successful=successful,
            failed=len(group_ids) - successful,
            errors=errors,
        )

    def _send_telegram_message(
        self, chat_id: str, text: str, parse_mode: Optional[str] = "Markdown"
    ) -> Dict[str, Any]:
        """
        Send message via Telegram Bot API.

        Args:
            chat_id: Target chat ID
            text: Message text
            parse_mode: Message formatting (Markdown, HTML, or None)

        Returns:
            Telegram API response
        """
        logger.debug("Sending broadcast to chat_id: %s", chat_id)

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(url, json=payload)
                result = response.json()
                logger.debug("Telegram response: %s", result)

                # If markdown parse error, retry without parse_mode
                if not result.get("ok") and parse_mode:
                    error_desc = result.get("description", "").lower()
                    if "parse" in error_desc or "can't parse" in error_desc:
                        logger.warning(
                            "Markdown parse error for %s, retrying without parse_mode", chat_id
                        )
                        return self._send_telegram_message(chat_id, text, parse_mode=None)

                return dict(result)

        except Exception as e:
            logger.error("Error sending to %s: %s", chat_id, e)
            return {"ok": False, "description": str(e)}

    def _send_telegram_photo(
        self, chat_id: str, image: "ImageData", file_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send a single photo via Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendPhoto"
        try:
            with httpx.Client(timeout=30) as client:
                if file_id:
                    response = client.post(url, data={"chat_id": chat_id, "photo": file_id})
                else:
                    response = client.post(
                        url,
                        data={"chat_id": chat_id},
                        files={"photo": (image.filename, image.data, image.content_type)},
                    )
                return dict(response.json())
        except Exception as e:
            logger.error("Error sending photo to %s: %s", chat_id, e)
            return {"ok": False, "description": str(e)}

    def _send_telegram_media_group(
        self,
        chat_id: str,
        images: List["ImageData"],
        file_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Send multiple photos as an album via Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMediaGroup"
        try:
            with httpx.Client(timeout=60) as client:
                if file_ids:
                    media = [{"type": "photo", "media": fid} for fid in file_ids]
                    response = client.post(
                        url, data={"chat_id": chat_id, "media": json.dumps(media)}
                    )
                else:
                    media = []
                    files = {}
                    for i, img in enumerate(images):
                        attach_key = f"photo_{i}"
                        media.append({"type": "photo", "media": f"attach://{attach_key}"})
                        files[attach_key] = (img.filename, img.data, img.content_type)
                    response = client.post(
                        url, data={"chat_id": chat_id, "media": json.dumps(media)}, files=files
                    )
                return dict(response.json())
        except Exception as e:
            logger.error("Error sending media group to %s: %s", chat_id, e)
            return {"ok": False, "description": str(e)}

    def _send_broadcast_images(
        self,
        chat_id: str,
        images: List["ImageData"],
        cached_file_ids: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[List[str]], Optional[str]]:
        """
        Send broadcast images to a chat.

        Returns:
            Tuple of (success, file_ids_for_caching, error_string)
        """
        try:
            if len(images) == 1:
                fid = cached_file_ids[0] if cached_file_ids else None
                result = self._send_telegram_photo(chat_id, images[0], file_id=fid)
                if result.get("ok"):
                    photos = result.get("result", {}).get("photo", [])
                    new_fid = photos[-1]["file_id"] if photos else None
                    return True, [new_fid] if new_fid else None, None
                return False, None, result.get("description", "Photo send failed")
            else:
                result = self._send_telegram_media_group(chat_id, images, file_ids=cached_file_ids)
                if result.get("ok"):
                    new_fids = []
                    for msg in result.get("result", []):
                        photos = msg.get("photo", [])
                        if photos:
                            new_fids.append(photos[-1]["file_id"])
                    return True, new_fids if new_fids else None, None
                return False, None, result.get("description", "Media group send failed")
        except Exception as e:
            logger.error("Error in _send_broadcast_images for %s: %s", chat_id, e)
            return False, None, str(e)

    def _save_broadcast_to_chat_history(
        self,
        chat_id: str,
        enriched_message: str,
        created_by: str,
    ) -> None:
        """
        Save broadcast message to chat_messages table for conversation history.

        This ensures broadcast messages appear in the admin UI chat history view
        alongside regular bot responses.

        Args:
            chat_id: Telegram chat ID of the recipient
            enriched_message: The actual message sent (after placeholder enrichment)
            created_by: Admin email who sent the broadcast
        """
        if not self._supabase:
            return

        try:
            # Find existing session for this chat_id
            # Try telegram_chat_id column first (works for both hashed and legacy sessions)
            session_response = (
                self._supabase.table("chat_sessions")
                .select("id")
                .eq("telegram_chat_id", chat_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )

            if not session_response.data:
                # No existing session - skip saving (broadcasts should go to existing chats)
                logger.debug("No session found for chat_id %s, skipping chat history save", chat_id)
                return

            session_uuid = session_response.data[0]["id"]

            # Get current max message_index for this session
            max_index_response = (
                self._supabase.table("chat_messages")
                .select("message_index")
                .eq("session_id", session_uuid)
                .order("message_index", desc=True)
                .limit(1)
                .execute()
            )

            next_index = 0
            if max_index_response.data:
                next_index = max_index_response.data[0]["message_index"] + 1

            # Save broadcast as a model message
            message_data = {
                "session_id": session_uuid,
                "role": "model",
                "content": enriched_message,
                "message_index": next_index,
                "from_chat_id": chat_id,
                "metadata": {
                    "source": "broadcast",
                    "created_by": created_by,
                },
            }

            self._supabase.table("chat_messages").insert(message_data).execute()
            logger.info("Saved broadcast to chat history for session %s", session_uuid)

        except Exception as e:
            # Log but don't fail the broadcast send
            logger.error("Error saving broadcast to chat history for %s: %s", chat_id, e)

    # =========================================================================
    # Broadcast History
    # =========================================================================

    def get_broadcast_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get recent broadcast history.

        Args:
            limit: Maximum records to return

        Returns:
            List of broadcast records with logs
        """
        if not self._supabase:
            return []

        try:
            result = (
                self._supabase.table("broadcasts")
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return list(result.data) if result.data else []
        except Exception as e:
            logger.error("Error fetching broadcast history: %s", e)
            return []

    def get_broadcast_logs(self, broadcast_id: str) -> List[Dict[str, Any]]:
        """
        Get delivery logs for a specific broadcast.

        Args:
            broadcast_id: Broadcast UUID

        Returns:
            List of log records
        """
        if not self._supabase:
            return []

        try:
            result = (
                self._supabase.table("broadcast_logs")
                .select("*")
                .eq("broadcast_id", broadcast_id)
                .order("sent_at", desc=True)
                .execute()
            )
            return list(result.data) if result.data else []
        except Exception as e:
            logger.error("Error fetching broadcast logs: %s", e)
            return []

    def retry_failed_sends(self, broadcast_id: str, created_by: str) -> BroadcastResult:
        """
        Retry failed sends for a broadcast.

        Args:
            broadcast_id: Original broadcast UUID
            created_by: Admin email

        Returns:
            BroadcastResult for retry attempt
        """
        if not self._supabase:
            return BroadcastResult(
                broadcast_id="", total=0, successful=0, failed=0, errors=["Not configured"]
            )

        try:
            # Get original broadcast (including metadata for images)
            broadcast = (
                self._supabase.table("broadcasts")
                .select("message, metadata")
                .eq("id", broadcast_id)
                .single()
                .execute()
            )
            message = broadcast.data["message"]
            images = None
            metadata = broadcast.data.get("metadata") or {}
            if metadata.get("images"):
                images = [ImageData.from_dict(img) for img in metadata["images"]]

            # Get failed logs
            failed_logs = (
                self._supabase.table("broadcast_logs")
                .select("chat_id")
                .eq("broadcast_id", broadcast_id)
                .eq("success", False)
                .execute()
            )

            if not failed_logs.data:
                return BroadcastResult(
                    broadcast_id=broadcast_id,
                    total=0,
                    successful=0,
                    failed=0,
                    errors=["No failed sends to retry"],
                )

            # Retry with failed chat IDs
            failed_chat_ids = [log["chat_id"] for log in failed_logs.data]
            return self.send_broadcast(message, failed_chat_ids, created_by, images=images)

        except Exception as e:
            logger.error("Error retrying failed sends: %s", e)
            return BroadcastResult(
                broadcast_id="",
                total=0,
                successful=0,
                failed=0,
                errors=[f"Error: {str(e)}"],
            )

    # =========================================================================
    # Scheduled Broadcasts
    # =========================================================================

    def get_scheduled_broadcasts(self) -> List[Dict[str, Any]]:
        """
        Get pending scheduled broadcasts (future only).

        Returns:
            List of scheduled broadcast records that are still in the future
        """
        if not self._supabase:
            return []

        try:
            now = datetime.now(timezone.utc).isoformat()
            result = (
                self._supabase.table("broadcasts")
                .select("*")
                .eq("status", "scheduled")
                .not_.is_("scheduled_for", "null")
                .gt("scheduled_for", now)  # Only future scheduled broadcasts
                .order("scheduled_for", desc=False)
                .execute()
            )
            return list(result.data) if result.data else []
        except Exception as e:
            logger.error("Error fetching scheduled broadcasts: %s", e)
            return []

    def cancel_scheduled_broadcast(self, broadcast_id: str) -> Tuple[bool, str]:
        """
        Cancel a scheduled broadcast.

        Args:
            broadcast_id: Broadcast UUID

        Returns:
            Tuple of (success, message)
        """
        if not self._supabase:
            return False, "Not configured"

        try:
            self._supabase.table("broadcasts").update({"status": "cancelled"}).eq(
                "id", broadcast_id
            ).eq("status", "scheduled").execute()  # Fixed: was "pending", should be "scheduled"
            return True, "Broadcast cancelled"
        except Exception as e:
            logger.error("Error cancelling broadcast: %s", e)
            return False, f"Error: {str(e)}"

    def execute_scheduled_broadcast(self, broadcast_id: str) -> BroadcastResult:
        """
        Execute an existing scheduled broadcast by updating its record.

        This is called by the scheduler to send a previously scheduled broadcast.
        Instead of creating a new record, it updates the existing one with delivery stats.

        Args:
            broadcast_id: The existing broadcast UUID to execute

        Returns:
            BroadcastResult with delivery status
        """
        if not self._supabase or not self._bot_token:
            return BroadcastResult(
                broadcast_id=broadcast_id,
                total=0,
                successful=0,
                failed=0,
                errors=["Service not configured"],
            )

        # Get the existing broadcast record
        try:
            broadcast_response = (
                self._supabase.table("broadcasts")
                .select("*")
                .eq("id", broadcast_id)
                .single()
                .execute()
            )
            broadcast = broadcast_response.data
        except Exception as e:
            logger.error("Error fetching broadcast %s: %s", broadcast_id, e)
            return BroadcastResult(
                broadcast_id=broadcast_id,
                total=0,
                successful=0,
                failed=0,
                errors=[f"Broadcast not found: {str(e)}"],
            )

        message = broadcast.get("message", "")
        group_ids = broadcast.get("target_group_ids", [])
        created_by = broadcast.get("created_by", "scheduler")

        # Load images from metadata (stored at schedule time)
        images = None
        metadata = broadcast.get("metadata") or {}
        if metadata.get("images"):
            images = [ImageData.from_dict(img) for img in metadata["images"]]

        if not message or not group_ids:
            return BroadcastResult(
                broadcast_id=broadcast_id,
                total=0,
                successful=0,
                failed=0,
                errors=["Missing message or group_ids"],
            )

        # Update status to sending
        try:
            self._supabase.table("broadcasts").update({"status": "sending"}).eq(
                "id", broadcast_id
            ).execute()
        except Exception as e:
            logger.error("Error updating broadcast status to sending: %s", e)

        # Preload org cache for all recipients
        self.preload_org_cache(group_ids)

        # Send to all groups
        successful = 0
        errors: List[str] = []
        cached_file_ids = None

        for chat_id in group_ids:
            enriched_message = self.enrich_message(message, chat_id)
            org_data = self._org_cache.get(chat_id, {})
            chat_name = org_data.get("formal_name") or org_data.get("name") or chat_id

            # Send images FIRST (if any)
            image_ok = True
            image_error = None
            if images:
                image_ok, new_fids, image_error = self._send_broadcast_images(
                    chat_id, images, cached_file_ids
                )
                if image_ok and new_fids:
                    cached_file_ids = new_fids

            # Send text message
            result = self._send_telegram_message(chat_id, enriched_message)

            # Overall success = both images and text
            text_ok = result.get("ok", False)
            overall_ok = text_ok and image_ok
            error_msg = (
                image_error
                if not image_ok
                else (result.get("description") if not text_ok else None)
            )

            # Log delivery result
            log_entry = {
                "broadcast_id": broadcast_id,
                "chat_id": chat_id,
                "chat_name": chat_name,
                "enriched_message": enriched_message,
                "success": overall_ok,
                "telegram_message_id": result.get("result", {}).get("message_id"),
                "error_message": error_msg,
            }
            try:
                self._supabase.table("broadcast_logs").insert(log_entry).execute()
            except Exception as e:
                logger.error("Error logging broadcast delivery: %s", e)

            if overall_ok:
                successful += 1
                # Save broadcast message to chat_messages for conversation history
                self._save_broadcast_to_chat_history(
                    chat_id=chat_id,
                    enriched_message=enriched_message,
                    created_by=created_by,
                )
            else:
                errors.append(f"{chat_name}: {error_msg or 'Unknown error'}")

            time.sleep(self.RATE_LIMIT_DELAY)

        # Update broadcast status to completed
        try:
            self._supabase.table("broadcasts").update(
                {
                    "status": "completed",
                    "successful_sends": successful,
                    "failed_sends": len(group_ids) - successful,
                }
            ).eq("id", broadcast_id).execute()
        except Exception as e:
            logger.error("Error updating broadcast status: %s", e)

        return BroadcastResult(
            broadcast_id=broadcast_id,
            total=len(group_ids),
            successful=successful,
            failed=len(group_ids) - successful,
            errors=errors,
        )


__all__ = ["BroadcastService", "BroadcastResult", "ImageData"]
