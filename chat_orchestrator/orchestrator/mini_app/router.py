"""FastAPI router for Telegram Mini App form endpoints.

Provides endpoints:
- GET /api/mini-app/form-data: Returns form schema + pre-populated values for a packet
- POST /api/mini-app/submit: Accepts form values, writes to pending_param_overrides, resumes workflow
- GET /api/mini-app/agent-state: Returns read-only state view for a persistent agent instance
- GET /api/mini-app/sign-data: Proxy PDF bytes for signing (signer-only)
- POST /api/mini-app/sign/submit: Accept signature PNG, stamp PDF in background
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from orchestrator.mini_app.audit import (
    _generate_audit_page,
    _merge_audit_page_sync,
    _stamp_pdf_sync,
)
from orchestrator.mini_app.auth import get_validated_user, validate_init_data
from orchestrator.mini_app.schemas import (
    FORM_SCHEMAS,
    FORM_SUBMITTED_SENTINEL,
    StateDataResponse,
    StateEntry,
    WorkflowStepProgress,
    _sign_identifier,
    validate_form_values,
    verify_signature,
)
from orchestrator.services.work_packet_service import WorkPacketService
from shared.utils.error_messages import ErrorCategory, get_user_message
from shared.utils.logging import get_logger
from shared.utils.telegram_markdown import escape_markdown

LOGGER = get_logger(__name__)

router = APIRouter(prefix="/api/mini-app", tags=["mini-app"])


class FormSubmission(BaseModel):
    """POST body for form submission."""

    packet_id: str
    form_type: str
    values: Dict[str, Any]


def _get_packet_service() -> WorkPacketService:
    """FastAPI dependency for WorkPacketService."""
    return WorkPacketService()


async def get_optional_user(request: Request) -> Dict[str, Any] | None:
    """Try initData auth, return None instead of raising on failure.

    Used by the state-data endpoint which also accepts sig-based auth.
    """
    try:
        result: Dict[str, Any] = await get_validated_user(request)
        return result
    except HTTPException:
        return None


# Keys filtered from user-visible state view. When adding new internal
# state keys in workflow_executor.py or step handlers, add them here
# to prevent leaking implementation details to users.
_INTERNAL_KEYS = {
    "awaiting_user_input",
    "awaiting_prompt",
    "awaiting_param_confirmation",
    "pending_param_overrides",
    "auto_continue_enabled",
    "confirmed_editable_snapshot",
    "param_confirmation_context",
    "accumulated_results",
    "execution_summary",
    "parsed_inputs",
    "buttons_message_id",
    "buttons_chat_id",
    "_progress_msg_id",
    "_heartbeat",
    "site_folder_id",
}


def _format_label(key: str) -> str:
    """Convert snake_case key to Title Case label."""
    return key.replace("_", " ").title()


def _drive_id_to_proxy_url(file_id: str, packet_id: str = "") -> str:
    """Convert a Google Drive file ID to a signed proxy URL for the Mini App.

    The sig is bound to both packet_id and file_id to prevent using a URL
    from one packet context to access files from another packet.
    """
    sig = _sign_identifier(f"{packet_id}:{file_id}")
    return f"/api/mini-app/drive-image?packet_id={packet_id}&file_id={file_id}&sig={sig}"


# Substrings in a _drive_id key that indicate the file is NOT a viewable image.
# Keys matching any of these are skipped in the Mini App state view rather than
# being proxied as <img> tags (get_media() fails for non-binary Drive formats).
_NON_IMAGE_DRIVE_ID_PATTERNS = frozenset(
    {"drawio", "pdf", "boundary", "document", "audit_trail", "qgis", "xml", "geojson"}
)


def _is_image_drive_id(key: str) -> bool:
    """Return True if a _drive_id key points to a viewable image (PNG/JPEG/SVG)."""
    key_lower = key.lower()
    return not any(pattern in key_lower for pattern in _NON_IMAGE_DRIVE_ID_PATTERNS)


def _extract_visible_state(
    state: Dict[str, Any], inputs: Dict[str, Any], packet_id: str = ""
) -> list[StateEntry]:
    """Extract user-visible state entries. Single pass, simple filtering.

    Keys ending in ``_drive_id`` are converted to image proxy URLs so the
    Mini App frontend renders them as ``<img>`` tags — but only when the key
    indicates a viewable image format. Non-image Drive IDs (drawio, PDF,
    GeoJSON, etc.) are skipped to avoid 502s from get_media() on native formats.
    """
    entries: list[StateEntry] = []
    seen_keys: set[str] = set()

    for key, val in inputs.items():
        if val not in (None, "", []):
            entries.append(StateEntry(key=key, label=_format_label(key), value=val))
            seen_keys.add(key)

    for key, val in state.items():
        if key in _INTERNAL_KEYS or key in seen_keys or val in (None, "", []):
            continue
        if key.endswith("_drive_id") and isinstance(val, str) and len(val) > 5:
            if not _is_image_drive_id(key):
                # Non-image Drive file (PDF, drawio, GeoJSON, etc.) — skip entirely.
                # get_media() only works for binary files; native Google formats return 403.
                continue
            # Convert Drive file ID to a proxy image URL
            image_key = key.replace("_drive_id", "_image_url")
            label = _format_label(key.replace("_drive_id", ""))
            entries.append(
                StateEntry(
                    key=image_key,
                    label=label,
                    value=_drive_id_to_proxy_url(val, packet_id),
                )
            )
            continue
        if key.startswith("editable_"):
            label = _format_label(key.replace("editable_", "", 1))
        else:
            label = _format_label(key)
        entries.append(StateEntry(key=key, label=label, value=val))

    return entries


def _check_staleness(
    packet: Dict[str, Any],
    in_progress_timeout: int = 15,
    awaiting_input_timeout: int = 180,
) -> tuple[bool, int | None]:
    """Check if a packet appears stuck based on updated_at.

    Returns (is_stale, minutes_since_update).
    """
    from datetime import datetime, timezone

    status = packet.get("packet_status")
    if status not in ("in_progress", "awaiting_input"):
        return False, None

    updated_at_str = packet.get("updated_at")
    if not updated_at_str:
        return False, None

    try:
        updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
        age_minutes = int((datetime.now(timezone.utc) - updated_at).total_seconds() / 60)
    except (ValueError, TypeError):
        return False, None

    threshold = in_progress_timeout if status == "in_progress" else awaiting_input_timeout
    if age_minutes > threshold:
        return True, age_minutes
    return False, None


def _extract_workflow_progress(state: Dict[str, Any]) -> list[WorkflowStepProgress]:
    """Extract workflow step progress from persisted execution_summary."""
    execution_summary = state.get("execution_summary", {})
    step_records = execution_summary.get("steps", [])
    return [
        WorkflowStepProgress(
            name=r["step_name"],
            description=r.get("description", ""),
            status=r["status"],
        )
        for r in step_records
    ]


@router.get("/form-data")
async def get_form_data(
    packet_id: str = Query(..., description="Work packet ID"),
    form_type: str = Query(..., description="Form type key"),
    user: Dict[str, Any] = Depends(get_validated_user),
    service: WorkPacketService = Depends(_get_packet_service),
) -> Dict[str, Any]:
    """Return form schema and pre-populated values for a work packet.

    Validates that the packet exists, belongs to the user's organization,
    and is currently awaiting input.
    """
    schema = FORM_SCHEMAS.get(form_type)
    if not schema:
        raise HTTPException(status_code=400, detail=f"Unknown form type: {form_type}")

    packet = await service.get_packet(packet_id)
    if not packet:
        raise HTTPException(status_code=404, detail="Packet not found")

    # Verify organization match
    if packet.get("organization_id") != user.get("organization_id"):
        raise HTTPException(status_code=403, detail="Organization mismatch")

    # Verify packet is awaiting input
    state = packet.get("packet_state") or {}
    if not state.get("awaiting_user_input"):
        raise HTTPException(status_code=410, detail="Packet is not awaiting input")

    # Pre-populate values from packet state (editable_ keys) and inputs
    values: Dict[str, Any] = {}
    inputs = packet.get("packet_inputs") or {}
    overrides = state.get("pending_param_overrides") or {}

    for field in schema:
        key = field["key"]
        # Priority: overrides > state (editable_) > state (bare) > inputs
        # Skip empty strings — they indicate "not yet computed"
        bare_key = key.replace("editable_", "", 1)
        if key in overrides and overrides[key] not in ("", None):
            values[key] = overrides[key]
        elif key in state and state[key] not in ("", None):
            values[key] = state[key]
        elif bare_key in state and state[bare_key] not in ("", None):
            values[key] = state[bare_key]
        elif bare_key in inputs and inputs[bare_key] not in ("", None):
            values[key] = inputs[bare_key]

    return {
        "form_type": form_type,
        "packet_id": packet_id,
        "packet_title": packet.get("packet_title", ""),
        "fields": schema,
        "values": values,
    }


@router.post("/submit")
async def submit_form(
    body: FormSubmission,
    user: Dict[str, Any] = Depends(get_validated_user),
    service: WorkPacketService = Depends(_get_packet_service),
) -> Dict[str, Any]:
    """Accept form values, write to pending_param_overrides, resume workflow.

    Flow:
    1. Validate initData (via dependency)
    2. Validate form values against schema
    3. Load packet, verify awaiting_input and org match
    4. Merge validated values into packet_state.pending_param_overrides
    5. Call resume_from_input to unblock the workflow
    """
    # Validate form values against schema (067: server-side validation)
    try:
        validated_values = validate_form_values(body.form_type, body.values)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Single packet fetch — reuse for all checks (069: reduce round-trips)
    packet = await service.get_packet(body.packet_id)
    if not packet:
        raise HTTPException(status_code=404, detail="Packet not found")

    if packet.get("organization_id") != user.get("organization_id"):
        raise HTTPException(status_code=403, detail="Organization mismatch")

    state = packet.get("packet_state") or {}
    if not state.get("awaiting_user_input"):
        raise HTTPException(status_code=410, detail="Packet is not awaiting input")

    # Reject submission if the workflow appears dead
    is_stale, stale_min = _check_staleness(packet)
    if is_stale:
        raise HTTPException(
            status_code=410,
            detail=f"Workflow appears stuck (no activity for {stale_min} min). "
            "Please send a new command in the chat to retry.",
        )

    # Merge validated values into pending_param_overrides
    overrides = state.get("pending_param_overrides") or {}
    overrides.update(validated_values)

    # Update overrides and resume in sequence (2 DB calls total)
    try:
        await service.update_state(
            body.packet_id,
            {"pending_param_overrides": overrides},
        )
    except Exception:
        LOGGER.exception(
            "Failed to save form overrides for packet=%s (write contention?)", body.packet_id
        )
        raise HTTPException(
            status_code=503,
            detail=get_user_message(ErrorCategory.TRANSIENT, "service_unavailable"),
        )

    session_id = packet.get("requested_in_session")
    await service.resume_from_input(
        body.packet_id,
        FORM_SUBMITTED_SENTINEL,
        session_id=session_id,
    )

    LOGGER.info(
        "Mini App form submitted for packet=%s by telegram_id=%s",
        body.packet_id,
        user.get("user", {}).get("id"),
    )

    return {"status": "ok", "packet_id": body.packet_id}


@router.get("/state-data", response_model=StateDataResponse)
async def get_state_data(
    packet_id: str = Query(..., description="Work packet ID"),
    sig: str = Query("", description="HMAC signature (fallback auth for desktop/web)"),
    user: Dict[str, Any] | None = Depends(get_optional_user),
    service: WorkPacketService = Depends(_get_packet_service),
) -> StateDataResponse:
    """Return read-only state view for a work packet.

    Unlike form-data, does NOT require awaiting_user_input.
    Works for in_progress, awaiting_input, completed, and failed packets.

    Auth: Accepts either Telegram initData (tma header) or a signed URL
    token (sig parameter). The sig fallback ensures the state view works
    on Telegram Desktop/Web where initData may be unavailable.
    """
    if not user:
        if not sig or not verify_signature(packet_id, sig):
            raise HTTPException(status_code=401, detail="Authentication failed")

    packet = await service.get_packet(packet_id)
    if not packet:
        raise HTTPException(status_code=404, detail="Packet not found")

    # Only check org match if we have user auth (sig-based skips this
    # since the sig is packet-bound and only sent to the owner)
    if user and packet.get("organization_id") != user.get("organization_id"):
        raise HTTPException(status_code=403, detail="Organization mismatch")

    state = packet.get("packet_state") or {}
    inputs = packet.get("packet_inputs") or {}

    # Detect stale workflows (process crashed mid-execution)
    is_stale, stale_minutes = _check_staleness(packet)

    return StateDataResponse(
        packet_id=packet_id,
        packet_title=packet.get("packet_title", ""),
        packet_type=packet.get("packet_type", ""),
        packet_status=packet.get("packet_status", ""),
        state=_extract_visible_state(state, inputs, packet_id),
        workflow_steps=_extract_workflow_progress(state),
        is_stale=is_stale,
        stale_minutes=stale_minutes,
    )


# ── Drive Image Proxy ───────────────────────────────────────────────────


@router.get("/drive-image")
async def proxy_drive_image(
    file_id: str = Query(..., description="Google Drive file ID"),
    packet_id: str = Query("", description="Work packet ID (binds sig scope)"),
    sig: str = Query(..., description="HMAC signature bound to packet_id:file_id"),
) -> Response:
    """Proxy a Google Drive image file for the Mini App frontend.

    The sig is bound to both packet_id and file_id to prevent a URL from one
    packet being reused to access files belonging to another packet.
    """
    if not verify_signature(f"{packet_id}:{file_id}", sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

    from shared.utils.drive_upload import download_drive_file

    try:
        image_bytes = await download_drive_file(file_id)
    except Exception:
        LOGGER.warning("Failed to proxy Drive image file_id=%s", file_id, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not fetch image from Drive")

    # Sniff content type from first bytes; default to PNG
    content_type = "image/png"
    if image_bytes[:3] == b"\xff\xd8\xff":
        content_type = "image/jpeg"
    elif image_bytes[:4] == b"\x89PNG":
        content_type = "image/png"
    elif image_bytes[:4] == b"<svg" or image_bytes[:5] == b"<?xml":
        content_type = "image/svg+xml"

    return Response(
        content=image_bytes,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ── PDF Signing ─────────────────────────────────────────────────────────

# Auth expiry for signing endpoints: 30 minutes.
# Justification: PDF load + scroll + place + draw + submit can exceed 5 min.
_SIGN_AUTH_MAX_AGE = 1800

# Signature PNG size limit: 512 KB decoded.
_SIG_MAX_BYTES = 512 * 1024

# Keep background tasks alive (prevents GC before completion).
_background_tasks: set = set()

# PNG/JPEG magic bytes for image validation before passing to MuPDF.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _check_image_magic(b64_str: str) -> None:
    """Raise HTTP 400 if the decoded bytes are not a PNG or JPEG."""
    try:
        header = base64.b64decode(b64_str[:16] + "==")[:8]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image data")
    if not (header.startswith(_PNG_MAGIC) or header.startswith(_JPEG_MAGIC)):
        raise HTTPException(status_code=400, detail="Signature must be a PNG or JPEG image")


def _extract_client_ip(request: Request) -> str:
    """Return the originating client IP, handling reverse-proxy X-Forwarded-For headers.

    DigitalOcean App Platform places a load balancer in front of anansi-bot, so
    request.client.host is the proxy address, not the client's real IP.
    X-Forwarded-For is a comma-separated list; the LAST value is appended by the trusted
    load balancer and cannot be spoofed by the client (the first value is client-controlled).
    Falls back to request.client.host if the header is absent.
    """
    forwarded_for: str = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[-1].strip()
    return str(request.client.host) if request.client else "unknown"


def _get_validated_user_sign(request: Request) -> Dict[str, Any]:
    """Validate initData with extended 30-minute expiry for sign endpoints."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("tma "):
        raise HTTPException(status_code=401, detail="Authentication failed")
    init_data_raw = auth_header[4:]
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        raise HTTPException(status_code=500, detail="Server configuration error")
    try:
        data = validate_init_data(init_data_raw, bot_token, max_age_seconds=_SIGN_AUTH_MAX_AGE)
    except ValueError as e:
        LOGGER.warning("Sign endpoint auth failed: %s", e)
        raise HTTPException(status_code=401, detail="Authentication failed")
    return dict(data)


