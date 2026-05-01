"""
Core distribution layout algorithm: pole placement, building connection,
backbone optimization, and coverage iteration.

All computations are done in projected UTM coordinates for accurate distances.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
from scipy.spatial import KDTree
from shapely import line_interpolate_point
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

logger = logging.getLogger(__name__)

# Minimum distance between poles before deduplication (meters)
POLE_DEDUP_DISTANCE_M = float(os.getenv("LAYOUT_POLE_DEDUP_DISTANCE_M", "5.0"))

# Intersection-aware snapping constants
_SNAP_NODE_TOLERANCE_M = float(os.getenv("LAYOUT_SNAP_NODE_TOLERANCE_M", "1.0"))
_MERGE_GAP_THRESHOLD_M = float(os.getenv("LAYOUT_MERGE_GAP_THRESHOLD_M", "5.0"))
_REDISTRIBUTE_GAP_MAX_M = float(os.getenv("LAYOUT_REDISTRIBUTE_GAP_MAX_M", "10.0"))

# Weight penalty applied to building-path adjacency in backbone Dijkstra.
# Higher values make the backbone prefer road-based routes over building paths.
_BUILDING_PATH_WEIGHT_PENALTY = float(os.getenv("LAYOUT_PATH_WEIGHT_PENALTY", "3.0"))

# Maximum distance (meters) for bridge edges between disconnected components.
# Pairs farther apart than this are not bridged (avoids unrealistic long cables).
_MAX_BRIDGE_DISTANCE_M = float(os.getenv("LAYOUT_MAX_BRIDGE_DISTANCE_M", "200.0"))

# Plant pole force-connect: search radius and neighbor count.
_PLANT_CONNECT_DISTANCE_M = float(os.getenv("LAYOUT_PLANT_CONNECT_DISTANCE_M", "150.0"))
_PLANT_CONNECT_K = int(os.getenv("LAYOUT_PLANT_CONNECT_K", "5"))

# Default power factor for LV distribution cables (configurable).
DEFAULT_POWER_FACTOR = float(os.getenv("LAYOUT_POWER_FACTOR", "0.95"))


def _build_node_lookup(
    nodes_gdf: gpd.GeoDataFrame,
) -> tuple[KDTree, np.ndarray]:
    """Build a KDTree from node coordinates for fast endpoint matching.

    Returns (KDTree, node_ids_array) where node_ids_array[i] is the
    original index of the i-th point in the tree.
    """
    node_ids = np.array(nodes_gdf.index)
    coords = np.array([(pt.x, pt.y) for pt in nodes_gdf.geometry])
    return KDTree(coords), node_ids


def _match_endpoint_to_node(
    pt_coords: tuple[float, float],
    node_tree: KDTree,
    node_ids: np.ndarray,
    tolerance: float = _SNAP_NODE_TOLERANCE_M,
) -> int | None:
    """Match a coordinate to the nearest road network node within tolerance.

    Returns the node index (from nodes_gdf.index) or None if no match.
    """
    dist, idx = node_tree.query(pt_coords)
    if dist <= tolerance:
        return int(node_ids[idx])
    return None


def _compute_snapped_positions(
    length: float,
    spacing_m: float,
    has_start: bool,
    has_end: bool,
) -> list[float]:
    """Compute intermediate pole positions along an edge, snapping to intersection anchors.

    Pure function — no geometry, just distances along the edge.

    Args:
        length: Edge length in meters.
        spacing_m: Target pole spacing.
        has_start: True if start endpoint has an intersection pole.
        has_end: True if end endpoint has an intersection pole.

    Returns:
        List of distances along the edge for intermediate poles.
    """
    # Raw positions: 0, spacing, 2*spacing, ..., up to length
    positions = []
    d = 0.0
    while d <= length + 0.1:
        positions.append(min(d, length))
        d += spacing_m
        if d > length and (not positions or positions[-1] < length):
            positions.append(length)
            break

    if not positions:
        return []

    # Remove start if intersection pole covers it
    if has_start and positions and positions[0] < 0.1:
        positions.pop(0)

    # Remove end if intersection pole covers it
    if has_end and positions and abs(positions[-1] - length) < 0.1:
        positions.pop()

    # Far-end snapping: adjust last pole(s) when close to end intersection
    if has_end and positions:
        gap = length - positions[-1]
        if gap < _MERGE_GAP_THRESHOLD_M:
            # Absorbed: last pole too close to intersection
            positions.pop()
        elif gap < _REDISTRIBUTE_GAP_MAX_M and len(positions) >= 2:
            # Redistribute: shift last two poles forward
            positions[-1] += gap / 2
            positions[-2] += gap / 4

    return positions


def place_poles_along_roads(
    edges_gdf: Any,
    nodes_gdf: Any,
    spacing_m: float = 45.0,
    boundary: Any = None,
) -> gpd.GeoDataFrame:
    """Place poles at regular intervals along road edges and at intersections.

    Uses intersection-aware snapping: intersection poles are placed first as
    anchors, then each edge walk snaps to those anchors — producing exactly
    one pole per junction and clean, evenly-spaced intermediates.

    Args:
        edges_gdf: Road edges GeoDataFrame in projected UTM CRS.
        nodes_gdf: Road nodes GeoDataFrame in projected UTM CRS.
        spacing_m: Distance between consecutive poles in meters.
        boundary: Optional Shapely Polygon (projected UTM) — poles outside
            this boundary are skipped.

    Returns:
        GeoDataFrame with Point geometry for each pole, columns:
        - geometry: Point (UTM)
        - pole_type: "intermediate" or "intersection"
        - road_node_id: nearest road graph node ID (for backbone optimization)
    """
    empty_result = gpd.GeoDataFrame(
        columns=["geometry", "pole_type", "road_node_id"],
        crs=edges_gdf.crs,
    )

    if len(nodes_gdf) == 0 and len(edges_gdf) == 0:
        return empty_result

    # --- Phase 1: Intersection poles as anchors ---
    intersection_poles: dict[int, dict] = {}
    if len(nodes_gdf) > 0:
        node_tree, node_ids = _build_node_lookup(nodes_gdf)

        for node_id, row in nodes_gdf.iterrows():
            pt = Point(row.geometry.x, row.geometry.y)
            if boundary is not None and not boundary.contains(pt):
                continue
            intersection_poles[node_id] = {
                "geometry": pt,
                "pole_type": "intersection",
                "road_node_id": node_id,
            }
    else:
        node_tree = None
        node_ids = np.array([])

    # --- Phase 2: Walk each edge, snapping to anchors ---
    intermediate_poles: list[dict] = []

    for _, row in edges_gdf.iterrows():
        line_geom = row.geometry
        if line_geom is None or line_geom.is_empty:
            continue

        length = line_geom.length
        if length < 1.0:
            continue

        # Match endpoints to road network nodes
        coords = list(line_geom.coords)
        start_node = None
        end_node = None
        if node_tree is not None and len(node_ids) > 0:
            start_node = _match_endpoint_to_node(coords[0], node_tree, node_ids)
            end_node = _match_endpoint_to_node(coords[-1], node_tree, node_ids)

        has_start = start_node is not None and start_node in intersection_poles
        has_end = end_node is not None and end_node in intersection_poles

        # Short edges: skip intermediates if both endpoints are anchored
        if length < spacing_m:
            if has_start and has_end:
                continue  # Both endpoints covered by intersection poles
            if not has_start and not has_end and length >= spacing_m * 0.5:
                # Place a midpoint pole
                pt = line_interpolate_point(line_geom, length / 2)
                if boundary is None or boundary.contains(pt):
                    intermediate_poles.append(
                        {"geometry": pt, "pole_type": "intermediate", "road_node_id": None}
                    )
            elif (has_start or has_end) and length >= spacing_m * 0.5:
                # One end anchored — place midpoint
                pt = line_interpolate_point(line_geom, length / 2)
                if boundary is None or boundary.contains(pt):
                    intermediate_poles.append(
                        {"geometry": pt, "pole_type": "intermediate", "road_node_id": None}
                    )
            continue

        # Normal edges: compute snapped positions
        positions = _compute_snapped_positions(length, spacing_m, has_start, has_end)

        for d in positions:
            pt = line_interpolate_point(line_geom, d)
            if boundary is not None and not boundary.contains(pt):
                continue  # skip this pole but keep walking — road may re-enter
            intermediate_poles.append(
                {"geometry": pt, "pole_type": "intermediate", "road_node_id": None}
            )

    # --- Phase 3: Collect and deduplicate ---
    all_poles = list(intersection_poles.values()) + intermediate_poles

    if not all_poles:
        return empty_result

    poles_gdf = gpd.GeoDataFrame(all_poles, crs=edges_gdf.crs)

    # Safety-net dedup for any remaining overlaps
    poles_gdf = _deduplicate_poles(poles_gdf)

    logger.info(f"Placed {len(poles_gdf)} poles (spacing={spacing_m}m)")
    return poles_gdf


def _deduplicate_poles(poles_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Remove poles within POLE_DEDUP_DISTANCE_M of each other.

    Prefers keeping intersection poles over intermediate poles.
    """
    if len(poles_gdf) < 2:
        return poles_gdf

    coords = np.array([(p.x, p.y) for p in poles_gdf.geometry])
    tree = KDTree(coords)
    pairs = tree.query_pairs(r=POLE_DEDUP_DISTANCE_M)

    to_remove: set[int] = set()
    for i, j in pairs:
        if i in to_remove or j in to_remove:
            continue
        # Prefer keeping plant and intersection poles
        i_ptype = poles_gdf.iloc[i]["pole_type"]
        j_ptype = poles_gdf.iloc[j]["pole_type"]
        i_priority = i_ptype in ("plant", "intersection")
        j_priority = j_ptype in ("plant", "intersection")
        if j_priority and not i_priority:
            to_remove.add(i)
        else:
            to_remove.add(j)

    if to_remove:
        logger.debug(f"Deduplicated {len(to_remove)} poles within {POLE_DEDUP_DISTANCE_M}m")
        poles_gdf = poles_gdf.drop(index=list(to_remove)).reset_index(drop=True)

    return poles_gdf


