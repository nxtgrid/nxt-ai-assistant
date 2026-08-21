"""Create a new Drive file from any Doc or Sheet template.

The generic half of copy_lpp_template. What it deliberately does NOT do is
copy_lpp_template's site_submissions validation -- that check is what makes
that step LPP-only, and it stays there.

Naming comes from the template's own filename: GoogleTemplateCreator
substitutes [placeholder] tokens in the copied title, so a template named
"[site_name] Site Package - [date]" names every copy of itself. That is the
whole naming convention -- there is no separate pattern to configure.
"""

import asyncio
import re
from datetime import datetime

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import MockSpec, OutputSpec, ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.utils.gdrive_template_creator import create_from_template as create_from_template_file
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Same two URL shapes copy_template.py normalises -- a "/d/<id>" path segment
# or an "id=<id>" query parameter. Duplicated rather than imported because
# this module must not depend on package_generator, which is the LPP-specific
# package this step exists to be independent of.
_DRIVE_URL_ID_PATTERNS = (
    re.compile(r"/d/([a-zA-Z0-9_-]{10,})"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]{10,})"),
)


def extract_drive_file_id(value: str) -> str:
    """Normalize a full Drive URL or a bare file ID to a bare file ID."""
    value = (value or "").strip()
    for pattern in _DRIVE_URL_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value


async def _resolve_default_output_folder(template_id: str) -> str:
    """The template's own parent folder, when no output_folder_id was given.

    Backs the "defaults to the folder the template lives in" promise below.
    GoogleTemplateCreator.copy_template has no such fallback itself -- it
    forwards folder_id straight into the Drive API's parents list, so an
    empty string there would 400 rather than resolve to anything. Best
    effort: returns "" (same as before this existed) if the template has no
    listed parent or the lookup itself fails, rather than raising.
    """
    try:
        from googleapiclient.discovery import build

        from shared.utils.google_auth import get_drive_credentials

        service = build("drive", "v3", credentials=get_drive_credentials())
        meta = await asyncio.to_thread(
            lambda: service.files()
            .get(fileId=template_id, fields="parents", supportsAllDrives=True)
            .execute()
        )
        parents = meta.get("parents") or []
        return str(parents[0]) if parents else ""
    except Exception as e:
        LOGGER.warning(f"Could not resolve parent folder for template {template_id}: {e}")
        return ""


@register_step(
    "create_from_template",
    contract=StepContract(
        description=(
            "Copies any Google Docs or Sheets template into a target folder, naming "
            "the copy from the template's own filename placeholders."
        ),
        params=(
            ParamSpec(
                name="template_id",
                param_type="string",
                description=(
                    "The template to copy -- a full doc link (paste the URL from the "
                    "browser address bar) or a bare Drive file ID both work."
                ),
                synonyms=("template", "template_url", "template_link"),
                required=True,
            ),
            ParamSpec(
                name="output_folder_id",
                param_type="string",
                description=(
                    "Drive folder the copy is created in -- a folder link or a bare "
                    "folder ID. Defaults to the folder the template lives in."
                ),
                synonyms=("folder", "output_folder", "destination_folder"),
                required=False,
            ),
            ParamSpec(
                name="name_variables",
                param_type="object",
                description=(
                    "Values for [placeholder] tokens in the template's filename, e.g. "
                    '{"site_name": "ExampleGrid"}. A "date" token is filled automatically.'
                ),
                required=False,
            ),
        ),
        optional_consumes_state=("site_name", "site_folder_id", "document_id"),
        produces_state=("document_id", "document_url", "document_title"),
        outputs=(
            OutputSpec(
                name="document_id",
                value_type="string",
                description="Drive file ID of the newly created document.",
            ),
            OutputSpec(
                name="document_url",
                value_type="string",
                description="Shareable URL of the newly created document.",
            ),
            OutputSpec(
                name="document_title",
                value_type="string",
                description="Final title of the copy, after placeholder substitution.",
            ),
        ),
        side_effects="Copies a Google Drive template file into a target folder.",
        mutates=True,
        mutation_kind="external_write",
        mock=MockSpec(
            state_updates={
                "document_id": "MOCK-document-id",
                "document_url": "https://docs.google.com/document/d/MOCK-document-id",
                "document_title": "MOCK Document From Template",
            },
            data={
                "document_id": "MOCK-document-id",
                "document_url": "https://docs.google.com/document/d/MOCK-document-id",
                "document_title": "MOCK Document From Template",
            },
            message="Would have copied the template into a new document.",
        ),
    ),
)
async def create_from_template(context: StepContext) -> StepResult:
    """Copy a template file and return the new document's id, url and title."""
    template_ref = context.get_parameter_value("template_id")
    if not template_ref:
        return StepResult.soft_failure(
            code="missing_parameter",
            message="No template was given, so there is nothing to copy.",
            remediation=(
                "Call this step again with template_id set to the template's "
                "Google Docs/Sheets link, or its Drive file ID."
            ),
        )
    template_id = extract_drive_file_id(str(template_ref))

    folder_ref = context.get_parameter_value("output_folder_id") or context.get_state(
        "site_folder_id"
    )
    if folder_ref:
        output_folder_id = extract_drive_file_id(str(folder_ref))
    else:
        output_folder_id = await _resolve_default_output_folder(template_id)

    variables = dict(context.get_parameter_value("name_variables") or {})
    variables.setdefault("date", datetime.now().strftime("%Y%m%d-%H%M"))
    site_name = context.get_state("site_name")
    if site_name:
        variables.setdefault("site_name", site_name)

    await context.send_progress_to_user("Creating a new document from the template...")

    try:
        result = await create_from_template_file(
            template_id=template_id,
            output_folder_id=output_folder_id,
            variables=variables,
            register_with_doc_tracker=True,
        )
    except Exception as e:
        LOGGER.exception(f"Error creating document from template {template_id}: {e}")
        return StepResult.failure(f"Error creating document: {str(e)}")

    if not result.success:
        return StepResult.failure(f"Failed to create document: {result.error_message}")

    LOGGER.info(f"Created {result.final_title} ({result.document_id}) from {template_id}")
    return StepResult(
        data={
            "document_id": result.document_id,
            "document_url": result.document_url,
            "document_title": result.final_title,
            "template_type": result.template_type,
        },
        state_updates={
            "document_id": result.document_id,
            "document_url": result.document_url,
            "document_title": result.final_title,
        },
        progress_message=f"Created: {result.final_title}",
    )
