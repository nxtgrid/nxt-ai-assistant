"""Geometry engine for power plant site layout.

Brick-pattern packing of panel boxes within a Shapely Polygon boundary,
with optimal placement of energy systems, infrastructure, and lightning arresters.

Each box is a fixed-size rectangle (default 14.5m x 5.1m, 20 panels) including
all internal keepouts. The packing algorithm places these boxes in rows,
centering each row within the polygon via bisection.
"""

import logging
import math

from shapely.geometry import Point, Polygon, box
from shapely.ops import nearest_points

logger = logging.getLogger(__name__)

from shared.site_layout import (
    ARRAY_FENCE_SETBACK_M,
    COMMS_BOX_SIZE_M,
    DEFAULT_BOX_HEIGHT_M,
    DEFAULT_BOX_WIDTH_M,
    DEFAULT_PANEL_WATT,
    DEFAULT_PANELS_PER_BOX,
    EARTH_PIT2_MAX_DIST_M,
    EARTH_PIT2_MIN_DIST_M,
    EARTH_PIT_COVERAGE_M,
    EARTH_PIT_MAX_COUNT,
    EARTH_PIT_SPACING_MIN_M,
    ESS_MAX_MODULES,
    ESS_MODULE_DEPTH_M,
    ESS_MODULE_FRONT_M,
    ESS_PLINTH_DEPTH_M,
    ESS_PLINTH_WIDTH_M,
    FEEDER_PILLAR_SIZE_M,
    FENCE_SETBACK_M,
    INTER_ARRAY_SPACING_EW_M,
    LIGHTNING_RADIUS_M,
    PLINTH_SIZE_M,
    VICTRON_CABIN_DEPTH_M,
    VICTRON_CABIN_WIDTH_M,
    CableRoute,
    PanelArray,
    SiteLayout,
)