async def _notify_signing_party(
    bot_token: str,
    telegram_id: str,
    signed_bytes: bytes,
    signed_name: str,
    caption: str,
    fallback_text: str,
    packet_id: str,
    role: str,
) -> None:
    """Send signed PDF to one party (requester or signer), falling back to text."""
    from shared.utils.telegram_send import send_telegram_document, send_telegram_message

    try:
        await send_telegram_document(
            bot_token,
            telegram_id,
            signed_bytes,
            signed_name,
            caption=caption,
            parse_mode="Markdown",
        )
    except Exception:
        LOGGER.warning("PDF send to %s failed for packet=%s, falling back to text", role, packet_id)
        await send_telegram_message(bot_token, telegram_id, fallback_text, parse_mode="Markdown")


async def _stamp_and_notify(
    packet_id: str,
    page: int,
    x: float,
    y: float,
    sig_bytes: bytes,
    signer_name: str,
    signer_username: str,
    state: Dict[str, Any],
    w_frac: float = 0.25,
    h_frac: float = 0.08,
    requester_session_id: Optional[str] = None,
) -> None:
    """Background task: stamp PDF, upload to Drive, update state, notify both parties."""
    from shared.utils.drive_upload import download_drive_file, upload_to_drive
    from shared.utils.telegram_send import send_telegram_message

    # Fresh service instance — independent of the FastAPI request that launched this task.
    service = WorkPacketService()

    document_drive_id: str = state.get("signing_document_drive_id", "")
    signer_telegram_id: str = state.get("signing_signer_telegram_id", "")
    requester_telegram_id: str = state.get("signing_requester_telegram_id", "")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    try:
        # 1. Download original PDF
        original_bytes = await download_drive_file(document_drive_id)

        # 2. SHA-256 of original
        original_sha256 = hashlib.sha256(original_bytes).hexdigest()

        # 3. Stamp signature + fetch Drive metadata concurrently (no data dependency).
        def _get_file_metadata(file_id: str) -> dict[str, Any]:
            from shared.utils.drive_upload import _get_service

            svc = _get_service()
            return dict(
                svc.files()
                .get(fileId=file_id, fields="name,parents", supportsAllDrives=True)
                .execute()
            )

        try:
            (signed_bytes, x1, y1, x2, y2), meta = await asyncio.gather(
                asyncio.to_thread(
                    _stamp_pdf_sync, original_bytes, page, x, y, sig_bytes, w_frac, h_frac
                ),
                asyncio.to_thread(_get_file_metadata, document_drive_id),
            )
        except ImportError:
            LOGGER.error("pymupdf not installed — cannot stamp PDF")
            await service.update_state(packet_id, {"signing_status": "failed"})
            return

        parents = meta.get("parents", [])
        parent_folder_id = str(parents[0]) if parents else None
        if not parent_folder_id:
            LOGGER.warning("Could not determine parent folder for %s", document_drive_id)
            await service.update_state(packet_id, {"signing_status": "failed"})
            return

        # Prefer document_name stored at request time; fall back to Drive filename.
        original_name = state.get("signing_document_name") or str(meta.get("name", "document"))
        stem = original_name.rsplit(".", 1)[0] if "." in original_name else original_name
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        signed_name = f"{stem}_signed_{ts}.pdf"

        # 4. Compute SHA-256 of the stamped (pre-audit-page) document.
        #    This hash covers the signed content only — excluding the audit page
        #    itself (which would create a circular dependency).
        signed_sha256 = hashlib.sha256(signed_bytes).hexdigest()

        # 5. Generate the audit trail certificate page and append it to the
        #    signed PDF so the full record travels with the document.
        now_iso = datetime.now(timezone.utc).isoformat()
        # Re-read audit fields from state (they may have been written by get_sign_data).
        # Use the state dict we already have; a fresh DB read is not necessary because
        # these fields were written before the signer submitted, so they are in the
        # state dict passed to this task from submit_signature.
        audit_data = {
            "document_name": original_name,
            "packet_id": packet_id,
            "requester_name": state.get("signing_requester_name", ""),
            "requester_email": state.get("signing_requester_email", ""),
            "sent_at": state.get("created_at", ""),
            "viewed_at": state.get("signing_audit_viewed_at"),
            "signer_name": signer_name,
            "signer_ip": state.get("signing_audit_signer_ip"),
            "signed_at": now_iso,
            "original_sha256": original_sha256,
            "signed_sha256": signed_sha256,
        }
        try:
            audit_page_bytes = await asyncio.to_thread(_generate_audit_page, audit_data)
            # Merge audit page into signed PDF off the event loop (CPU-bound tobytes)
            final_bytes = await asyncio.to_thread(
                _merge_audit_page_sync, signed_bytes, audit_page_bytes
            )
        except Exception:
            LOGGER.warning(
                "Audit page generation failed for packet=%s — uploading without it", packet_id
            )
            audit_page_bytes = b""
            final_bytes = signed_bytes

        # 6+7. Upload signed PDF and standalone audit trail concurrently (no dependency)
        audit_trail_name = f"{stem}_audit_trail_{ts}.pdf"
        upload_tasks: list = [
            asyncio.to_thread(
                upload_to_drive, final_bytes, "application/pdf", signed_name, parent_folder_id
            )
        ]
        if audit_page_bytes:
            upload_tasks.append(
                asyncio.to_thread(
                    upload_to_drive,
                    audit_page_bytes,
                    "application/pdf",
                    audit_trail_name,
                    parent_folder_id,
                )
            )
        upload_results = await asyncio.gather(*upload_tasks, return_exceptions=True)
        signed_result = upload_results[0]
        if isinstance(signed_result, BaseException):
            raise signed_result
        signed_result_dict: dict = signed_result
        signed_drive_id = signed_result_dict["id"]
        signed_url = signed_result_dict.get("webViewLink", "")

        audit_trail_drive_id = ""
        if len(upload_results) > 1:
            audit_result = upload_results[1]
            if isinstance(audit_result, BaseException):
                LOGGER.warning("Failed to upload standalone audit trail for packet=%s", packet_id)
            else:
                audit_trail_drive_id = audit_result["id"]

        # 8. Update packet_state with audit fields
        await service.update_state(
            packet_id,
            {
                "signing_status": "signed",
                "signing_signed_at": now_iso,
                "signing_signed_pdf_drive_id": signed_drive_id,
                "signing_audit_original_sha256": original_sha256,
                "signing_audit_signed_pdf_sha256": signed_sha256,
                "signing_audit_signer_name": signer_name,
                "signing_audit_signer_username": signer_username,
                "signing_audit_trail_drive_id": audit_trail_drive_id,
                "signing_audit_placement_normalised": json.dumps(
                    {"page": page, "x": x, "y": y, "w_frac": w_frac, "h_frac": h_frac}
                ),
                "signing_audit_placement_pixels": json.dumps(
                    {"page": page, "x1": x1, "y1": y1, "x2": x2, "y2": y2}
                ),
            },
        )

        doc_label = escape_markdown(original_name)
        display_name = signer_name or "Someone"

        # 9. Notify requester (send PDF with audit page appended)
        if requester_telegram_id and bot_token:
            await _notify_signing_party(
                bot_token=bot_token,
                telegram_id=requester_telegram_id,
                signed_bytes=final_bytes,
                signed_name=signed_name,
                caption=f"*{escape_markdown(display_name)}* has signed *{doc_label}*.",
                fallback_text=(
                    f"*{escape_markdown(display_name)}* has signed *{doc_label}*.\n"
                    f"[View signed document]({signed_url})"
                ),
                packet_id=packet_id,
                role="requester",
            )

        # 10. Notify signer (send PDF with audit page appended)
        if signer_telegram_id and bot_token:
            await _notify_signing_party(
                bot_token=bot_token,
                telegram_id=signer_telegram_id,
                signed_bytes=final_bytes,
                signed_name=signed_name,
                caption=f"Your signature on *{doc_label}* has been recorded.",
                fallback_text=(
                    f"Your signature on *{doc_label}* has been recorded.\n"
                    f"[Download signed document]({signed_url})"
                ),
                packet_id=packet_id,
                role="signer",
            )

        # 11. Save signing completion event to requester's chat history (non-fatal)
        if requester_session_id:
            try:
                from orchestrator.models.schemas import ConversationMessage
                from orchestrator.services.supabase_client import get_supabase_client

                sb = get_supabase_client()
                session_row = await sb.get_session(requester_session_id)
                if session_row and session_row.id:
                    safe_name = escape_markdown(original_name)[:200]
                    completion_msg = ConversationMessage(
                        role="model",
                        content=(
                            f"{display_name} has signed *{safe_name}*.\n"
                            f"Signed at: {now_iso}\n"
                            f"[View signed document]({signed_url})"
                        ),
                        timestamp=now_iso,
                        metadata={"message_type": "signing_event"},
                    )
                    await sb.save_messages(session_row.id, [completion_msg])
            except Exception:
                LOGGER.warning(
                    "Failed to save signing event to chat history for packet=%s", packet_id
                )

    except Exception:
        LOGGER.exception("_stamp_and_notify failed for packet=%s", packet_id)
        try:
            await service.update_state(packet_id, {"signing_status": "failed"})
        except Exception:
            LOGGER.warning("Failed to update signing_status to failed for packet=%s", packet_id)
        # Notify signer to retry (non-fatal — failure to notify should not mask the original error)
        if bot_token and signer_telegram_id:
            try:
                await send_telegram_message(
                    bot_token,
                    signer_telegram_id,
                    "Sorry, there was an error processing your signature. "
                    "Please tap the sign button again to retry.",
                )
            except Exception:
                LOGGER.warning(
                    "Could not send failure notification to signer for packet=%s", packet_id
                )


