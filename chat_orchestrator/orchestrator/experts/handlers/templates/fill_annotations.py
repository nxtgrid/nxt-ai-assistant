"""Fill a Doc or Sheet from its @anansi-chatbot comments.

The generic replacement for populate_lpp_cells' cell-filling half. Where that
step reads a ## Cell Mapping table out of an expert's Google Doc, this reads
the target file's own comments and matches them against the run's value
catalogue.

All comments resolve in ONE LLM call (see annotations.resolve_values). That is
what lets a forty-slot template fill cheaply, and it is why MAX_EDITS_PER_RUN
-- a cap on per-comment *generative* rewriting in process_doc_edits -- does
not apply here.

Nothing is ever guessed. A comment that cannot be matched to a catalogue entry,
or whose quoted text no longer appears in the file, gets a reply explaining why
and its thread is left OPEN, so the human can see there is something
outstanding.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from orchestrator.experts.output_catalogue import (
    CatalogueEntry,
    build_catalogue,
    render_catalogue,
)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import MockSpec, OutputSpec, ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.prompts import PROMPTS
from shared.utils.logging import get_logger
from shared.utils.sheet_editing import offset_a1

LOGGER = get_logger(__name__)

_TOKEN_RE = re.compile(r"^\{\{[^{}]+\}\}$")


def is_placeholder_token(quoted_text: str) -> bool:
    """Whether a comment's quoted text is a {{token}} rather than a label.

    A token is filled in place; a label is filled in the cell beside it. This
    is the whole difference between the primary and degraded paths, and it is
    decided by what the template author typed, not by configuration.
    """
    return bool(_TOKEN_RE.match((quoted_text or "").strip()))


@dataclass
class FillPlan:
    """What a run intends to do, before it does any of it.

    Separated from execution so `dry_run` can return exactly what a real run
    would write, and so the resolution logic is unit-testable without Drive.
    """

    writes: list[tuple[str, str, Any]] = field(default_factory=list)  # (tab, a1, value)
    replies: list[str] = field(default_factory=list)  # audit message per write
    reply_comment_ids: list[str] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)  # (comment_id, why)


def plan_writes(
    resolutions: list[dict],
    matches_by_request: dict[int, list],
    catalogue: list[CatalogueEntry],
    comment_ids_by_request: Optional[dict[int, str]] = None,
    quoted_by_request: Optional[dict[int, str]] = None,
) -> FillPlan:
    """Turn LLM resolutions plus cell matches into a concrete write plan."""
    comment_ids_by_request = comment_ids_by_request or {}
    by_path = {e.path: e for e in catalogue}
    plan = FillPlan()

    for resolution in resolutions:
        request_no = int(resolution.get("request", 0))
        comment_id = comment_ids_by_request.get(request_no, str(request_no))
        path = resolution.get("path")
        matches = matches_by_request.get(request_no, [])

        if not path or path not in by_path:
            plan.unresolved.append(
                (
                    comment_id,
                    "I could not find a value for this in the data available from "
                    "this run. Could you say which figure you mean?",
                )
            )
            continue

        if not matches:
            plan.unresolved.append(
                (
                    comment_id,
                    "The text this comment quotes no longer appears in the file — "
                    "it may have been edited since the comment was made.",
                )
            )
            continue

        entry = by_path[path]
        quoted = (quoted_by_request or {}).get(request_no, "")
        # A token is the slot itself; a label names the slot beside it.
        fill_in_place = is_placeholder_token(quoted) if quoted else True
        for match in matches:
            target_a1 = match.a1 if fill_in_place else offset_a1(match, 1)
            plan.writes.append((match.tab, target_a1, entry.value))
        plan.replies.append(f"Done: {entry.path} = {entry.value}")
        plan.reply_comment_ids.append(comment_id)

    return plan


async def _resolve_against_catalogue(
    instructions: list[str], catalogue: list[CatalogueEntry]
) -> list[dict]:
    """One LLM call for every comment in the file."""
    from orchestrator.config.settings import get_settings
    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway

    settings = get_settings()
    gateway = get_default_generation_gateway(default_model=settings.gemini.model)

    requests_block = "\n".join(f'{i + 1}. "{text}"' for i, text in enumerate(instructions))
    prompt = PROMPTS.text(
        "annotations.resolve_values",
        catalogue_block=render_catalogue(catalogue),
        requests_block=requests_block,
    )

    response = await gateway.generate(
        [LLMMessage(role="user", text=prompt)],
        GenerationOptions(model=settings.gemini.model, temperature=0.1, max_output_tokens=2000),
    )

    text = str(response.text).strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, ValueError) as e:
        LOGGER.warning(f"Could not parse value resolutions: {e}")
        return []


@register_step(
    "fill_annotations",
    contract=StepContract(
        description=(
            "Fills a Google Doc or Sheet from its own @anansi-chatbot comments, "
            "matching each comment against the values this run has produced."
        ),
        params=(
            ParamSpec(
                name="file_id",
                param_type="string",
                description=(
                    "The document or spreadsheet to fill -- a link or a bare Drive "
                    "file ID. Defaults to the document this run just created."
                ),
                synonyms=("document_id", "sheet_id", "spreadsheet_id", "file"),
                required=False,
            ),
            ParamSpec(
                name="dry_run",
                param_type="boolean",
                description=(
                    "Report what would be filled in, without writing anything or "
                    "resolving any comment. Use this to confirm before writing."
                ),
                required=False,
                default=False,
            ),
        ),
        optional_consumes_state=("document_id", "annotations_filled"),
        produces_state=("annotations_filled",),
        outputs=(
            OutputSpec(
                name="cells_filled",
                value_type="integer",
                where="data",
                description="Number of cells or sections written.",
            ),
        ),
        guard_keys=("annotations_filled",),
        side_effects=(
            "Writes values into a Google Doc or Sheet and resolves the comments "
            "that requested them."
        ),
        mutates=True,
        mutation_kind="external_write",
        mock=MockSpec(
            state_updates={"annotations_filled": True},
            data={"cells_filled": 0},
            message="Would have filled the template's annotated slots.",
        ),
    ),
)
async def fill_annotations(context: StepContext) -> StepResult:
    """Scan a file's bot comments, resolve them, write the values, resolve threads."""
    from shared.utils.file_annotations import (
        MIME_SHEET,
        get_file_mime_type,
        pin_revision,
        reply_and_resolve,
        reply_without_resolving,
        scan_annotations,
    )
    from shared.utils.sheet_editing import fetch_all_grids, find_cells_in_grids, write_cells

    file_ref = context.get_parameter_value("file_id") or context.get_state("document_id")
    if not file_ref:
        return StepResult.soft_failure(
            code="unmet_prerequisite",
            message="No file was given and this run has not created one yet.",
            remediation="Call create_from_template first, or pass file_id explicitly.",
        )

    from orchestrator.experts.handlers.templates.create_from_template import (
        extract_drive_file_id,
    )

    file_id = extract_drive_file_id(str(file_ref))
    dry_run = bool(context.get_parameter_value("dry_run") or False)

    mime = await get_file_mime_type(file_id)
    if mime != MIME_SHEET:
        return StepResult.soft_failure(
            code="invalid_parameter",
            message=(
                "fill_annotations currently fills spreadsheets only; this file is "
                f"a {mime or 'unknown type'}."
            ),
            remediation="Use process_doc_edits for Google Docs.",
        )

    await context.send_progress_to_user("Checking the template for @anansi-chatbot comments...")

    annotations = await scan_annotations(file_id)
    if not annotations:
        return StepResult(
            data={"cells_filled": 0},
            state_updates={"annotations_filled": True},
            progress_message="No pending @anansi-chatbot comments in that file.",
        )

    catalogue = build_catalogue(context)
    if not catalogue:
        return StepResult.soft_failure(
            code="unmet_prerequisite",
            message="No values are available from this run yet, so nothing can be filled in.",
            remediation=(
                "Run the steps that produce the data first — the catalogue is built "
                "from the outputs steps have already returned."
            ),
        )

    grids = await fetch_all_grids(file_id)

    matches_by_request: dict[int, list] = {}
    comment_ids_by_request: dict[int, str] = {}
    quoted_by_request: dict[int, str] = {}
    instructions: list[str] = []
    for i, ann in enumerate(annotations, start=1):
        instructions.append(ann.instruction)
        comment_ids_by_request[i] = ann.comment_id
        quoted_by_request[i] = ann.quoted_text
        matches_by_request[i] = find_cells_in_grids(grids, ann.quoted_text)

    resolutions = await _resolve_against_catalogue(instructions, catalogue)
    plan = plan_writes(
        resolutions, matches_by_request, catalogue, comment_ids_by_request, quoted_by_request
    )

    if dry_run:
        return StepResult(
            data={
                "cells_filled": 0,
                "dry_run": True,
                "planned_writes": [
                    {"tab": t, "cell": a, "value": v} for t, a, v in plan.writes
                ],
                "unresolved": [{"comment_id": c, "reason": r} for c, r in plan.unresolved],
            },
            progress_message=(
                f"Would fill {len(plan.writes)} cell(s); "
                f"{len(plan.unresolved)} comment(s) need a human."
            ),
        )

    await pin_revision(file_id)
    cells_written = await write_cells(file_id, plan.writes)

    # Resolve threads only AFTER confirming the write landed -- an unresolved
    # thread is the only signal a human gets that the bot did not finish.
    if cells_written:
        for comment_id, message in zip(plan.reply_comment_ids, plan.replies):
            await reply_and_resolve(file_id, comment_id, message)

    for comment_id, reason in plan.unresolved:
        await reply_without_resolving(file_id, comment_id, reason)

    return StepResult(
        data={
            "cells_filled": cells_written,
            "unresolved": [{"comment_id": c, "reason": r} for c, r in plan.unresolved],
        },
        state_updates={"annotations_filled": True},
        progress_message=(
            f"Filled {cells_written} cell(s)"
            + (f", {len(plan.unresolved)} comment(s) need a human" if plan.unresolved else "")
        ),
    )
