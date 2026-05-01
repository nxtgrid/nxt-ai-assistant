"""Create per-site subfolder in Google Drive for LPP outputs.

Runs after resolve_sites (site_name available in state) and before
copy_lpp_template, so the spreadsheet and all subsequent file uploads
land in an organized folder structure.
"""

import asyncio
import os

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.drive_upload import DEFAULT_LPP_OUTPUT_FOLDER_ID
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step("create_site_folder")
async def create_site_folder(context: StepContext) -> StepResult:
    """Create per-site subfolder in the LPP output folder.

    Reads: site_name from state (set by resolve_sites)
    Writes: site_folder_id to state
    """
    # Idempotency guard: folder already created (handles recovery re-entry)
    existing_folder_id = context.get_state("site_folder_id")
    if existing_folder_id:
        LOGGER.info(f"create_site_folder: already done (folder_id={existing_folder_id}), skipping")
        return StepResult(
            data={"site_folder_id": existing_folder_id},
            state_updates={},  # Already in state — no DB write needed
            progress_message="Site folder already exists.",
        )

    site_name = context.get_state("site_name") or context.get_input("site_name")
    if not site_name:
        return StepResult.failure("No site_name in state")

    parent_folder_id = os.getenv("LPP_OUTPUT_FOLDER_ID", DEFAULT_LPP_OUTPUT_FOLDER_ID)

    try:
        from shared.utils.drive_upload import get_or_create_folder

        folder_id = await asyncio.to_thread(
            get_or_create_folder, site_name.strip(), parent_folder_id
        )
    except Exception as e:
        LOGGER.warning(f"Failed to create site folder (non-fatal): {e}")
        return StepResult(
            data={"site_folder_id": None},
            state_updates={"site_folder_id": None},
            progress_message=f"Could not create Drive folder for {site_name} (continuing)",
        )

    LOGGER.info(f"Site folder ready: {site_name} -> {folder_id}")
    return StepResult(
        data={"site_folder_id": folder_id},
        state_updates={"site_folder_id": folder_id},
        progress_message=f"Site folder ready: {site_name}",
    )
