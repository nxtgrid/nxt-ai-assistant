"""Backbone graph reconstruction from layout GeoJSON output.

Shared by lightning arrestor and power jumper placement algorithms.
"""

from __future__ import annotations

import networkx as nx
from shapely.geometry import Point

MAX_BACKBONE_POLES = 5000


def build_backbone_graph(
    poles_geojson: dict,
    distribution_geojson: dict,
) -> tuple[nx.Graph, dict[int, Point]]:
    """Reconstruct the backbone NetworkX graph from layout GeoJSON output.

    Args:
        poles_geojson: poles_geo_flat FeatureCollection.
        distribution_geojson: distribution_geo_flat FeatureCollection.

    Returns:
        Tuple of (graph, pole_points) where graph has pole indices as nodes
        and cable lengths as edge weights, and pole_points maps index to
        WGS84 Point geometry.
    """
    G = nx.Graph()
    pole_points: dict[int, Point] = {}

    for i, f in enumerate(poles_geojson.get("features", [])):
        coords = f.get("geometry", {}).get("coordinates", [])
        if len(coords) >= 2:
            pole_points[i] = Point(coords[0], coords[1])
            G.add_node(i)

    for f in distribution_geojson.get("features", []):
        props = f.get("properties", {})
        if props.get("cable_type") != "backbone":
            continue
        from_idx = props.get("from_pole_idx")
        to_idx = props.get("to_pole_idx")
        length_m = props.get("length_meters", 0.0)
        if from_idx is not None and to_idx is not None:
            G.add_edge(int(from_idx), int(to_idx), weight=length_m)

    return G, pole_points
