"""Google Drive file upload utilities for workflow step outputs.

Provides folder creation and file upload functions with a cached Drive service.
Used by LPP workflow steps to organize outputs in per-site subfolders.

Usage:
    from shared.utils.drive_upload import upload_step_output

    upload_step_output(
        site_folder_id="abc123",
        subfolder_name="Distribution Design",
        site_name="ExampleGrid",
        files=[(png_bytes, "image/png", "distribution_map")],
    )
"""

import asyncio
import functools
import logging
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from shared.utils.google_auth import get_drive_write_credentials

LOGGER = logging.getLogger(__name__)

TIMESTAMP_FORMAT = "%Y%m%d-%H%M"

# Set LPP_OUTPUT_FOLDER_ID env var or configure via the Settings UI.
DEFAULT_LPP_OUTPUT_FOLDER_ID = ""


@functools.lru_cache(maxsize=1)
def _get_service():
    """Cached Drive v3 service (built once per process)."""
    return build("drive", "v3", credentials=get_drive_write_credentials())


def get_or_create_folder(folder_name: str, parent_id: str) -> str:
    """Find existing folder by name under parent, or create it. Returns folder ID.

    Race condition note: concurrent calls for the same folder_name may create
    duplicates. We sort by createdTime and take the earliest to be deterministic.
    """
    service = _get_service()
    safe_name = folder_name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe_name}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents "
        f"and trashed = false"
    )
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id, name, createdTime)",
            orderBy="createdTime",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    existing = results.get("files", [])
    if existing:
        return str(existing[0]["id"])

    folder = (
        service.files()
        .create(
            body={
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return str(folder["id"])


def upload_to_drive(
    content: bytes,
    mime_type: str,
    file_name: str,
    folder_id: str,
) -> dict:
    """Upload bytes to a Drive folder. Returns {"id", "name", "webViewLink"}."""
    service = _get_service()
    media = MediaInMemoryUpload(content, mimetype=mime_type, resumable=False)
    return dict(
        service.files()
        .create(
            body={"name": file_name, "parents": [folder_id]},
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )


async def upload_step_output(
    site_folder_id: str | None,
    subfolder_name: str | None,
    site_name: str,
    files: list[tuple[bytes, str, str]],
    explicit_extension: str | None = None,
) -> dict[str, str]:
    """Upload step output files to a Drive subfolder. Non-fatal on failure.

    Args:
        site_folder_id: Parent site folder ID (skip if None).
        subfolder_name: Sub-subfolder name (e.g. "Distribution Design").
                        If None, uploads directly to site_folder_id.
        site_name: Site name for filename prefix (spaces replaced with underscores).
        files: List of (content_bytes, mime_type, artifact_label) tuples.
               artifact_label becomes part of the filename, e.g. "distribution_map".
        explicit_extension: Override file extension (e.g. "qgz") instead of
               deriving from mime_type. Useful for application/octet-stream.

    Returns:
        Dict mapping artifact_label -> Drive file ID. Empty dict on failure.
    """
    if not site_folder_id:
        return {}
    try:
        ts = datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
        safe_name = site_name.replace(" ", "_")
        if subfolder_name:
            target_id = await asyncio.to_thread(
                get_or_create_folder, subfolder_name, site_folder_id
            )
        else:
            target_id = site_folder_id
        uploaded: dict[str, str] = {}
        for content, mime_type, label in files:
            ext = explicit_extension or mime_type.split("/")[-1]
            file_name = f"{safe_name}_{label}_{ts}.{ext}"
            result = await asyncio.to_thread(
                upload_to_drive, content, mime_type, file_name, target_id
            )
            LOGGER.info(f"Uploaded {label} to Drive: {result.get('webViewLink')}")
            uploaded[label] = result["id"]
        return uploaded
    except Exception as e:
        LOGGER.warning(f"Drive upload failed (non-fatal): {e}")
        return {}


def download_from_drive(file_id: str) -> bytes:
    """Download file content from Drive by ID. Uses write creds (same service account)."""
    service = _get_service()
    return bytes(service.files().get_media(fileId=file_id, supportsAllDrives=True).execute())


async def download_drive_file(file_id: str, retries: int = 2) -> bytes:
    """Async wrapper: download file content from Drive by ID. Retries on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await asyncio.to_thread(download_from_drive, file_id)
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(0.5)
    raise last_exc  # type: ignore[misc]
