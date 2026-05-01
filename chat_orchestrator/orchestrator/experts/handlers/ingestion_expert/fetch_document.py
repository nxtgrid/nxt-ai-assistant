"""Fetch document step handler for Document Ingestion Expert.

This handler retrieves document content from Google Drive or direct text input:
- Google Docs (exported as plain text)
- PDFs (text extraction with preprocessing)
- Interactive mode (/learn interactive) - guided type selection + paste
- Inline text mode (/learn <text>) - auto-detect and ingest pasted text

Documents from Google Drive must be shared with the service account or in a shared folder.
"""

import asyncio
import hashlib
import os
import re
from typing import Optional, Tuple

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Regex to identify Google Drive file IDs (25-60 characters, alphanumeric + hyphen/underscore)
GDRIVE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{25,60}$")

# Regex patterns to extract file ID from various Google URLs
GDOC_URL_PATTERN = re.compile(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)")
GDRIVE_URL_PATTERN = re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
GDRIVE_OPEN_PATTERN = re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)")

# Pattern to detect folder URLs (not ingestible as a single document)
GDRIVE_FOLDER_PATTERN = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]+)")

# Broad pattern to detect any Google Drive/Docs URL (for error handling)
GOOGLE_URL_PATTERN = re.compile(r"(?:docs|drive)\.google\.com/")

CANCEL_WORDS = {"cancel", "skip", "abort", "quit", "exit", "stop", "no"}

TYPE_MAP = {
    "1": "support_example",
    "2": "technical",
    "3": "sop",
    "4": "faq",
    "5": "policy",
}

TYPE_SELECTION_PROMPT = (
    "What type of document are you adding?\n\n"
    "1. Support Example (customer interaction, chat transcript)\n"
    "2. Technical (specs, architecture, implementation guide)\n"
    "3. SOP (standard operating procedure, checklist)\n"
    "4. FAQ (frequently asked questions)\n"
    "5. Policy (company rules, guidelines)\n\n"
    "Reply with a number (1-5), or `cancel` to abort."
)


def extract_file_id(text: str) -> Optional[str]:
    """Extract Google Drive file ID from text that might be a URL or raw ID."""
    text = text.strip()

    # Check various URL patterns
    for pattern in [GDOC_URL_PATTERN, GDRIVE_URL_PATTERN, GDRIVE_OPEN_PATTERN]:
        match = pattern.search(text)
        if match:
            return match.group(1)

    # Check if it's a raw file ID
    if GDRIVE_ID_PATTERN.match(text):
        return text

    return None


