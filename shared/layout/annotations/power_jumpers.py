"""Power jumper placement at branch points and along linear runs.

Placement rule: power jumpers placed at all major branches of the LV network
and at every 10 poles along long LV transmission or distribution paths.
"""

from __future__ import annotations

import logging

import geopandas as gpd
import networkx as nx
import pandas as pd

from shared.layout.annotations._graph import MAX_BACKBONE_POLES, build_backbone_graph

logger = logging.getLogger(__name__)


def place_power_jumpers(
    poles_geojson: dict,
    distribution_geojson: dict,
    interval_poles: int = 10,
) -> gpd.GeoDataFrame:
    """Place power jumpers at branch points and every N poles along paths.

    Algorithm:
    1. Build backbone graph from GeoJSON.
    2. Identify branch points (graph nodes with degree > 2).
    3. Identify endpoints (graph nodes with degree == 1).
    4. For each linear path between branch/endpoints, walk and place
       a jumper every interval_poles poles.
    5. Mark placement_reason: "branch", "interval", "endpoint".

    Args:
        poles_geojson: poles_geo_flat FeatureCollection.
        distribution_geojson: distribution_geo_flat FeatureCollection.
        interval_poles: Place jumper every N poles on linear runs (default 10).

    Returns:
        GeoDataFrame with columns: geometry (Point), pole_idx (int),
        placement_reason (str). CRS is EPSG:4326.
    """
    if interval_poles < 1:
        interval_poles = 10

    n_features = len(poles_geojson.get("features", []))
    if n_features > MAX_BACKBONE_POLES:
        logger.warning(
            f"Power jumpers: {n_features} poles exceeds limit of "
            f"{MAX_BACKBONE_POLES} — skipping placement"
        )
        return _empty_jumpers_gdf()

    G, pole_points = build_backbone_graph(poles_geojson, distribution_geojson)

    if len(G.nodes) == 0:
        return _empty_jumpers_gdf()

    # Classify poles by degree
    selected: dict[int, str] = {}  # pole_idx -> reason

    for node in G.nodes():
        deg = G.degree(node)
        if deg > 2:
            selected[node] = "branch"
        elif deg == 1:
            selected[node] = "endpoint"

    # Walk linear paths between branch/endpoint nodes and place at intervals
    # Find all simple paths along degree-2 chains
    visited_edges: set[tuple[int, int]] = set()

    for start_node in list(G.nodes()):
        if G.degree(start_node) == 2:
            continue  # Start walks from branch/endpoint nodes only

        for neighbor in G.neighbors(start_node):
            edge_key = (min(start_node, neighbor), max(start_node, neighbor))
            if edge_key in visited_edges:
                continue

            # Walk the chain from start_node through neighbor
            path = _walk_chain(G, start_node, neighbor, visited_edges)

            # Place jumpers at intervals along this path
            # path[0] is start_node (already a branch/endpoint)
            # path[-1] is the far end (branch/endpoint)
            # Intermediate poles at indices 1..len-2 are degree-2 chain poles
            if len(path) <= 1:
                continue

            for i in range(interval_poles, len(path) - 1, interval_poles):
                pole_idx = path[i]
                if pole_idx not in selected:
                    selected[pole_idx] = "interval"

    # Build GeoDataFrame
    rows = []
    for pole_idx, reason in selected.items():
        if pole_idx in pole_points:
            rows.append(
                {
                    "geometry": pole_points[pole_idx],
                    "pole_idx": pole_idx,
                    "placement_reason": reason,
                }
            )

    if not rows:
        return _empty_jumpers_gdf()

    branch_count = sum(1 for r in selected.values() if r == "branch")
    interval_count = sum(1 for r in selected.values() if r == "interval")
    endpoint_count = sum(1 for r in selected.values() if r == "endpoint")
    logger.info(
        f"Power jumpers: {len(rows)} placed "
        f"(branch={branch_count}, interval={interval_count}, endpoint={endpoint_count})"
    )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _walk_chain(
    G: nx.Graph,
    start: int,
    first_step: int,
    visited_edges: set[tuple[int, int]],
) -> list[int]:
    """Walk a degree-2 chain from start through first_step until a non-degree-2 node.

    Records visited edges and returns the full path including start and end.
    """
    path = [start, first_step]
    edge_key = (min(start, first_step), max(start, first_step))
    visited_edges.add(edge_key)

    current = first_step
    prev = start

    while G.degree(current) == 2:
        neighbors = list(G.neighbors(current))
        next_node = neighbors[0] if neighbors[1] == prev else neighbors[1]
        edge_key = (min(current, next_node), max(current, next_node))
        if edge_key in visited_edges:
            break
        visited_edges.add(edge_key)
        path.append(next_node)
        prev = current
        current = next_node

    return path


def _empty_jumpers_gdf() -> gpd.GeoDataFrame:
    """Return empty GeoDataFrame with correct schema."""
    return gpd.GeoDataFrame(
        {
            "pole_idx": pd.Series(dtype="int"),
            "placement_reason": pd.Series(dtype="str"),
        },
        geometry=[],
        crs="EPSG:4326",
    )
