"""Lightning arrestor placement using greedy set cover on backbone network.

Placement rule: use fully intersecting circles of given radius (indicative 500m)
to cover the full network.

Uses **network distance** (cable path length), not Euclidean distance.
A pole 400m away as the crow flies may be 600m of network path away.
"""

from __future__ import annotations

import logging

import geopandas as gpd
import networkx as nx
import pandas as pd

from shared.layout.annotations._graph import MAX_BACKBONE_POLES, build_backbone_graph

logger = logging.getLogger(__name__)

# Re-export for backwards compatibility
__all__ = ["build_backbone_graph", "place_lightning_arrestors"]


def place_lightning_arrestors(
    poles_geojson: dict,
    distribution_geojson: dict,
    coverage_radius_m: float = 500.0,
) -> gpd.GeoDataFrame:
    """Place lightning arrestors using greedy set cover on the backbone network.

    Algorithm:
    1. Build backbone graph from GeoJSON (from_pole_idx/to_pole_idx edges).
    2. For each pole, compute which other poles are within coverage_radius_m
       network distance using Dijkstra with cutoff.
    3. Greedy set cover: pick pole covering most uncovered poles, repeat
       until 100% coverage.

    Args:
        poles_geojson: poles_geo_flat FeatureCollection.
        distribution_geojson: distribution_geo_flat FeatureCollection.
        coverage_radius_m: Network distance radius in meters (default 500).

    Returns:
        GeoDataFrame with columns: geometry (Point), pole_idx (int),
        coverage_radius_m (float). CRS is EPSG:4326.
    """
    if coverage_radius_m <= 0:
        return _empty_arrestors_gdf()

    n_features = len(poles_geojson.get("features", []))
    if n_features > MAX_BACKBONE_POLES:
        logger.warning(
            f"Lightning arrestors: {n_features} poles exceeds limit of "
            f"{MAX_BACKBONE_POLES} — skipping placement"
        )
        return _empty_arrestors_gdf()

    G, pole_points = build_backbone_graph(poles_geojson, distribution_geojson)

    if len(G.nodes) == 0:
        return _empty_arrestors_gdf()

    # Only consider poles that are on the backbone (connected in the graph)
    backbone_poles = set()
    for u, v in G.edges():
        backbone_poles.add(u)
        backbone_poles.add(v)

    if not backbone_poles:
        return _empty_arrestors_gdf()

    # Compute coverage sets: for each pole, which backbone poles it can reach
    coverage_sets: dict[int, set[int]] = {}
    for pole_idx in backbone_poles:
        try:
            lengths = nx.single_source_dijkstra_path_length(
                G, pole_idx, cutoff=coverage_radius_m, weight="weight"
            )
            reachable = {p for p in lengths if p in backbone_poles}
            coverage_sets[pole_idx] = reachable
        except nx.NetworkXError:
            coverage_sets[pole_idx] = {pole_idx}

    # Greedy set cover — iterate over uncovered to shrink search space
    uncovered = set(backbone_poles)
    selected_poles: list[int] = []

    while uncovered:
        best_pole = max(
            uncovered,
            key=lambda p: len(coverage_sets.get(p, set()) & uncovered),
        )
        newly_covered = coverage_sets.get(best_pole, set()) & uncovered
        if not newly_covered:
            # Remaining poles are unreachable — place arrestor on each
            for p in list(uncovered):
                selected_poles.append(p)
                uncovered.discard(p)
            break
        selected_poles.append(best_pole)
        uncovered -= newly_covered

    # Build GeoDataFrame
    rows = []
    for pole_idx in selected_poles:
        if pole_idx in pole_points:
            rows.append(
                {
                    "geometry": pole_points[pole_idx],
                    "pole_idx": pole_idx,
                    "coverage_radius_m": coverage_radius_m,
                }
            )

    if not rows:
        return _empty_arrestors_gdf()

    logger.info(
        f"Lightning arrestors: {len(rows)} placed to cover "
        f"{len(backbone_poles)} backbone poles (radius={coverage_radius_m}m)"
    )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _empty_arrestors_gdf() -> gpd.GeoDataFrame:
    """Return empty GeoDataFrame with correct schema."""
    return gpd.GeoDataFrame(
        {
            "pole_idx": pd.Series(dtype="int"),
            "coverage_radius_m": pd.Series(dtype="float"),
        },
        geometry=[],
        crs="EPSG:4326",
    )