@register_step("fetch_document")
async def fetch_document(context: StepContext) -> StepResult:
    """Fetch document content from Google Drive.

    Supported formats:
    - Google Docs (exported as plain text)
    - PDFs (text extraction)

    If user provides a file ID or URL directly in args, auto-detect it.

    Args:
        context: Step execution context

    Returns:
        StepResult with document_content or prompts for file ID
    """
    # --- Resume: type selection (interactive mode) ---
    if context.get_state("awaiting_type_selection") and context.user_input:
        user_response = context.user_input.strip().lower()
        if user_response in CANCEL_WORDS:
            LOGGER.info("User cancelled interactive type selection")
            return StepResult(
                skip_remaining=True,
                progress_message="Ingestion cancelled.",
            )

        if user_response not in TYPE_MAP:
            return StepResult(
                needs_user_input=True,
                user_prompt=(f"Please reply with a number 1-5.\n\n{TYPE_SELECTION_PROMPT}"),
            )

        doc_type = TYPE_MAP[user_response]
        LOGGER.info(f"User selected document type: {doc_type}")
        return StepResult(
            state_updates={
                "detected_doc_type": doc_type,
                "user_selected_doc_type": True,
                "awaiting_type_selection": False,
                "awaiting_content_paste": True,
            },
            needs_user_input=True,
            user_prompt=(
                "Now paste the content you want to add to the knowledge base.\n\n"
                "You can paste plain text, markdown, or a chat transcript.\n"
                "Reply `cancel` to abort."
            ),
        )

    # --- Resume: content paste (interactive mode) ---
    if context.get_state("awaiting_content_paste") and context.user_input:
        user_response = context.user_input.strip()
        if user_response.lower() in CANCEL_WORDS:
            LOGGER.info("User cancelled content paste")
            return StepResult(
                skip_remaining=True,
                progress_message="Ingestion cancelled.",
            )

        if len(user_response) < 20:
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "That's too short (minimum 20 characters).\n"
                    "Please paste the full content, or reply `cancel` to abort."
                ),
            )

        content = user_response
        source_id = f"manual_{hashlib.sha256(content.encode()).hexdigest()[:16]}"
        LOGGER.info(f"Received pasted content ({len(content)} chars), source_id={source_id}")

        return StepResult(
            data={
                "document_content": content,
                "source_type": "manual_input",
                "source_id": source_id,
                "source_url": None,
                "title": "User-submitted text",
                "file_type": "text",
            },
            state_updates={
                "document_content": content,
                "source_type": "manual_input",
                "source_id": source_id,
                "document_title": "User-submitted text",
                "file_type": "text",
                "input_mode": "interactive",
                "awaiting_content_paste": False,
            },
            progress_message=f"Received text ({len(content)} characters)",
        )

    # --- Resume: awaiting file ID (existing flow) ---
    awaiting_file_id = context.get_state("awaiting_file_id")

    if awaiting_file_id and context.user_input:
        user_response = context.user_input.strip().lower()

        # Check for cancel commands first
        if user_response in CANCEL_WORDS:
            LOGGER.info("User cancelled document fetch")
            return StepResult(
                skip_remaining=True,
                progress_message="Ingestion cancelled.",
            )

        file_id = extract_file_id(context.user_input)
        if not file_id:
            # Check if it's a folder URL
            if GDRIVE_FOLDER_PATTERN.search(context.user_input):
                return StepResult(
                    needs_user_input=True,
                    user_prompt=(
                        "That's a Google Drive **folder** link. I need a direct file link.\n\n"
                        "Open the folder, find the file, and share its URL."
                    ),
                )
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "That doesn't look like a valid Google Drive file ID or URL.\n\n"
                    "Please provide:\n"
                    "• A file ID like `1ABC...xyz`\n"
                    "• A Google Docs URL\n"
                    "• A Google Drive share link"
                ),
            )
        return await _fetch_from_gdrive(context, file_id)

    # First run - check if user provided args with file ID
    doc_id_input = context.get_input("document_id") or ""
    args_text = context.packet_inputs.get("args", "") or ""

    detected_file_id = extract_file_id(args_text) or extract_file_id(doc_id_input)

    if detected_file_id:
        LOGGER.info(f"Auto-detected Google Drive file ID: {detected_file_id}")
        return await _fetch_from_gdrive(context, detected_file_id)

    # --- Interactive mode: /learn interactive ---
    if args_text.strip().lower() == "interactive":
        LOGGER.info("Starting interactive document input mode")
        return StepResult(
            state_updates={
                "input_mode": "interactive",
                "awaiting_type_selection": True,
            },
            needs_user_input=True,
            user_prompt=TYPE_SELECTION_PROMPT,
        )

    # --- Try name-based resolution (e.g., "/ingest ExampleSite Visit Plan") ---
    if args_text and not detected_file_id:
        try:
            from shared.utils.drive_resolver import AmbiguousDocumentMatch, resolve_document

            resolved = await resolve_document(args_text.strip(), user_email=context.effective_email)
            if resolved:
                LOGGER.info(
                    f"Resolved document by name: '{args_text.strip()}' → {resolved['name']} "
                    f"({resolved['file_id'][:12]}...)"
                )
                return await _fetch_from_gdrive(context, resolved["file_id"])
        except AmbiguousDocumentMatch as e:
            match_list = "\n".join(f"• {m['name']} ({m['url']})" for m in e.matches[:5])
            return StepResult(
                state_updates={"awaiting_file_id": True},
                needs_user_input=True,
                user_prompt=(
                    f"Multiple documents match '{args_text.strip()}':\n{match_list}\n\n"
                    "Please share the direct link to the one you want to ingest."
                ),
            )
        except Exception as e:
            LOGGER.debug(f"Name resolution failed for '{args_text.strip()}': {e}")

    # --- Check for Google Drive URLs that didn't match a file pattern ---
    if args_text and not detected_file_id:
        text = args_text.strip()

        # Detect folder URLs (can't ingest a folder directly)
        folder_match = GDRIVE_FOLDER_PATTERN.search(text)
        if folder_match:
            LOGGER.info(f"Detected Google Drive folder URL: {folder_match.group(0)}")
            return StepResult(
                state_updates={"awaiting_file_id": True},
                needs_user_input=True,
                user_prompt=(
                    "That's a Google Drive **folder** link. I can only ingest individual files.\n\n"
                    "Please share the direct link to a specific file:\n"
                    "• A **Google Doc** URL (docs.google.com/document/d/...)\n"
                    "• A **PDF** file URL (drive.google.com/file/d/...)\n\n"
                    "Open the folder, find the file you want to ingest, and share its link."
                ),
            )

        # Detect unrecognized Google Drive/Docs URLs (not a file we can fetch)
        if GOOGLE_URL_PATTERN.search(text):
            LOGGER.warning(f"Unrecognized Google URL: {text[:100]}")
            return StepResult(
                state_updates={"awaiting_file_id": True},
                needs_user_input=True,
                user_prompt=(
                    "I couldn't extract a file ID from that Google link.\n\n"
                    "Supported URL formats:\n"
                    "• Google Docs: `docs.google.com/document/d/{file_id}`\n"
                    "• Google Drive file: `drive.google.com/file/d/{file_id}`\n"
                    "• Share link: `drive.google.com/open?id={file_id}`\n\n"
                    "Please share the direct link to the file, not a folder."
                ),
            )

    # --- Inline text mode: /learn <text> ---
    if args_text and not detected_file_id:
        text = args_text.strip()
        if len(text) < 20:
            return StepResult(
                state_updates={"awaiting_file_id": True},
                needs_user_input=True,
                user_prompt=(
                    "That input is too short to be document content (minimum 20 characters).\n\n"
                    "You can:\n"
                    "• Paste a **Google Drive URL** or file ID\n"
                    "• Type `/learn interactive` for guided input\n"
                    "• Type `/learn <your full text>` to paste content directly"
                ),
            )

        source_id = f"manual_{hashlib.sha256(text.encode()).hexdigest()[:16]}"
        LOGGER.info(f"Inline text detected ({len(text)} chars), source_id={source_id}")

        return StepResult(
            data={
                "document_content": text,
                "source_type": "manual_input",
                "source_id": source_id,
                "source_url": None,
                "title": "User-submitted text",
                "file_type": "text",
            },
            state_updates={
                "document_content": text,
                "source_type": "manual_input",
                "source_id": source_id,
                "document_title": "User-submitted text",
                "file_type": "text",
                "input_mode": "inline_text",
            },
            progress_message=f"Received text ({len(text)} characters)",
        )

    # No input provided - ask user for file ID or text
    return StepResult(
        state_updates={"awaiting_file_id": True},
        needs_user_input=True,
        user_prompt=(
            "What document would you like to ingest?\n\n"
            "You can:\n"
            "• Paste a **Google Drive URL** or file ID\n"
            "• Type `/learn interactive` for guided text input\n"
            "• Type `/learn <your text>` to paste content directly\n\n"
            "Google Drive files must be shared with the bot's service account.\n\n"
            "Example: `1ABC...xyz` or paste the full Google Docs/Drive URL"
        ),
    )