def compute_site_layout(
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
    """Compute complete site layout placement.

    Algorithm:
    1. Compute usable area (boundary inset by fence + array setback)
    2. Entrance & feeder pillar (independent of energy system)
    3. Victron: place cabin first, pack boxes around it
       ESS: pack all boxes (brick pattern), then replace optimal slot with ESS
    4. Place plinths, infrastructure, lightning arresters

    Args:
        ess_placement: "outer" (near feeder pillar/entrance) or "center".
    """
    site_type = site_type.lower().strip()
    if site_type not in ("victron", "ess"):
        raise ValueError(f"site_type must be 'victron' or 'ess', got '{site_type}'")

    fence = boundary.buffer(-FENCE_SETBACK_M)
    if fence.is_empty or not isinstance(fence, Polygon):
        fence = boundary

    total_setback = FENCE_SETBACK_M + ARRAY_FENCE_SETBACK_M
    usable = boundary.buffer(-total_setback)
    if usable.is_empty or not isinstance(usable, Polygon):
        return _empty_layout(boundary, fence, site_type, site_name, target_kwp)

    # Entrance: use gate from site identification if provided, else longest edge
    if gate_pos is not None:
        entrance_pos, entrance_edge = _entrance_from_gate(boundary, gate_pos)
    else:
        entrance_pos, entrance_edge = _find_entrance(boundary)

    # Gate keepout: 6m clear zone on the gate side for vehicle access
    gate_keepout = _gate_keepout(entrance_edge, boundary)
    if gate_keepout is not None:
        usable_after_gate = usable.difference(gate_keepout)
        if not usable_after_gate.is_empty and isinstance(usable_after_gate, Polygon):
            usable = usable_after_gate

    # Feeder pillar near entrance
    feeder_x = entrance_pos[0]
    feeder_y = entrance_pos[1] + FEEDER_PILLAR_SIZE_M * 2
    fp_box = box(
        feeder_x, feeder_y, feeder_x + FEEDER_PILLAR_SIZE_M, feeder_y + FEEDER_PILLAR_SIZE_M
    )
    if not boundary.contains(fp_box):
        feeder_y = entrance_pos[1] - FEEDER_PILLAR_SIZE_M * 2
    feeder_pos = (feeder_x, feeder_y)

    if site_type == "victron":
        usable_minx, usable_miny, _, _ = usable.bounds
        ex, ey = usable_minx, usable_miny
        ew, eh = VICTRON_CABIN_WIDTH_M, VICTRON_CABIN_DEPTH_M
        energy_rect = (ex, ey, ew, eh)
        energy_zone = box(ex - 1.0, ey - 1.0, ex + ew + 1.0, ey + eh + 1.0)
        ess_modules: list[tuple[float, float]] = []
        ess_plinth_rect = None

        arrays = _pack_boxes(
            usable=usable,
            box_w=box_width,
            box_h=box_height,
            panels_per_box=panels_per_box,
            panel_watt=panel_watt,
            target_kwp=target_kwp,
            energy_zone=energy_zone,
        )
    else:
        # ESS: reserve a central zone for ESS plinths (with 2x expansion room),
        # then pack boxes around it — same approach as Victron cabin.
        energy_rect, ess_modules, ess_plinth_rect, energy_zone = _compute_ess_zone(
            usable=usable,
            panels_per_box=panels_per_box,
            panel_watt=panel_watt,
            target_kwp=target_kwp,
            ess_module_count=ess_module_count,
            placement=ess_placement,
        )
        arrays = _pack_boxes(
            usable=usable,
            box_w=box_width,
            box_h=box_height,
            panels_per_box=panels_per_box,
            panel_watt=panel_watt,
            target_kwp=target_kwp,
            energy_zone=energy_zone,
        )

    # Plinths per box
    for arr in arrays:
        arr.plinths = _place_plinths(arr)

    total_modules = sum(arr.panel_count for arr in arrays)
    achieved_kwp = total_modules * panel_watt / 1000

    # Comms box near energy system
    ex, ey, ew, eh = energy_rect
    comms_x = ex + ew + 1.0
    comms_y = ey
    cb_box = box(comms_x, comms_y, comms_x + COMMS_BOX_SIZE_M, comms_y + COMMS_BOX_SIZE_M)
    if not boundary.contains(cb_box):
        comms_x = ex - COMMS_BOX_SIZE_M - 1.0
        comms_y = ey

    lightning_positions = _place_lightning(
        arrays=arrays,
        energy_rect=energy_rect,
        feeder_pos=feeder_pos,
        boundary=boundary,
    )

    earth_pit_positions = _place_earth_pits(
        arrays=arrays,
        energy_rect=energy_rect,
        lightning_positions=lightning_positions,
        boundary=boundary,
    )

    cable_routes = _compute_cable_routes(
        arrays=arrays,
        energy_rect=energy_rect,
        feeder_pos=feeder_pos,
    )

    warnings = []
    if achieved_kwp < target_kwp * 0.95:
        warnings.append(
            f"Achieved {achieved_kwp:.1f} kWp is below target {target_kwp:.1f} kWp "
            f"({achieved_kwp / target_kwp * 100:.0f}%). Site boundary may be too small."
        )

    return SiteLayout(
        boundary=boundary,
        arrays=arrays,
        energy_system_rect=energy_rect,
        energy_system_type=site_type,
        ess_modules=ess_modules,
        ess_plinth_rect=ess_plinth_rect,
        feeder_pillar=feeder_pos,
        comms_box=(comms_x, comms_y),
        lightning_positions=lightning_positions,
        earth_pit_positions=earth_pit_positions,
        fence=fence,
        entrance_pos=entrance_pos,
        entrance_edge=entrance_edge,
        site_name=site_name,
        total_modules=total_modules,
        achieved_kwp=achieved_kwp,
        target_kwp=target_kwp,
        cable_routes=cable_routes,
        warnings=warnings,
    )


def _empty_layout(
    boundary: Polygon, fence: Polygon, site_type: str, site_name: str, target_kwp: float
) -> SiteLayout:
    """Return an empty layout when boundary is too small."""
    minx, miny, _, _ = boundary.bounds
    return SiteLayout(
        boundary=boundary,
        arrays=[],
        energy_system_rect=(minx, miny, 0, 0),
        energy_system_type=site_type,
        fence=fence,
        site_name=site_name,
        target_kwp=target_kwp,
        warnings=["Site boundary too small for any equipment placement."],
    )


# ---------------------------------------------------------------------------
# Two-pass brick-pattern box packing
# ---------------------------------------------------------------------------
def _pack_boxes(
    usable: Polygon,
    box_w: float,
    box_h: float,
    panels_per_box: int,
    panel_watt: float,
    target_kwp: float,
    energy_zone: Polygon | None = None,
) -> list[PanelArray]:
    """Brick-pattern packing with vertical offset optimization.

    Tries multiple vertical grid offsets (0 to box_h) and picks the one
    that yields the most panels. This ensures the grid aligns with the
    widest part of the polygon (e.g. the center of a diamond).

    Each row is centered horizontally via bisection.
    """
    usable_minx, usable_miny, usable_maxx, usable_maxy = usable.bounds

    def _fits(bx: float, by: float, bw: float, bh: float) -> bool:
        r = box(bx, by, bx + bw, by + bh)
        if not usable.contains(r):
            return False
        if energy_zone is not None and r.intersects(energy_zone):
            return False
        return True

    # Try multiple vertical offsets and pick the best
    best_arrays: list[PanelArray] = []
    num_offsets = max(1, int(box_h / 0.5))  # try every 0.5m

    for i in range(num_offsets):
        y_offset = i * box_h / num_offsets
        arrays: list[PanelArray] = []

        def _achieved_kwp():
            return sum(a.panel_count for a in arrays) * panel_watt / 1000

        y = usable_maxy - box_h - y_offset

        while y >= usable_miny:
            if _achieved_kwp() >= target_kwp:
                break

            _pack_row(
                seg_minx=usable_minx,
                seg_maxx=usable_maxx,
                row_y=y,
                box_w=box_w,
                box_h=box_h,
                panels_per_box=panels_per_box,
                arrays=arrays,
                fits_fn=_fits,
                achieved_kwp_fn=_achieved_kwp,
                target_kwp=target_kwp,
            )

            y -= box_h

        if len(arrays) > len(best_arrays):
            best_arrays = arrays

    return best_arrays


def _pack_row(
    seg_minx: float,
    seg_maxx: float,
    row_y: float,
    box_w: float,
    box_h: float,
    panels_per_box: int,
    arrays: list[PanelArray],
    fits_fn,
    achieved_kwp_fn,
    target_kwp: float,
) -> None:
    """Pack one row with two-pass centering.

    Pass 1: scan left-to-right to find all valid box placements.
    Pass 2: center the group within the row via offset bisection.
    """
    # --- Pass 1: scan and collect placements ---
    placements: list[float] = []  # x positions
    scan_step = max(INTER_ARRAY_SPACING_EW_M, 0.5)  # minimum 0.5m scan step
    x = seg_minx

    while x + box_w <= seg_maxx + 0.01:
        if fits_fn(x, row_y, box_w, box_h):
            placements.append(x)
            x += box_w + INTER_ARRAY_SPACING_EW_M  # abut (0 gap) or spaced
        else:
            x += scan_step

    if not placements:
        return

    # --- Pass 2: center via offset bisection ---
    group_left = placements[0]
    group_right = placements[-1] + box_w
    group_center = (group_left + group_right) / 2
    seg_center = (seg_minx + seg_maxx) / 2
    desired_offset = seg_center - group_center

    def _all_fit(off: float) -> bool:
        return all(fits_fn(px + off, row_y, box_w, box_h) for px in placements)

    if _all_fit(desired_offset):
        offset = desired_offset
    else:
        lo, hi = 0.0, 1.0
        for _ in range(10):
            mid = (lo + hi) / 2
            if _all_fit(desired_offset * mid):
                lo = mid
            else:
                hi = mid
        offset = desired_offset * lo

    for px in placements:
        if achieved_kwp_fn() >= target_kwp:
            return
        new_x = px + offset
        if fits_fn(new_x, row_y, box_w, box_h):
            arrays.append(
                PanelArray(
                    origin_x=new_x,
                    origin_y=row_y,
                    panel_count=panels_per_box,
                    box_width=box_w,
                    box_height=box_h,
                )
            )


# ---------------------------------------------------------------------------
# ESS optimal placement
# ---------------------------------------------------------------------------
ESS_KEEPOUT_M = 1.5  # Minimum clearance around ESS to adjacent arrays


def _compute_ess_zone(
    usable: Polygon,
    panels_per_box: int,
    panel_watt: float,
    target_kwp: float,
    ess_module_count: int | None = None,
    placement: str = "outer",
) -> tuple[
    tuple[float, float, float, float],
    list[tuple[float, float]],
    tuple[float, float, float, float] | None,
    Polygon,
]:
    """Compute ESS zone before box packing.

    placement="outer": near feeder pillar / entrance edge (default).
    placement="center": centered in usable area (legacy behavior).

    Reserves space for current ESS plinths plus 2x expansion (capped at 10).
    Returns (energy_rect, ess_modules, ess_plinth_rect, energy_zone_polygon).
    The energy_zone polygon (plinth + keepouts) is passed to _pack_boxes so
    panel boxes pack tightly around it.
    """
    if ess_module_count is not None:
        ess_count = min(ESS_MAX_MODULES, max(1, ess_module_count))
    else:
        kwp_per_ess = panels_per_box * panel_watt / 1000
        if kwp_per_ess <= 0:
            kwp_per_ess = 9.1
        ess_count = min(ESS_MAX_MODULES, math.ceil(target_kwp / kwp_per_ess))
        ess_count = max(1, ess_count)

    # Reserve space for 2x expansion, capped at ESS_MAX_MODULES
    reserved_count = min(ess_count * 2, ESS_MAX_MODULES)

    reserved_plinth_w = reserved_count * ESS_PLINTH_WIDTH_M
    reserved_plinth_h = ESS_PLINTH_DEPTH_M

    ux0, uy0, ux1, uy1 = usable.bounds

    if placement == "center":
        # Center in usable area — ESS sits among the arrays
        cx = (ux0 + ux1) / 2
        cy = (uy0 + uy1) / 2
        px = cx - reserved_plinth_w / 2
        py = cy - reserved_plinth_h / 2
    else:
        # Outer: at the edge of the usable area, not among the arrays.
        # Try corners/edges of the usable polygon until the ESS rect fits inside.
        cx = (ux0 + ux1) / 2
        cy = (uy0 + uy1) / 2
        # Candidate anchors: polygon vertices + edge midpoints.
        # For each, nudge the plinth inward toward the centroid so it sits just
        # inside the boundary rather than straddling it.
        coords = list(usable.exterior.coords[:-1])
        anchors = []
        for i, (vx, vy) in enumerate(coords):
            anchors.append((vx, vy))
            nx, ny = coords[(i + 1) % len(coords)]
            anchors.append(((vx + nx) / 2, (vy + ny) / 2))

        # Sort by Y ascending (bottom first), then by distance from center-X
        anchors.sort(key=lambda c: (c[1], abs(c[0] - cx)))

        px, py = cx - reserved_plinth_w / 2, cy - reserved_plinth_h / 2  # fallback
        for ax, ay in anchors:
            # Nudge anchor toward centroid so plinth lands inside
            dx, dy = cx - ax, cy - ay
            norm = math.hypot(dx, dy) or 1.0
            nudge = max(reserved_plinth_w, reserved_plinth_h) / 2 + 0.5
            nx = ax + dx / norm * nudge
            ny = ay + dy / norm * nudge
            test_px = nx - reserved_plinth_w / 2
            test_py = ny - reserved_plinth_h / 2
            test_rect = box(
                test_px, test_py, test_px + reserved_plinth_w, test_py + reserved_plinth_h
            )
            if usable.contains(test_rect):
                px, py = test_px, test_py
                break

    # Energy zone = reserved plinth + keepouts (what boxes must avoid)
    energy_zone = box(
        px - ESS_KEEPOUT_M,
        py - ESS_KEEPOUT_M,
        px + reserved_plinth_w + ESS_KEEPOUT_M,
        py + reserved_plinth_h + ESS_KEEPOUT_M,
    )

    # Actual ESS modules placed within the reserved plinth area (left-aligned)
    actual_plinth_w = ess_count * ESS_PLINTH_WIDTH_M
    # Center the actual modules within the reserved width
    mod_start_x = px + (reserved_plinth_w - actual_plinth_w) / 2
    modules = _create_ess_modules(mod_start_x, py, ess_count)

    energy_rect = (px, py, reserved_plinth_w, reserved_plinth_h)
    plinth_rect = (px, py, reserved_plinth_w, reserved_plinth_h)

    return energy_rect, modules, plinth_rect, energy_zone


def _create_ess_modules(ex: float, ey: float, ess_count: int) -> list[tuple[float, float]]:
    """Create ESS module origins within the combined plinth."""
    modules = []
    for i in range(ess_count):
        mod_x = ex + i * ESS_PLINTH_WIDTH_M + (ESS_PLINTH_WIDTH_M - ESS_MODULE_FRONT_M) / 2
        mod_y = ey + (ESS_PLINTH_DEPTH_M - ESS_MODULE_DEPTH_M) / 2
        modules.append((mod_x, mod_y))
    return modules


# ---------------------------------------------------------------------------
# Plinths, entrance, lightning
# ---------------------------------------------------------------------------
def _place_plinths(array: PanelArray) -> list[tuple[float, float]]:
    """Compute plinth positions within a box.

    Formula: ceil(panel_count / 3) plinths, evenly spaced along the box width.
    """
    cols = math.ceil(array.panel_count / 3)
    plinths = []
    aw = array.array_width

    for c in range(cols):
        if cols > 1:
            px = array.origin_x + (c / (cols - 1)) * (aw - PLINTH_SIZE_M)
        else:
            px = array.origin_x + (aw - PLINTH_SIZE_M) / 2
        py = array.origin_y + (array.array_height - PLINTH_SIZE_M) / 2
        plinths.append((px, py))

    return plinths


GATE_KEEPOUT_M = 6.0  # Clear zone depth on the gate side for vehicle access


def _entrance_from_gate(
    boundary: Polygon,
    gate_pos: tuple[float, float],
) -> tuple[tuple[float, float], tuple[tuple[float, float], tuple[float, float]]]:
    """Find entrance position and edge from a gate point on the boundary.

    Snaps gate_pos to the nearest boundary edge and returns the midpoint
    and edge endpoints.
    """
    from shapely.geometry import Point

    gate_point = Point(gate_pos)
    coords = list(boundary.exterior.coords)
    best_dist = float("inf")
    best_edge = (coords[0], coords[1])

    for i in range(len(coords) - 1):
        p1 = coords[i]
        p2 = coords[i + 1]
        from shapely.geometry import LineString

        edge_line = LineString([p1, p2])
        dist = edge_line.distance(gate_point)
        if dist < best_dist:
            best_dist = dist
            best_edge = (p1, p2)

    # Snap to the nearest point on the edge
    from shapely.geometry import LineString as LS

    edge_line = LS([best_edge[0], best_edge[1]])
    nearest = edge_line.interpolate(edge_line.project(gate_point))
    return (nearest.x, nearest.y), best_edge


def _gate_keepout(
    entrance_edge: tuple[tuple[float, float], tuple[float, float]] | None,
    boundary: Polygon,
) -> Polygon | None:
    """Create a 6m deep keepout zone along the gate edge for vehicle access.

    Returns a polygon covering the gate edge buffered inward by GATE_KEEPOUT_M,
    or None if the edge is invalid.
    """
    if entrance_edge is None:
        return None

    from shapely.geometry import LineString

    edge_line = LineString([entrance_edge[0], entrance_edge[1]])
    if edge_line.length < 0.1:
        return None

    # Buffer the edge on both sides, then intersect with boundary to get only
    # the inward-facing keepout strip
    keepout_strip = edge_line.buffer(GATE_KEEPOUT_M, cap_style="flat")
    keepout_inside = keepout_strip.intersection(boundary)
    if keepout_inside.is_empty:
        return None

    return keepout_inside if isinstance(keepout_inside, Polygon) else None


def _find_entrance(
    boundary: Polygon,
) -> tuple[tuple[float, float], tuple[tuple[float, float], tuple[float, float]]]:
    """Find entrance position at midpoint of the longest boundary edge."""
    coords = list(boundary.exterior.coords)
    longest_len = 0.0
    longest_edge = (coords[0], coords[1])

    for i in range(len(coords) - 1):
        p1 = coords[i]
        p2 = coords[i + 1]
        edge_len = math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)
        if edge_len > longest_len:
            longest_len = edge_len
            longest_edge = (p1, p2)

    mid_x = (longest_edge[0][0] + longest_edge[1][0]) / 2
    mid_y = (longest_edge[0][1] + longest_edge[1][1]) / 2
    return (mid_x, mid_y), longest_edge


