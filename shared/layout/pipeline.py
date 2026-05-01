"""Distribution layout pipeline — top-level orchestration logic.

Contains the public ``generate_layout`` entry-point and its private helpers.
All heavy lifting is delegated to sibling modules (``distribution``,
``road_network``, ``output_formatter``).

Unified layout strategy:
- Extract OSM road network
- Detect building paths for off-road clusters and merge into the road network
- Place poles along all edges (roads + building paths)
- Connect buildings via drop cables, optimize backbone via SPT

When no OSM roads exist, a synthetic centroid "road" is created and building
paths alone form the network.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from shared.layout.distribution import (
    DEFAULT_POWER_FACTOR,
    compute_building_envelope,
    connect_buildings,
    optimize_backbone,
    place_poles_along_roads,
)
from shared.layout.output_formatter import format_layout_output
from shared.layout.road_network import (
    RoadNetworkResult,
    _estimate_kwp_from_buildings,
    augment_road_network,
    detect_building_paths,
    extract_road_network,
    find_plant_sites,
    locate_power_plant,
)

logger = logging.getLogger(__name__)


def generate_layout(
    boundary: Polygon,
    buildings_geojson: dict[str, Any],
    spacing_m: float = 45.0,
    max_drop_distance_m: float = 50.0,
    target_coverage: float = 90.0,
    plant_location: Point | None = None,
    kwp: float | None = None,
    site_name: str = "",
    render_site_options: bool = True,
    max_candidates: int = 3,
    site_candidates_only: bool = False,
    power_factor: float = DEFAULT_POWER_FACTOR,
) -> dict[str, Any] | None:
    """Generate a complete distribution layout for a community.

    This is the top-level orchestrator that runs the full pipeline:
    1. Extract road network from OpenStreetMap (or build path-only network)
    2. Find candidate plant sites (or use pre-supplied location)
    3. Detect building paths for off-road clusters, merge into road network
    4. Place poles along all edges (roads + building paths)
    5. Connect buildings via drop cables
    6. Optimize backbone (BFS spanning tree from plant)
    7. Format output as GeoJSON

    Args:
        boundary: Community boundary polygon in EPSG:4326.
        buildings_geojson: Original buildings_geo_flat dict from database.
        spacing_m: Distance between poles in meters (default 45).
        max_drop_distance_m: Maximum drop cable length in meters (default 50).
        target_coverage: Target coverage percentage, 0-100 (default 90).
        plant_location: Override power plant location (projected UTM Point).
            When provided, skips automatic plant siting.
        kwp: System size in kWp for site rectangle sizing. When None,
            estimated from building count (conservative).
        site_name: Site name for map title on site options visualization.
        render_site_options: If True, generate site options map when
            auto-detecting plant location.

    Returns:
        Dict with poles_geo_flat, distribution_geo_flat, buildings_geo_flat,
        meta_geo_flat, site_options_map_b64 — all matching pd_site_submissions
        schema (except site_options_map_b64). Or None if layout generation
        failed completely.
    """
    # Phase 1: Extract road network
    road_result = extract_road_network(boundary)

    # Project boundary
    target_crs = road_result.crs if road_result and not road_result.is_empty else None

    # Prepare buildings early — we need them to decide strategy
    if target_crs is None:
        # No roads at all — estimate UTM zone via geopandas (handles Norway/Svalbard zones)
        target_crs = str(gpd.GeoDataFrame(geometry=[boundary], crs="EPSG:4326").estimate_utm_crs())

    boundary_gdf = gpd.GeoDataFrame(geometry=[boundary], crs="EPSG:4326")
    boundary_proj = boundary_gdf.to_crs(target_crs).geometry.iloc[0]

    buildings_gdf = _prepare_buildings(buildings_geojson, target_crs)
    if len(buildings_gdf) == 0:
        logger.warning("No buildings to connect")
        return None

    # Check road relevance: if roads exist but no buildings are reachable,
    # discard useless distant roads (common when buffer expansion finds
    # roads far outside the community).
    if road_result is not None and not road_result.is_empty:
        reach = max_drop_distance_m * 2
        from shapely.ops import unary_union

        road_union = unary_union(road_result.edges_gdf.geometry)
        any_reachable = buildings_gdf.geometry.distance(road_union).min() <= reach
        if not any_reachable:
            logger.info(
                f"Road network found but no buildings within {reach:.0f}m "
                f"— discarding, will use building-path-only network"
            )
            road_result = None

    # Intelligent site selection (when no explicit plant_location).
    # Runs even when there are no OSM roads — in that case edges_gdf=None and
    # find_plant_sites skips road-proximity filtering, selecting purely on
    # building proximity and exclusion zones (trees, water, buildings).
    site_options_map_b64 = None
    site_candidates_wgs84: list[dict[str, Any]] = []
    if plant_location is None:
        estimated_kwp = kwp or _estimate_kwp_from_buildings(len(buildings_gdf))
        edges_for_siting = (
            road_result.edges_gdf if (road_result and not road_result.is_empty) else None
        )
        site_result = find_plant_sites(
            boundary_proj=boundary_proj,
            buildings_gdf=buildings_gdf,
            kwp=estimated_kwp,
            edges_gdf=edges_for_siting,
            spacing_m=spacing_m,
            boundary_wgs84=boundary,
            buildings_geojson=buildings_geojson,
            site_name=site_name,
            render_map=render_site_options,
            canopy_height_threshold_m=5.0,
            building_setback_m=15.0,
            title_suffix="Automatically Detected Potential Power Plant Locations",
            max_candidates=max_candidates,
            skip_exclusion_zones=edges_for_siting is None,
        )
        site_options_map_b64 = site_result.site_map_b64

        # Serialize candidate coordinates to WGS84 for downstream use
        for i, cand in enumerate(site_result.candidates):
            cand_gdf = gpd.GeoDataFrame(geometry=[cand.centroid], crs=target_crs)
            cand_wgs84 = cand_gdf.to_crs("EPSG:4326").geometry.iloc[0]
            # Serialize plant site polygon to WGS84 GeoJSON for use by generate_site_layout.
            # Note: cand.polygon is in UTM meters; the round-trip through WGS84 is intentional —
            # generate_site_layout re-projects via _project_boundary_to_utm().
            polygon_gdf = gpd.GeoDataFrame(geometry=[cand.polygon], crs=target_crs)
            polygon_wgs84 = polygon_gdf.to_crs("EPSG:4326").geometry.iloc[0]
            # Round coords to 6 dp (~10 cm precision) to reduce packet_state JSONB size
            # and eliminate float64 drift through the JSON round-trip.
            polygon_wgs84_rounded = Polygon(
                [(round(x, 6), round(y, 6)) for x, y in polygon_wgs84.exterior.coords]
            )
            candidate_dict: dict[str, Any] = {
                "rank": i + 1,
                "lat": round(cand_wgs84.y, 6),
                "lon": round(cand_wgs84.x, 6),
                "area_sqm": round(cand.area_sqm, 1),
                "distance_to_load_center_m": round(cand.distance_to_load_center_m, 1),
                "polygon": polygon_wgs84_rounded.__geo_interface__,
                "utm_crs": str(target_crs),  # preserve projection CRS to avoid zone re-estimation
            }
            if cand.gate_line is not None:
                gate_gdf = gpd.GeoDataFrame(geometry=[cand.gate_line], crs=target_crs)
                gate_wgs84 = gate_gdf.to_crs("EPSG:4326").geometry.iloc[0]
                gate_centroid = gate_wgs84.centroid
                candidate_dict["gate_lat"] = round(gate_centroid.y, 6)
                candidate_dict["gate_lon"] = round(gate_centroid.x, 6)
                candidate_dict["gate_road_type"] = cand.gate_road_type
                candidate_dict["gate_width_m"] = 3.0
            site_candidates_wgs84.append(candidate_dict)

        if site_result.candidates:
            plant_location = site_result.candidates[0].centroid
            logger.info(
                f"Site selection: using best candidate at "
                f"({plant_location.x:.1f}, {plant_location.y:.1f}), "
                f"{site_result.candidates[0].area_sqm:.0f} sqm"
            )
        else:
            logger.info("Site selection: no candidates — falling back to locate_power_plant()")

    # Early return for site_candidates_only mode (e.g. QGIS sites that skip full layout)
    if site_candidates_only:
        return {
            "site_candidates": site_candidates_wgs84,
            "site_options_map_b64": site_options_map_b64,
        }

    # Phase 1.5: Detect building paths and augment road network.
    # Building paths connect off-road building clusters back to the road
    # network, eliminating the need for a separate cluster-based fallback.
    if road_result is not None and not road_result.is_empty:
        path_edges, path_nodes = detect_building_paths(
            edges_gdf=road_result.edges_gdf,
            nodes_gdf=road_result.nodes_gdf,
            buildings_gdf=buildings_gdf,
            boundary_wgs84=boundary,
            target_crs=str(road_result.crs),
            spacing_m=spacing_m,
            max_drop_distance_m=max_drop_distance_m,
        )
        if len(path_edges) > 0:
            edges_before = len(road_result.edges_gdf)
            road_result = augment_road_network(road_result, path_edges, path_nodes)
            added = len(road_result.edges_gdf) - edges_before
            if added > 0:
                logger.info(f"Augmented road network with {added} building path segments")
            else:
                logger.info("All building path segments were redundant; road network unchanged")

    # When no roads at all, run building path detection standalone to create
    # paths between building clusters (replaces old cluster-based layout).
    if road_result is None or road_result.is_empty:
        road_result = _create_building_path_only_network(
            buildings_gdf=buildings_gdf,
            target_crs=target_crs,
            spacing_m=spacing_m,
            max_drop_distance_m=max_drop_distance_m,
        )
        if road_result is None or road_result.is_empty:
            logger.warning("No roads and no building paths — cannot generate layout")
            return None

    # Compute tight building envelope to avoid placing poles far beyond buildings
    building_envelope = compute_building_envelope(buildings_gdf, boundary_proj)

    result = _road_based_layout(
        road_result=road_result,
        boundary_proj=boundary_proj,
        buildings_gdf=buildings_gdf,
        buildings_geojson=buildings_geojson,
        spacing_m=spacing_m,
        max_drop_distance_m=max_drop_distance_m,
        plant_location=plant_location,
        pole_boundary=building_envelope,
        kwp=kwp,
        power_factor=power_factor,
    )

    if result is not None:
        result["site_options_map_b64"] = site_options_map_b64
        # Store site_candidates independently of the map render so that
        # generate_site_layout can always use the correct plant site polygon,
        # even when render_site_options=False.
        if site_candidates_wgs84:
            result["site_candidates"] = site_candidates_wgs84
    return result


def _road_based_layout(
    road_result,
    boundary_proj: Polygon,
    buildings_gdf: gpd.GeoDataFrame,
    buildings_geojson: dict,
    spacing_m: float,
    max_drop_distance_m: float,
    plant_location: Point | None = None,
    pole_boundary: Polygon | MultiPolygon | None = None,
    kwp: float | None = None,
    power_factor: float = DEFAULT_POWER_FACTOR,
) -> dict[str, Any] | None:
    """Road-based layout: poles along roads + building paths, radial SPT backbone."""
    # Phase 2: Locate power plant on nearest road node (or use override)
    if plant_location is None:
        plant_location = locate_power_plant(boundary_proj, road_result.nodes_gdf)
    elif not boundary_proj.buffer(100).contains(plant_location):
        logger.warning(
            f"plant_location ({plant_location.x:.1f}, {plant_location.y:.1f}) is far outside "
            f"boundary — did you pass WGS84 instead of projected UTM?"
        )
    logger.info(f"Power plant located at ({plant_location.x:.1f}, {plant_location.y:.1f})")

    # Phase 3: Place poles along roads + add plant pole (only inside boundary)
    effective_boundary = pole_boundary if pole_boundary is not None else boundary_proj
    poles_gdf = place_poles_along_roads(
        road_result.edges_gdf, road_result.nodes_gdf, spacing_m, boundary=effective_boundary
    )
    if len(poles_gdf) == 0:
        logger.warning("No poles could be placed along road network")
        return None

    plant_pole = gpd.GeoDataFrame(
        [{"geometry": plant_location, "pole_type": "plant", "road_node_id": None}],
        crs=poles_gdf.crs,
    )
    poles_gdf = gpd.GeoDataFrame(
        pd.concat([plant_pole, poles_gdf], ignore_index=True), crs=poles_gdf.crs
    )

    # Phase 4: Connect buildings to nearest poles
    drop_cables_gdf, updated_buildings, coverage_pct = connect_buildings(
        buildings_gdf=buildings_gdf,
        poles_gdf=poles_gdf,
        max_drop_distance_m=max_drop_distance_m,
    )
    logger.info(f"Building coverage: {coverage_pct:.1f}%")

    # Phase 5: Optimize backbone (Dijkstra SPT from plant)
    backbone_gdf, poles_gdf = optimize_backbone(
        road_graph=road_result.graph,
        poles_gdf=poles_gdf,
        drop_cables_gdf=drop_cables_gdf,
        plant_location=plant_location,
        edges_gdf=road_result.edges_gdf,
    )

    # Phase 5a: Tag backbone segments with edge_type from nearest edge
    backbone_gdf = _tag_backbone_edge_type(backbone_gdf, road_result.edges_gdf)

    # Phase 6: Format output
    # Derive kW per household for power flow heatmap.
    # Priority: kwp / connected_buildings / 2  →  env KWP_PER_BUILDING / 2  →  100W fallback.
    connected_count = (
        int(updated_buildings["connected"].sum()) if "connected" in updated_buildings.columns else 0
    )
    kw_per_household = _derive_kw_per_household(kwp, connected_count)

    return format_layout_output(
        poles_gdf=poles_gdf,
        backbone_gdf=backbone_gdf,
        drop_cables_gdf=drop_cables_gdf,
        buildings_gdf=updated_buildings,
        original_buildings_geojson=buildings_geojson,
        spacing_m=spacing_m,
        max_drop_distance_m=max_drop_distance_m,
        kw_per_household=kw_per_household,
        power_factor=power_factor,
    )


def _derive_kw_per_household(kwp: float | None, connected_count: int) -> float:
    """Derive kW per household for power flow computation.

    Priority order:
    1. Env var LAYOUT_KW_PER_HOUSEHOLD if set (explicit override)
    2. kwp / connected_count / 2  (kwp-per-connection ÷ 2 = nominal kW demand)
    3. _KWP_PER_BUILDING / 2 from road_network constants
    4. 100W absolute fallback

    The /2 factor converts kWp (peak PV capacity) to an estimate of average
    consumption, consistent with typical LV mini-grid sizing assumptions.
    """
    from shared.layout.road_network import _KWP_PER_BUILDING

    explicit = float(os.getenv("LAYOUT_KW_PER_HOUSEHOLD", "0.0"))
    if explicit > 0:
        return explicit

    if kwp and kwp > 0 and connected_count > 0:
        return (kwp / connected_count) / 2.0

    # Fall back to the global KWP_PER_BUILDING estimate / 2
    return max(_KWP_PER_BUILDING / 2.0, 0.1)


def _tag_backbone_edge_type(
    backbone_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Tag each backbone segment with edge_type from the nearest road/path edge.

    For each backbone segment, finds the nearest edge in edges_gdf and copies
    its edge_type. Defaults to "road" if edge_type column is missing.
    """
    if len(backbone_gdf) == 0 or len(edges_gdf) == 0:
        if "edge_type" not in backbone_gdf.columns:
            backbone_gdf = backbone_gdf.copy()
            backbone_gdf["edge_type"] = "road"
        return backbone_gdf

    if "edge_type" not in edges_gdf.columns:
        backbone_gdf = backbone_gdf.copy()
        backbone_gdf["edge_type"] = "road"
        return backbone_gdf

    from shapely.strtree import STRtree

    backbone_gdf = backbone_gdf.copy()
    edge_tree = STRtree(edges_gdf.geometry.values)
    edge_type_values = edges_gdf["edge_type"].values
    edge_types = []

    for _, seg in backbone_gdf.iterrows():
        midpoint = seg.geometry.interpolate(0.5, normalized=True)
        nearest_idx = edge_tree.nearest(midpoint)
        edge_types.append(edge_type_values[nearest_idx])

    backbone_gdf["edge_type"] = edge_types
    return backbone_gdf


