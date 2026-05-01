"""Resolve grid sheets step handler for GTR Expert.

This handler parses grid names from expert instructions and extracts
Sheet URLs for each grid to review.
"""

import re
from typing import Dict, List

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Patterns to extract grid name and sheet URL from instructions
# Supports multiple formats:
# 1. With bullet: "- GridName: https://docs.google.com/spreadsheets/d/..."
# 2. Without bullet: "GridName: https://docs.google.com/spreadsheets/d/..."
GRID_SHEET_PATTERNS = [
    # With bullet point (- or *)
    re.compile(
        r"^\s*[-*]\s*([^:]+):\s*(https://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9_-]+)",
        re.MULTILINE,
    ),
    # Without bullet point (line starts with grid name)
    re.compile(
        r"^([A-Za-z][A-Za-z0-9\s]*):\s*(https://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9_-]+)",
        re.MULTILINE,
    ),
]

# Pattern to extract spreadsheet ID from URL
SPREADSHEET_ID_PATTERN = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def extract_grid_sheet_mappings(text: str) -> Dict[str, Dict[str, str]]:
    """Extract grid name to sheet URL mappings from text.

    Tries multiple patterns to be resilient to formatting variations:
    - With bullet points: "- GridName: https://..."
    - Without bullet points: "GridName: https://..."

    Args:
        text: Text containing grid sheet mappings

    Returns:
        Dict mapping grid name (lowercase) to dict with 'name', 'url', 'spreadsheet_id'
    """
    mappings: Dict[str, Dict[str, str]] = {}

    # Try each pattern
    for pattern in GRID_SHEET_PATTERNS:
        for match in pattern.finditer(text):
            grid_name = match.group(1).strip()
            sheet_url = match.group(2).strip()

            # Skip if grid name looks like a header or label
            if grid_name.lower() in ["grid", "name", "url", "sheet"]:
                continue

            # Extract spreadsheet ID
            id_match = SPREADSHEET_ID_PATTERN.search(sheet_url)
            spreadsheet_id = id_match.group(1) if id_match else ""

            # Store with lowercase key for case-insensitive lookup
            # Don't overwrite if already found (first pattern wins)
            key = grid_name.lower()
            if key not in mappings:
                mappings[key] = {
                    "name": grid_name,
                    "url": sheet_url,
                    "spreadsheet_id": spreadsheet_id,
                }

    return mappings


def find_matching_grids(
    requested_names: List[str],
    available_grids: Dict[str, Dict[str, str]],
) -> tuple[List[Dict[str, str]], List[str]]:
    """Find grids matching the requested names with fuzzy matching.

    Uses fuzzy matching (80% threshold) to handle typos and misspellings.

    Args:
        requested_names: List of grid names requested by user
        available_grids: Dict of available grid mappings (keyed by lowercase name)

    Returns:
        Tuple of (matched grids list, unmatched names list)
    """
    from shared.utils.grid_matcher import find_best_grid_match

    matched = []
    unmatched = []

    # Build list of available grid names for fuzzy matching
    available_names = [g["name"] for g in available_grids.values()]

    for name in requested_names:
        name_lower = name.lower().strip()

        # Try exact match first (case-insensitive)
        if name_lower in available_grids:
            matched.append(available_grids[name_lower])
            continue

        # Try fuzzy match using shared utility
        fuzzy_match, was_fuzzy, score = find_best_grid_match(name, available_names)

        if fuzzy_match:
            # Find the grid info for the matched name
            match_lower = fuzzy_match.lower()
            if match_lower in available_grids:
                matched.append(available_grids[match_lower])
                if was_fuzzy:
                    LOGGER.info(f"Fuzzy matched '{name}' -> '{fuzzy_match}' (score: {score}%)")
            else:
                unmatched.append(name)
        else:
            unmatched.append(name)

    return matched, unmatched