def _place_lightning(
    arrays: list[PanelArray],
    energy_rect: tuple[float, float, float, float],
    feeder_pos: tuple[float, float],
    boundary: Polygon,
) -> list[tuple[float, float]]:
    """Place lightning arresters on a grid covering all energy assets.

    The first arrester is always placed adjacent to the energy cabin (at the
    midpoint of the cabin's south-facing exterior edge). Subsequent arresters
    are placed on a regular grid covering the asset bounding box, skipping
    any position already covered by the cabin arrester.
    """
    if not arrays and energy_rect[2] == 0:
        return []

    asset_minx = float("inf")
    asset_miny = float("inf")
    asset_maxx = float("-inf")
    asset_maxy = float("-inf")

    for arr in arrays:
        asset_minx = min(asset_minx, arr.origin_x)
        asset_miny = min(asset_miny, arr.origin_y)
        asset_maxx = max(asset_maxx, arr.origin_x + arr.array_width)
        asset_maxy = max(asset_maxy, arr.origin_y + arr.array_height)

    ex, ey, ew, eh = energy_rect
    if ew > 0:
        asset_minx = min(asset_minx, ex)
        asset_miny = min(asset_miny, ey)
        asset_maxx = max(asset_maxx, ex + ew)
        asset_maxy = max(asset_maxy, ey + eh)

    # Expand bounding box to include feeder pillar (do NOT remove this block)
    asset_minx = min(asset_minx, feeder_pos[0])
    asset_miny = min(asset_miny, feeder_pos[1])
    asset_maxx = max(asset_maxx, feeder_pos[0] + FEEDER_PILLAR_SIZE_M)
    asset_maxy = max(asset_maxy, feeder_pos[1] + FEEDER_PILLAR_SIZE_M)

    spacing = LIGHTNING_RADIUS_M * math.sqrt(2)

    # First arrester: adjacent to power plant at the south face midpoint of the cabin.
    # This is the exterior edge of the cabin, not the centre (which is inside the footprint).
    cabin_arrester: tuple[float, float] = (ex + ew / 2, ey)
    positions: list[tuple[float, float]] = [cabin_arrester]

    # Grid scan — skip candidates already covered by the cabin arrester
    y = asset_miny + LIGHTNING_RADIUS_M / 2
    while y <= asset_maxy:
        x = asset_minx + LIGHTNING_RADIUS_M / 2
        while x <= asset_maxx:
            if boundary.contains(Point(x, y)):
                if math.hypot(x - cabin_arrester[0], y - cabin_arrester[1]) >= LIGHTNING_RADIUS_M:
                    positions.append((x, y))
            x += spacing
        y += spacing

    return positions