@router.get("/sign-data")
async def get_sign_data(
    request: Request,
    packet_id: str = Query(..., description="Work packet ID"),
    service: WorkPacketService = Depends(_get_packet_service),
) -> Response:
    """Proxy PDF bytes for the signing Mini App.

    Auth: initData HMAC with 30-minute expiry.
    Only the intended signer (signing_signer_telegram_id) may call this.
    """
    user = _get_validated_user_sign(request)
    caller_telegram_id = str(user.get("user", {}).get("id", ""))

    packet = await service.get_packet(packet_id)
    # Return 403 (not 404) to prevent packet existence probing.
    if not packet:
        raise HTTPException(
            status_code=403, detail="This signing link is not valid for your account"
        )

    state = packet.get("packet_state") or {}
    expected_signer = str(state.get("signing_signer_telegram_id", ""))

    if not expected_signer or caller_telegram_id != expected_signer:
        raise HTTPException(
            status_code=403, detail="This signing link is not valid for your account"
        )

    signing_status = state.get("signing_status", "")
    if signing_status == "signed":
        raise HTTPException(status_code=409, detail="This document has already been signed")
    # "signing" with no signed_at and updated_at > 10 min is a stuck background task;
    # allow the signer to re-fetch and resubmit (submit_signature accepts "failed" too).

    document_drive_id = state.get("signing_document_drive_id", "")
    if not document_drive_id:
        raise HTTPException(status_code=400, detail="No document attached to this signing request")

    from shared.utils.drive_upload import download_drive_file

    try:
        pdf_bytes = await download_drive_file(document_drive_id)
    except Exception:
        LOGGER.exception("Failed to proxy PDF file_id=%s", document_drive_id)
        raise HTTPException(status_code=502, detail="Could not fetch document from Drive")

    # Record first-view timestamp and signer IP for the audit trail.
    # Only write once — retries should not overwrite the original view time.
    # Non-atomic: if the signer opens two tabs simultaneously both requests may write;
    # last writer wins with ~equal timestamps, which is acceptable for an audit trail.
    if not state.get("signing_audit_viewed_at"):
        viewed_at = datetime.now(timezone.utc).isoformat()
        signer_ip = _extract_client_ip(request)
        try:
            await service.update_state(
                packet_id,
                {
                    "signing_audit_viewed_at": viewed_at,
                    "signing_audit_signer_ip": signer_ip,
                },
            )
        except Exception:
            LOGGER.warning("Failed to record view audit fields for packet=%s", packet_id)

    def _safe_header(value: str) -> str:
        """Strip CR/LF to prevent HTTP response splitting via packet_state values."""
        return value.replace("\r", "").replace("\n", "")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Cache-Control": "private, max-age=0, no-store",
            "X-Document-Name": _safe_header(state.get("signing_document_name", "")),
            "X-Requester-Name": _safe_header(state.get("signing_requester_name", "")),
        },
    )


