"""Automated power plant site layout generation.

Produces to-scale site layout diagrams with solar arrays, energy systems,
lightning arresters, and infrastructure. Supports Victron (container cabin)
and ESS (Deye modules) configurations.

Outputs: Draw.io XML (editable vector) and PNG (base64 for Telegram/state).
"""

import os
import re
from dataclasses import dataclass, field

from shapely.geometry import Polygon

# ---------------------------------------------------------------------------
# Default box dimensions (overridable per-site)
# ---------------------------------------------------------------------------
DEFAULT_BOX_WIDTH_M = 14.5  # E-W width of one panel box (including keepouts)
DEFAULT_BOX_HEIGHT_M = 5.1  # N-S height of one panel box (including keepouts)
DEFAULT_PANELS_PER_BOX = 20  # Number of panels in one box

INTER_ARRAY_SPACING_EW_M = 0.0  # No gap — keepout included in box dimensions
ARRAY_FENCE_SETBACK_M = 0.0  # Keepout already included in box dimensions

# Victron energy cabin: 20ft shipping container
VICTRON_CABIN_WIDTH_M = 6.058
VICTRON_CABIN_DEPTH_M = 2.438

# ESS module (single Deye 60kWh)
ESS_MODULE_FRONT_M = 0.735  # Front face width
ESS_MODULE_DEPTH_M = 1.050
ESS_PLINTH_WIDTH_M = 1.2  # Per-module plinth (combine for neighbors)
ESS_PLINTH_DEPTH_M = 1.5
ESS_MAX_MODULES = 10

# Infrastructure
FEEDER_PILLAR_SIZE_M = 1.0
COMMS_BOX_SIZE_M = 0.8
FENCE_SETBACK_M = 1.0

# Lightning arrester coverage radius (metres). Each arrester covers a circle of this radius.
# Grid placement uses spacing = radius * sqrt(2) so adjacent circles fully intersect.
LIGHTNING_RADIUS_M = float(os.getenv("LAYOUT_LIGHTNING_RADIUS_M", "13.5"))

# Earth pits (grounding electrodes) — IEC 60364-5-54, IEEE 142
EARTH_PIT_SPACING_MIN_M: float = 10.0  # minimum spacing between any two pits
EARTH_PIT2_MIN_DIST_M: float = 10.0  # minimum distance of pit 2 from pit 1
EARTH_PIT2_MAX_DIST_M: float = 25.0  # maximum distance of pit 2 from pit 1
EARTH_PIT_COVERAGE_M: float = 30.0  # max acceptable distance from any array edge to nearest pit
EARTH_PIT_MAX_COUNT: int = 4  # hard cap on total pits per site

# Plinths: 300mm x 300mm concrete pads, one every 3 panels
PLINTH_SIZE_M = 0.3

# Earth pit marker radius (symbolic 1m circle on drawings)
EP_RADIUS_M = 1.0

# Default panel wattage
DEFAULT_PANEL_WATT = 455.0


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
# (x, y) coordinate in site-local UTM meters
Point2D = tuple[float, float]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class PanelArray:
    """A single panel box placed on the site.

    Each box has a fixed width and height (including keepouts) and contains
    a specified number of panels. The internal panel arrangement within the
    box is not modelled — just the outer dimensions.
    """

    origin_x: float  # Bottom-left in site-local meters
    origin_y: float
    panel_count: int = DEFAULT_PANELS_PER_BOX
    box_width: float = DEFAULT_BOX_WIDTH_M
    box_height: float = DEFAULT_BOX_HEIGHT_M
    plinths: list[tuple[float, float]] = field(default_factory=list)

    @property
    def array_width(self) -> float:
        """E-W width of the box."""
        return self.box_width

    @property
    def array_height(self) -> float:
        """N-S height of the box."""
        return self.box_height


@dataclass
class CableRoute:
    """A cable route between two points."""

    start: tuple[float, float]
    end: tuple[float, float]
    waypoints: list[tuple[float, float]]  # intermediate points (detour for obstacle avoidance)
    cable_type: str  # "dc" or "ac"
    length_m: float
    label: str  # e.g. "DC-1", "AC"
    bunch_id: int = 0  # shared cable trench grouping (0 = solo)


@dataclass
class SiteLayout:
    """Complete site layout with all placed elements."""

    boundary: Polygon
    arrays: list[PanelArray]
    energy_system_rect: tuple[float, float, float, float]  # x, y, w, h
    energy_system_type: str  # "victron" or "ess"
    ess_modules: list[tuple[float, float]] = field(default_factory=list)
    ess_plinth_rect: tuple[float, float, float, float] | None = None
    feeder_pillar: tuple[float, float] = (0, 0)
    comms_box: tuple[float, float] = (0, 0)
    lightning_positions: list[Point2D] = field(default_factory=list)
    earth_pit_positions: list[Point2D] = field(default_factory=list)
    fence: Polygon | None = None
    entrance_pos: tuple[float, float] = (0, 0)
    entrance_edge: tuple[tuple[float, float], tuple[float, float]] | None = None
    site_name: str = ""
    total_modules: int = 0
    achieved_kwp: float = 0.0
    target_kwp: float = 0.0
    cable_routes: list[CableRoute] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_panel_config(config_str: str) -> tuple[int, int]:
    """Parse panel config string like '17S2P' into (series, parallel).

    Returns (series_count, parallel_count).
    Raises ValueError on invalid format.
    """
    match = re.match(r"^(\d+)S(\d+)P$", config_str.upper().strip())
    if not match:
        raise ValueError(f"Invalid panel config format: '{config_str}'. Expected e.g. '17S2P'")
    return int(match.group(1)), int(match.group(2))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def generate_site_layout(
    boundary: Polygon,
    target_kwp: float,
    site_type: str,
    latitude: float,
    site_name: str,
    panels_per_box: int = DEFAULT_PANELS_PER_BOX,
    box_width: float = DEFAULT_BOX_WIDTH_M,
    box_height: float = DEFAULT_BOX_HEIGHT_M,
    panel_watt: float = DEFAULT_PANEL_WATT,
    ess_module_count: int | None = None,
    ess_placement: str = "outer",
    gate_pos: tuple[float, float] | None = None,
) -> SiteLayout:
    """Generate a complete site layout.

    Args:
        boundary: Site boundary polygon in meters (local CRS or raw coordinates).
        target_kwp: Target system size in kWp.
        site_type: "victron" or "ess".
        latitude: Site latitude (sign determines azimuth).
        site_name: Human-readable site name.
        panels_per_box: Number of panels in each box (default 20).
        box_width: E-W width of each box in meters (default 14.5).
        box_height: N-S height of each box in meters (default 5.1).
        panel_watt: Watt-peak per panel (default 455W).
        ess_module_count: Number of ESS modules (ESS sites only). If None, computed
            from target_kwp.
        ess_placement: "outer" (near feeder pillar/entrance) or "center".

    Returns:
        SiteLayout with all placements computed.
    """
    from shared.site_layout.geometry import compute_site_layout

    return compute_site_layout(
        boundary=boundary,
        target_kwp=target_kwp,
        site_type=site_type,
        latitude=latitude,
        site_name=site_name,
        panels_per_box=panels_per_box,
        box_width=box_width,
        box_height=box_height,
        panel_watt=panel_watt,
        ess_module_count=ess_module_count,
        ess_placement=ess_placement,
        gate_pos=gate_pos,
    )
