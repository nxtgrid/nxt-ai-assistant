"""Unified Google Drive document resolver.

Resolves documents from any input: URL, raw file ID, doc-code, or partial name.
Consolidates URL regex patterns from fetch_document.py and doc_editing.py,
and Drive API name search from knowledge_mcp_server.py.

Permission checks run automatically when user_email is provided (fail-safe,
no opt-out). If user_email is None, permission is skipped (system-level lookup).

Usage:
    from shared.utils.drive_resolver import resolve_document, AmbiguousDocumentMatch

    doc = await resolve_document("DOC-1234", user_email="user@example.com")
    if doc:
        print(doc["file_id"], doc["name"])

    # Multiple matches raise AmbiguousDocumentMatch
    try:
        doc = await resolve_document("Site Visit Plan", user_email="user@example.com")
    except AmbiguousDocumentMatch as e:
        for match in e.matches:
            print(match["name"])
"""

import asyncio
import functools
import logging
import os
import re

from shared.utils.google_auth import get_drive_credentials

LOGGER = logging.getLogger(__name__)

# Consolidated URL patterns (previously duplicated in fetch_document.py and doc_editing.py)
GDOC_URL_PATTERN = re.compile(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)")
GDRIVE_URL_PATTERN = re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
GDRIVE_OPEN_PATTERN = re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)")
RAW_FILE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{25,60}$")
_DOC_CODE_PREFIX = os.getenv("DOC_CODE_PREFIX", "DOC")
DOC_CODE_PATTERN = re.compile(rf"{re.escape(_DOC_CODE_PREFIX)}-\d{{3,5}}")
QUOTED_NAME_PATTERN = re.compile(r'"([^"]{3,})"')

# Folder URL pattern (for rejection)
GDRIVE_FOLDER_PATTERN = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]+)")


class AmbiguousDocumentMatch(Exception):
    """Raised when a name query matches multiple documents."""

    def __init__(self, matches: list[dict]):
        self.matches = matches
        super().__init__(f"Query matched {len(matches)} documents")


@functools.lru_cache(maxsize=1)
def _get_drive_service():
    """Cached Drive v3 service for resolution queries."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=get_drive_credentials())


def extract_file_id(text: str) -> str | None:
    """Extract Google Drive file ID from text that might be a URL or raw ID.

    Returns the file ID or None. Does NOT search by name — pure extraction.
    """
    text = text.strip()

    # Reject folder URLs
    if GDRIVE_FOLDER_PATTERN.search(text):
        return None

    # Check URL patterns
    for pattern in [GDOC_URL_PATTERN, GDRIVE_URL_PATTERN, GDRIVE_OPEN_PATTERN]:
        match = pattern.search(text)
        if match:
            return match.group(1)

    # Check raw file ID
    if RAW_FILE_ID_PATTERN.match(text):
        return text

    return None


def extract_document_references(text: str) -> list[str]:
    """Extract all document references from text: URLs, doc-codes, and quoted names.

    Used to find document references in comment instructions.
    Returns a list of query strings (file IDs, doc-codes, or quoted names).
    """
    refs: list[str] = []

    # Extract file IDs from URLs
    for pattern in [GDOC_URL_PATTERN, GDRIVE_URL_PATTERN, GDRIVE_OPEN_PATTERN]:
        for match in pattern.finditer(text):
            file_id = match.group(1)
            if file_id not in refs:
                refs.append(file_id)

    # Extract doc-codes (e.g., DOC-1234; prefix set via DOC_CODE_PREFIX env var)
    for match in DOC_CODE_PATTERN.finditer(text):
        code = match.group(0)
        if code not in refs:
            refs.append(code)

    # Extract quoted document names (e.g., "ExampleSite Visit Plan")
    for match in QUOTED_NAME_PATTERN.finditer(text):
        name = match.group(1)
        if name not in refs:
            refs.append(name)

    return refs


def _get_metadata(file_id: str) -> dict | None:
    """Fetch file metadata by ID (sync, for use with asyncio.to_thread)."""
    try:
        service = _get_drive_service()
        result = (
            service.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, webViewLink, modifiedTime",
                supportsAllDrives=True,
            )
            .execute()
        )
        return {
            "file_id": result["id"],
            "name": result.get("name", ""),
            "mime_type": result.get("mimeType", ""),
            "url": result.get(
                "webViewLink",
                f"https://docs.google.com/document/d/{result['id']}",
            ),
        }
    except Exception as e:
        LOGGER.warning(f"Could not fetch metadata for {file_id[:12]}...: {e}")
        return None


def _search_by_name(query: str) -> list[dict]:
    """Search Drive for documents by name fragment (sync, for use with asyncio.to_thread)."""
    try:
        service = _get_drive_service()
        safe_query = query.replace("\\", "\\\\").replace("'", "\\'")
        # No mimeType filter — resolve any file type (Docs, Sheets, PDFs)
        drive_query = f"name contains '{safe_query}' and trashed = false"

        results = (
            service.files()
            .list(
                q=drive_query,
                fields="files(id, name, mimeType, webViewLink, modifiedTime)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                orderBy="modifiedTime desc",
            )
            .execute()
        )

        return [
            {
                "file_id": f["id"],
                "name": f.get("name", ""),
                "mime_type": f.get("mimeType", ""),
                "url": f.get(
                    "webViewLink",
                    f"https://drive.google.com/file/d/{f['id']}",
                ),
            }
            for f in results.get("files", [])
        ]
    except Exception as e:
        LOGGER.error(f"Drive search failed for '{query}': {e}")
        return []


async def resolve_document(
    query: str,
    user_email: str | None = None,
) -> dict | None:
    """Resolve a document from any input: URL, file ID, doc-code, or partial name.

    Returns dict {"file_id", "name", "mime_type", "url"} for single match,
    or None if not found / access denied.

    Raises AmbiguousDocumentMatch (with .matches list) if multiple results.

    Permission check runs automatically when user_email is provided.
    No opt-out — fail-safe by design.
    """
    query = query.strip()
    if not query:
        return None

    # Step 1: Try direct ID/URL extraction (fast path, no API call)
    file_id = extract_file_id(query)
    if file_id:
        metadata = await asyncio.to_thread(_get_metadata, file_id)
        if not metadata:
            return None
        # Permission check
        if user_email:
            from shared.utils.drive_permissions import user_can_access

            if not await user_can_access(metadata["file_id"], user_email, need_write=False):
                LOGGER.warning(
                    f"Document {metadata['file_id'][:12]}... access denied for {user_email}"
                )
                return None
        return metadata

    # Step 2: Search by name
    results = await asyncio.to_thread(_search_by_name, query)

    if not results:
        return None

    # Permission filter: if user_email provided, filter to accessible docs
    if user_email:
        from shared.utils.drive_permissions import user_can_access

        accessible = []
        for doc in results:
            if await user_can_access(doc["file_id"], user_email, need_write=False):
                accessible.append(doc)
        results = accessible

    if not results:
        return None

    if len(results) == 1:
        return results[0]

    # Multiple matches — let the caller decide
    raise AmbiguousDocumentMatch(matches=results)
