"""Copy template step handler for Light Preliminary Package generation.

This handler copies a Google Slides/Docs template to the output folder
and registers it with the Apps Script document tracker to get a doc code (e.g., "DOC-0042").

Template ID resolution order:
1. Workflow input: template_id
2. Environment variable: LPP_TEMPLATE_ID
3. Default: (empty — LPP_TEMPLATE_ID env var is required)

Output Folder ID resolution order:
1. Workflow input: output_folder_id
2. Environment variable: LPP_OUTPUT_FOLDER_ID
3. Default: (empty — LPP_OUTPUT_FOLDER_ID env var or workflow input is required)

Doc tracker registration is resilient - if it fails, the document is still created
and subsequent steps continue. User can rename the document later.
"""

import os
from datetime import datetime

from orchestrator.experts.handlers.package_generator.generate_map import (
    _get_db_config,
    _lookup_site_by_name,
)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.drive_upload import DEFAULT_LPP_OUTPUT_FOLDER_ID
from shared.utils.gdrive_template_creator import create_from_template
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Default LPP template ID — must be set via LPP_TEMPLATE_ID env var or workflow input
DEFAULT_LPP_TEMPLATE_ID = ""


def _resolve_template_id(context: StepContext) -> str:
    """Resolve template ID from input, env var, or default.

    Resolution order:
    1. Workflow input: template_id
    2. Environment variable: LPP_TEMPLATE_ID
    3. Default constant

    Args:
        context: Step execution context

    Returns:
        Template ID string (always returns a valid string)
    """
    # Check workflow input first
    template_id = context.get_input("template_id")
    if template_id:
        LOGGER.info(f"Using template_id from workflow input: {template_id}")
        return str(template_id)

    # Check environment variable
    template_id = os.getenv("LPP_TEMPLATE_ID")
    if template_id:
        LOGGER.info(f"Using LPP_TEMPLATE_ID from environment: {template_id}")
        return str(template_id)

    # Use default
    LOGGER.info(f"Using default LPP template ID: {DEFAULT_LPP_TEMPLATE_ID}")
    return DEFAULT_LPP_TEMPLATE_ID


@register_step("copy_lpp_template")
async def copy_lpp_template(context: StepContext) -> StepResult:
    """Copy LPP template and register with document tracker for a doc code.

    Uses shared.utils.gdrive_template_creator to:
    1. Copy template to output folder
    2. Replace placeholders in title (site_name, date)
    3. Register with Apps Script doc tracker to get document code (resilient to failure)

    Accepts inputs:
    - site_name: Name of the site (required)
    - template_id: Google Drive ID of the template (optional, has default)
    - output_folder_id: Google Drive folder ID (optional, falls back to env var)

    Environment variables (fallbacks):
    - LPP_TEMPLATE_ID: Default template ID
    - LPP_OUTPUT_FOLDER_ID: Output folder ID (required if not in input)

    Args:
        context: Step execution context with packet inputs

    Returns:
        StepResult with document_id, document_url, document_title
    """
    # Idempotency guard: template already copied (handles recovery re-entry)
    if context.get_state("template_copied"):
        doc_id = context.get_state("document_id")
        doc_url = context.get_state("document_url")
        LOGGER.info(f"copy_lpp_template: already done (doc_id={doc_id}), skipping")
        return StepResult(
            data={"document_id": doc_id, "document_url": doc_url},
            state_updates={},
            progress_message="Template already copied.",
        )

    await context.send_progress_to_user("Creating LPP spreadsheet from template...")

    # Extract site name from inputs
    site_name = context.get_input("site_name")
    if not site_name:
        # Try to get from state if we looked it up during map generation
        site_name = context.get_state("site_name")

    if not site_name:
        return StepResult.failure("No site name provided for LPP template")

    # Validate site exists in site_submissions BEFORE creating document
    db_config = _get_db_config()
    if db_config.get("host"):
        try:
            lookup = _lookup_site_by_name(site_name, db_config)
            if not lookup["found"]:
                LOGGER.warning(f"Site '{site_name}' not found in site_submissions")
                return StepResult.failure(
                    f"Site '{site_name}' not found in site submissions. "
                    "Only sites with completed submissions can have an LPP generated. "
                    "Please check the spelling or verify the site submission exists."
                )
            # Use the actual site name from database (handles fuzzy matches)
            if lookup.get("site_name"):
                site_name = lookup["site_name"]
                LOGGER.info(f"Validated site exists: {site_name} (ID: {lookup.get('site_id')})")
        except Exception as e:
            LOGGER.exception(f"Error validating site: {e}")
            return StepResult.failure(f"Error validating site: {str(e)}")
    else:
        LOGGER.warning("Database not configured, skipping site validation")

    # Resolve template ID (input > env > default)
    template_id = _resolve_template_id(context)

    # Resolve output folder ID: site subfolder > input > env > default
    site_folder_id = context.get_state("site_folder_id")
    if site_folder_id:
        output_folder_id = site_folder_id
        LOGGER.info(f"Using site subfolder from state: {output_folder_id}")
    else:
        output_folder_id = (
            context.get_input("output_folder_id")
            or os.getenv("LPP_OUTPUT_FOLDER_ID")
            or DEFAULT_LPP_OUTPUT_FOLDER_ID
        )
        LOGGER.info(f"Using output folder: {output_folder_id}")

    LOGGER.info(
        f"Creating LPP document for site: {site_name} "
        f"(template: {template_id}, folder: {output_folder_id})"
    )

    # Prepare placeholder variables
    variables = {
        "site_name": site_name,
        "date": datetime.now().strftime("%Y%m%d-%H%M"),
    }

    try:
        result = await create_from_template(
            template_id=template_id,
            output_folder_id=output_folder_id,
            variables=variables,
            register_with_doc_tracker=True,
        )

        if not result.success:
            LOGGER.error(f"Failed to create LPP document: {result.error_message}")
            return StepResult.failure(f"Failed to create document: {result.error_message}")

        LOGGER.info(f"Created LPP document: {result.final_title} (ID: {result.document_id})")

        return StepResult(
            data={
                "document_id": result.document_id,
                "document_url": result.document_url,
                "document_title": result.final_title,
                "template_type": result.template_type,
            },
            state_updates={
                "template_copied": True,
                "document_id": result.document_id,
                "document_url": result.document_url,
                "document_title": result.final_title,
            },
            progress_message=f"Created: {result.final_title}",
        )

    except Exception as e:
        LOGGER.exception(f"Error creating LPP document: {e}")
        return StepResult.failure(f"Error creating document: {str(e)}")