class SignSubmission(BaseModel):
    """POST body for signature submission."""

    packet_id: str
    page: int = Field(ge=0, lt=1000)
    x: float = Field(ge=0.0, lt=1.0)
    y: float = Field(ge=0.0, lt=1.0)
    w_frac: float = Field(ge=0.07, le=0.6, default=0.25)
    h_frac: float = Field(ge=0.03, le=0.35, default=0.08)
    sig_png_b64: str


@router.post("/sign/submit")
async def submit_signature(
    body: SignSubmission,
    request: Request,
    service: WorkPacketService = Depends(_get_packet_service),
) -> Dict[str, Any]:
    """Accept a drawn signature and stamp the PDF in the background.

    Returns {status: "accepted"} immediately. Stamping, Drive upload,
    and requester notification happen in a background task.
    """
    user = _get_validated_user_sign(request)
    caller_telegram_id = str(user.get("user", {}).get("id", ""))

    # Format check before full decode (fast-fail on non-image payloads).
    _check_image_magic(body.sig_png_b64)

    # Decode once — reused by the background task, avoiding a second base64 decode.
    try:
        sig_bytes = base64.b64decode(body.sig_png_b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 in sig_png_b64")
    if len(sig_bytes) > _SIG_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Signature image too large ({len(sig_bytes)} bytes, max {_SIG_MAX_BYTES})",
        )

    packet = await service.get_packet(body.packet_id)
    # Return 403 (not 404) to prevent packet existence probing.
    if not packet:
        raise HTTPException(
            status_code=403, detail="This signing link is not valid for your account"
        )

    state = packet.get("packet_state") or {}
    expected_signer = str(state.get("signing_signer_telegram_id", ""))

    if not expected_signer or caller_telegram_id != expected_signer:
        raise HTTPException(
            status_code=403, detail="This signing link is not valid for your account"
        )

    # Atomic CAS: "pending"/"failed" → "signing".
    # claim_signing uses a conditional Supabase update so concurrent submits
    # cannot both succeed.
    claimed = await service.claim_signing(body.packet_id)
    if not claimed:
        raise HTTPException(status_code=409, detail="This document has already been signed")

    # Extract signer identity from verified initData
    tg_user = user.get("user", {})
    signer_name = (
        f"{tg_user.get('first_name', '')} {tg_user.get('last_name', '')}".strip()
        or tg_user.get("username", "Unknown")
    )
    signer_username = tg_user.get("username", "")

    requester_session_id = packet.get("requested_in_session")

    task = asyncio.create_task(
        _stamp_and_notify(
            packet_id=body.packet_id,
            page=body.page,
            x=body.x,
            y=body.y,
            sig_bytes=sig_bytes,
            signer_name=signer_name,
            signer_username=signer_username,
            state=state,
            w_frac=body.w_frac,
            h_frac=body.h_frac,
            requester_session_id=requester_session_id,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    LOGGER.info(
        "Signature accepted for packet=%s signer_telegram_id=%s",
        body.packet_id,
        caller_telegram_id,
    )
    return {"status": "accepted"}


# ── Agent Instance State View ───────────────────────────────────────────


# Internal anchor_metadata keys filtered from the agent state view.
_AGENT_INTERNAL_METADATA_KEYS = {
    "telegram_chat_id",
    "telegram_topic_id",
    "vrm_site_id",
    "organization_id",
}

# Progressive update keys written to metadata during execution — not user-facing.
_AGENT_INTERNAL_PROGRESS_KEYS = {
    "_step",
    "_step_status",
    "last_assessment",
    "last_observations",
    "last_actions",
}


def _humanize_tool_name(tool_name: str) -> str:
    """Convert tool names to human-readable labels.

    'customer_customer_get_grid_status' → 'Get Grid Status'
    'jira_search_issues_with_comments' → 'Search Issues With Comments'
    """
    # Strip common server prefixes (customer_customer_, jira_, vrm_, etc.)
    name = tool_name
    for prefix in ("customer_customer_", "jira_", "equipment_diagnostics_"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    # Strip single-word server prefixes (vrm_, logs_, messaging_)
    parts = name.split("_", 1)
    if len(parts) == 2 and len(parts[0]) <= 10:
        # Only strip if remainder still makes sense
        remainder = parts[1]
        if len(remainder) > 3:
            name = remainder
    return name.replace("_", " ").title()


# The 3 graph nodes in the persistent agent graph, in execution order.
_AGENT_GRAPH_STEPS = [
    ("load_context", "Load entity data & instructions"),
    ("think_and_act", "Assess situation & take actions"),
    ("save_and_wait", "Save results"),
]


def _build_agent_state_entries(
    instance: Dict[str, Any],
    latest_event: Dict[str, Any] | None,
) -> list[StateEntry]:
    """Build state entries for an agent instance View State display.

    Order: anchor entity → assessment/actions → instance status → accumulated metadata → error.
    """
    entries: list[StateEntry] = []

    # 1. Anchor entity fields (grid name, org, etc.)
    anchor = instance.get("anchor_metadata") or {}
    for key, val in anchor.items():
        if key in _AGENT_INTERNAL_METADATA_KEYS or val in (None, "", []):
            continue
        entries.append(StateEntry(key=key, label=_format_label(key), value=val))

    # 2. Assessment + observations + actions from latest completed event
    if latest_event:
        result = latest_event.get("result") or {}
        assessment = result.get("assessment", "")
        if assessment:
            entries.append(StateEntry(key="assessment", label="Last Assessment", value=assessment))

        # Observations: quiet summary — these are read-only data gathering, not actions.
        # For backwards compat with old events that stored everything in "actions",
        # treat "actions" as observations when "observations" key is absent.
        obs = result.get("observations", [])
        actions_raw = result.get("actions", [])
        if not obs and actions_raw and "observations" not in result:
            # Legacy event: all tool calls in "actions", split them now
            from orchestrator.graphs.persistent_agent_graph import _ACTION_TOOLS

            obs = [a for a in actions_raw if a.get("tool", "") not in _ACTION_TOOLS]
            actions_raw = [a for a in actions_raw if a.get("tool", "") in _ACTION_TOOLS]

        if obs:
            # Deduplicate tool names and humanize
            seen_tools: list[str] = []
            for o in obs:
                h = _humanize_tool_name(o.get("tool", "?"))
                if h not in seen_tools:
                    seen_tools.append(h)
            entries.append(
                StateEntry(
                    key="data_sources",
                    label="Data Sources Consulted",
                    value=", ".join(seen_tools),
                )
            )

        # Actions: only real mutations — show each with human name
        actions = actions_raw
        if actions:
            act_lines = []
            for a in actions:
                h = _humanize_tool_name(a.get("tool", "?"))
                ok = "✓" if a.get("success") else "✗"
                act_lines.append(f"{ok} {h}")
            entries.append(
                StateEntry(
                    key="actions_taken",
                    label="Actions Taken",
                    value="\n".join(act_lines),
                )
            )

    # 3. Instance status fields
    status = instance.get("status", "")
    if status:
        entries.append(StateEntry(key="status", label="Status", value=status))

    last_woke = instance.get("last_woke_at")
    if last_woke:
        entries.append(StateEntry(key="last_woke_at", label="Last Wake", value=str(last_woke)))

    wake_count = instance.get("wake_count")
    if wake_count is not None:
        entries.append(StateEntry(key="wake_count", label="Wake Count", value=wake_count))

    schedule = instance.get("wake_schedule")
    if schedule:
        entries.append(StateEntry(key="wake_schedule", label="Wake Schedule", value=schedule))

    # 4. Accumulated metadata (agent-learned knowledge)
    metadata = instance.get("metadata") or {}
    for key, val in metadata.items():
        if key in _AGENT_INTERNAL_METADATA_KEYS or val in (None, "", [], {}):
            continue
        if key in _AGENT_INTERNAL_PROGRESS_KEYS:
            continue
        entries.append(StateEntry(key=key, label=_format_label(key), value=val))

    # 5. Error (if present)
    error = instance.get("error_message")
    if error:
        entries.append(StateEntry(key="error_message", label="Error", value=error))

    return entries


def _build_agent_workflow_steps(
    latest_event: Dict[str, Any] | None,
    instance_status: str,
) -> list[WorkflowStepProgress]:
    """Build workflow step progress from the latest agent event.

    Maps the 3 persistent agent graph nodes to WorkflowStepProgress entries.
    """
    if not latest_event:
        return []

    event_status = latest_event.get("status", "")
    event_error = latest_event.get("error", "")

    # If the agent is currently executing, show in_progress
    if instance_status == "executing":
        return [
            WorkflowStepProgress(name=name, description=desc, status="in_progress")
            for name, desc in _AGENT_GRAPH_STEPS
        ]

    if event_status == "completed":
        return [
            WorkflowStepProgress(name=name, description=desc, status="success")
            for name, desc in _AGENT_GRAPH_STEPS
        ]

    if event_status == "failed":
        # Linear graph: if it failed, mark last step as failed
        steps = []
        for i, (name, desc) in enumerate(_AGENT_GRAPH_STEPS):
            if i < len(_AGENT_GRAPH_STEPS) - 1:
                steps.append(WorkflowStepProgress(name=name, description=desc, status="success"))
            else:
                fail_desc = event_error[:200] if event_error else desc
                steps.append(
                    WorkflowStepProgress(name=name, description=fail_desc, status="failed")
                )
        return steps

    return []


@router.get("/agent-state", response_model=StateDataResponse)
async def get_agent_state_data(
    instance_id: str = Query(..., description="Persistent agent instance ID"),
    sig: str = Query("", description="HMAC signature (fallback auth for desktop/web)"),
    user: Dict[str, Any] | None = Depends(get_optional_user),
) -> StateDataResponse:
    """Return read-only state view for a persistent agent instance.

    Reads from persistent_agent_instances + agent_events and returns the same
    StateDataResponse shape used by work packets, so the mini app frontend
    can render it with the existing renderStateView() function.

    Auth: same as state-data — accepts Telegram initData or HMAC sig.
    """
    import asyncio
    import uuid

    from orchestrator.services.supabase_client import get_supabase_client

    # Validate instance_id is a valid UUID
    try:
        uuid.UUID(instance_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid instance ID")

    if not user:
        if not sig or not verify_signature(instance_id, sig):
            raise HTTPException(status_code=401, detail="Authentication failed")

    supabase = get_supabase_client()._get_client()

    # Fetch instance and latest event in parallel
    instance_future = asyncio.to_thread(
        lambda: supabase.table("persistent_agent_instances")
        .select(
            "id, instance_name, expert_id, status, anchor_metadata, metadata,"
            " last_woke_at, wake_count, wake_schedule, error_message, organization_id"
        )
        .eq("id", instance_id)
        .limit(1)
        .execute()
    )
    event_future = asyncio.to_thread(
        lambda: supabase.table("agent_events")
        .select("status, result, error, processed_at")
        .eq("target_instance_id", instance_id)
        .in_("status", ["completed", "failed"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    result, event_result = await asyncio.gather(instance_future, event_future)

    if not result.data:
        raise HTTPException(status_code=404, detail="Agent instance not found")

    instance = result.data[0]

    # Verify org match for initData-authenticated users
    if user and instance.get("organization_id") != user.get("organization_id"):
        raise HTTPException(status_code=403, detail="Organization mismatch")

    latest_event = event_result.data[0] if event_result.data else None

    instance_status = instance.get("status", "")

    return StateDataResponse(
        packet_id=str(instance["id"]),
        packet_title=instance.get("instance_name", ""),
        packet_type=instance.get("expert_id", ""),
        packet_status=instance_status,
        state=_build_agent_state_entries(instance, latest_event),
        workflow_steps=_build_agent_workflow_steps(latest_event, instance_status),
        is_stale=False,
        stale_minutes=None,
    )
