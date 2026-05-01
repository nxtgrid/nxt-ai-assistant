r"""Expert step handler: parse GPS coordinates for community sizing.

Extracts lat/lon/anchor_name from /csize command args using regex and writes
them into parsed_inputs so the detect_community_boundary step can read them
via context.get_input().

Register in Expert Instructions Google Doc:
    [function:parse_community_sizing_args]

Workflow inputs (from packet_inputs["args"]):
    "<lat> <lon> [anchor_name]"
    e.g. "6.12345 3.98765"
    e.g. "6.12345, 3.98765 EXAMPLE_SITE_001"

State written:
    parsed_inputs.latitude (str)
    parsed_inputs.longitude (str)
    parsed_inputs.anchor_name (str)  — sanitised to [\w.\-], max 100 chars
"""

from __future__ import annotations

import re

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Matches coordinate pairs: "6.12345 3.98765", "6.12345, 3.98765", "-1.234 45.678"
_COORD_RE = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)")

# Characters allowed in anchor_name by detect_community_boundary validation
_ANCHOR_SAFE_RE = re.compile(r"[^\w.\-]")

_CANCEL_WORDS = {"cancel", "skip", "abort", "quit", "exit", "stop", "no"}


@register_step("parse_community_sizing_args")
async def parse_community_sizing_args(context: StepContext) -> StepResult:
    """Extract lat, lon, and optional anchor name from /csize command args.

    The command registry enforces requires_args=True so args are always present
    on the initial call. Cancel-word handling is provided for edge cases where
    the step is resumed after a needs_input pause.
    """
    raw = (context.get_input("args") or context.user_input or "").strip()[:500]

    # Cancel check — must be first
    if raw.lower() in _CANCEL_WORDS:
        return StepResult(skip_remaining=True)

    if not raw:
        return StepResult.needs_input(
            "Please provide GPS coordinates, e.g.:\n"
            "  /csize 6.12345 3.98765\n"
            "  /csize 6.12345 3.98765 EXAMPLE_SITE_001"
        )

    m = _COORD_RE.search(raw)
    if not m:
        return StepResult.needs_input(
            f"Couldn't find coordinates in {raw[:80]!r}.\n"
            "Please provide latitude and longitude, e.g. /csize 6.12345 3.98765"
        )

    lat_str, lon_str = m.group(1), m.group(2)
    lat, lon = float(lat_str), float(lon_str)

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return StepResult.failure(
            f"Coordinates out of range: lat={lat}, lon={lon}. "
            "Latitude must be -90 to 90, longitude -180 to 180."
        )

    # Anchor name: text after the coordinate pair, sanitised to [\w.\-]
    remainder = raw[m.end() :].strip().lstrip(",").strip()
    if remainder:
        anchor_name = _ANCHOR_SAFE_RE.sub("_", remainder)[:100]
        anchor_name = anchor_name.strip("_") or "community_anchor"
    else:
        anchor_name = "community_anchor"

    LOGGER.info(f"Community sizing args parsed: lat={lat}, lon={lon}, anchor={anchor_name!r}")

    return StepResult(
        data={"latitude": lat_str, "longitude": lon_str, "anchor_name": anchor_name},
        state_updates={
            # Write into parsed_inputs so context.get_input() in the next step finds them
            "parsed_inputs": {
                "latitude": lat_str,
                "longitude": lon_str,
                "anchor_name": anchor_name,
            }
        },
        progress_message=f"Detecting community boundary at ({lat:.5f}, {lon:.5f})...",
    )
