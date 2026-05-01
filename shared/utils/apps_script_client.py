#!/usr/bin/env python3
"""
Anansi Apps Script Client

Generic client for calling the Anansi Helper Google Apps Script.
Provides a single interface for all Apps Script operations.

Usage:
    from shared.utils.apps_script_client import AnansiAppsScriptClient

    client = AnansiAppsScriptClient()
    result = await client.call_action("ping", {"message": "hello"})

    # Or use convenience functions:
    from shared.utils.apps_script_client import replace_sheet_image
    result = await replace_sheet_image(sheet_id, worksheet, image_b64)

Environment Variables:
    ANANSI_HELPER_DEPLOYMENT_ID: Apps Script deployment ID
    ANANSI_HELPER_API_KEY: API key for the endpoint

Migration Note:
    This client replaces the previous environment variables:
    - GSHEET_IMAGE_DEPLOYMENT_ID -> ANANSI_HELPER_DEPLOYMENT_ID
    - GSHEET_IMAGE_API_KEY -> ANANSI_HELPER_API_KEY

    For backwards compatibility, the old env vars are still checked as fallbacks.
"""

import base64
import io
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass
class AppsScriptResult:
    """Result from an Apps Script action call."""

    success: bool
    action: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    available_actions: Optional[list[str]] = None


