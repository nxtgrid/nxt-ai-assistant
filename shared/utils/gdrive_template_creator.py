#!/usr/bin/env python3
"""
Google Drive Template Document Creator

Creates documents from Google Doc/Sheet templates with placeholder replacement
and optional document code registration via Apps Script.

Flow:
    Template: "[Site Name] Monthly Report - [Date]"
         ↓
    Copy to output folder
         ↓
    Replace placeholders → "ExampleGrid Monthly Report - 2026-01"
         ↓
    Call Apps Script (optional)
         ↓
    Final: "DOC-0042 - ExampleGrid Monthly Report - 2026-01"

Usage:
    from shared.utils.gdrive_template_creator import create_from_template

    result = await create_from_template(
        template_id="1abc123xyz",
        output_folder_id="your-output-folder-id",
        variables={
            "site_name": "ExampleGrid",
            "date": "January 2026",
        },
    )

    if result.success:
        print(f"Created: {result.final_title}")
        print(f"URL: {result.document_url}")
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, Optional

import httpx

from shared.utils.google_auth import get_drive_write_credentials

logger = logging.getLogger(__name__)


@dataclass
class DocumentCreationResult:
    """Result from creating a document from template."""

    document_id: str
    document_url: str
    final_title: str
    template_type: str  # "document" or "spreadsheet"
    success: bool
    error_message: Optional[str] = None


class GoogleTemplateCreator:
    """
    Creates documents from Google Doc/Sheet templates.

    Features:
    - Copies template to output folder
    - Replaces [placeholder] syntax in title
    - Optionally registers with an Apps Script doc-tracker for document codes
    """

    # MIME type to template type mapping
    MIME_TYPE_MAP = {
        "application/vnd.google-apps.document": "document",
        "application/vnd.google-apps.spreadsheet": "spreadsheet",
    }

    def __init__(self):
        """Initialize the template creator with Google Drive service."""
        self.service = None
        self._initialize_service()

    def _initialize_service(self):
        """Initialize Google Drive API service with write access."""
        try:
            from googleapiclient.discovery import build

            credentials = get_drive_write_credentials()
            self.service = build("drive", "v3", credentials=credentials)
        except ImportError as e:
            raise Exception(
                f"Required packages not available: {str(e)}. "
                f"Install with: pip install google-auth google-api-python-client"
            )
        except Exception as e:
            raise Exception(f"Failed to initialize Google Drive service: {str(e)}")

    def get_template_metadata(self, template_id: str) -> Optional[Dict]:
        """
        Fetch template name and MIME type.

        Args:
            template_id: Google Drive file ID

        Returns:
            Dict with 'name' and 'mimeType', or None if not found
        """
        if not self.service:
            raise Exception("Service not initialized")

        try:
            metadata = (
                self.service.files()
                .get(fileId=template_id, fields="id, name, mimeType", supportsAllDrives=True)
                .execute()
            )
            return dict(metadata)
        except Exception as e:
            logger.error(f"Error fetching template metadata for {template_id}: {e}")
            return None

    def replace_title_placeholders(self, title: str, variables: Dict[str, str]) -> str:
        """
        Replace [placeholder] syntax in title with variable values.

        Matching is case-insensitive. Placeholders use underscores or spaces.
        Unreplaced placeholders are left as-is for visibility.
        Common template suffixes like " - TEMPLATE" are automatically removed.

        Args:
            title: Original title with placeholders like "[Site Name]"
            variables: Dict mapping placeholder names to values

        Returns:
            Title with placeholders replaced and template suffix removed

        Example:
            title = "[Site Name] Report - [Date] - TEMPLATE"
            variables = {"site_name": "ExampleGrid", "date": "January 2026"}
            result = "ExampleGrid Report - January 2026"
        """
        result = title

        # Build a lookup dict with normalized keys (lowercase, underscores)
        normalized_vars = {}
        for key, value in variables.items():
            normalized_key = key.lower().replace(" ", "_")
            normalized_vars[normalized_key] = value

        # Find all placeholders in title
        pattern = r"\[([^\]]+)\]"
        matches = re.findall(pattern, title)

        for match in matches:
            # Normalize the placeholder name
            normalized_match = match.lower().replace(" ", "_")

            if normalized_match in normalized_vars:
                # Replace [placeholder] with value
                result = result.replace(f"[{match}]", normalized_vars[normalized_match])

        # Remove common template suffixes (case-insensitive)
        template_suffixes = [
            " - TEMPLATE",
            " - Template",
            " - template",
            " (TEMPLATE)",
            " (Template)",
        ]
        for suffix in template_suffixes:
            if result.endswith(suffix):
                result = result[: -len(suffix)]
                break

        return result

    def copy_template(self, template_id: str, folder_id: str, title: str) -> Optional[Dict]:
        """
        Copy template file to output folder with new title.

        Args:
            template_id: Source template file ID
            folder_id: Destination folder ID
            title: Title for the new document

        Returns:
            Dict with new file metadata (id, name, mimeType), or None on error
        """
        if not self.service:
            raise Exception("Service not initialized")

        try:
            copy_metadata = {"name": title, "parents": [folder_id]}

            copied_file = (
                self.service.files()
                .copy(
                    fileId=template_id,
                    body=copy_metadata,
                    fields="id, name, mimeType",
                    supportsAllDrives=True,
                )
                .execute()
            )

            logger.info(
                f"Copied template to: {copied_file.get('name')} (ID: {copied_file.get('id')})"
            )
            return dict(copied_file)

        except Exception as e:
            logger.error(f"Error copying template {template_id}: {e}")
            return None

    async def register_with_doc_tracker(self, document_id: str, title: str) -> bool:
        """
        Call the Apps Script doc-tracker to register a document and get a code prefix.

        The Apps Script:
        1. Gets next document code from tracking spreadsheet (e.g., "DOC-0042")
        2. Renames the document to "{code} - {title}"
        3. Returns "Processed."

        Configure via env vars:
          DOC_TRACKER_DEPLOYMENT_ID — Apps Script web app deployment ID
          DOC_TRACKER_API_KEY       — API key set in Script Properties

        Args:
            document_id: Google Drive file ID
            title: Current document title

        Returns:
            True if registration succeeded, False otherwise
        """
        deployment_id = os.getenv("DOC_TRACKER_DEPLOYMENT_ID")
        api_key = os.getenv("DOC_TRACKER_API_KEY")

        if not deployment_id or not api_key:
            logger.warning("DOC_TRACKER_DEPLOYMENT_ID or DOC_TRACKER_API_KEY not configured")
            return False

        url = f"https://script.google.com/macros/s/{deployment_id}/exec"
        params = {
            "api_key": api_key,
            "document_id": document_id,
            "title": title,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url, params=params)

                if response.status_code == 200:
                    # Apps Script returns "Processed." on success
                    text = response.text.strip()
                    if text == "Processed.":
                        logger.info(f"Doc-tracker registration successful for {document_id}")
                        return True
                    else:
                        logger.warning(f"Unexpected doc-tracker response: {text}")
                        return False
                else:
                    logger.error(f"Doc-tracker registration failed: HTTP {response.status_code}")
                    return False

        except httpx.TimeoutException:
            logger.error("Doc-tracker registration timed out")
            return False
        except Exception as e:
            logger.error(f"Doc-tracker registration error: {e}")
            return False

    def get_final_document_metadata(self, document_id: str) -> Optional[Dict]:
        """
        Re-fetch document metadata after Apps Script may have renamed it.

        Args:
            document_id: Google Drive file ID

        Returns:
            Dict with 'id', 'name', 'mimeType', 'webViewLink', or None on error
        """
        if not self.service:
            raise Exception("Service not initialized")

        try:
            metadata = (
                self.service.files()
                .get(
                    fileId=document_id,
                    fields="id, name, mimeType, webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return dict(metadata)
        except Exception as e:
            logger.error(f"Error fetching final metadata for {document_id}: {e}")
            return None

    async def create_from_template(
        self,
        template_id: str,
        output_folder_id: str,
        variables: Dict[str, str],
        title_override: Optional[str] = None,
        register_with_doc_tracker: bool = True,
    ) -> DocumentCreationResult:
        """
        Create a document from template with placeholder replacement.

        Args:
            template_id: Google Doc/Sheet template file ID
            output_folder_id: Destination folder ID
            variables: Placeholder values like {"site_name": "ExampleGrid", "date": "2026-01"}
            title_override: Skip placeholder replacement, use this title exactly
            register_with_doc_tracker: Call Apps Script to add document code prefix

        Returns:
            DocumentCreationResult with document details
        """
        # Get template metadata
        template_meta = self.get_template_metadata(template_id)
        if not template_meta:
            return DocumentCreationResult(
                document_id="",
                document_url="",
                final_title="",
                template_type="",
                success=False,
                error_message=f"Template not found: {template_id}",
            )

        mime_type = template_meta.get("mimeType", "")
        template_type = self.MIME_TYPE_MAP.get(mime_type, "unknown")

        if template_type == "unknown":
            return DocumentCreationResult(
                document_id="",
                document_url="",
                final_title="",
                template_type="",
                success=False,
                error_message=f"Unsupported template type: {mime_type}",
            )

        # Determine title
        if title_override:
            new_title = title_override
        else:
            original_title = template_meta.get("name", "Untitled")
            new_title = self.replace_title_placeholders(original_title, variables)

        # Copy template to output folder
        copied_file = self.copy_template(template_id, output_folder_id, new_title)
        if not copied_file:
            return DocumentCreationResult(
                document_id="",
                document_url="",
                final_title="",
                template_type=template_type,
                success=False,
                error_message="Failed to copy template to output folder",
            )

        document_id = copied_file.get("id", "")

        # Register with doc-tracker Apps Script if enabled
        if register_with_doc_tracker:
            tracker_success = await self.register_with_doc_tracker(document_id, new_title)
            if not tracker_success:
                logger.warning(
                    f"Doc-tracker registration failed for {document_id}, document still created"
                )

        # Fetch final metadata (title may have changed after doc-tracker registration)
        final_meta = self.get_final_document_metadata(document_id)
        if final_meta:
            final_title = final_meta.get("name", new_title)
            document_url = final_meta.get(
                "webViewLink", f"https://docs.google.com/document/d/{document_id}"
            )
        else:
            final_title = new_title
            document_url = f"https://docs.google.com/document/d/{document_id}"

        return DocumentCreationResult(
            document_id=document_id,
            document_url=document_url,
            final_title=final_title,
            template_type=template_type,
            success=True,
        )


# Convenience function for direct use
async def create_from_template(
    template_id: str,
    output_folder_id: str,
    variables: Dict[str, str],
    title_override: Optional[str] = None,
    register_with_doc_tracker: bool = True,
) -> DocumentCreationResult:
    """
    Create a document from a Google Doc/Sheet template.

    This is a convenience function that creates a GoogleTemplateCreator instance
    and calls create_from_template on it.

    Args:
        template_id: Google Doc/Sheet template file ID
        output_folder_id: Destination folder ID
        variables: Placeholder values like {"site_name": "ExampleGrid", "date": "2026-01"}
        title_override: Skip placeholder replacement, use this title exactly
        register_with_doc_tracker: Call Apps Script to add document code prefix

    Returns:
        DocumentCreationResult with document details

    Example:
        result = await create_from_template(
            template_id="1abc123xyz",
            output_folder_id="your-output-folder-id",
            variables={"site_name": "ExampleGrid", "date": "January 2026"},
        )

        if result.success:
            print(f"Created: {result.final_title}")
            print(f"URL: {result.document_url}")
    """
    creator = GoogleTemplateCreator()
    return await creator.create_from_template(
        template_id=template_id,
        output_folder_id=output_folder_id,
        variables=variables,
        title_override=title_override,
        register_with_doc_tracker=register_with_doc_tracker,
    )


if __name__ == "__main__":
    """
    Test the template creator with command line arguments.

    Usage:
        python -m shared.utils.gdrive_template_creator <template_id> <folder_id> [--no-tracker]

    Example:
        python -m shared.utils.gdrive_template_creator 1abc123xyz your-output-folder-id
    """
    import asyncio
    import sys

    if len(sys.argv) < 3:
        print(
            "Usage: python -m shared.utils.gdrive_template_creator <template_id> <folder_id> [--no-tracker]"
        )
        print()
        print("Arguments:")
        print("  template_id    Google Drive template file ID")
        print("  folder_id      Destination folder ID")
        print("  --no-tracker   Skip Apps Script doc-tracker registration")
        print()
        print("Environment variables:")
        print("  DOC_TRACKER_DEPLOYMENT_ID  Apps Script deployment ID")
        print("  DOC_TRACKER_API_KEY        Apps Script API key")
        sys.exit(1)

    template_id = sys.argv[1]
    folder_id = sys.argv[2]
    use_tracker = "--no-tracker" not in sys.argv

    # Test variables
    test_variables = {
        "site_name": "Test Site",
        "date": "2026-01",
        "customer": "Test Customer",
    }

    async def main():
        print(f"Creating document from template: {template_id}")
        print(f"Output folder: {folder_id}")
        print(f"Variables: {test_variables}")
        print(f"Doc-tracker registration: {'enabled' if use_tracker else 'disabled'}")
        print()

        result = await create_from_template(
            template_id=template_id,
            output_folder_id=folder_id,
            variables=test_variables,
            register_with_doc_tracker=use_tracker,
        )

        if result.success:
            print("Success!")
            print(f"  Document ID: {result.document_id}")
            print(f"  Final Title: {result.final_title}")
            print(f"  Type: {result.template_type}")
            print(f"  URL: {result.document_url}")
        else:
            print(f"Failed: {result.error_message}")
            sys.exit(1)

    asyncio.run(main())