async def _fetch_from_gdrive(context: StepContext, file_id: str) -> StepResult:
    """Fetch document content from Google Drive.

    Supports:
    - Google Docs (exported as plain text)
    - PDFs (extracted and preprocessed with unstructured)

    Args:
        context: Step context
        file_id: Google Drive file ID

    Returns:
        StepResult with document content or error
    """
    LOGGER.info(f"Fetching Google Drive file: {file_id}")

    # Permission check: verify the requesting user has read access
    user_email = context.effective_email
    try:
        from shared.utils.drive_permissions import user_can_access

        if not await user_can_access(file_id, user_email, need_write=False):
            return StepResult.failure(
                "You don't have permission to access this file. "
                "Please ask the file owner to share it with you."
            )
    except Exception as perm_err:
        LOGGER.warning(f"Permission check failed for {file_id}: {perm_err}")
        # Fail closed — don't proceed if we can't verify permissions
        return StepResult.failure("Could not verify your access to this file. Please try again.")

    try:
        from googleapiclient.discovery import build

        from shared.utils.google_auth import get_drive_credentials
    except ImportError as e:
        LOGGER.error(f"Google API packages not available: {e}")
        return StepResult.failure("Google Drive integration not configured. Contact support.")

    try:
        # Send progress before any blocking I/O so user sees immediate feedback
        await context.send_progress_to_user("Fetching document from Google Drive...")

        # Initialize Drive service (keep build() on main thread — lru_cache not thread-safe)
        credentials = get_drive_credentials()
        service = build("drive", "v3", credentials=credentials)

        # Get file metadata including version info for change detection
        # supportsAllDrives=True is required to access files in shared folders/drives
        # Wrap .execute() in asyncio.to_thread to avoid blocking the event loop
        file_meta = await asyncio.to_thread(
            lambda: service.files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,size,version,modifiedTime,headRevisionId",
                supportsAllDrives=True,
            )
            .execute()
        )

        title = file_meta.get("name", "Untitled")
        mime_type = file_meta.get("mimeType", "")
        # Version tracking for change detection
        revision_id = file_meta.get("headRevisionId") or file_meta.get("version")
        modified_time = file_meta.get("modifiedTime")

        LOGGER.info(f"File: {title}, MIME type: {mime_type}")

        # Determine file type and extract content
        if mime_type == "application/vnd.google-apps.document":
            # Google Doc - export as plain text
            content, file_type = await _export_google_doc(service, file_id, title)

        elif mime_type == "application/pdf":
            # PDF - download and preprocess
            await context.send_progress_to_user(f"Processing PDF: {title}")
            content, file_type = await _extract_pdf_content(service, file_id, title)

        elif mime_type in [
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ]:
            # DOCX/DOC - not supported yet
            return StepResult.failure(
                "Word documents (.docx) are not yet supported. "
                "Please convert to Google Doc or PDF first."
            )

        else:
            return StepResult.failure(
                f"Unsupported file type: {mime_type}\n\n"
                f"Supported formats:\n"
                f"• Google Docs\n"
                f"• PDF files"
            )

        if not content or len(content.strip()) < 50:
            return StepResult.failure(
                "Document is empty or too short. Please provide a document with more content."
            )

        LOGGER.info(f"Fetched '{title}' ({len(content)} chars, type: {file_type})")

        # Construct source URL based on file type
        if mime_type == "application/vnd.google-apps.document":
            source_url = f"https://docs.google.com/document/d/{file_id}/edit"
        else:
            source_url = f"https://drive.google.com/file/d/{file_id}/view"

        return StepResult(
            data={
                "document_content": content,
                "source_type": "gdrive",
                "source_id": file_id,
                "source_url": source_url,
                "title": title,
                "file_type": file_type,
                "mime_type": mime_type,
                "revision_id": revision_id,
                "modified_time": modified_time,
            },
            state_updates={
                "document_content": content,
                "source_type": "gdrive",
                "source_id": file_id,
                "source_url": source_url,
                "document_title": title,
                "file_type": file_type,
                "revision_id": revision_id,
                "modified_time": modified_time,
                "awaiting_file_id": False,
            },
            progress_message=f"Fetched '{title}' ({len(content)} characters)",
        )

    except Exception as e:
        error_msg = str(e)
        LOGGER.error(f"Google Drive API error for file {file_id}: {error_msg}")

        if "404" in error_msg or "notFound" in error_msg:
            LOGGER.error(
                f"File {file_id} not found. This can happen if: "
                "1) File doesn't exist, 2) Not shared with service account, "
                "3) File is in a shared drive without supportsAllDrives=True"
            )
            return StepResult.failure(
                "File not found. Make sure the file exists and is shared with the bot's service account."
            )
        elif "403" in error_msg or "forbidden" in error_msg:
            return StepResult.failure(
                "Access denied. Please share the file with the bot's service account."
            )
        LOGGER.exception(f"Failed to fetch from Google Drive: {e}")
        from shared.utils.error_messages import ErrorCategory, get_user_message

        return StepResult.failure(get_user_message(ErrorCategory.SYSTEM, "internal_error"))