def _create_building_path_only_network(
    buildings_gdf: gpd.GeoDataFrame,
    target_crs: str,
    spacing_m: float,
    max_drop_distance_m: float,
) -> RoadNetworkResult | None:
    """Create a synthetic road network from building paths when no OSM roads exist.

    Uses the building path detection algorithm with a synthetic single-edge
    "road" at the centroid of the buildings so that detect_building_paths()
    can find road-attachment cells. The resulting building paths become the
    entire road network.
    """
    from shared.layout.road_network import RoadNetworkResult

    if len(buildings_gdf) < 3:
        return None

    # Create a synthetic road edge at the building centroid
    coords = np.array([(p.x, p.y) for p in buildings_gdf.geometry])
    cx, cy = coords.mean(axis=0)
    # Tiny synthetic road so detect_building_paths has something to attach to
    synthetic_edge = LineString([(cx - 1, cy), (cx + 1, cy)])
    edges_gdf = gpd.GeoDataFrame(
        [
            {
                "geometry": synthetic_edge,
                "edge_type": "road",
            }
        ],
        crs=target_crs,
    )
    nodes_gdf = gpd.GeoDataFrame(
        [
            {"geometry": Point(cx - 1, cy)},
            {"geometry": Point(cx + 1, cy)},
        ],
        crs=target_crs,
    )

    # Build a minimal graph
    G = nx.Graph()
    G.graph["crs"] = target_crs
    G.add_node(0, x=cx - 1, y=cy)
    G.add_node(1, x=cx + 1, y=cy)
    G.add_edge(0, 1, length=2.0, geometry=synthetic_edge)

    path_edges, path_nodes = detect_building_paths(
        edges_gdf=edges_gdf,
        nodes_gdf=nodes_gdf,
        buildings_gdf=buildings_gdf,
        spacing_m=spacing_m,
        max_drop_distance_m=max_drop_distance_m,
        min_path_length_m=0.0,  # No minimum for standalone mode
    )

    if len(path_edges) == 0:
        return None

    # Combine synthetic edge with building paths
    import pandas as pd

    combined_edges = gpd.GeoDataFrame(
        pd.concat([edges_gdf, path_edges], ignore_index=True),
        crs=target_crs,
    )
    combined_nodes = gpd.GeoDataFrame(
        pd.concat([nodes_gdf, path_nodes], ignore_index=True),
        crs=target_crs,
    )

    logger.info(
        f"Created building-path-only network: {len(path_edges)} path segments "
        f"(no OSM roads available)"
    )

    return RoadNetworkResult(
        graph=G,
        nodes_gdf=combined_nodes,
        edges_gdf=combined_edges,
        crs=target_crs,
    )