def connect_buildings(
    buildings_gdf: gpd.GeoDataFrame,
    poles_gdf: gpd.GeoDataFrame,
    max_drop_distance_m: float = 40.0,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, float]:
    """Connect buildings to nearest poles via drop cables.

    Args:
        buildings_gdf: Buildings with Point geometry (centroids) in projected UTM.
        poles_gdf: Poles with Point geometry in projected UTM.
        max_drop_distance_m: Maximum drop cable length in meters.

    Returns:
        Tuple of:
        - drop_cables_gdf: LineString features with length_meters and cable_type="drop"
        - updated_buildings_gdf: with 'connected' and 'closest_pole_point' columns
        - coverage_pct: percentage of buildings connected (0-100)
    """
    if len(buildings_gdf) == 0 or len(poles_gdf) == 0:
        return (
            gpd.GeoDataFrame(
                columns=["geometry", "length_meters", "cable_type"], crs=poles_gdf.crs
            ),
            buildings_gdf,
            0.0,
        )

    pole_coords = np.array([(p.x, p.y) for p in poles_gdf.geometry])
    bldg_coords = np.array([(p.x, p.y) for p in buildings_gdf.geometry])
    tree = KDTree(pole_coords)
    distances, nearest_indices = tree.query(bldg_coords)

    drop_cables = []
    connected_flags = []
    closest_points = []

    for bldg_idx in range(len(buildings_gdf)):
        dist = distances[bldg_idx]
        pole_idx = nearest_indices[bldg_idx]

        if dist <= max_drop_distance_m:
            bldg_pt = buildings_gdf.iloc[bldg_idx].geometry
            pole_pt = poles_gdf.iloc[pole_idx].geometry
            cable_line = LineString([pole_pt, bldg_pt])
            drop_cables.append(
                {
                    "geometry": cable_line,
                    "length_meters": dist,
                    "cable_type": "drop",
                    "building_idx": bldg_idx,
                    "pole_idx": int(pole_idx),
                }
            )
            connected_flags.append(True)
            closest_points.append((pole_pt.x, pole_pt.y))
        else:
            connected_flags.append(False)
            closest_points.append(None)

    # Update buildings with connection status
    updated_buildings = buildings_gdf.copy()
    updated_buildings["connected"] = connected_flags
    updated_buildings["closest_pole_point"] = closest_points

    # Create drop cables GeoDataFrame
    if drop_cables:
        drop_cables_gdf = gpd.GeoDataFrame(drop_cables, crs=poles_gdf.crs)
    else:
        drop_cables_gdf = gpd.GeoDataFrame(
            columns=["geometry", "length_meters", "cable_type"],
            crs=poles_gdf.crs,
        )

    connected_count = sum(connected_flags)
    total_count = len(buildings_gdf)
    coverage_pct = (connected_count / total_count * 100) if total_count > 0 else 0.0

    logger.info(
        f"Connected {connected_count}/{total_count} buildings "
        f"({coverage_pct:.1f}% coverage, max_drop={max_drop_distance_m}m)"
    )

    return drop_cables_gdf, updated_buildings, coverage_pct


