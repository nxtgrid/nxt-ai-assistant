"""Copy template step handler for Light Preliminary Package generation.

This handler copies a Google Slides/Docs template to the output folder
and registers it with the Apps Script document tracker to get a doc code (e.g., "DOC-0042").

Template ID resolution order:
1. Parameter override: template_id (set via `context.set_parameter_override`,
   e.g. an `[llm]` step's tool call, or a Skill Builder step-level default) --
   see `_resolve_template_id`'s docstring for why this must be
   `get_parameter_value`, not `get_input`.
2. Packet input: template_id (a caller that supplied it directly at trigger time)
3. Environment variable: LPP_TEMPLATE_ID (operator-configured, editable via the
   Settings UI -- see shared/config/flag_registry.py's "documents" group --
   without a code change, but is one global default for every run)
4. Default: (empty — one of the above is required)

Any of the above may be a full Google Docs/Sheets/Slides/Drive URL (e.g.
pasted straight from the browser address bar) instead of a bare file ID --
`_extract_drive_file_id` normalizes either form. This is what lets a skill
author or the LLM offer "paste the template doc link" instead of requiring
an opaque Drive file ID.

Output Folder ID resolution order:
1. Workflow input: output_folder_id
2. Environment variable: LPP_OUTPUT_FOLDER_ID
3. Default: (empty — LPP_OUTPUT_FOLDER_ID env var or workflow input is required)

Doc tracker registration is resilient - if it fails, the document is still created
and subsequent steps continue. User can rename the document later.
"""

import os
import re
from datetime import datetime

from orchestrator.experts.handlers.package_generator.generate_map import (
    _get_db_config,
    _lookup_site_by_name,
)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import MockSpec, OutputSpec, ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.utils.drive_upload import DEFAULT_LPP_OUTPUT_FOLDER_ID
from shared.utils.gdrive_template_creator import create_from_template
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Default LPP template ID — must be set via LPP_TEMPLATE_ID env var or workflow input
DEFAULT_LPP_TEMPLATE_ID = ""

# Every Google Docs/Sheets/Slides/Drive URL shape puts the file ID in one of
# two places: a "/d/<id>" path segment (docs.google.com/*/d/..., drive.google.
# com/file/d/...) or an "id=<id>" query parameter (drive.google.com/open?id=...,
# drive.google.com/uc?id=...). {10,} keeps this from matching some unrelated
# short "id=" query string elsewhere; real Drive file IDs run far longer than
# that in practice, but there's no single official fixed length to assert.
_DRIVE_URL_ID_PATTERNS = (
    re.compile(r"/d/([a-zA-Z0-9_-]{10,})"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]{10,})"),
)


def _extract_drive_file_id(value: str) -> str:
    """Normalize a template reference to a bare Google Drive file ID.

    Accepts either a full Google Docs/Sheets/Slides/Drive URL (pulls the ID
    out of the URL's "/d/<id>" or "id=<id>" segment -- whichever the
    specific URL shape uses) or an already-bare file ID (returned
    unchanged, since it matches neither pattern). Letting a real doc link
    work here -- not just an opaque Drive ID -- is what makes "paste the
    template you want to use" a workable chat/tool-call answer instead of
    requiring the caller to already know how to extract a Drive ID.
    """
    value = (value or "").strip()
    for pattern in _DRIVE_URL_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value


def _resolve_template_id(context: StepContext) -> str:
    """Resolve template ID from a parameter override, packet input, env var, or default.

    Resolution order:
    1. `context.get_parameter_value("template_id")` -- checks, in order,
       a pending parameter override (what `_execute_declared_function_step_call`
       sets from an LLM tool-call argument named `template_id`, since this
       step's contract declares it as a `ParamSpec` -- see the `@register_step`
       call below), then packet inputs, then packet state.
       Deliberately NOT `context.get_input(...)`: `get_input` never consults
       `pending_param_overrides` at all (see StepContext.get_input's own
       resolution order), so a caller-supplied `template_id` tool-call
       argument would be silently ignored if this checked `get_input`
       instead -- `get_parameter_value` is the one read path `ParamSpec`
       values are documented to use (see step_contracts.py's `ParamSpec`
       docstring).
    2. Environment variable: LPP_TEMPLATE_ID (operator-wide default; see
       this module's docstring)
    3. Default constant (empty)

    Either of the first two may be a full Drive URL instead of a bare file
    ID -- normalized via `_extract_drive_file_id` before returning.

    Args:
        context: Step execution context

    Returns:
        Template ID string (always returns a valid string)
    """
    # Check parameter override / packet input first
    template_id = context.get_parameter_value("template_id")
    if template_id:
        resolved = _extract_drive_file_id(str(template_id))
        LOGGER.info(f"Using template_id from parameter/input: {resolved}")
        return resolved

    # Check environment variable
    template_id = os.getenv("LPP_TEMPLATE_ID")
    if template_id:
        resolved = _extract_drive_file_id(template_id)
        LOGGER.info(f"Using LPP_TEMPLATE_ID from environment: {resolved}")
        return resolved

    # Use default
    LOGGER.info(f"Using default LPP template ID: {DEFAULT_LPP_TEMPLATE_ID}")
    return DEFAULT_LPP_TEMPLATE_ID