# ---------------------------------------------------------------------------
# Earth pit (grounding electrode) placement — IEC 60364-5-54, IEEE 142
# ---------------------------------------------------------------------------
def _find_pit2(
    pit1: Point,
    array_boxes: list[Polygon],
    edge_midpoints: list[Point],
    boundary: Polygon,
) -> Point | None:
    """Find the best position for Pit 2: 10–25m from Pit 1, close to an array edge.

    Uses the precomputed edge_midpoints (4 per array bounding box) to avoid
    recomputing them in both _find_pit2 and the extra-pits loop. Picks the
    midpoint inside the 10–25m donut closest to any array perimeter. Falls
    back to the nearest point on the closest array exterior if no midpoint qualifies.
    """
    best: Point | None = None
    best_dist_to_edge = float("inf")

    for mid in edge_midpoints:
        d = pit1.distance(mid)
        if EARTH_PIT2_MIN_DIST_M <= d <= EARTH_PIT2_MAX_DIST_M:
            if not boundary.contains(mid):
                continue
            dist_to_edge = min(mid.distance(b.exterior) for b in array_boxes)
            if dist_to_edge < best_dist_to_edge:
                best_dist_to_edge = dist_to_edge
                best = mid

    if best is not None:
        return best

    # Fallback: nearest point on any array exterior, clamped to 10–25m from pit1
    for arr_box in array_boxes:
        near_on_arr, _ = nearest_points(arr_box.exterior, pit1)
        d = pit1.distance(near_on_arr)
        if d < EARTH_PIT2_MIN_DIST_M and d > 0:
            # Array is closer than 10m — project outward to 10m minimum
            dx = near_on_arr.x - pit1.x
            dy = near_on_arr.y - pit1.y
            near_on_arr = Point(
                pit1.x + EARTH_PIT2_MIN_DIST_M * dx / d,
                pit1.y + EARTH_PIT2_MIN_DIST_M * dy / d,
            )
            d = EARTH_PIT2_MIN_DIST_M
        if d > EARTH_PIT2_MAX_DIST_M or not boundary.contains(near_on_arr):
            continue
        dist_to_edge = min(near_on_arr.distance(b.exterior) for b in array_boxes)
        if dist_to_edge < best_dist_to_edge:
            best_dist_to_edge = dist_to_edge
            best = near_on_arr

    if best is None:
        logger.warning("earth_pits: could not place Pit 2 in 10–25m donut from pit1 %s", pit1)
    return best