def build_pole_adjacency(
    poles_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
    snap_m: float = 25.0,
) -> dict[int, dict[int, float]]:
    """Build pole-to-pole weighted adjacency by projecting poles onto road edges.

    For each road edge, finds all poles within *snap_m* distance, sorts them
    by projection along the edge, and connects consecutive poles as neighbors.
    Edge weights are the pole-to-pole distance, with a penalty multiplier for
    building-path edges so Dijkstra prefers road-based routes.

    Returns dict[int, dict[int, float]] mapping pole index -> {neighbor: weight}.
    """
    if len(poles_gdf) == 0 or len(edges_gdf) == 0:
        return {}

    pole_coords = np.array([(p.x, p.y) for p in poles_gdf.geometry])
    pole_tree = KDTree(pole_coords)
    adjacency: dict[int, dict[int, float]] = {}

    has_edge_type = "edge_type" in edges_gdf.columns

    for _, row in edges_gdf.iterrows():
        edge_geom = row.geometry
        if edge_geom is None or edge_geom.is_empty or edge_geom.length < 1.0:
            continue

        is_building_path = has_edge_type and row.get("edge_type") == "building_path"
        weight_penalty = _BUILDING_PATH_WEIGHT_PENALTY if is_building_path else 1.0

        # Find all poles near this edge using bounding box + snap buffer
        minx, miny, maxx, maxy = edge_geom.bounds
        half_diag = np.hypot((maxx - minx) / 2, (maxy - miny) / 2)
        center = [(minx + maxx) / 2, (miny + maxy) / 2]
        candidates = pole_tree.query_ball_point(center, half_diag + snap_m + 1)

        # Filter to poles actually within snap distance of the edge
        on_edge = []
        for pidx in candidates:
            pt = poles_gdf.iloc[pidx].geometry
            if edge_geom.distance(pt) <= snap_m:
                proj = edge_geom.project(pt)
                on_edge.append((proj, int(pidx)))

        if len(on_edge) < 2:
            continue

        # Sort by projection distance and connect consecutive poles
        on_edge.sort(key=lambda x: x[0])
        for i in range(len(on_edge) - 1):
            a = on_edge[i][1]
            b = on_edge[i + 1][1]
            if a != b:
                dist = abs(on_edge[i + 1][0] - on_edge[i][0])
                weight = max(dist, 1.0) * weight_penalty
                a_nb = adjacency.setdefault(a, {})
                b_nb = adjacency.setdefault(b, {})
                # Keep the lowest weight if multiple edges connect the same pair
                if b not in a_nb or weight < a_nb[b]:
                    a_nb[b] = weight
                    b_nb[a] = weight

    return adjacency