@register_step(
    "copy_lpp_template",
    contract=StepContract(
        description=(
            "Copies the LPP Google Sheets/Slides template into the output folder and "
            "registers it with the Apps Script document tracker for a doc code."
        ),
        # Optional: lets a caller (LLM tool call or skill-step default) pick
        # which template to copy, instead of always falling back to the
        # single operator-wide LPP_TEMPLATE_ID setting -- see this module's
        # docstring and _resolve_template_id.
        params=(
            ParamSpec(
                name="template_id",
                param_type="string",
                description=(
                    "Google Docs/Sheets/Slides template to copy -- a full doc link "
                    "(paste the URL from the browser address bar) or a bare Drive "
                    "file ID both work. Omit to use the operator-configured default "
                    "template."
                ),
                synonyms=("template", "template_url", "template_link"),
                required=False,
            ),
        ),
        # site_name is a hard requirement: `if not site_name: return
        # StepResult.failure(...)` -- there's no LPP document without one.
        consumes_state=("site_name",),
        # template_copied/document_id/document_url: idempotency guard only.
        # geo_source: branch selector for skipping site_submissions
        # validation on the community route; absence defaults to the
        # (primary) submission-route validation. site_folder_id: `if
        # site_folder_id: ... else: <fall back to input/env/default>`.
        optional_consumes_state=(
            "template_copied",
            "document_id",
            "document_url",
            "geo_source",
            "site_folder_id",
        ),
        produces_state=("template_copied", "document_id", "document_url", "document_title"),
        outputs=(
            OutputSpec(
                name="document_id",
                value_type="string",
                description="Google Drive file ID of the new LPP document -- every later LPP step's precondition.",
            ),
        ),
        guard_keys=("template_copied",),
        side_effects=(
            "Copies a Google Drive template document and registers it with the Apps "
            "Script document tracker."
        ),
        mutates=True,
        mutation_kind="external_write",
        # Task 9.3/1.5: this mock MUST populate document_id -- every other LPP
        # step's consumes_state=("document_id",) precondition (and, per
        # step_tool_schema.py's fact 2, every other step's tool-argument
        # exclusion for it) depends on this exact key being present, or a
        # mocked LPP run collapses at the very next step.
        mock=MockSpec(
            state_updates={
                "template_copied": True,
                "document_id": "MOCK-document-id",
                "document_url": "https://docs.google.com/spreadsheets/d/MOCK-document-id",
                "document_title": "MOCK LPP Document",
            },
            data={
                "document_id": "MOCK-document-id",
                "document_url": "https://docs.google.com/spreadsheets/d/MOCK-document-id",
                "document_title": "MOCK LPP Document",
            },
            message="Would have copied the LPP template and created a new document.",
        ),
    ),
)
async def copy_lpp_template(context: StepContext) -> StepResult:
    """Copy LPP template and register with document tracker for a doc code.

    Uses shared.utils.gdrive_template_creator to:
    1. Copy template to output folder
    2. Replace placeholders in title (site_name, date)
    3. Register with Apps Script doc tracker to get document code (resilient to failure)

    Accepts inputs:
    - site_name: Name of the site (required)
    - template_id: Google Doc/Sheet/Slide template -- a full doc URL or a bare
      Drive file ID, either works (optional, falls back to LPP_TEMPLATE_ID)
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

    # Validate site exists in site_submissions BEFORE creating document.
    # Community route (Route B) derives its site from a GPS anchor + GRID3
    # boundary, so it has no site_submissions row. Skip the lookup there — it
    # would otherwise fail every community run with a misleading "site wasn't
    # found in your submissions" message. resolve_sites.py guards the same way.
    is_community = context.get_state("geo_source") == "community"
    db_config = _get_db_config()
    if is_community:
        LOGGER.info(f"Community route — skipping site_submissions validation for '{site_name}'")
    elif db_config.get("host"):
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

    # Resolve template ID (parameter/input > env > default)
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