async def _export_google_doc(service=None, file_id: str = "", title: str = "") -> Tuple[str, str]:
    """Export Google Doc as markdown with preserved formatting.

    Uses the shared gdrive_doc_fetcher which:
    - Converts headings to markdown (# ## ###)
    - Preserves bold (**text**) and italic (*text*)
    - Converts tables to markdown format
    - Strips title pages, headers/footers, and images

    This ensures consistent Google Doc handling across Anansi
    (same as instructions provider and artifacts provider).

    Args:
        service: Google Drive API service (unused - using shared fetcher)
        file_id: File ID
        title: File title for logging

    Returns:
        Tuple of (content, file_type)
    """
    from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown

    # fetch_google_doc_markdown is fully synchronous (Google Docs API chain) — offload to thread
    content = await asyncio.to_thread(fetch_google_doc_markdown, file_id)

    if not content:
        # Fallback to plain text export if markdown conversion fails
        LOGGER.warning(f"Markdown conversion failed for {file_id}, falling back to plain text")
        content = await asyncio.to_thread(
            lambda: service.files()
            .export_media(fileId=file_id, mimeType="text/plain")
            .execute()
            .decode("utf-8")
        )

    return content, "google_doc"


async def _extract_pdf_content(service, file_id: str, title: str) -> Tuple[str, str]:
    """Extract and preprocess PDF content.

    Uses pymupdf4llm for intelligent extraction optimized for LLM workflows:
    - Outputs clean markdown directly
    - Handles tables automatically
    - Preserves document structure
    - Lightweight (no PyTorch/CUDA dependencies)

    Falls back to basic PyMuPDF text extraction if markdown conversion fails.

    Args:
        service: Google Drive API service
        file_id: File ID
        title: File title for logging

    Returns:
        Tuple of (content, file_type)
    """
    import tempfile

    # Download PDF bytes
    request = service.files().get_media(fileId=file_id)
    pdf_bytes = request.execute()

    # Write to temp file for pymupdf4llm
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        # Try pymupdf4llm first (best quality markdown output)
        try:
            import pymupdf4llm

            # Extract as markdown - handles tables, formatting automatically
            content = pymupdf4llm.to_markdown(tmp_path)

            # Basic cleanup - remove excessive whitespace
            content = _basic_pdf_cleanup(content)

            LOGGER.info(f"PDF extracted with pymupdf4llm: {len(content)} chars")
            return content, "pdf"

        except ImportError:
            LOGGER.warning("pymupdf4llm not available, falling back to basic PyMuPDF")

        # Fallback to basic PyMuPDF text extraction
        try:
            import pymupdf  # PyMuPDF is also known as fitz

            doc = pymupdf.open(tmp_path)
            text_parts = []

            for page in doc:
                page_text = page.get_text() or ""
                page_text = _basic_pdf_cleanup(page_text)
                if page_text.strip():
                    text_parts.append(page_text)

            page_count = len(doc)
            doc.close()
            content = "\n\n".join(text_parts)
            LOGGER.info(f"PDF extracted with PyMuPDF: {page_count} pages")
            return content, "pdf"

        except ImportError:
            LOGGER.error("Neither pymupdf4llm nor pymupdf available")
            raise ImportError(
                "PDF processing requires 'pymupdf4llm'. Install with: pip install pymupdf4llm"
            )
    finally:
        os.unlink(tmp_path)


def _basic_pdf_cleanup(text: str) -> str:
    """Basic cleanup for PDF text extracted with PyPDF2/pymupdf4llm.

    Removes common header/footer patterns, strips HTML tags that pymupdf4llm
    produces inside markdown table cells (e.g. <br>, <b>, </b>).

    Args:
        text: Raw extracted text

    Returns:
        Cleaned text
    """
    import re

    # Replace <br>, <br/>, <br /> tags with a single space
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)

    # Strip any other stray HTML tags (e.g. <b>, </b>, <i>, </i>)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)

    lines = text.split("\n")
    cleaned_lines: list[str] = []

    for line in lines:
        # Collapse multiple spaces to single space (from HTML tag removal)
        line = re.sub(r"  +", " ", line)

        stripped = line.strip()

        # Skip standalone page numbers
        if re.match(r"^[\d]+$", stripped):
            continue

        # Skip common footer patterns
        if re.match(r"^Page\s+\d+\s*(of\s+\d+)?$", stripped, re.IGNORECASE):
            continue

        # Skip lines that are just dashes or underscores (separators)
        if re.match(r"^[-_=]{10,}$", stripped):
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)
