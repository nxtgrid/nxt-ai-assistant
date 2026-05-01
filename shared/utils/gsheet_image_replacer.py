#!/usr/bin/env python3
"""
Google Sheet Image Replacer

Replaces over-cell images in Google Sheets while preserving position and centering.
Uses the Anansi Helper Google Apps Script to perform the actual image manipulation
since the Sheets REST API cannot insert over-cell images.

Flow:
    1. Python receives image as base64
    2. Calls Apps Script with sheet_id, worksheet, and image data
    3. Apps Script finds largest image (by height)
    4. Replaces image content, sets same height, centers horizontally
    5. Returns result with dimensions

Usage:
    from shared.utils.gsheet_image_replacer import replace_sheet_image

    result = await replace_sheet_image(
        sheet_id="1abc123xyz",
        worksheet_name="Sheet1",
        image_base64=base64_encoded_png,
    )

    if result.success:
        print(f"Replaced image: {result.new_width}x{result.new_height}")

Note:
    This module is a thin wrapper around shared.utils.apps_script_client for
    backwards compatibility. New code should use apps_script_client directly.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from shared.utils.apps_script_client import AnansiAppsScriptClient

logger = logging.getLogger(__name__)


@dataclass
class ImageReplacementResult:
    """Result from replacing an image in a Google Sheet."""

    success: bool
    sheet_id: str
    worksheet_name: str
    original_width: Optional[int] = None
    original_height: Optional[int] = None
    new_width: Optional[int] = None
    new_height: Optional[int] = None
    error_message: Optional[str] = None


class GoogleSheetImageReplacer:
    """
    Replaces over-cell images in Google Sheets.

    Uses the Anansi Helper Apps Script to perform the actual image manipulation,
    since the Sheets REST API cannot insert over-cell images.

    Environment Variables (supports both new and legacy names):
        ANANSI_HELPER_DEPLOYMENT_ID or GSHEET_IMAGE_DEPLOYMENT_ID
        ANANSI_HELPER_API_KEY or GSHEET_IMAGE_API_KEY
        GSHEET_IMAGE_MIN_HEIGHT: Minimum image height to match (default: 100)
    """

    DEFAULT_MIN_HEIGHT = 100

    def __init__(self):
        """Initialize the image replacer."""
        self._client = AnansiAppsScriptClient()
        self.default_min_height = int(
            os.getenv("GSHEET_IMAGE_MIN_HEIGHT", str(self.DEFAULT_MIN_HEIGHT))
        )

    async def replace_image(
        self,
        sheet_id: str,
        worksheet_name: str,
        image_base64: str,
        min_height_px: Optional[int] = None,
    ) -> ImageReplacementResult:
        """
        Replace an over-cell image in a Google Sheet.

        Finds the first image with height >= min_height_px and replaces it
        with the provided image, preserving vertical position and centering
        horizontally.

        Args:
            sheet_id: Google Sheets spreadsheet ID
            worksheet_name: Name of the worksheet/tab containing the image
            image_base64: Base64-encoded PNG image data (no data URI prefix)
            min_height_px: Minimum image height to match (defaults to env var or 100)

        Returns:
            ImageReplacementResult with success status and dimensions
        """
        min_height = min_height_px if min_height_px is not None else self.default_min_height

        params = {
            "sheet_id": sheet_id,
            "worksheet_name": worksheet_name,
            "image_base64": image_base64,
            "min_height": min_height,
        }

        logger.info(f"Replacing image in {sheet_id}/{worksheet_name}")
        result = await self._client.call_action("replace_sheet_image", params)

        if not result.success:
            return ImageReplacementResult(
                success=False,
                sheet_id=sheet_id,
                worksheet_name=worksheet_name,
                error_message=result.error_message,
            )

        data = result.data or {}
        logger.info(
            f"Image replaced successfully: "
            f"{data.get('original_width')}x{data.get('original_height')} -> "
            f"{data.get('new_width')}x{data.get('new_height')}"
        )

        return ImageReplacementResult(
            success=True,
            sheet_id=sheet_id,
            worksheet_name=worksheet_name,
            original_width=data.get("original_width"),
            original_height=data.get("original_height"),
            new_width=data.get("new_width"),
            new_height=data.get("new_height"),
        )


# Convenience function for direct use
async def replace_sheet_image(
    sheet_id: str,
    worksheet_name: str,
    image_base64: str,
    min_height_px: Optional[int] = None,
) -> ImageReplacementResult:
    """
    Replace an over-cell image in a Google Sheet.

    This is a convenience function that creates a GoogleSheetImageReplacer instance
    and calls replace_image on it.

    Args:
        sheet_id: Google Sheets spreadsheet ID
        worksheet_name: Name of the worksheet/tab containing the image
        image_base64: Base64-encoded PNG image data (no data URI prefix)
        min_height_px: Minimum image height to match (defaults to env var or 100)

    Returns:
        ImageReplacementResult with success status and dimensions

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
            print(f"Original size: {result.original_width}x{result.original_height}")
            print(f"New size: {result.new_width}x{result.new_height}")
    """
    replacer = GoogleSheetImageReplacer()
    return await replacer.replace_image(
        sheet_id=sheet_id,
        worksheet_name=worksheet_name,
        image_base64=image_base64,
        min_height_px=min_height_px,
    )


if __name__ == "__main__":
    """
    Test the image replacer with command line arguments.

    Usage:
        python -m shared.utils.gsheet_image_replacer <sheet_id> <worksheet_name> <image_path>

    Example:
        python -m shared.utils.gsheet_image_replacer 1abc123xyz Sheet1 ./chart.png
    """
    import asyncio
    import base64
    import sys

    if len(sys.argv) < 4:
        print(
            "Usage: python -m shared.utils.gsheet_image_replacer "
            "<sheet_id> <worksheet_name> <image_path>"
        )
        print()
        print("Arguments:")
        print("  sheet_id        Google Sheets spreadsheet ID")
        print("  worksheet_name  Name of the worksheet/tab")
        print("  image_path      Path to PNG image file")
        print()
        print("Environment variables:")
        print("  ANANSI_HELPER_DEPLOYMENT_ID  Apps Script deployment ID (preferred)")
        print("  ANANSI_HELPER_API_KEY        Apps Script API key (preferred)")
        print("  GSHEET_IMAGE_DEPLOYMENT_ID   Legacy deployment ID (fallback)")
        print("  GSHEET_IMAGE_API_KEY         Legacy API key (fallback)")
        print("  GSHEET_IMAGE_MIN_HEIGHT      Minimum image height (default: 100)")
        sys.exit(1)

    sheet_id = sys.argv[1]
    worksheet_name = sys.argv[2]
    image_path = sys.argv[3]

    # Read and encode image
    try:
        with open(image_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode()
    except FileNotFoundError:
        print(f"Error: Image file not found: {image_path}")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading image file: {e}")
        sys.exit(1)

    async def main():
        print(f"Replacing image in sheet: {sheet_id}")
        print(f"Worksheet: {worksheet_name}")
        print(f"Image: {image_path}")
        print()

        result = await replace_sheet_image(
            sheet_id=sheet_id,
            worksheet_name=worksheet_name,
            image_base64=image_base64,
        )

        if result.success:
            print("Success!")
            print(f"  Original size: {result.original_width}x{result.original_height}")
            print(f"  New size: {result.new_width}x{result.new_height}")
        else:
            print(f"Failed: {result.error_message}")
            sys.exit(1)

    asyncio.run(main())