def _place_earth_pits(
    arrays: list[PanelArray],
    energy_rect: tuple[float, float, float, float],
    lightning_positions: list[tuple[float, float]],
    boundary: Polygon,
) -> list[tuple[float, float]]:
    """Place earth grounding pits (IEC 60364-5-54).

    Returns list of (x, y) in site-local UTM meters, Pit 1 always first.
    Pit 1 is co-located with lightning_positions[0] (the cabin exterior edge arrester).

    Placement rules:
    - Pit 1: at the cabin exterior edge, same position as the first lightning arrester
    - Pit 2: 10–25m from Pit 1, as close to a solar array edge as possible
    - Extra pits (up to EARTH_PIT_MAX_COUNT total): added if any array edge midpoint
      is >30m from all existing pits; no two pits may be <10m apart
    """
    # Pit 1: co-located with first lightning arrester (cabin south edge midpoint)
    if lightning_positions:
        pit1 = Point(lightning_positions[0])
    else:
        ex, ey, ew, _eh = energy_rect
        pit1 = Point(ex + ew / 2, ey)
    pits: list[Point] = [pit1]

    if not arrays:
        return [(pit1.x, pit1.y)]

    # Build array bounding boxes and their edge midpoints once — reused by
    # _find_pit2 and the extra-pits coverage loop below.
    array_boxes_and_mids: list[tuple[Polygon, list[Point]]] = []
    for arr in arrays:
        arr_box = box(
            arr.origin_x,
            arr.origin_y,
            arr.origin_x + arr.box_width,
            arr.origin_y + arr.box_height,
        )
        minx, miny, maxx, maxy = arr_box.bounds
        mids = [
            Point((minx + maxx) / 2, miny),  # south
            Point((minx + maxx) / 2, maxy),  # north
            Point(minx, (miny + maxy) / 2),  # west
            Point(maxx, (miny + maxy) / 2),  # east
        ]
        array_boxes_and_mids.append((arr_box, mids))

    array_boxes = [ab for ab, _ in array_boxes_and_mids]
    all_edge_midpoints = [mid for _, mids in array_boxes_and_mids for mid in mids]

    # Pit 2: 10–25m from Pit 1, closest to any array edge
    pit2 = _find_pit2(pit1, array_boxes, all_edge_midpoints, boundary)
    if pit2 is not None:
        pits.append(pit2)

    # Extra pits: cover any array edge midpoint >30m from all existing pits
    for arr_box, edge_mids in array_boxes_and_mids:
        if len(pits) >= EARTH_PIT_MAX_COUNT:
            break
        cx = arr_box.centroid.x
        cy = arr_box.centroid.y
        for mid in edge_mids:
            if min(mid.distance(p) for p in pits) <= EARTH_PIT_COVERAGE_M:
                continue
            # This edge midpoint is uncovered — place a pit 2m inward toward array centroid
            dist = mid.distance(arr_box.centroid)
            if dist > 0:
                candidate = Point(
                    mid.x + 2.0 * (cx - mid.x) / dist,
                    mid.y + 2.0 * (cy - mid.y) / dist,
                )
            else:
                candidate = mid
            if boundary.contains(candidate) and all(
                candidate.distance(p) >= EARTH_PIT_SPACING_MIN_M for p in pits
            ):
                pits.append(candidate)
                if len(pits) >= EARTH_PIT_MAX_COUNT:
                    break

    return [(p.x, p.y) for p in pits]