def _bridge_disconnected_components(
    adjacency: dict[int, dict[int, float]],
    pole_coords: np.ndarray,
) -> None:
    """Bridge disconnected components in the adjacency graph.

    Finds connected components and greedily adds minimum-distance edges
    between nearest pole pairs until the graph is fully connected.
    Bridge edges are penalised with ``_BUILDING_PATH_WEIGHT_PENALTY`` so
    Dijkstra still prefers road-based routes when available.

    Pairs farther than ``_MAX_BRIDGE_DISTANCE_M`` are never bridged.

    Modifies *adjacency* in place.
    """
    if len(pole_coords) == 0 or len(adjacency) < 2:
        return

    # Find connected components via networkx
    G_adj = nx.Graph()
    for u, nbrs in adjacency.items():
        for v, w in nbrs.items():
            G_adj.add_edge(u, v, weight=w)
    components = [sorted(c) for c in nx.connected_components(G_adj)]

    if len(components) <= 1:
        return

    logger.debug(f"Bridging {len(components)} disconnected adjacency components")

    # Build per-component KDTree for nearest-pair queries
    comp_data: list[tuple[list[int], np.ndarray, KDTree]] = []
    for comp in components:
        coords = pole_coords[comp]
        comp_data.append((comp, coords, KDTree(coords)))

    # Greedily merge closest component pairs
    while len(comp_data) > 1:
        best_dist = float("inf")
        best_pair: tuple[int, int] | None = None
        best_poles: tuple[int, int] | None = None

        for i in range(len(comp_data)):
            nodes_i, coords_i, tree_i = comp_data[i]
            for j in range(i + 1, len(comp_data)):
                nodes_j, coords_j, _ = comp_data[j]
                dists, idxs = tree_i.query(coords_j)
                min_j_local = int(np.argmin(dists))
                min_i_local = int(idxs[min_j_local])
                d = float(dists[min_j_local])
                if d < best_dist:
                    best_dist = d
                    best_pair = (i, j)
                    best_poles = (nodes_i[min_i_local], nodes_j[min_j_local])

        if best_pair is None or best_poles is None or best_dist > _MAX_BRIDGE_DISTANCE_M:
            break

        # Add bridge edge with penalty so Dijkstra prefers road routes
        a, b = best_poles
        weight = max(best_dist, 1.0) * _BUILDING_PATH_WEIGHT_PENALTY
        adjacency.setdefault(a, {})[b] = weight
        adjacency.setdefault(b, {})[a] = weight

        # Merge the two components
        i, j = best_pair
        merged_nodes = comp_data[i][0] + comp_data[j][0]
        merged_coords = np.vstack([comp_data[i][1], comp_data[j][1]])
        comp_data[i] = (merged_nodes, merged_coords, KDTree(merged_coords))
        comp_data.pop(j)

    logger.debug(f"Adjacency bridged to {len(comp_data)} component(s)")


