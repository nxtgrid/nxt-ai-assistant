#!/usr/bin/env python3
"""
Google Drive Document Fetcher

Shared utility for fetching and parsing Google Drive documents across Anansi.
Supports Google Docs, Sheets, Slides, PDFs, DOCX, and plain text files.

This is used by both chat_orchestrator and rag_pipeline to fetch individual documents by file ID.

Usage:
    from shared.utils.gdrive_doc_fetcher import GoogleDriveDocFetcher, fetch_google_doc

    # Option 1: Direct function
    content = fetch_google_doc('1abc123xyz456')

    # Option 2: Class instance
    fetcher = GoogleDriveDocFetcher()
    content = fetcher.fetch_document('1abc123xyz456', auto_detect_type=True)
"""

import functools
import sys
from typing import Optional, Tuple

# Import google_auth from shared.utils
from shared.utils.google_auth import get_drive_credentials


@functools.lru_cache(maxsize=1)
def _cached_drive_service():
    """Module-level cached Drive v3 service. Built once per process."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=get_drive_credentials())


@functools.lru_cache(maxsize=1)
def _cached_docs_service():
    """Module-level cached Docs v1 service. Built once per process."""
    from googleapiclient.discovery import build

    from shared.utils.google_auth import get_docs_credentials

    return build("docs", "v1", credentials=get_docs_credentials())


class GoogleDriveDocFetcher:
    """
    Lightweight utility for fetching individual Google Drive documents.

    This wraps Google Drive API functionality for single-file fetching
    rather than folder indexing.
    """

    def __init__(self):
        """Initialize the fetcher with Google Drive service."""
        self.service = None
        self._initialize_service()

    def _initialize_service(self):
        """Initialize Google Drive API service using cached module-level services."""
        try:
            self.service = _cached_drive_service()
            self.docs_service = _cached_docs_service()
        except ImportError as e:
            raise Exception(
                f"Required packages not available: {str(e)}. "
                f"Install with: pip install google-auth google-auth-oauthlib google-api-python-client"
            )
        except Exception as e:
            raise Exception(f"Failed to initialize Google Drive service: {str(e)}")

    def get_file_metadata(self, file_id: str) -> Optional[dict]:
        """
        Get metadata for a file.

        Args:
            file_id: Google Drive file ID

        Returns:
            Dict with file metadata (name, mimeType, etc.) or None if not found
        """
        if not self.service:
            raise Exception("Service not initialized")

        try:
            file_metadata = (
                self.service.files()
                .get(
                    fileId=file_id,
                    fields="id, name, mimeType, size, createdTime, modifiedTime, owners",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return dict(file_metadata)  # Ensure it's a dict
        except Exception as e:
            print(f"Error fetching metadata for {file_id}: {e}", file=sys.stderr)
            return None

    def fetch_document_with_formatting(self, file_id: str) -> Optional[str]:
        """
        Fetch a Google Doc with rich formatting converted to markdown.

        Uses Google Docs API to extract styled content and converts:
        - Heading 1 → # Heading
        - Heading 2 → ## Heading
        - Heading 3 → ### Heading
        - Bold → **text**
        - Italic → *text*
        - Lists → Proper markdown lists

        Automatically strips:
        - Title page (everything before first page break)
        - Headers and footers
        - Inline images

        Args:
            file_id: Google Drive document ID

        Returns:
            Document content as markdown, or None if fetch fails
        """
        if not self.docs_service:
            raise Exception("Docs service not initialized")

        try:
            # Fetch the document with full content structure
            doc = self.docs_service.documents().get(documentId=file_id).execute()

            markdown_lines = []

            # Process document body content
            content = doc.get("body", {}).get("content", [])

            # First pass: check if document has any page breaks
            # (documents with title pages have page breaks; plain text uploads don't)
            has_page_breaks = any(
                "pageBreak" in elem
                for element in content
                if "paragraph" in element
                for elem in element["paragraph"].get("elements", [])
            )

            first_page_break_seen = not has_page_breaks  # If no page breaks, include all content

            for element in content:
                # Skip headers and footers (always)
                if "sectionBreak" in element:
                    section = element["sectionBreak"]
                    # Skip content in headers/footers
                    if section.get("sectionStyle", {}).get("sectionType") in ["HEADER", "FOOTER"]:
                        continue

                # Handle page breaks for title page stripping (only if doc has page breaks)
                if has_page_breaks and "paragraph" in element:
                    para = element["paragraph"]
                    # Check if this paragraph contains a page break
                    for elem in para.get("elements", []):
                        if "pageBreak" in elem:
                            first_page_break_seen = True
                            break

                    # Skip content before first page break (title page)
                    if not first_page_break_seen:
                        continue

                # Process paragraphs
                if "paragraph" in element:
                    para = element["paragraph"]
                    para_style = para.get("paragraphStyle", {})

                    # Get heading level
                    named_style = para_style.get("namedStyleType", "NORMAL_TEXT")

                    # Build paragraph text with inline formatting
                    para_text = self._extract_paragraph_text(
                        para,
                        strip_images=True,  # Always strip images
                    )

                    if not para_text.strip():
                        continue

                    # Convert to markdown based on style
                    if named_style == "HEADING_1":
                        markdown_lines.append(f"# {para_text}")
                    elif named_style == "HEADING_2":
                        markdown_lines.append(f"## {para_text}")
                    elif named_style == "HEADING_3":
                        markdown_lines.append(f"### {para_text}")
                    elif named_style == "HEADING_4":
                        markdown_lines.append(f"#### {para_text}")
                    elif named_style == "HEADING_5":
                        markdown_lines.append(f"##### {para_text}")
                    elif named_style == "HEADING_6":
                        markdown_lines.append(f"###### {para_text}")
                    else:
                        # Regular paragraph
                        markdown_lines.append(para_text)

                    markdown_lines.append("")  # Add blank line after paragraph

                # Process tables
                elif "table" in element:
                    table_md = self._convert_table_to_markdown(element["table"])
                    if table_md:
                        markdown_lines.append(table_md)
                        markdown_lines.append("")

            # Join and clean up extra blank lines
            markdown = "\n".join(markdown_lines)
            # Replace 3+ consecutive newlines with just 2
            while "\n\n\n" in markdown:
                markdown = markdown.replace("\n\n\n", "\n\n")

            return markdown.strip()

        except Exception as e:
            print(f"Error fetching formatted document {file_id}: {e}", file=sys.stderr)
            import traceback

            traceback.print_exc(file=sys.stderr)
            return None

    def _extract_paragraph_text(self, paragraph: dict, strip_images: bool = True) -> str:
        """
        Extract text from a paragraph with inline formatting.

        Args:
            paragraph: Paragraph element from Docs API
            strip_images: Skip inline images

        Returns:
            Formatted text with markdown
        """
        text_parts = []

        for element in paragraph.get("elements", []):
            # Skip images if requested
            if strip_images and "inlineObjectElement" in element:
                continue

            # Extract text content
            text_run = element.get("textRun")
            if not text_run:
                continue

            content = text_run.get("content", "")
            if not content:
                continue

            # Get text style
            style = text_run.get("textStyle", {})

            # Apply formatting
            formatted = content

            # Bold
            if style.get("bold"):
                formatted = f"**{formatted}**"

            # Italic
            if style.get("italic"):
                formatted = f"*{formatted}*"

            # Strikethrough
            if style.get("strikethrough"):
                formatted = f"~~{formatted}~~"

            # Code (monospace font)
            if style.get("weightedFontFamily", {}).get("fontFamily") == "Courier New":
                formatted = f"`{formatted}`"

            text_parts.append(formatted)

        return "".join(text_parts).rstrip("\n")

    def _convert_table_to_markdown(self, table: dict) -> str:
        """
        Convert a Google Docs table to markdown.

        For 2-column "key-value" tables (where first column has short labels and
        second has long content), converts to heading/content sections which are
        better for LLM ingestion and RAG retrieval.

        For other tables, outputs standard markdown table format.

        Args:
            table: Table element from Docs API

        Returns:
            Markdown string (either sections or table format)
        """
        rows = []

        for row in table.get("tableRows", []):
            cells = []
            for cell in row.get("tableCells", []):
                # Extract text from all paragraphs in cell
                cell_text = []
                for content in cell.get("content", []):
                    if "paragraph" in content:
                        text = self._extract_paragraph_text(content["paragraph"], strip_images=True)
                        if text.strip():
                            cell_text.append(text.strip())

                # Join paragraphs and replace any newlines with spaces
                # (markdown table cells must stay on a single line)
                cell_content = " ".join(cell_text).replace("\n", " ")
                # Clean up any multiple consecutive spaces
                while "  " in cell_content:
                    cell_content = cell_content.replace("  ", " ")
                cells.append(cell_content)

            # Skip empty rows (all cells empty)
            if any(cell.strip() for cell in cells):
                rows.append(cells)

        if not rows:
            return ""

        # Detect key-value style tables: 2 columns where first column has short
        # labels (< 50 chars) and second column has longer content (avg > 100 chars)
        is_key_value_table = False
        if len(rows) > 1 and len(rows[0]) == 2:
            data_rows = rows[1:]  # Skip header
            if data_rows:
                first_col_max = max(len(r[0]) for r in data_rows) if data_rows else 0
                second_col_avg = (
                    sum(len(r[1]) for r in data_rows) / len(data_rows) if data_rows else 0
                )
                # First column short (labels), second column long (content)
                if first_col_max < 50 and second_col_avg > 100:
                    is_key_value_table = True

        if is_key_value_table:
            # Convert to heading/content sections for better LLM ingestion
            md_lines = []
            for row in rows[1:]:  # Skip header row
                key = row[0].strip().strip("*")  # Remove bold markers if present
                value = row[1].strip()
                if key and value:
                    md_lines.append(f"### {key}")
                    md_lines.append(value)
                    md_lines.append("")
            return "\n".join(md_lines)

        # Standard markdown table format
        md_lines = []

        # Header row
        md_lines.append("| " + " | ".join(rows[0]) + " |")
        # Separator
        md_lines.append("| " + " | ".join(["---"] * len(rows[0])) + " |")

        # Data rows
        for row in rows[1:]:
            md_lines.append("| " + " | ".join(row) + " |")

        return "\n".join(md_lines)

    def fetch_document(
        self, file_id: str, mime_type: Optional[str] = None, auto_detect_type: bool = True
    ) -> Optional[str]:
        """
        Fetch and parse a Google Drive document by file ID.

        Args:
            file_id: Google Drive file ID (from URL)
            mime_type: Optional MIME type (if known). If not provided and
                      auto_detect_type=True, will fetch metadata to determine type.
            auto_detect_type: If True and mime_type not provided, fetch metadata
                             to determine document type.

        Returns:
            Document content as plain text, or None if fetch fails

        Supported types:
            - Google Docs: exported as plain text
            - Google Sheets: exported as CSV
            - Google Slides: exported as plain text
            - PDFs: text extraction via PyPDF2
            - DOCX: text extraction via python-docx
            - Plain text: direct read
        """
        if not self.service:
            raise Exception("Service not initialized")

        # Auto-detect mime type if needed
        if mime_type is None and auto_detect_type:
            metadata = self.get_file_metadata(file_id)
            if not metadata:
                return None
            mime_type = metadata.get("mimeType")

        if not mime_type:
            raise ValueError("mime_type must be provided or auto_detect_type must be True")

        # Fetch content using appropriate method
        try:
            content, _ = self._download_file_content(file_id, mime_type)
            return content if content else None
        except Exception as e:
            print(f"Error fetching document {file_id}: {e}", file=sys.stderr)
            return None

    def _download_file_content(self, file_id: str, mime_type: str) -> Tuple[str, str]:
        """
        Download and extract text content from a file.

        Args:
            file_id: Google Drive file ID
            mime_type: File MIME type

        Returns:
            Tuple of (content_text, actual_mime_type_used)
        """
        try:
            # Google Docs - export as plain text
            if mime_type == "application/vnd.google-apps.document":
                request = self.service.files().export_media(fileId=file_id, mimeType="text/plain")
                content = request.execute().decode("utf-8")
                return content, "text/plain"

            # Google Sheets - export as CSV
            elif mime_type == "application/vnd.google-apps.spreadsheet":
                request = self.service.files().export_media(fileId=file_id, mimeType="text/csv")
                content = request.execute().decode("utf-8")
                return content, "text/csv"

            # Google Slides - export as plain text
            elif mime_type == "application/vnd.google-apps.presentation":
                request = self.service.files().export_media(fileId=file_id, mimeType="text/plain")
                content = request.execute().decode("utf-8")
                return content, "text/plain"

            # Regular files - download directly
            else:
                request = self.service.files().get_media(fileId=file_id)
                content_bytes = request.execute()

                # Try different decoding approaches based on mime type
                if mime_type == "application/pdf":
                    # Extract text from PDF
                    try:
                        import io

                        import PyPDF2

                        pdf_file = io.BytesIO(content_bytes)
                        pdf_reader = PyPDF2.PdfReader(pdf_file)
                        text_parts = []
                        for page in pdf_reader.pages:
                            text_parts.append(page.extract_text())
                        return "\n\n".join(text_parts), "application/pdf"
                    except ImportError:
                        print("  ⚠ PyPDF2 not available, skipping PDF", file=sys.stderr)
                        return "", mime_type
                    except Exception as e:
                        print(f"  ⚠ Error extracting PDF text: {e}", file=sys.stderr)
                        return "", mime_type

                elif (
                    mime_type
                    == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ):
                    # Extract text from DOCX
                    try:
                        import io

                        import docx

                        doc_file = io.BytesIO(content_bytes)
                        doc = docx.Document(doc_file)
                        text_parts = [para.text for para in doc.paragraphs]
                        return "\n\n".join(text_parts), mime_type
                    except ImportError:
                        print("  ⚠ python-docx not available, skipping DOCX", file=sys.stderr)
                        return "", mime_type
                    except Exception as e:
                        print(f"  ⚠ Error extracting DOCX text: {e}", file=sys.stderr)
                        return "", mime_type

                else:
                    # Assume text file
                    try:
                        return content_bytes.decode("utf-8"), mime_type
                    except UnicodeDecodeError:
                        return content_bytes.decode("utf-8", errors="ignore"), mime_type

        except Exception as e:
            print(f"  ✗ Error downloading file {file_id}: {str(e)}", file=sys.stderr)
            return "", mime_type


def parse_sections(content: str, start_section: Optional[str] = None) -> dict[str, str]:
    """
    Parse a Google Doc into sections based on markdown-style headers.

    Top-level sections are identified by lines starting with '# ' (H1 with space).
    Subheadings (##, ###, etc.) are included as part of the section content.
    Everything between two top-level headers becomes the content of that section.

    Content before the first header (or before start_section if specified) is ignored.

    Args:
        content: Full document content
        start_section: Optional section name to start from (e.g., 'system instructions')
                      Everything before this section is ignored

    Returns:
        Dictionary mapping section names to their content

    Example:
        content = '''
        Title Page Content

        # System Instructions

        ## Purpose
        Be helpful and professional.

        ## Tone
        Be empathetic.

        # QnA
        Q: What is X?
        A: X is Y.
        '''

        sections = parse_sections(content, start_section='system instructions')
        # Returns: {
        #   'system_instructions': '## Purpose\nBe helpful and professional.\n\n## Tone\nBe empathetic.',
        #   'qna': 'Q: What is X?\nA: X is Y.'
        # }
        # (Title page content is ignored, subheadings included in section content)
    """
    sections = {}
    current_section = None
    current_content: list[str] = []
    started = start_section is None  # Start immediately if no start_section specified

    for line in content.split("\n"):
        # Check if line is a top-level section header (starts with '# ' - H1 with space)
        # This excludes subheadings (##, ###, etc.)
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("##"):
            # Save previous section if exists
            if current_section and started:
                sections[current_section] = "\n".join(current_content).strip()

            # Start new section
            # Remove '# ' and convert to lowercase with underscores
            section_name = stripped[2:].strip()  # Remove '# ' prefix
            section_key = section_name.lower().replace(" ", "_")

            # Check if this is the start section we're waiting for
            if not started:
                if start_section and section_key == start_section.lower().replace(" ", "_"):
                    started = True
                    current_section = section_key
                    current_content = []
                # Skip this section if we haven't started yet
                continue
            else:
                current_section = section_key
                current_content = []
        else:
            # Add line to current section only if we've started
            # This includes subheadings (##, ###, etc.) and all content
            if current_section and started:
                current_content.append(line)

    # Save last section
    if current_section and started:
        sections[current_section] = "\n".join(current_content).strip()

    return sections


def fetch_google_doc(doc_id: str) -> Optional[str]:
    """
    Convenience function to fetch a Google Doc by ID.

    Args:
        doc_id: Google Drive document ID

    Returns:
        Document content as plain text, or None if fetch fails

    Example:
        from shared.utils.gdrive_doc_fetcher import fetch_google_doc
        content = fetch_google_doc('1abc123xyz456')
    """
    fetcher = GoogleDriveDocFetcher()
    return fetcher.fetch_document(doc_id, auto_detect_type=True)


def fetch_google_doc_sections(doc_id: str) -> Optional[dict[str, str]]:
    """
    Fetch a Google Doc and parse it into sections.

    Args:
        doc_id: Google Drive document ID

    Returns:
        Dictionary mapping section names to their content, or None if fetch fails

    Example:
        from shared.utils.gdrive_doc_fetcher import fetch_google_doc_sections
        sections = fetch_google_doc_sections('1abc123xyz456')

        if sections:
            system_instructions = sections.get('system_instructions', '')
            qna = sections.get('qna', '')
            examples = sections.get('example_conversations', '')
    """
    content = fetch_google_doc(doc_id)
    if not content:
        return None

    return parse_sections(content)


def fetch_google_doc_markdown(doc_id: str) -> Optional[str]:
    """
    Fetch a Google Doc with rich formatting converted to markdown.

    Preserves document structure (headings, bold, italic, etc.) and
    automatically strips title pages, headers/footers, and images.

    Args:
        doc_id: Google Drive document ID

    Returns:
        Document content as markdown, or None if fetch fails

    Example:
        from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown
        markdown = fetch_google_doc_markdown('1abc123xyz456')

        if markdown:
            print(markdown)
    """
    fetcher = GoogleDriveDocFetcher()
    return fetcher.fetch_document_with_formatting(doc_id)


def fetch_google_doc_markdown_sections(
    doc_id: str, start_section: str = "system instructions"
) -> Optional[dict[str, str]]:
    """
    Fetch a Google Doc with formatting and parse into sections.

    Combines markdown conversion with section parsing for structured documents.
    Automatically strips title pages, headers/footers, and images.
    Everything before the start_section is also automatically ignored.

    Args:
        doc_id: Google Drive document ID
        start_section: Section name to start from (default: 'system instructions')
                      Everything before this section is ignored

    Returns:
        Dictionary mapping section names to their content, or None if fetch fails

    Example:
        from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown_sections
        sections = fetch_google_doc_markdown_sections('1abc123xyz456')

        if sections:
            # Only sections from "System Instructions" onward are included
            # (Title page and any content before it is automatically ignored)
            system_instructions = sections.get('system_instructions', '')
            qna = sections.get('qna_knowledge_base', '')
    """
    markdown = fetch_google_doc_markdown(doc_id)

    if not markdown:
        return None

    return parse_sections(markdown, start_section=start_section)


if __name__ == "__main__":
    """
    Test the fetcher with a document ID.

    Usage:
        python -m shared.utils.gdrive_doc_fetcher <doc_id>
    """
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m shared.utils.gdrive_doc_fetcher <doc_id>")
        sys.exit(1)

    doc_id = sys.argv[1]
    print(f"Fetching document: {doc_id}")

    try:
        content = fetch_google_doc(doc_id)
        if content:
            print(f"\n✓ Successfully fetched {len(content)} characters")
            print("\nFirst 500 characters:")
            print("-" * 80)
            print(content[:500])
            print("-" * 80)
        else:
            print("\n✗ Failed to fetch document")
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback

        traceback.print_exc()