# ---------------------------------------------------------------------------
# Cable routing — diagonal with obstacle avoidance and bunching
# ---------------------------------------------------------------------------
def _point_to_segment_dist(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    """Perpendicular distance from point (px, py) to line segment (x1,y1)-(x2,y2)."""
    dx = x2 - x1
    dy = y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)


def _project_onto_segment(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> tuple[float, float]:
    """Closest point on segment (x1,y1)-(x2,y2) to point (px,py)."""
    dx = x2 - x1
    dy = y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return (x1, y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    return (x1 + t * dx, y1 + t * dy)


def _sample_path(route: CableRoute, step: float = 1.0) -> list[tuple[float, float, float]]:
    """Sample points along route path at `step` intervals. Returns [(x, y, dist_to_end)]."""
    pts = [route.start] + route.waypoints + [route.end]
    # Compute cumulative distances from each vertex to the end
    seg_lengths = []
    for i in range(len(pts) - 1):
        seg_lengths.append(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]))
    total_length = sum(seg_lengths)

    samples: list[tuple[float, float, float]] = []
    cum_from_start = 0.0
    for i in range(len(pts) - 1):
        seg_len = seg_lengths[i]
        if seg_len < 1e-9:
            continue
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        d = 0.0
        while d <= seg_len:
            frac = d / seg_len
            x = pts[i][0] + frac * dx
            y = pts[i][1] + frac * dy
            dist_to_end = total_length - (cum_from_start + d)
            samples.append((x, y, dist_to_end))
            d += step
        cum_from_start += seg_len
    # Always include endpoint
    if not samples or math.hypot(samples[-1][0] - pts[-1][0], samples[-1][1] - pts[-1][1]) > 0.01:
        samples.append((pts[-1][0], pts[-1][1], 0.0))
    return samples


