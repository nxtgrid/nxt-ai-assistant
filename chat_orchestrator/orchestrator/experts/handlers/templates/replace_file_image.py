"""Replace an image in a Sheet, targeted by token or alt text.

The generic form of populate_lpp_cells' map-image swap. Two things make it
reusable where that one was not: the image is named rather than found by a
height heuristic, and its size comes from the sheet's own geometry.

Sizing precedence (spec section "Image slots and sizing"):
  1. merged range containing the token cell
  2. an explicit "fit B6:F20" in the comment
  3. the replaced image's own dimensions   <- Apps Script default when fit_range is None
  4. the min_height heuristic              <- Apps Script default when target is None too
"""

import re
from typing import Optional

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import MockSpec, ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

_FIT_RANGE_RE = re.compile(r"\bfit\s+([A-Z]{1,3}\d+:[A-Z]{1,3}\d+)\b", re.IGNORECASE)


def parse_fit_range(comment_text: str) -> Optional[str]:
    """Pull an explicit 'fit B6:F20' out of a comment, if present."""
    match = _FIT_RANGE_RE.search(comment_text or "")
    return match.group(1).upper() if match else None


def choose_fit_range(
    merged_range: Optional[str], comment_fit_range: Optional[str]
) -> Optional[str]:
    """Sizing layers 1 and 2. None means 'let the replaced image's size stand'."""
    return merged_range or comment_fit_range


@register_step(
    "replace_file_image",
    contract=StepContract(
        description=(
            "Replaces an image in a Google Sheet, selected by a placeholder token in "
            "its anchor cell or by its alt text, and sized to fit its slot."
        ),
        params=(
            ParamSpec(
                name="file_id",
                param_type="string",
                description="The spreadsheet holding the image -- a link or a bare Drive file ID.",
                synonyms=("sheet_id", "spreadsheet_id", "document_id"),
                required=False,
            ),
            ParamSpec(
                name="worksheet_name",
                param_type="string",
                description="Name of the tab holding the image.",
                synonyms=("tab", "sheet_name", "worksheet"),
                required=True,
            ),
            ParamSpec(
                name="target",
                param_type="string",
                description=(
                    "Which image to replace: the placeholder token in its anchor cell "
                    "(e.g. {{site_map}}), the anchor cell's A1 address, or its alt text. "
                    "Omit to replace the first large image, as the LPP flow does."
                ),
                synonyms=("image", "slot", "placeholder"),
                required=False,
            ),
            ParamSpec(
                name="fit_range",
                param_type="string",
                description="A1 range the image should be sized to fit inside, e.g. B6:F20.",
                required=False,
            ),
            ParamSpec(
                name="image_base64",
                param_type="string",
                description=(
                    "The replacement image, base64-encoded. Omit to use the map "
                    "produced earlier in this run."
                ),
                required=False,
            ),
        ),
        optional_consumes_state=("document_id",),
        consumes_results=("generate_distribution_map",),
        produces_state=("image_replaced",),
        side_effects="Replaces an image in a Google Sheet via the Apps Script bridge.",
        mutates=True,
        mutation_kind="external_write",
        mock=MockSpec(
            state_updates={"image_replaced": True},
            data={"image_replaced": True},
            message="Would have replaced the image in the target sheet.",
        ),
    ),
)
async def replace_file_image(context: StepContext) -> StepResult:
    """Replace a named image slot with an image produced earlier in the run."""
    from orchestrator.experts.handlers.templates.create_from_template import (
        extract_drive_file_id,
    )
    from shared.utils.apps_script_client import get_merged_range_at, replace_sheet_image

    file_ref = context.get_parameter_value("file_id") or context.get_state("document_id")
    if not file_ref:
        return StepResult.soft_failure(
            code="unmet_prerequisite",
            message="No spreadsheet was given and this run has not created one yet.",
            remediation="Call create_from_template first, or pass file_id explicitly.",
        )
    file_id = extract_drive_file_id(str(file_ref))

    worksheet_name = context.get_parameter_value("worksheet_name")
    if not worksheet_name:
        return StepResult.soft_failure(
            code="missing_parameter",
            message="No worksheet name was given, so the image cannot be located.",
            remediation="Pass worksheet_name — the name of the tab holding the image.",
        )

    target = context.get_parameter_value("target")
    fit_range = context.get_parameter_value("fit_range")

    image_b64 = context.get_parameter_value("image_base64")
    if not image_b64:
        map_result = context.get_previous_result("generate_distribution_map") or {}
        image_b64 = map_result.get("map_image_b64")
    if not image_b64:
        return StepResult.soft_failure(
            code="missing_parameter",
            message="There is no image to insert.",
            remediation="Run the step that produces the image first, or pass image_base64.",
        )

    # Sizing layer 1: a merged range containing the target cell wins over
    # anything the comment said.
    merged_range = None
    if target and re.fullmatch(r"[A-Z]{1,3}\d+", str(target).upper()):
        merged = await get_merged_range_at(file_id, str(worksheet_name), str(target).upper())
        if merged.success and (merged.data or {}).get("merged"):
            merged_range = (merged.data or {}).get("range")

    chosen_fit = choose_fit_range(merged_range, fit_range)

    await context.send_progress_to_user("Replacing the image in the sheet...")

    result = await replace_sheet_image(
        sheet_id=file_id,
        worksheet_name=str(worksheet_name),
        image_base64=image_b64,
        target=str(target) if target else None,
        fit_range=chosen_fit,
    )

    if not result.success:
        return StepResult.soft_failure(
            code="invalid_parameter",
            message=f"Could not replace the image: {result.error_message}",
            remediation=(
                "Check that the target token or alt text matches an image in that tab. "
                "Omit target to fall back to the first large image."
            ),
        )

    LOGGER.info(f"Replaced image in {file_id}/{worksheet_name} (target={target})")
    return StepResult(
        data={"image_replaced": True, **(result.data or {})},
        state_updates={"image_replaced": True},
        progress_message="Image updated.",
    )