class AnansiAppsScriptClient:
    """
    Generic client for calling the Anansi Helper Apps Script.

    Provides a unified interface for all Apps Script operations, routing
    requests via the 'action' parameter to the appropriate handler.

    Example:
        client = AnansiAppsScriptClient()

        # Ping (health check)
        result = await client.call_action("ping", {"message": "test"})

        # List worksheets
        result = await client.call_action("list_worksheets", {
            "sheet_id": "1abc123..."
        })

        # Get images in a sheet
        result = await client.call_action("get_sheet_images", {
            "sheet_id": "1abc123...",
            "worksheet_name": "Sheet1"
        })

        # Replace an image
        result = await client.call_action("replace_sheet_image", {
            "sheet_id": "1abc123...",
            "worksheet_name": "Sheet1",
            "image_base64": "iVBORw0KGgo...",
            "min_height": 100
        })
    """

    DEFAULT_TIMEOUT = 60.0  # Apps Script operations can be slow

    def __init__(
        self,
        deployment_id: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        """
        Initialize the Apps Script client.

        Args:
            deployment_id: Apps Script deployment ID (defaults to env var)
            api_key: API key for authentication (defaults to env var)
        """
        # Support both new and legacy env var names for migration
        self.deployment_id = deployment_id or os.getenv(
            "ANANSI_HELPER_DEPLOYMENT_ID",
            os.getenv("GSHEET_IMAGE_DEPLOYMENT_ID"),  # Fallback for migration
        )
        self.api_key = api_key or os.getenv(
            "ANANSI_HELPER_API_KEY",
            os.getenv("GSHEET_IMAGE_API_KEY"),  # Fallback for migration
        )

    def _get_endpoint_url(self) -> str:
        """Get the Apps Script web app URL."""
        if not self.deployment_id:
            raise ValueError("ANANSI_HELPER_DEPLOYMENT_ID environment variable not set")
        return f"https://script.google.com/macros/s/{self.deployment_id}/exec"

    async def call_action(
        self,
        action: str,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> AppsScriptResult:
        """
        Call an action on the Anansi Helper Apps Script.

        Args:
            action: The action to perform (e.g., "replace_sheet_image")
            params: Parameters for the action
            timeout: Request timeout in seconds (defaults to 60)

        Returns:
            AppsScriptResult with success status and data/error
        """
        if not self.deployment_id:
            return AppsScriptResult(
                success=False,
                action=action,
                error_message="ANANSI_HELPER_DEPLOYMENT_ID not configured",
            )

        if not self.api_key:
            return AppsScriptResult(
                success=False,
                action=action,
                error_message="ANANSI_HELPER_API_KEY not configured",
            )

        url = self._get_endpoint_url()
        payload = {
            "api_key": self.api_key,
            "action": action,
            "params": params or {},
        }

        request_timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT

        try:
            async with httpx.AsyncClient(timeout=request_timeout, follow_redirects=True) as client:
                logger.info(f"Calling Apps Script action: {action}")
                response = await client.post(url, json=payload)

                if response.status_code != 200:
                    logger.error(
                        f"Apps Script returned HTTP {response.status_code}: {response.text}"
                    )
                    return AppsScriptResult(
                        success=False,
                        action=action,
                        error_message=f"HTTP {response.status_code}: {response.text}",
                    )

                result = response.json()

                if not result.get("success"):
                    error = result.get("error", "Unknown error from Apps Script")
                    logger.warning(f"Apps Script action '{action}' failed: {error}")
                    return AppsScriptResult(
                        success=False,
                        action=action,
                        error_message=error,
                        available_actions=result.get("available_actions"),
                    )

                logger.info(f"Apps Script action '{action}' succeeded")
                return AppsScriptResult(
                    success=True,
                    action=result.get("action", action),
                    data=result.get("data"),
                )

        except httpx.TimeoutException:
            logger.error(f"Apps Script request timed out for action: {action}")
            return AppsScriptResult(
                success=False,
                action=action,
                error_message="Request timed out (Apps Script operations can be slow)",
            )
        except httpx.RequestError as e:
            logger.error(f"HTTP request error for action {action}: {e}")
            return AppsScriptResult(
                success=False,
                action=action,
                error_message=f"Request error: {str(e)}",
            )
        except Exception as e:
            logger.error(f"Unexpected error for action {action}: {e}")
            return AppsScriptResult(
                success=False,
                action=action,
                error_message=f"Unexpected error: {str(e)}",
            )


# === IMAGE UTILITIES ===

# Google Sheets has a 1 million pixel limit for inserted images
MAX_PIXELS = 1_000_000


def resize_image_for_sheets(image_base64: str) -> str:
    """
    Resize an image if it exceeds Google Sheets' 1 million pixel limit.

    Args:
        image_base64: Base64-encoded image (with or without data URI prefix)

    Returns:
        Base64-encoded image, resized if necessary (no data URI prefix)
    """
    # Strip data URI prefix if present
    if "," in image_base64:
        image_base64 = image_base64.split(",")[-1]

    # Decode and open image
    image_data = base64.b64decode(image_base64)
    img = Image.open(io.BytesIO(image_data))

    width, height = img.size
    total_pixels = width * height

    if total_pixels <= MAX_PIXELS:
        logger.debug(f"Image {width}x{height} ({total_pixels:,} pixels) is within limit")
        return image_base64

    # Calculate scale factor to fit within limit
    scale = (MAX_PIXELS / total_pixels) ** 0.5
    new_width = int(width * scale)
    new_height = int(height * scale)

    logger.info(
        f"Resizing image from {width}x{height} ({total_pixels:,} pixels) "
        f"to {new_width}x{new_height} ({new_width * new_height:,} pixels)"
    )

    # Resize with high-quality resampling
    resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

    # Encode back to base64, preserving format
    output = io.BytesIO()
    img_format = img.format or "PNG"
    resized.save(output, format=img_format, quality=90)
    output.seek(0)

    return base64.b64encode(output.read()).decode()


# === CONVENIENCE FUNCTIONS ===


async def ping(message: Optional[str] = None) -> AppsScriptResult:
    """
    Ping the Apps Script to check connectivity.

    Args:
        message: Optional message to echo back

    Returns:
        AppsScriptResult with pong response
    """
    client = AnansiAppsScriptClient()
    params = {}
    if message:
        params["message"] = message
    return await client.call_action("ping", params)


async def list_worksheets(sheet_id: str) -> AppsScriptResult:
    """
    List all worksheets in a Google Spreadsheet.

    Args:
        sheet_id: Google Sheets spreadsheet ID

    Returns:
        AppsScriptResult with worksheet list in data
    """
    client = AnansiAppsScriptClient()
    return await client.call_action("list_worksheets", {"sheet_id": sheet_id})


async def get_sheet_images(sheet_id: str, worksheet_name: str) -> AppsScriptResult:
    """
    Get all images in a worksheet with their properties.

    Useful for debugging image replacement issues.

    Args:
        sheet_id: Google Sheets spreadsheet ID
        worksheet_name: Name of the worksheet/tab

    Returns:
        AppsScriptResult with image list in data
    """
    client = AnansiAppsScriptClient()
    return await client.call_action(
        "get_sheet_images",
        {"sheet_id": sheet_id, "worksheet_name": worksheet_name},
    )


async def replace_sheet_image(
    sheet_id: str,
    worksheet_name: str,
    image_base64: str,
    min_height_px: Optional[int] = None,
) -> AppsScriptResult:
    """
    Replace an over-cell image in a Google Sheet.

    Finds the first image with height >= min_height_px and replaces it
    with the provided image, preserving vertical position and centering
    horizontally.

    Args:
        sheet_id: Google Sheets spreadsheet ID
        worksheet_name: Name of the worksheet/tab containing the image
        image_base64: Base64-encoded PNG image data (no data URI prefix)
        min_height_px: Minimum image height to match (default: 100)

    Returns:
        AppsScriptResult with dimension info in data

    Example:
        import base64

        with open('chart.png', 'rb') as f:
            image_b64 = base64.b64encode(f.read()).decode()

        result = await replace_sheet_image(
            sheet_id="1abc123xyz",
            worksheet_name="Sheet1",
            image_base64=image_b64,
        )

        if result.success:
            print(f"New size: {result.data['new_width']}x{result.data['new_height']}")
    """
    # Resize image if it exceeds Google Sheets' pixel limit
    resized_image = resize_image_for_sheets(image_base64)

    client = AnansiAppsScriptClient()
    params: dict[str, Any] = {
        "sheet_id": sheet_id,
        "worksheet_name": worksheet_name,
        "image_base64": resized_image,
    }
    if min_height_px is not None:
        params["min_height"] = min_height_px

    return await client.call_action("replace_sheet_image", params)


async def write_doc_markdown(
    doc_id: str,
    target_text: str,
    markdown: str,
) -> AppsScriptResult:
    """
    Write formatted markdown content to a Google Doc section via Apps Script.

    Finds the target_text in the document, removes it, and inserts
    markdown-formatted content at the same position. The markdown is
    parsed and rendered by Apps Script using DocumentApp methods.

    Args:
        doc_id: Google Doc file ID
        target_text: Exact text to find and replace in the doc
        markdown: Raw markdown string (parsed by Apps Script)

    Returns:
        AppsScriptResult with elements_written count in data
    """
    client = AnansiAppsScriptClient()
    return await client.call_action(
        "write_doc_markdown",
        {
            "doc_id": doc_id,
            "target_text": target_text,
            "markdown": markdown,
        },
        timeout=120.0,  # Complex formatting may take time
    )


if __name__ == "__main__":
    """
    Test the Apps Script client from command line.

    Usage:
        python -m shared.utils.apps_script_client ping
        python -m shared.utils.apps_script_client list_worksheets <sheet_id>
        python -m shared.utils.apps_script_client get_images <sheet_id> <worksheet>
        python -m shared.utils.apps_script_client replace <sheet_id> <worksheet> <image_path>
    """
    import asyncio
    import sys

    async def main():
        if len(sys.argv) < 2:
            print("Usage:")
            print("  python -m shared.utils.apps_script_client ping [message]")
            print("  python -m shared.utils.apps_script_client list_worksheets <sheet_id>")
            print("  python -m shared.utils.apps_script_client get_images <sheet_id> <worksheet>")
            print(
                "  python -m shared.utils.apps_script_client replace <sheet_id> <worksheet> <image_path>"
            )
            print()
            print("Environment variables:")
            print("  ANANSI_HELPER_DEPLOYMENT_ID  Apps Script deployment ID")
            print("  ANANSI_HELPER_API_KEY        Apps Script API key")
            sys.exit(1)

        command = sys.argv[1]

        if command == "ping":
            message = sys.argv[2] if len(sys.argv) > 2 else None
            result = await ping(message)
            if result.success:
                print(f"Success: {result.data}")
            else:
                print(f"Failed: {result.error_message}")

        elif command == "list_worksheets":
            if len(sys.argv) < 3:
                print("Usage: python -m shared.utils.apps_script_client list_worksheets <sheet_id>")
                sys.exit(1)
            sheet_id = sys.argv[2]
            result = await list_worksheets(sheet_id)
            if result.success:
                print(f"Spreadsheet: {result.data['spreadsheet_name']}")
                print("Worksheets:")
                for ws in result.data["worksheets"]:
                    print(f"  - {ws['name']} ({ws['row_count']} rows, {ws['column_count']} cols)")
            else:
                print(f"Failed: {result.error_message}")

        elif command == "get_images":
            if len(sys.argv) < 4:
                print(
                    "Usage: python -m shared.utils.apps_script_client get_images <sheet_id> <worksheet>"
                )
                sys.exit(1)
            sheet_id = sys.argv[2]
            worksheet = sys.argv[3]
            result = await get_sheet_images(sheet_id, worksheet)
            if result.success:
                print(f"Found {result.data['image_count']} images:")
                for img in result.data["images"]:
                    print(
                        f"  [{img['index']}] {img['width']}x{img['height']} at {img['anchor_cell']}"
                    )
            else:
                print(f"Failed: {result.error_message}")

        elif command == "replace":
            if len(sys.argv) < 5:
                print(
                    "Usage: python -m shared.utils.apps_script_client replace "
                    "<sheet_id> <worksheet> <image_path>"
                )
                sys.exit(1)
            sheet_id = sys.argv[2]
            worksheet = sys.argv[3]
            image_path = sys.argv[4]

            try:
                with open(image_path, "rb") as f:
                    image_b64 = base64.b64encode(f.read()).decode()
            except FileNotFoundError:
                print(f"Error: Image file not found: {image_path}")
                sys.exit(1)

            print(f"Replacing image in {sheet_id}/{worksheet}...")
            result = await replace_sheet_image(sheet_id, worksheet, image_b64)
            if result.success:
                data = result.data
                print(
                    f"Success: {data['original_width']}x{data['original_height']} -> "
                    f"{data['new_width']}x{data['new_height']}"
                )
            else:
                print(f"Failed: {result.error_message}")

        else:
            print(f"Unknown command: {command}")
            sys.exit(1)

    asyncio.run(main())