def _apply_magnetic_trenches(
    routes: list[CableRoute],
    merge_dist: float = 3.0,
    min_end_dist: float = 8.0,
    max_per_bunch: int = 4,
) -> None:
    """Merge converging DC cables into shared trenches.

    Detects pairs of DC cables from different bunches that approach within
    merge_dist of each other while still min_end_dist from the ESS endpoint,
    and reroutes one to merge into the other's trench.
    """
    from collections import Counter

    dc_indices = [i for i, r in enumerate(routes) if r.cable_type == "dc"]
    if len(dc_indices) < 2:
        return

    # Sample all DC paths
    samples_by_idx: dict[int, list[tuple[float, float, float]]] = {}
    for i in dc_indices:
        samples_by_idx[i] = _sample_path(routes[i])

    # Find candidate merges
    candidates: list[tuple[float, int, int, float, float]] = []  # (dist, i, j, mx, my)
    merged: set[int] = set()

    for ai, idx_a in enumerate(dc_indices):
        for idx_b in dc_indices[ai + 1 :]:
            if routes[idx_a].bunch_id == routes[idx_b].bunch_id:
                continue
            best_dist = float("inf")
            best_point: tuple[float, float] | None = None
            for xa, ya, da in samples_by_idx[idx_a]:
                if da < min_end_dist:
                    continue
                for xb, yb, db in samples_by_idx[idx_b]:
                    if db < min_end_dist:
                        continue
                    d = math.hypot(xa - xb, ya - yb)
                    if d < best_dist:
                        best_dist = d
                        best_point = ((xa + xb) / 2, (ya + yb) / 2)
            if best_dist < merge_dist and best_point is not None:
                candidates.append((best_dist, idx_a, idx_b, best_point[0], best_point[1]))

    candidates.sort(key=lambda c: c[0])

    for _dist, idx_a, idx_b, mx, my in candidates:
        if idx_a in merged or idx_b in merged:
            continue

        bunch_a = routes[idx_a].bunch_id
        bunch_b = routes[idx_b].bunch_id

        # Count current bunch sizes
        bunch_counts = Counter(routes[i].bunch_id for i in dc_indices)
        size_a = bunch_counts.get(bunch_a, 0)
        size_b = bunch_counts.get(bunch_b, 0)

        if size_a + size_b > max_per_bunch:
            continue

        # Host = route from larger bunch (or a if equal)
        if size_b > size_a:
            host_idx, joiner_idx = idx_b, idx_a
            host_bunch = bunch_b
            joiner_bunch = bunch_a
        else:
            host_idx, joiner_idx = idx_a, idx_b
            host_bunch = bunch_a
            joiner_bunch = bunch_b

        # Project merge point onto host path
        host_pts = [routes[host_idx].start] + routes[host_idx].waypoints + [routes[host_idx].end]
        best_proj = host_pts[0]
        best_proj_dist = float("inf")
        for si in range(len(host_pts) - 1):
            proj = _project_onto_segment(
                mx,
                my,
                host_pts[si][0],
                host_pts[si][1],
                host_pts[si + 1][0],
                host_pts[si + 1][1],
            )
            d = math.hypot(proj[0] - mx, proj[1] - my)
            if d < best_proj_dist:
                best_proj_dist = d
                best_proj = proj

        # Reroute joiner: start → projection → ESS endpoint
        joiner = routes[joiner_idx]
        joiner.waypoints = [best_proj]

        # Recalculate joiner length
        pts = [joiner.start] + joiner.waypoints + [joiner.end]
        joiner.length_m = sum(
            math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1])
            for k in range(len(pts) - 1)
        )

        # Reassign all routes with joiner's old bunch_id to host's bunch_id
        for i in dc_indices:
            if routes[i].bunch_id == joiner_bunch:
                routes[i].bunch_id = host_bunch

        merged.add(joiner_idx)


def _assign_bunches(routes: list[CableRoute], max_per_bunch: int = 4) -> None:
    """Group DC routes by row Y and direction, cap at max_per_bunch.

    Modifies route.bunch_id in place. Bunch IDs start at 1.
    AC routes keep bunch_id=0.
    """
    from collections import defaultdict

    groups: dict[tuple[float, bool], list[CableRoute]] = defaultdict(list)
    for r in routes:
        if r.cable_type != "dc":
            continue
        row_y = round(r.start[1], 1)
        goes_right = r.start[0] < r.end[0]
        groups[(row_y, goes_right)].append(r)

    bunch_id = 1
    for _key, group in groups.items():
        for i in range(0, len(group), max_per_bunch):
            chunk = group[i : i + max_per_bunch]
            for r in chunk:
                r.bunch_id = bunch_id
            bunch_id += 1