def optimize_backbone(
    road_graph: nx.Graph,
    poles_gdf: gpd.GeoDataFrame,
    drop_cables_gdf: gpd.GeoDataFrame,
    plant_location: Point,
    edges_gdf: gpd.GeoDataFrame | None = None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Build a radial tree backbone from plant to all served poles.

    Builds a pole-to-pole weighted adjacency graph along road edges, then
    Dijkstra from the plant pole to create a shortest-path tree.  Building-path
    edges are penalised so that the backbone prefers to route along roads.
    Each pole gets a ``from_pole_idx`` (the parent pole electrically closer to
    the plant).  Backbone cables are always straight 2-point LineStrings between
    each pole and its parent.

    Args:
        road_graph: Undirected road graph in projected UTM (used only for
            fallback nearest-node lookup; adjacency is built from poles).
        poles_gdf: All poles GeoDataFrame.
        drop_cables_gdf: Drop cables with pole_idx column.
        plant_location: Power plant Point in projected UTM.
        edges_gdf: Road edges GeoDataFrame for building pole adjacency.

    Returns:
        Tuple of (backbone_gdf, updated_poles_gdf) where:
        - backbone_gdf: LineString features with length_meters, cable_type="backbone",
          from_pole_idx, and to_pole_idx columns. All geometries are straight
          2-point lines.
        - updated_poles_gdf: poles with ``from_pole_idx`` column (None for plant).
    """
    import heapq

    empty_backbone = gpd.GeoDataFrame(
        columns=["geometry", "length_meters", "cable_type", "from_pole_idx", "to_pole_idx"],
        crs=poles_gdf.crs,
    )

    if len(drop_cables_gdf) == 0:
        poles_gdf = poles_gdf.copy()
        poles_gdf["from_pole_idx"] = None
        return empty_backbone, poles_gdf

    # Find plant pole index
    plant_idx = None
    for idx in range(len(poles_gdf)):
        if poles_gdf.iloc[idx]["pole_type"] == "plant":
            plant_idx = idx
            break

    if plant_idx is None:
        # Fallback: use nearest pole to plant_location
        pole_coords = np.array([(p.x, p.y) for p in poles_gdf.geometry])
        dists = np.hypot(pole_coords[:, 0] - plant_location.x, pole_coords[:, 1] - plant_location.y)
        plant_idx = int(np.argmin(dists))

    # Build pole-to-pole weighted adjacency along road edges
    if edges_gdf is not None and len(edges_gdf) > 0:
        adjacency = build_pole_adjacency(poles_gdf, edges_gdf)
    else:
        adjacency = {}

    pole_coords = np.array([(p.x, p.y) for p in poles_gdf.geometry])
    pole_tree = KDTree(pole_coords)

    # Ensure plant pole connects to its nearest poles.  The plant may be
    # far from any road/path edge (e.g. 49m in Bagga) and would otherwise
    # be completely isolated in the adjacency graph.
    dists_p, idxs_p = pole_tree.query(
        pole_coords[plant_idx], k=min(_PLANT_CONNECT_K, len(poles_gdf))
    )
    dists_p, idxs_p = np.atleast_1d(dists_p), np.atleast_1d(idxs_p)
    for d, ni in zip(dists_p, idxs_p):
        ni = int(ni)
        if ni != plant_idx and d < _PLANT_CONNECT_DISTANCE_M:
            w = max(d, 1.0)
            if ni not in adjacency.get(plant_idx, {}):
                adjacency.setdefault(plant_idx, {})[ni] = w
                adjacency.setdefault(ni, {})[plant_idx] = w

    # Ensure all served poles have at least one neighbor
    served_pole_indices = set(drop_cables_gdf["pole_idx"].unique())
    served_pole_indices.add(plant_idx)
    for pidx in served_pole_indices:
        if pidx not in adjacency or not adjacency[pidx]:
            dists, idxs = pole_tree.query(pole_coords[pidx], k=min(5, len(poles_gdf)))
            dists, idxs = np.atleast_1d(dists), np.atleast_1d(idxs)
            for d, ni in zip(dists, idxs):
                ni = int(ni)
                if ni != pidx and d < 100.0:
                    adjacency.setdefault(pidx, {})[ni] = d
                    adjacency.setdefault(ni, {})[pidx] = d
                    break

    # Bridge disconnected components so Dijkstra can reach all poles
    _bridge_disconnected_components(adjacency, pole_coords)

    # Dijkstra from plant pole — shortest weighted path prefers roads
    parent: dict[int, int | None] = {plant_idx: None}
    dist_to: dict[int, float] = {plant_idx: 0.0}
    heap: list[tuple[float, int]] = [(0.0, plant_idx)]
    while heap:
        cur_dist, cur = heapq.heappop(heap)
        if cur_dist > dist_to.get(cur, float("inf")):
            continue
        for nb, weight in adjacency.get(cur, {}).items():
            new_dist = cur_dist + weight
            if new_dist < dist_to.get(nb, float("inf")):
                dist_to[nb] = new_dist
                parent[nb] = cur
                heapq.heappush(heap, (new_dist, nb))

    # Build backbone: straight 2-point line from each pole to its parent
    backbone_cables = []
    for pole_idx, from_idx in parent.items():
        if from_idx is None:
            continue  # plant has no parent
        from_pt = poles_gdf.iloc[from_idx].geometry
        to_pt = poles_gdf.iloc[pole_idx].geometry
        geom = LineString([from_pt, to_pt])
        if geom.length < 0.5:
            continue
        backbone_cables.append(
            {
                "geometry": geom,
                "length_meters": geom.length,
                "cable_type": "backbone",
                "from_pole_idx": from_idx,
                "to_pole_idx": pole_idx,
            }
        )

    # Assign from_pole_idx to poles
    poles_gdf = poles_gdf.copy()
    from_pole_col: list[int | None] = [None] * len(poles_gdf)
    for pole_idx, from_idx in parent.items():
        if pole_idx < len(from_pole_col):
            from_pole_col[pole_idx] = from_idx
    poles_gdf["from_pole_idx"] = from_pole_col

    if backbone_cables:
        backbone_gdf = gpd.GeoDataFrame(backbone_cables, crs=poles_gdf.crs)
    else:
        backbone_gdf = empty_backbone

    total_backbone = sum(c["length_meters"] for c in backbone_cables)
    reachable = len(parent)
    total_poles = len(poles_gdf)
    logger.info(
        f"Backbone: {len(backbone_cables)} segments, "
        f"{total_backbone:.0f}m total (Dijkstra SPT, "
        f"{reachable}/{total_poles} poles reachable)"
    )

    return backbone_gdf, poles_gdf


def compute_power_flows(
    backbone_gdf: gpd.GeoDataFrame,
    drop_cables_gdf: gpd.GeoDataFrame,
    poles_gdf: gpd.GeoDataFrame,
    kw_per_household: float,
    power_factor: float = DEFAULT_POWER_FACTOR,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Compute real power flows through every cable for a heatmap overlay.

    Walks the Dijkstra SPT (stored in ``from_pole_idx`` on each pole) bottom-up
    to accumulate downstream building counts, then multiplies by kW per household.

    Each backbone cable carries the combined load of all buildings in the
    subtree rooted at its *to* pole.  Each drop cable carries one household's
    load.  ``power_kw`` is real power (kW); divide by ``power_factor`` for
    apparent power (kVA) if needed.

    Args:
        backbone_gdf: Backbone cables with ``from_pole_idx`` and ``to_pole_idx``.
        drop_cables_gdf: Drop cables with ``pole_idx`` (pole they attach to).
        poles_gdf: All poles with ``from_pole_idx`` SPT parent pointer.
        kw_per_household: Real power demand per connected building (kW).
        power_factor: Power factor (default 0.95, from LAYOUT_POWER_FACTOR env).

    Returns:
        ``(backbone_gdf, drop_cables_gdf)`` with ``power_kw`` column added.
    """
    if len(poles_gdf) == 0 or kw_per_household <= 0:
        backbone_out = backbone_gdf.copy()
        drop_out = drop_cables_gdf.copy()
        backbone_out["power_kw"] = 0.0
        drop_out["power_kw"] = 0.0
        return backbone_out, drop_out

    n_poles = len(poles_gdf)

    # --- Build children lookup from SPT parent pointers ---
    children: list[list[int]] = [[] for _ in range(n_poles)]
    plant_idx: int | None = None
    for i in range(n_poles):
        ptype = poles_gdf.iloc[i].get("pole_type", "")
        if ptype == "plant":
            plant_idx = i
        from_idx = poles_gdf.iloc[i].get("from_pole_idx")
        if from_idx is not None and not (isinstance(from_idx, float) and np.isnan(from_idx)):
            parent = int(from_idx)
            if 0 <= parent < n_poles:
                children[parent].append(i)

    if plant_idx is None:
        # Fallback: use pole with no parent
        has_parent = [False] * n_poles
        for i in range(n_poles):
            from_idx = poles_gdf.iloc[i].get("from_pole_idx")
            if from_idx is not None and not (isinstance(from_idx, float) and np.isnan(from_idx)):
                has_parent[i] = True
        roots = [i for i in range(n_poles) if not has_parent[i]]
        plant_idx = roots[0] if roots else 0

    # --- Count buildings directly connected to each pole ---
    direct_buildings = np.zeros(n_poles, dtype=int)
    if "pole_idx" in drop_cables_gdf.columns and len(drop_cables_gdf) > 0:
        for pidx in drop_cables_gdf["pole_idx"]:
            pidx = int(pidx)
            if 0 <= pidx < n_poles:
                direct_buildings[pidx] += 1

    # --- BFS from plant to get traversal order, then reverse for bottom-up ---
    bfs_order: list[int] = []
    queue = [plant_idx]
    visited: set[int] = {plant_idx}
    while queue:
        node = queue.pop(0)
        bfs_order.append(node)
        for child in children[node]:
            if child not in visited:
                visited.add(child)
                queue.append(child)

    # Bottom-up pass: accumulate subtree building counts
    subtree_buildings = direct_buildings.copy().astype(float)
    for node in reversed(bfs_order):
        for child in children[node]:
            subtree_buildings[node] += subtree_buildings[child]

    # --- Assign power_kw to backbone cables ---
    backbone_out = backbone_gdf.copy()
    if "to_pole_idx" in backbone_out.columns and len(backbone_out) > 0:
        power_kw_values = []
        for _, row in backbone_out.iterrows():
            to_idx = row.get("to_pole_idx")
            if to_idx is not None and not (isinstance(to_idx, float) and np.isnan(to_idx)):
                to_idx = int(to_idx)
                if 0 <= to_idx < n_poles:
                    power_kw_values.append(float(subtree_buildings[to_idx]) * kw_per_household)
                else:
                    power_kw_values.append(0.0)
            else:
                power_kw_values.append(0.0)
        backbone_out["power_kw"] = power_kw_values
    else:
        backbone_out["power_kw"] = 0.0

    # --- Assign power_kw to drop cables (each serves one household) ---
    drop_out = drop_cables_gdf.copy()
    drop_out["power_kw"] = kw_per_household

    logger.info(
        f"Power flows: {n_poles} poles, {kw_per_household:.3f} kW/household (pf={power_factor}), "
        f"peak backbone {backbone_out['power_kw'].max() if len(backbone_out) > 0 else 0:.1f} kW"
    )

    return backbone_out, drop_out


def compute_building_envelope(
    buildings_gdf: gpd.GeoDataFrame,
    site_boundary: Polygon,
    buffer_m: float = 80.0,
) -> Polygon | MultiPolygon:
    """Tight boundary around buildings for pole placement.

    Concave hull of building centroids, buffered outward and clipped to
    site boundary.  Falls back to site_boundary on degenerate input.

    Args:
        buildings_gdf: Building geometries in projected CRS.
        site_boundary: Site boundary Polygon in projected CRS.
        buffer_m: Buffer distance around the hull (meters).

    Returns:
        Polygon envelope clipped to site_boundary.
    """
    from shapely import concave_hull
    from shapely.geometry import MultiPoint

    centroids = [g.centroid for g in buildings_gdf.geometry]
    if len(centroids) < 3:
        return site_boundary

    points = MultiPoint(centroids)
    hull = concave_hull(points, ratio=0.3)
    if hull.is_empty or hull.area < 1.0:
        return site_boundary

    envelope = hull.buffer(buffer_m)

    result = envelope.intersection(site_boundary)
    if result.is_empty:
        return site_boundary

    logger.info(
        f"Building envelope: {result.area / 1e6:.2f} km² (site: {site_boundary.area / 1e6:.2f} km²)"
    )
    return result