async def get_grid_urls_from_expert_config() -> str:
    """Fetch grid URLs section from expert config.

    Looks in the expert's raw_sections for grid sheet URLs under
    various section names.

    Returns:
        Combined text from grid URL sections, or empty string
    """
    try:
        from orchestrator.services.expert_instructions_provider import ExpertInstructionsProvider

        provider = ExpertInstructionsProvider()
        config = await provider.get_expert_config("grids_technical_reviewer")

        if not config or not config.raw_sections:
            return ""

        # Look for grid URLs in various section names
        texts = []
        for key in config.raw_sections:
            key_lower = key.lower().replace(" ", "_")
            if any(pk in key_lower for pk in ["grid_sheet", "grid_url", "sheet_url"]):
                texts.append(config.raw_sections[key])

        # Also check system_instructions as fallback
        if config.system_instructions:
            texts.append(config.system_instructions)

        return "\n\n".join(texts)

    except Exception as e:
        LOGGER.warning(f"Failed to fetch expert config: {e}")
        return ""


@register_step("resolve_grid_sheets")
async def resolve_grid_sheets(context: StepContext) -> StepResult:
    """Resolve grid names to their Google Sheet URLs.

    Reads the expert instructions to extract grid-to-sheet mappings,
    then matches against requested grid names (or returns all if none specified).

    Searches for grid URLs in:
    1. Expert config's raw_sections (### Grid Sheet URLs section)
    2. Expert config's system_instructions
    3. RAG context (fallback)
    4. Packet state (fallback)

    Args:
        context: Step execution context

    Returns:
        StepResult with grids_to_review list or error
    """
    # Send progress to user
    await context.send_progress_to_user("🔍 Resolving grid sheet URLs...")

    # Get parsed grid names from LLM parsing step
    parsed_inputs = context.get_previous_result("parse_gtr_request") or {}
    requested_grid_names: List[str] = parsed_inputs.get("grid_names", [])

    # Fallback: if LLM didn't extract grid names, try to get from packet args directly
    if not requested_grid_names:
        args_text = context.packet_inputs.get("args", "") or ""
        if args_text.strip():
            # Split by comma or space for multiple grids, e.g. "GridA, GridB" or "GridA GridB"
            raw_names = [n.strip() for n in args_text.replace(",", " ").split()]
            requested_grid_names = [n for n in raw_names if n]
            if requested_grid_names:
                LOGGER.info(f"Extracted grid names from args: {requested_grid_names}")

    # Try to get grid URLs from expert config first (handles ### sections)
    instructions_text = await get_grid_urls_from_expert_config()

    # Fallback to RAG context
    if not instructions_text:
        LOGGER.info("No expert config sections found, trying RAG context")
        instructions_text = context.get_rag_context()

    # Fallback to packet state
    if not instructions_text:
        LOGGER.info("No RAG context, checking packet state")
        instructions_text = context.get_state("expert_instructions", "")

    if not instructions_text:
        return StepResult.failure(
            "Could not load expert instructions. Please ensure the GTR expert is configured."
        )

    # Extract grid-to-sheet mappings
    grid_mappings = extract_grid_sheet_mappings(instructions_text)

    if not grid_mappings:
        return StepResult.failure(
            "No grid sheet mappings found in expert instructions.\n\n"
            "Expected format (with or without bullets):\n"
            "GridName: https://docs.google.com/spreadsheets/d/...\n"
            "- AnotherGrid: https://docs.google.com/spreadsheets/d/..."
        )

    LOGGER.info(f"Found {len(grid_mappings)} grid sheet mappings in instructions")

    # Determine which grids to review
    if not requested_grid_names:
        # No specific grids requested - review all configured grids
        grids_to_review = list(grid_mappings.values())
        LOGGER.info(f"No grid names specified - reviewing all {len(grids_to_review)} grids")
    else:
        # Match requested names to available grids
        grids_to_review, unmatched = find_matching_grids(requested_grid_names, grid_mappings)

        if unmatched:
            available_names = [g["name"] for g in grid_mappings.values()]
            return StepResult.failure(
                f"Could not find grid(s): {', '.join(unmatched)}\n\n"
                f"Available grids: {', '.join(available_names)}"
            )

        if not grids_to_review:
            return StepResult.failure("No valid grids found to review.")

    # Build summary for progress message
    grid_names_str = ", ".join(g["name"] for g in grids_to_review)

    return StepResult(
        data={
            "grids_to_review": grids_to_review,
            "grid_count": len(grids_to_review),
        },
        state_updates={
            "grids_to_review": grids_to_review,
        },
        progress_message=f"Resolved {len(grids_to_review)} grid(s): {grid_names_str}",
    )