def _compute_cable_routes(
    arrays: list[PanelArray],
    energy_rect: tuple[float, float, float, float],
    feeder_pos: tuple[float, float],
) -> list[CableRoute]:
    """Compute cable routes with diagonal routing, obstacle avoidance, and bunching.

    DC cables run diagonally from the nearest short edge center of each PV array
    to the energy system center. If any plinth center is within 0.8m of the direct
    line (0.5m clearance + 0.3m plinth half-diagonal), the path detours vertically
    to clear all plinths in one pass (all plinths in a row share the same Y).

    AC cable runs directly diagonal from energy system to feeder pillar.

    Min curvature radius: detour waypoints are ≥2.4m apart horizontally with ≥1.15m
    vertical offset, giving a quadratic bezier radius >> 1m by construction.
    """
    ex, ey, ew, eh = energy_rect
    ess_cx = ex + ew / 2
    ess_cy = ey + eh / 2

    # Step 1: collect plinth centers for same-row neighbor arrays.
    # Only same-row plinths need avoidance — the cable exits into the row gap.
    # Plinths from other rows that the diagonal crosses are in separate zones.
    clearance = 0.8  # 0.5m clearance + 0.3m plinth half-diagonal
    routes: list[CableRoute] = []

    # Group arrays by row (same origin_y)
    row_arrays: dict[float, list[int]] = {}
    for i, arr in enumerate(arrays):
        row_key = round(arr.origin_y, 1)
        row_arrays.setdefault(row_key, []).append(i)

    # Step 2: route each DC cable
    for idx, arr in enumerate(arrays):
        arr_cy = arr.origin_y + arr.array_height / 2

        # Pick nearest short edge center (left or right face at Y-midpoint)
        left_edge = (arr.origin_x, arr_cy)
        right_edge = (arr.origin_x + arr.array_width, arr_cy)
        dist_left = math.hypot(left_edge[0] - ess_cx, left_edge[1] - ess_cy)
        dist_right = math.hypot(right_edge[0] - ess_cx, right_edge[1] - ess_cy)
        start = left_edge if dist_left <= dist_right else right_edge
        end = (ess_cx, ess_cy)

        # Collect same-row plinth centers (excluding own array and start-adjacent)
        row_key = round(arr.origin_y, 1)
        same_row_plinths: list[tuple[float, float]] = []
        for neighbor_idx in row_arrays.get(row_key, []):
            if neighbor_idx == idx:
                continue
            for px, py in arrays[neighbor_idx].plinths:
                cx, cy = px + PLINTH_SIZE_M / 2, py + PLINTH_SIZE_M / 2
                # Skip plinths right at the exit point (abutting boundary)
                if math.hypot(cx - start[0], cy - start[1]) <= clearance:
                    continue
                same_row_plinths.append((cx, cy))

        hits = [
            (pcx, pcy)
            for pcx, pcy in same_row_plinths
            if _point_to_segment_dist(pcx, pcy, start[0], start[1], end[0], end[1]) < clearance
        ]

        waypoints: list[tuple[float, float]] = []
        if hits:
            # All plinths in a row share the same Y — one offset clears them all
            hit_y = hits[0][1]
            mid_x = (start[0] + end[0]) / 2
            # Pick shorter detour direction (above or below)
            offset_above = hit_y + clearance
            offset_below = hit_y - clearance
            mid_y_direct = (start[1] + end[1]) / 2
            if abs(offset_above - mid_y_direct) <= abs(offset_below - mid_y_direct):
                detour_y = offset_above
            else:
                detour_y = offset_below
            waypoints = [(mid_x, detour_y)]

        # Compute length along the path
        pts = [start] + waypoints + [end]
        length = sum(
            math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
            for i in range(len(pts) - 1)
        )

        routes.append(
            CableRoute(
                start=start,
                end=end,
                waypoints=waypoints,
                cable_type="dc",
                length_m=length,
                label=f"DC-{idx + 1}",
            )
        )

    # Step 3: AC route — direct diagonal, no obstacle avoidance
    fp_cx = feeder_pos[0] + FEEDER_PILLAR_SIZE_M / 2
    fp_cy = feeder_pos[1] + FEEDER_PILLAR_SIZE_M / 2
    ac_start = (ess_cx, ess_cy)
    ac_end = (fp_cx, fp_cy)
    ac_length = math.hypot(ac_end[0] - ac_start[0], ac_end[1] - ac_start[1])

    routes.append(
        CableRoute(
            start=ac_start,
            end=ac_end,
            waypoints=[],
            cable_type="ac",
            length_m=ac_length,
            label="AC",
        )
    )

    # Step 4: assign bunches
    _assign_bunches(routes)

    # Step 5: magnetic trenches — merge converging cables
    _apply_magnetic_trenches(routes)

    return routes