def _prepare_buildings(
    buildings_geojson: dict[str, Any],
    target_crs: Any,
) -> gpd.GeoDataFrame:
    """Convert buildings GeoJSON to GeoDataFrame of centroids in target CRS.

    Args:
        buildings_geojson: GeoJSON FeatureCollection of building polygons.
        target_crs: Target CRS for projection (should match road network).

    Returns:
        GeoDataFrame with Point geometry (centroids) in target CRS.
    """
    features = buildings_geojson.get("features", [])
    if not features:
        return gpd.GeoDataFrame(columns=["geometry"], crs=target_crs)

    centroids = []
    for feature in features:
        geom = feature.get("geometry", {})
        geom_type = geom.get("type", "")
        coords = geom.get("coordinates", [[]])

        try:
            if geom_type == "MultiPolygon":
                # MultiPolygon: coords = [polygon1, polygon2, ...], polygon = [outer_ring, ...holes]
                for polygon_coords in coords:
                    outer_ring = polygon_coords[0] if polygon_coords else []
                    if len(outer_ring) >= 3:
                        poly = Polygon(outer_ring)
                        centroids.append({"geometry": poly.centroid})
            else:
                # Polygon: coords = [outer_ring, ...holes]
                outer_ring = coords[0] if coords else []
                if len(outer_ring) >= 3:
                    poly = Polygon(outer_ring)
                    centroids.append({"geometry": poly.centroid})
        except Exception:
            continue

    if not centroids:
        return gpd.GeoDataFrame(columns=["geometry"], crs=target_crs)

    gdf = gpd.GeoDataFrame(centroids, crs="EPSG:4326")
    return gdf.to_crs(target_crs)
