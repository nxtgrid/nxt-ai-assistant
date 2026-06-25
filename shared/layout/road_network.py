"""
Road network extraction and power plant location for distribution layout.

Uses OSMnx to download OpenStreetMap road networks and provides
power plant siting logic based on road proximity to community centroid.

Includes intelligent site selection that finds clear rectangular areas
for the solar farm, avoiding buildings and preferring proximity to the
load center.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from dataclasses import dataclass
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from scipy.spatial import KDTree
from shapely import prepared as shapely_prepared
from shapely import union_all
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Point, Polygon, box, shape
from shapely.strtree import STRtree

from shared.layout.building_alignment import detect_aligned_roads
from shared.layout.quadkeys import (
    bbox_to_quadkeys as _bbox_to_quadkeys,
)

logger = logging.getLogger(__name__)

# Reject boundaries larger than ~50 km² (approx 0.5 degrees in either axis)
MAX_BOUNDARY_SPAN_DEG = 0.5

# Default area per kWp for solar farm sizing (sqm). Overridable via function parameter.
SQM_PER_KWP = float(os.getenv("LAYOUT_SQM_PER_KWP", "15.5"))

# OSM highway types considered "backbone" roads for distribution layout.
# Shared between site selection and layout pipeline.
BACKBONE_HIGHWAY_TYPES = frozenset(
    {
        "primary",
        "secondary",
        "tertiary",
        "trunk",
        "residential",
        "unclassified",
        "primary_link",
        "secondary_link",
        "tertiary_link",
        "trunk_link",
    }
)

# Clearance corridor half-width (meters) around the route from site to road.
_CORRIDOR_CLEARANCE_M = float(os.getenv("LAYOUT_CORRIDOR_CLEARANCE_M", "10.0"))

# Conservative kWp estimate per building — only used for site rectangle sizing
# when actual kWp is unavailable. Does not affect solar design.
_KWP_PER_BUILDING = float(os.getenv("LAYOUT_KWP_PER_BUILDING", "0.25"))
_MIN_ESTIMATED_KWP = float(os.getenv("LAYOUT_MIN_ESTIMATED_KWP", "30.0"))

# Buffer (meters) around site boundary when clipping fetched road geometries.
# Roads outside this buffer are trimmed to prevent the road network from
# extending far beyond the site (common when buffer-expansion fetches distant roads).
_ROAD_CLIP_BUFFER_M = float(os.getenv("LAYOUT_ROAD_CLIP_BUFFER_M", "100.0"))

# Deduplication tolerances for pole/node merging.
# NOTE: _NODE_DEDUP_TOLERANCE_M shares env var with POLE_DEDUP_DISTANCE_M in distribution.py
# so that both pipeline stages use the same proximity threshold.
_NODE_DEDUP_TOLERANCE_M = float(os.getenv("LAYOUT_POLE_DEDUP_DISTANCE_M", "5.0"))
_PATH_REDUNDANCY_DISTANCE_M = float(os.getenv("LAYOUT_PATH_REDUNDANCY_DISTANCE_M", "22.5"))

# Ratio threshold: warn if inferred alignment roads exceed this multiple of OSM roads
_ALIGNMENT_TO_OSM_WARNING_RATIO = 3.0

# Weight factor for alignment centerlines in grid graph (lower = preferred routing)
_ALIGNMENT_WEIGHT_FACTOR = 0.7


# OSM highway types in priority order (biggest road first) for gate placement.
_HIGHWAY_PRIORITY = (
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "residential",
    "unclassified",
    "trunk_link",
    "primary_link",
    "secondary_link",
    "tertiary_link",
)

_GATE_WIDTH_M = 3.0


def _find_entrance_gate(
    site: Polygon,
    edges_gdf,
    spacing_m: float = 45.0,
) -> tuple[LineString | None, str | None]:
    """Find the entrance gate position for a candidate site.

    Places a 3m gate at the midpoint of the site boundary edge closest to the
    highest-priority road within 2 * spacing_m of the site.

    Args:
        site: Candidate site polygon in projected UTM.
        edges_gdf: Road edges GeoDataFrame with 'highway' column.
        spacing_m: Pole spacing (gate search radius = 2 * spacing_m).

    Returns:
        (gate_line, highway_type) or (None, None) if no road is nearby.
    """
    if edges_gdf is None or len(edges_gdf) == 0:
        return None, None

    search_radius = 2 * spacing_m
    site_buffer = site.buffer(search_radius)

    # Find road edges within search radius
    nearby_mask = edges_gdf.geometry.intersects(site_buffer)
    nearby = edges_gdf[nearby_mask]
    if len(nearby) == 0:
        return None, None

    # Rank by highway priority, break ties by distance to site boundary
    def _road_sort_key(row):
        hw = row.get("highway", "")
        if isinstance(hw, list):
            hw = hw[0] if hw else ""
        try:
            rank = _HIGHWAY_PRIORITY.index(hw)
        except ValueError:
            rank = len(_HIGHWAY_PRIORITY)
        dist = site.exterior.distance(row.geometry)
        return (rank, dist)

    best_idx = min(nearby.index, key=lambda i: _road_sort_key(nearby.loc[i]))
    best_road = nearby.loc[best_idx]
    best_hw = best_road.get("highway", "")
    if isinstance(best_hw, list):
        best_hw = best_hw[0] if best_hw else "unclassified"

    # Extract the 4 edges of the site rectangle
    coords = list(site.exterior.coords)
    edges = []
    for i in range(len(coords) - 1):
        edge = LineString([coords[i], coords[i + 1]])
        edges.append(edge)

    # Find the edge closest to the best road
    gate_edge = min(edges, key=lambda e: e.distance(best_road.geometry))

    # Place gate at midpoint of the closest edge
    edge_length = gate_edge.length
    half_gate = min(_GATE_WIDTH_M / 2, edge_length / 2)

    # Position along the edge where the midpoint falls
    center_frac = 0.5
    start_frac = max(0.0, center_frac - half_gate / edge_length)
    end_frac = min(1.0, center_frac + half_gate / edge_length)

    gate_start = gate_edge.interpolate(start_frac, normalized=True)
    gate_end = gate_edge.interpolate(end_frac, normalized=True)
    gate_line = LineString([gate_start, gate_end])

    return gate_line, best_hw


def _estimate_kwp_from_buildings(building_count: int) -> float:
    """Estimate kWp from building count for site rectangle sizing.

    Only used when actual kWp is unavailable. Does not affect solar design.
    """
    return max(_MIN_ESTIMATED_KWP, building_count * _KWP_PER_BUILDING)


def filter_backbone_edges(edges_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Filter road edges to backbone-worthy highway types only."""
    if "highway" not in edges_gdf.columns:
        return edges_gdf  # No highway data, use all edges

    def _is_backbone(val):
        if isinstance(val, list):
            return any(v in BACKBONE_HIGHWAY_TYPES for v in val)
        return val in BACKBONE_HIGHWAY_TYPES

    mask = edges_gdf["highway"].apply(_is_backbone)
    filtered = edges_gdf[mask]
    return filtered if len(filtered) > 0 else edges_gdf  # fallback to all if no matches


@dataclass(frozen=True)
class PlantSite:
    """A candidate solar farm site polygon with ranking metadata.

    All geometries are in projected UTM CRS (meters).
    """

    polygon: Polygon
    centroid: Point
    area_sqm: float
    distance_to_load_center_m: float
    route_to_road: LineString | None = None
    route_distance_m: float = 0.0
    gate_line: LineString | None = None  # 3m gate segment on site boundary
    gate_road_type: str | None = None  # OSM highway type of nearest approach road


@dataclass
class SiteSelectionResult:
    """Result of site selection including candidates and optional visualization."""

    candidates: list[PlantSite]
    site_map_b64: str | None = None  # base64-encoded PNG of site options map


@dataclass
class RoadNetworkResult:
    """Result of road network extraction."""

    graph: nx.Graph
    nodes_gdf: Any  # GeoDataFrame
    edges_gdf: Any  # GeoDataFrame
    crs: Any

    @property
    def is_empty(self) -> bool:
        return bool(self.graph.number_of_edges() == 0)


def _fetch_graph(ox, polygon: Polygon) -> nx.MultiDiGraph | None:
    """Try to download a road graph within a polygon. Returns None if no data."""
    try:
        G = ox.graph_from_polygon(
            polygon,
            network_type="all",
            retain_all=True,
            truncate_by_edge=True,
        )
    except ValueError:
        return None
    except Exception as e:
        err_name = type(e).__name__
        if "InsufficientResponse" in err_name or "EmptyOverpassResponse" in err_name:
            return None
        raise

    if G.number_of_edges() == 0:
        return None
    return G


# Progressive buffer distances (degrees) for small communities with no internal roads.
# ~0.005° ≈ 500m, ~0.01° ≈ 1.1km, ~0.02° ≈ 2.2km, ~0.05° ≈ 5.5km at equator.
_BUFFER_STEPS_DEG = [0.005, 0.01, 0.02, 0.05]


def _deduplicate_directed_edges(edges_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Remove duplicate directed edges from a MultiDiGraph GeoDataFrame.

    OSMnx's ``graph_to_gdfs`` on a directed graph produces both A→B and B→A
    edges for each road segment.  Normalize each (u, v, key) to
    (min(u,v), max(u,v), key) and keep the first occurrence only.
    """
    if edges_gdf.empty:
        return edges_gdf

    idx = edges_gdf.index
    if idx.nlevels == 3:
        # MultiIndex (u, v, key) from graph_to_gdfs
        u, v, k = idx.get_level_values(0), idx.get_level_values(1), idx.get_level_values(2)
        norm = list(zip(np.minimum(u, v), np.maximum(u, v), k))
        mask = ~pd.Series(norm).duplicated()
        return edges_gdf[mask.values]

    # Flat index (e.g. from concat) — nothing to deduplicate
    return edges_gdf


def _clip_roads_to_boundary(
    edges_gdf: gpd.GeoDataFrame,
    nodes_gdf: gpd.GeoDataFrame,
    graph: nx.Graph,
    boundary_wgs84: Polygon,
    target_crs: Any,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, nx.Graph]:
    """Clip road edge geometries to the site boundary plus a buffer.

    When roads are fetched using an expanded search area (buffer-expansion),
    they can extend far outside the site. This clips each LineString to the
    boundary buffered by ``_ROAD_CLIP_BUFFER_M`` meters, keeping only the
    portions within or near the site.

    After clipping, nodes and the graph are rebuilt to match the surviving
    edges so that downstream algorithms (pole placement, backbone optimization)
    operate on a consistent network.

    Args:
        edges_gdf: Road edges in projected CRS.
        nodes_gdf: Road nodes in projected CRS.
        graph: Undirected graph of the road network.
        boundary_wgs84: Site boundary in WGS84 (EPSG:4326).
        target_crs: Projected CRS of edges/nodes.

    Returns:
        Tuple of (clipped_edges_gdf, rebuilt_nodes_gdf, rebuilt_graph).
    """
    from shapely.geometry import MultiLineString

    # Project boundary to same CRS as edges
    boundary_gdf = gpd.GeoDataFrame(geometry=[boundary_wgs84], crs="EPSG:4326")
    boundary_proj = boundary_gdf.to_crs(target_crs).geometry.iloc[0]
    clip_region = boundary_proj.buffer(_ROAD_CLIP_BUFFER_M)

    prep_clip = shapely_prepared.prep(clip_region)

    clipped_geoms = []
    keep_mask = []
    for geom in edges_gdf.geometry:
        if prep_clip.contains(geom):
            # Entirely inside — keep as-is
            clipped_geoms.append(geom)
            keep_mask.append(True)
        elif prep_clip.intersects(geom):
            # Partially inside — clip
            clipped = geom.intersection(clip_region)
            if clipped.is_empty:
                clipped_geoms.append(geom)
                keep_mask.append(False)
            elif isinstance(clipped, LineString) and clipped.length > 0:
                clipped_geoms.append(clipped)
                keep_mask.append(True)
            elif isinstance(clipped, MultiLineString):
                # Keep longest fragment to maintain a single LineString per edge
                fragments = [g for g in clipped.geoms if isinstance(g, LineString) and g.length > 0]
                if fragments:
                    clipped_geoms.append(max(fragments, key=lambda g: g.length))
                    keep_mask.append(True)
                else:
                    clipped_geoms.append(geom)
                    keep_mask.append(False)
            else:
                # Point or other degenerate result
                clipped_geoms.append(geom)
                keep_mask.append(False)
        else:
            # Entirely outside
            clipped_geoms.append(geom)
            keep_mask.append(False)

    edges_clipped = edges_gdf.copy()
    edges_clipped.geometry = clipped_geoms
    edges_clipped = edges_clipped[keep_mask].reset_index(drop=True)

    if len(edges_clipped) == 0:
        return edges_clipped, nodes_gdf.iloc[0:0], nx.Graph()

    edges_before = len(edges_gdf)
    edges_after = len(edges_clipped)
    if edges_after < edges_before:
        logger.info(
            f"Clipped road network to boundary + {_ROAD_CLIP_BUFFER_M:.0f}m buffer: "
            f"{edges_before} → {edges_after} edges"
        )

    # Rebuild nodes and graph from clipped edges
    new_graph = nx.Graph()
    new_graph.graph["crs"] = target_crs
    node_points = {}  # id -> Point

    for i, row in edges_clipped.iterrows():
        geom = row.geometry
        start_pt = Point(geom.coords[0])
        end_pt = Point(geom.coords[-1])

        # Use coordinate-based node IDs for uniqueness
        start_id = (round(start_pt.x, 3), round(start_pt.y, 3))
        end_id = (round(end_pt.x, 3), round(end_pt.y, 3))

        node_points[start_id] = start_pt
        node_points[end_id] = end_pt

        new_graph.add_node(start_id, x=start_pt.x, y=start_pt.y)
        new_graph.add_node(end_id, x=end_pt.x, y=end_pt.y)
        new_graph.add_edge(start_id, end_id, length=geom.length, geometry=geom)

    # Build new nodes GeoDataFrame
    new_nodes = gpd.GeoDataFrame(
        [{"geometry": pt} for pt in node_points.values()],
        crs=target_crs,
    )

    return edges_clipped, new_nodes, new_graph


def extract_road_network(boundary: Polygon) -> RoadNetworkResult | None:
    """Extract road network within and near boundary from OpenStreetMap.

    Downloads the road graph via the Overpass API, projects to the appropriate
    UTM zone, and returns nodes/edges as GeoDataFrames plus the projected graph.

    For small community boundaries that contain no OSM roads, progressively
    expands the search area (up to ~5.5km buffer) to capture nearby roads
    that service lines would connect to. This is common for rural mini-grid
    sites where roads pass through or near the community but aren't mapped
    within the exact boundary polygon.

    Args:
        boundary: Community boundary polygon in EPSG:4326.

    Returns:
        RoadNetworkResult with projected graph and GeoDataFrames, or None if
        no roads found even after expanding the search area.
    """
    import osmnx as ox

    # Validate boundary size to avoid huge Overpass queries
    minx, miny, maxx, maxy = boundary.bounds
    if (maxx - minx) > MAX_BOUNDARY_SPAN_DEG or (maxy - miny) > MAX_BOUNDARY_SPAN_DEG:
        logger.warning(
            f"Boundary too large for road extraction: "
            f"{maxx - minx:.3f}° x {maxy - miny:.3f}° exceeds {MAX_BOUNDARY_SPAN_DEG}° limit"
        )
        return None

    ox.settings.use_cache = True
    ox.settings.timeout = 30

    # First try the exact boundary
    G = _fetch_graph(ox, boundary)

    # For small boundaries, progressively expand search if no roads found
    if G is None:
        for buf_deg in _BUFFER_STEPS_DEG:
            buffered = boundary.buffer(buf_deg)
            logger.info(
                f"No roads in boundary — expanding search by {buf_deg}° (~{buf_deg * 111:.0f}km)"
            )
            G = _fetch_graph(ox, buffered)
            if G is not None:
                logger.info(f"Found roads with {buf_deg}° buffer")
                break

    if G is None:
        logger.warning("No road data in OpenStreetMap even after expanding search area")
        return None

    G_proj = ox.project_graph(G)
    G_undirected = G_proj.to_undirected()
    nodes_gdf, edges_gdf = ox.graph_to_gdfs(G_proj)
    edges_gdf = _deduplicate_directed_edges(edges_gdf)
    edges_gdf["edge_type"] = "road"

    logger.info(
        f"Extracted road network: {G_undirected.number_of_nodes()} nodes, "
        f"{G_undirected.number_of_edges()} edges, CRS={nodes_gdf.crs}"
    )

    # Clip road geometries to boundary + buffer so roads fetched via
    # expanded search don't extend far outside the site.
    edges_gdf, nodes_gdf, G_undirected = _clip_roads_to_boundary(
        edges_gdf, nodes_gdf, G_undirected, boundary, nodes_gdf.crs
    )

    if len(edges_gdf) == 0:
        logger.warning("No road edges remaining after clipping to boundary")
        return None

    return RoadNetworkResult(
        graph=G_undirected,
        nodes_gdf=nodes_gdf,
        edges_gdf=edges_gdf,
        crs=nodes_gdf.crs,
    )


def locate_power_plant(boundary_projected: Polygon, nodes_gdf: Any) -> Point:
    """Find road network node nearest to community centroid.

    The power plant should be centrally located and adjacent to the road
    network for optimal backbone cable routing. Uses a KDTree for efficient
    nearest-neighbour lookup instead of unary_union over all edges.

    Args:
        boundary_projected: Community boundary in projected UTM CRS.
        nodes_gdf: Road nodes GeoDataFrame in projected UTM CRS.

    Returns:
        Point on the road network nearest to the boundary centroid (UTM coords).
    """
    centroid = boundary_projected.centroid
    coords = np.array([(pt.x, pt.y) for pt in nodes_gdf.geometry])
    tree = KDTree(coords)
    _, idx = tree.query([centroid.x, centroid.y])
    return Point(coords[idx])


# ---------------------------------------------------------------------------
# Intelligent site selection
# ---------------------------------------------------------------------------

_SITE_SETBACK_M = float(os.getenv("LAYOUT_SITE_SETBACK_M", "5.0"))
_BUILDING_BUFFER_M = float(os.getenv("LAYOUT_BUILDING_BUFFER_M", "15.0"))
_ROAD_SETBACK_M = float(os.getenv("LAYOUT_ROAD_SETBACK_M", "5.0"))
_DEFAULT_CANOPY_THRESHOLD_M = float(os.getenv("LAYOUT_CANOPY_THRESHOLD_M", "5.0"))
_WATERWAY_BUFFER_M = float(os.getenv("LAYOUT_WATERWAY_BUFFER_M", "200.0"))
_MIN_CANDIDATE_SEPARATION_M = float(os.getenv("LAYOUT_MIN_CANDIDATE_SEPARATION_M", "100.0"))
_ASPECT_RATIOS = [1.0, 1.6]
_MAX_CANDIDATES = int(os.getenv("LAYOUT_MAX_CANDIDATES", "3"))
_MAX_RAW_CANDIDATES = 50_000  # Safety cap on grid search to prevent OOM

# CHM (Canopy Height Model) constants
_CHM_S3_BUCKET = "dataforgood-fb-data"
_CHM_S3_KEY_PREFIX = "forests/v1/alsgedi_global_v6_float/chm"
_CHM_ZOOM = 9  # Dataset uses zoom-9 QuadKeys


# ---------------------------------------------------------------------------
# Canopy Height Model (CHM) — tree exclusion zone
# ---------------------------------------------------------------------------


def _fetch_chm_exclusion_zone(
    boundary_wgs84: Polygon,
    threshold_m: float,
    target_crs: str,
) -> Polygon | None:
    """Fetch CHM data and build a tree exclusion polygon for areas >= threshold_m.

    Uses GDAL /vsis3/ virtual filesystem for windowed reads from Meta/WRI
    Global Canopy Height Map. Only fetches ~7MB per site (not full 500MB tile).

    Returns exclusion polygon in target CRS, or None on failure.
    """
    try:
        import rasterio
        import rasterio.features
        import rasterio.merge
        from rasterio.windows import from_bounds
    except ImportError:
        logger.warning("rasterio not available — skipping CHM tree exclusion")
        return None

    try:
        minx, miny, maxx, maxy = boundary_wgs84.bounds
        quadkeys = _bbox_to_quadkeys(minx, miny, maxx, maxy, _CHM_ZOOM)
        logger.info(f"CHM: fetching tiles {quadkeys}")

        # Reproject boundary to EPSG:3857 (tiles are in Web Mercator)
        boundary_3857 = (
            gpd.GeoDataFrame(geometry=[boundary_wgs84], crs="EPSG:4326")
            .to_crs("EPSG:3857")
            .geometry.iloc[0]
        )
        b_minx, b_miny, b_maxx, b_maxy = boundary_3857.bounds
        buf = 50  # 50m buffer for edge coverage
        b_minx -= buf
        b_miny -= buf
        b_maxx += buf
        b_maxy += buf

        # Read windowed data from each tile (scoped S3 env, no global side effect)
        tile_arrays = []
        tile_transforms = []
        with rasterio.Env(AWS_NO_SIGN_REQUEST="YES"):
            for qk in quadkeys:
                vsis3_path = f"/vsis3/{_CHM_S3_BUCKET}/{_CHM_S3_KEY_PREFIX}/{qk}.tif"
                logger.info(f"CHM: reading window from {qk}.tif via /vsis3/")
                with rasterio.open(vsis3_path) as src:
                    window = from_bounds(b_minx, b_miny, b_maxx, b_maxy, src.transform)
                    window = window.intersection(
                        rasterio.windows.Window(0, 0, src.width, src.height)
                    )
                    data = src.read(1, window=window)
                    win_transform = src.window_transform(window)
                    tile_arrays.append(data)
                    tile_transforms.append(win_transform)
                    logger.info(
                        f"CHM:   {data.shape[1]}x{data.shape[0]} pixels "
                        f"({data.nbytes / 1024 / 1024:.1f} MB)"
                    )

        if not tile_arrays:
            logger.warning("CHM: no data read from S3")
            return None

        # Merge tiles if multiple
        if len(tile_arrays) == 1:
            merged_data = tile_arrays[0]
            merged_transform = tile_transforms[0]
        else:
            tmp_paths: list[str] = []
            try:
                for arr, tfm in zip(tile_arrays, tile_transforms):
                    tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                    profile = {
                        "driver": "GTiff",
                        "dtype": arr.dtype,
                        "width": arr.shape[1],
                        "height": arr.shape[0],
                        "count": 1,
                        "crs": "EPSG:3857",
                        "transform": tfm,
                    }
                    with rasterio.open(tmp.name, "w", **profile) as dst:
                        dst.write(arr, 1)
                    tmp_paths.append(tmp.name)

                datasets = [rasterio.open(p) for p in tmp_paths]
                try:
                    merged_arr, merged_transform = rasterio.merge.merge(datasets)
                    merged_data = merged_arr[0]
                finally:
                    for ds in datasets:
                        ds.close()
            finally:
                for p in tmp_paths:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

        # Threshold directly on merged data (windowed read already scoped to boundary)
        binary_mask = (merged_data >= threshold_m).astype(np.uint8)
        tree_pixel_count = int(binary_mask.sum())
        if tree_pixel_count == 0:
            logger.info(f"CHM: no pixels >= {threshold_m}m — no tree exclusion needed")
            return None

        logger.info(f"CHM: {tree_pixel_count:,} pixels >= {threshold_m}m")

        exclusion_utm = _vectorize_and_reproject(
            binary_mask, merged_transform, "EPSG:3857", target_crs
        )
        if exclusion_utm is not None:
            logger.info(f"CHM: tree exclusion zone = {exclusion_utm.area:.0f} sqm")
        return exclusion_utm

    except (OSError, ValueError):
        logger.warning(
            "CHM: failed to fetch/process canopy data — proceeding without", exc_info=True
        )
        return None


def _vectorize_and_reproject(
    binary_mask: np.ndarray,
    transform,
    source_crs: str,
    target_crs: str,
):
    """Downsample binary mask, vectorize to polygons, union, and reproject.

    Shared by CHM tree exclusion and water body exclusion.
    Returns exclusion geometry in target CRS, or None if no pixels set.
    """
    import rasterio.features

    if int(binary_mask.sum()) == 0:
        return None

    # Downsample binary mask for faster vectorization (max-pool 4x)
    ds_factor = 4
    h, w = binary_mask.shape
    vectorize_transform = transform
    if h > ds_factor * 10 and w > ds_factor * 10:
        h_trim = (h // ds_factor) * ds_factor
        w_trim = (w // ds_factor) * ds_factor
        vectorize_mask = (
            binary_mask[:h_trim, :w_trim]
            .reshape(h_trim // ds_factor, ds_factor, w_trim // ds_factor, ds_factor)
            .max(axis=(1, 3))
        )
        a, b, c, d, e, f = transform[:6]
        vectorize_transform = type(transform)(a * ds_factor, b, c, d, e * ds_factor, f)
        logger.info(f"  downsampled mask ({h}, {w}) → {vectorize_mask.shape}")
    else:
        vectorize_mask = binary_mask

    polygons = [
        shape(geom_dict)
        for geom_dict, value in rasterio.features.shapes(
            vectorize_mask, transform=vectorize_transform
        )
        if value == 1
    ]

    if not polygons:
        return None

    merged = union_all(polygons)
    exclusion_gdf = gpd.GeoDataFrame(geometry=[merged], crs=source_crs)
    return exclusion_gdf.to_crs(target_crs).geometry.iloc[0]


# ---------------------------------------------------------------------------
# ESA WorldCover — water body exclusion zone
# ---------------------------------------------------------------------------

_WORLDCOVER_S3_BUCKET = "esa-worldcover"
_WORLDCOVER_S3_PREFIX = "v200/2021/map/ESA_WorldCover_10m_2021_v200"
_WATER_CLASS = 80


def _worldcover_tile_name(lat: float, lon: float) -> str:
    """Compute ESA WorldCover tile name for a lat/lon point.

    Tiles are 3x3 degree blocks aligned to multiples of 3.
    """
    lat_base = math.floor(lat / 3) * 3
    lon_base = math.floor(lon / 3) * 3
    ns = "N" if lat_base >= 0 else "S"
    ew = "E" if lon_base >= 0 else "W"
    return f"ESA_WorldCover_10m_2021_v200_{ns}{abs(lat_base):02d}{ew}{abs(lon_base):03d}_Map.tif"


def _fetch_water_exclusion_zone(
    boundary_wgs84: Polygon,
    target_crs: str,
):
    """Fetch ESA WorldCover water pixels and return exclusion polygon in target CRS.

    Uses the same windowed S3 COG read pattern as CHM tree exclusion.
    Returns exclusion polygon in target CRS, or None on failure.
    """
    try:
        import rasterio
        from rasterio.windows import from_bounds
    except ImportError:
        logger.warning("rasterio not available — skipping water exclusion")
        return None

    try:
        centroid = boundary_wgs84.centroid
        tile_name = _worldcover_tile_name(centroid.y, centroid.x)
        logger.info(f"Water: fetching WorldCover tile {tile_name}")

        minx, miny, maxx, maxy = boundary_wgs84.bounds
        buf_deg = 0.0005  # ~50m buffer at equator
        minx -= buf_deg
        miny -= buf_deg
        maxx += buf_deg
        maxy += buf_deg

        with rasterio.Env(AWS_NO_SIGN_REQUEST="YES"):
            full_path = f"/vsis3/{_WORLDCOVER_S3_BUCKET}/v200/2021/map/{tile_name}"
            with rasterio.open(full_path) as src:
                window = from_bounds(minx, miny, maxx, maxy, src.transform)
                window = window.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
                data = src.read(1, window=window)
                win_transform = src.window_transform(window)
                logger.info(
                    f"Water:   {data.shape[1]}x{data.shape[0]} pixels "
                    f"({data.nbytes / 1024 / 1024:.1f} MB)"
                )

        binary_mask = (data == _WATER_CLASS).astype(np.uint8)
        water_pixel_count = int(binary_mask.sum())
        if water_pixel_count == 0:
            logger.info("Water: no water pixels in boundary — no exclusion needed")
            return None

        logger.info(f"Water: {water_pixel_count:,} water pixels found")

        # WorldCover tiles are EPSG:4326 — vectorize and reproject to target UTM
        exclusion_utm = _vectorize_and_reproject(
            binary_mask, win_transform, "EPSG:4326", target_crs
        )
        if exclusion_utm is not None:
            logger.info(f"Water: exclusion zone = {exclusion_utm.area:.0f} sqm")
        return exclusion_utm

    except (OSError, ValueError):
        logger.warning(
            "Water: failed to fetch/process WorldCover data — proceeding without",
            exc_info=True,
        )
        return None


def _draw_gate_on_axes(ax, gate_line: LineString, utm_crs: str, color: str, lw: float = 4.0):
    """Draw a gate segment on matplotlib axes (reprojects UTM → WGS84)."""
    gate_gdf = gpd.GeoDataFrame(geometry=[gate_line], crs=utm_crs)
    gate_wgs84 = gate_gdf.to_crs("EPSG:4326").geometry.iloc[0]
    gx, gy = gate_wgs84.xy
    ax.plot(gx, gy, color="white", linewidth=lw + 2, solid_capstyle="butt", zorder=7)
    ax.plot(gx, gy, color=color, linewidth=lw, solid_capstyle="butt", zorder=7.5)


def render_site_options_b64(
    boundary_wgs84: Polygon,
    buildings_geojson: dict,
    candidates: list[PlantSite],
    site_name: str = "",
    dpi: int = 150,
    edges_gdf: gpd.GeoDataFrame | None = None,
    tree_exclusion_wgs84: Polygon | None = None,
    water_exclusion_wgs84=None,
    canopy_threshold_label: float = 0.0,
    title_suffix: str = "",
) -> str:
    """Render site selection candidates onto a satellite map, return base64 PNG.

    Takes WGS84 boundary, buildings GeoJSON, and PlantSite candidates (in UTM).
    Reprojects candidate UTM geometries back to WGS84 for display.

    Args:
        boundary_wgs84: Community boundary polygon in EPSG:4326.
        buildings_geojson: Buildings as GeoJSON FeatureCollection.
        candidates: PlantSite objects (UTM-projected polygons).
        site_name: Site name for the title.
        dpi: Image resolution.
        edges_gdf: Road edges GeoDataFrame for subtle road overlay (any CRS).
        tree_exclusion_wgs84: Tree exclusion polygon in WGS84 for overlay.
        canopy_threshold_label: Canopy threshold (m) for legend label.

    Returns:
        Base64-encoded PNG string.
    """
    import base64
    import io

    import matplotlib
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    matplotlib.use("Agg")

    candidate_colors = ["#00ff88", "#ffcc00", "#ff6644"]

    fig, ax = plt.subplots(1, 1, figsize=(18, 14))
    ax.set_aspect("equal")

    # Boundary
    bx, by = boundary_wgs84.exterior.xy
    ax.plot(bx, by, color="#ffffff", linewidth=2.5, linestyle="--", zorder=2)
    ax.fill(bx, by, alpha=0.05, color="#ffffff", zorder=1)

    # Tree exclusion overlay (subtle, behind candidates)
    has_tree_overlay = False
    if tree_exclusion_wgs84 is not None:
        try:
            tree_gdf = gpd.GeoDataFrame(geometry=[tree_exclusion_wgs84], crs="EPSG:4326")
            tree_gdf.plot(
                ax=ax,
                facecolor="#ff4444",
                edgecolor="#ff4444",
                alpha=0.12,
                linewidth=0.5,
                zorder=2,
            )
            has_tree_overlay = True
        except Exception:
            logger.debug("Failed to render tree exclusion overlay", exc_info=True)

    # Water exclusion overlay (subtle blue)
    has_water_overlay = False
    if water_exclusion_wgs84 is not None:
        try:
            water_gdf = gpd.GeoDataFrame(geometry=[water_exclusion_wgs84], crs="EPSG:4326")
            water_gdf.plot(
                ax=ax,
                facecolor="#4488ff",
                edgecolor="#4488ff",
                alpha=0.15,
                linewidth=0.5,
                zorder=1.5,
            )
            has_water_overlay = True
        except Exception:
            logger.debug("Failed to render water exclusion overlay", exc_info=True)

    # Road network overlay (subtle, behind candidates)
    has_road_overlay = False
    if edges_gdf is not None and len(edges_gdf) > 0:
        try:
            edges_wgs84 = edges_gdf.to_crs("EPSG:4326")
            edges_wgs84.plot(ax=ax, color="#ffffff", linewidth=0.8, alpha=0.25, zorder=2.5)
            has_road_overlay = True
        except Exception:
            logger.debug("Failed to render road overlay", exc_info=True)

    # Buildings as small polygons or points
    building_polys = []
    building_points_x, building_points_y = [], []
    for feature in buildings_geojson.get("features", []):
        geom = feature.get("geometry", {})
        geom_type = geom.get("type")
        if geom_type == "Polygon":
            building_polys.append(shape(geom))
        elif geom_type == "MultiPolygon":
            multi = shape(geom)
            building_polys.extend(multi.geoms)
        elif geom_type == "Point":
            coords = geom["coordinates"]
            building_points_x.append(coords[0])
            building_points_y.append(coords[1])

    for poly in building_polys:
        px, py = poly.exterior.xy
        ax.fill(px, py, alpha=0.4, color="#888888", zorder=3)
        ax.plot(px, py, color="#aaaaaa", linewidth=0.3, zorder=3)

    if building_points_x:
        ax.scatter(building_points_x, building_points_y, c="#999999", s=4, alpha=0.6, zorder=3)

    building_count = len(building_polys) + len(building_points_x)

    # Estimate UTM CRS from boundary centroid
    centroid = boundary_wgs84.centroid
    zone = int((centroid.x + 180) / 6) + 1
    hemisphere = 32600 + zone if centroid.y >= 0 else 32700 + zone
    utm_crs = f"EPSG:{hemisphere}"

    # Candidate site polygons (reproject from UTM to WGS84)
    candidate_wgs84_polys = []
    for i, site in enumerate(candidates):
        site_gdf = gpd.GeoDataFrame(geometry=[site.polygon], crs=utm_crs)
        site_wgs84 = site_gdf.to_crs("EPSG:4326")
        poly_wgs84 = site_wgs84.geometry.iloc[0]
        candidate_wgs84_polys.append(poly_wgs84)

        color = candidate_colors[i % len(candidate_colors)]
        px, py = poly_wgs84.exterior.xy
        ax.fill(px, py, alpha=0.2, color=color, zorder=5)
        ax.plot(px, py, color=color, linewidth=2.5, zorder=6)

        # Draw route-to-road line
        if site.route_to_road is not None:
            route_gdf = gpd.GeoDataFrame(geometry=[site.route_to_road], crs=utm_crs)
            route_wgs84 = route_gdf.to_crs("EPSG:4326").geometry.iloc[0]
            rx, ry = route_wgs84.xy
            ax.plot(rx, ry, color=color, linewidth=1.5, linestyle="--", alpha=0.7, zorder=4)

        # Gate — line + marker on main map
        if site.gate_line is not None:
            _draw_gate_on_axes(ax, site.gate_line, utm_crs, color, lw=4.0)
            gate_gdf_main = gpd.GeoDataFrame(geometry=[site.gate_line], crs=utm_crs)
            gate_wgs84_main = gate_gdf_main.to_crs("EPSG:4326").geometry.iloc[0]
            gc_main = gate_wgs84_main.centroid
            ax.plot(
                gc_main.x,
                gc_main.y,
                marker="$\u2302$",
                color=color,
                markersize=10,
                markeredgecolor="white",
                markeredgewidth=0.5,
                zorder=8,
            )

        # Label
        cx, cy = poly_wgs84.centroid.x, poly_wgs84.centroid.y
        ax.annotate(
            f"#{i + 1}",
            xy=(cx, cy),
            xytext=(0, 35),
            textcoords="offset points",
            fontsize=12,
            fontweight="bold",
            color=color,
            ha="center",
            va="center",
            zorder=7,
            bbox=dict(boxstyle="round,pad=0.2", facecolor="black", alpha=0.7, edgecolor=color),
            arrowprops=dict(arrowstyle="-|>", color=color, lw=1.5, alpha=0.6),
        )

    # Legend
    legend_handles = [
        Patch(facecolor="none", edgecolor="#ffffff", linestyle="--", linewidth=2, label="Boundary"),
        Patch(facecolor="#888888", alpha=0.4, label=f"Buildings ({building_count})"),
    ]
    if has_road_overlay:
        legend_handles.append(Patch(facecolor="#ffffff", alpha=0.25, label="Roads/paths"))
    if has_tree_overlay:
        threshold_label = f"{canopy_threshold_label:.0f}" if canopy_threshold_label > 0 else "5"
        legend_handles.append(
            Patch(facecolor="#ff4444", alpha=0.12, label=f"Tree canopy \u2265 {threshold_label}m")
        )
    if has_water_overlay:
        legend_handles.append(Patch(facecolor="#4488ff", alpha=0.15, label="Water bodies"))
    # Gate legend entry (if any candidate has a gate)
    if any(site.gate_line is not None for site in candidates):
        legend_handles.append(
            plt.Line2D(
                [],
                [],
                marker="$\u2302$",
                color="#ffffff",
                markersize=8,
                linestyle="None",
                label="Entrance gate",
            )
        )
    for i, site in enumerate(candidates):
        color = candidate_colors[i % len(candidate_colors)]
        route_label = f", road={site.route_distance_m:.0f}m" if site.route_to_road else ""
        legend_handles.append(
            Patch(
                facecolor=color,
                alpha=0.35,
                edgecolor=color,
                label=f"Site #{i + 1}: {site.area_sqm:.0f}sqm, "
                f"dist={site.distance_to_load_center_m:.0f}m{route_label}",
            )
        )

    # Satellite basemap
    try:
        import contextily as ctx

        ctx.add_basemap(ax, crs="EPSG:4326", source=ctx.providers.Esri.WorldImagery, zoom=16)
    except Exception:
        ax.set_facecolor("#1a1a2e")

    # Inset panels per candidate
    n_insets = len(candidate_wgs84_polys)
    if n_insets > 0:
        try:
            import contextily as ctx  # noqa: F811
        except ImportError:
            ctx = None

        inset_h = 0.22
        inset_w = 0.20
        inset_left = 0.02
        inset_top_start = 0.92

        for i, poly_wgs84 in enumerate(candidate_wgs84_polys):
            color = candidate_colors[i % len(candidate_colors)]
            site = candidates[i]

            top = inset_top_start - i * (inset_h + 0.03)
            bottom = top - inset_h
            ax_in = fig.add_axes([inset_left, bottom, inset_w, inset_h], zorder=10)
            ax_in.set_aspect("equal")

            b = poly_wgs84.bounds
            cx_in = (b[0] + b[2]) / 2
            cy_in = (b[1] + b[3]) / 2
            # Zoom to fit site with ~40m padding on each side
            pad_deg = 0.0004  # ~40-45m at Nigerian latitudes
            half_ext = max(b[2] - b[0], b[3] - b[1]) / 2 + pad_deg

            ax_in.set_xlim(cx_in - half_ext, cx_in + half_ext)
            ax_in.set_ylim(cy_in - half_ext, cy_in + half_ext)

            if ctx is not None:
                try:
                    ctx.add_basemap(
                        ax_in,
                        crs="EPSG:4326",
                        source=ctx.providers.Esri.WorldImagery,
                        zoom=18,
                    )
                except Exception:
                    ax_in.set_facecolor("#1a1a2e")
            else:
                ax_in.set_facecolor("#1a1a2e")

            # Buildings in inset
            for bpoly in building_polys:
                bpx, bpy = bpoly.exterior.xy
                ax_in.fill(bpx, bpy, alpha=0.5, color="#888888", zorder=3)
                ax_in.plot(bpx, bpy, color="#aaaaaa", linewidth=0.5, zorder=3)
            if building_points_x:
                ax_in.scatter(
                    building_points_x, building_points_y, c="#999999", s=10, alpha=0.7, zorder=3
                )

            # Candidate site
            spx, spy = poly_wgs84.exterior.xy
            ax_in.fill(spx, spy, alpha=0.3, color=color, zorder=5)
            ax_in.plot(spx, spy, color=color, linewidth=3.5, zorder=6)

            # Route-to-road line
            if site.route_to_road is not None:
                route_gdf = gpd.GeoDataFrame(geometry=[site.route_to_road], crs=utm_crs)
                route_wgs84 = route_gdf.to_crs("EPSG:4326").geometry.iloc[0]
                rx, ry = route_wgs84.xy
                ax_in.plot(rx, ry, color=color, linewidth=2.0, linestyle="--", alpha=0.8, zorder=4)

            # Gate in inset — prominent marker for visibility
            if site.gate_line is not None:
                gate_gdf = gpd.GeoDataFrame(geometry=[site.gate_line], crs=utm_crs)
                gate_wgs84 = gate_gdf.to_crs("EPSG:4326").geometry.iloc[0]
                gc = gate_wgs84.centroid
                # White halo circle + colored gate icon
                ax_in.plot(
                    gc.x,
                    gc.y,
                    marker="o",
                    color="white",
                    markersize=12,
                    zorder=7,
                )
                ax_in.plot(
                    gc.x,
                    gc.y,
                    marker="$\u2302$",
                    color=color,
                    markersize=10,
                    markeredgecolor="white",
                    markeredgewidth=0.5,
                    zorder=8,
                )

            # Edge dimensions
            utm_poly = site.polygon
            utm_coords = list(utm_poly.exterior.coords)
            wgs_coords = list(poly_wgs84.exterior.coords)
            shown_lengths = set()
            for j in range(len(utm_coords) - 1):
                edge_len_m = np.sqrt(
                    (utm_coords[j + 1][0] - utm_coords[j][0]) ** 2
                    + (utm_coords[j + 1][1] - utm_coords[j][1]) ** 2
                )
                rounded = round(edge_len_m, 0)
                if rounded in shown_lengths:
                    continue
                shown_lengths.add(rounded)
                mid_x = (wgs_coords[j][0] + wgs_coords[j + 1][0]) / 2
                mid_y = (wgs_coords[j][1] + wgs_coords[j + 1][1]) / 2
                ax_in.annotate(
                    f"{edge_len_m:.0f}m",
                    (mid_x, mid_y),
                    fontsize=8,
                    color="white",
                    ha="center",
                    va="center",
                    zorder=8,
                    bbox=dict(boxstyle="round,pad=0.1", facecolor="black", alpha=0.7),
                )

            dist_label = f"{site.distance_to_load_center_m:.0f}m to load"
            ax_in.set_title(
                f"#{i + 1}  {site.area_sqm:.0f}sqm \u00b7 {dist_label}",
                fontsize=9,
                fontweight="bold",
                color=color,
                pad=3,
                bbox=dict(facecolor="black", alpha=0.8, edgecolor=color, pad=2),
            )

            for spine in ax_in.spines.values():
                spine.set_edgecolor(color)
                spine.set_linewidth(2.5)
            ax_in.set_xticks([])
            ax_in.set_yticks([])

    # Title and styling
    candidate_text = f"{len(candidates)} candidate(s)" if candidates else "No candidates found"
    title_name = f"{site_name} \u2014 {title_suffix}" if title_suffix else site_name
    ax.set_title(
        f"{title_name}\n{building_count} buildings | {candidate_text}",
        fontsize=16,
        fontweight="bold",
        color="white",
        pad=15,
    )
    ax.legend(
        handles=legend_handles,
        loc="lower right",
        fontsize=9,
        facecolor="black",
        edgecolor="white",
        labelcolor="white",
        framealpha=0.85,
    )
    ax.set_xlabel("Longitude", color="white", fontsize=10)
    ax.set_ylabel("Latitude", color="white", fontsize=10)
    ax.tick_params(colors="white", labelsize=8)
    ax.grid(True, alpha=0.1, color="white")

    # Export to base64 PNG
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="#1a1a2e")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def _weighted_building_centroid(
    buildings_gdf: gpd.GeoDataFrame, fallback: Point | None = None
) -> Point:
    """Compute area-weighted centroid of buildings (the 'load center').

    Larger buildings are assumed to have higher consumption, so they
    pull the load center toward them. Falls back to unweighted centroid
    for Point geometries (area=0), or to *fallback* for empty input.
    """
    if len(buildings_gdf) == 0:
        if fallback is not None:
            return fallback
        raise ValueError("Cannot compute load center from empty buildings GeoDataFrame")
    areas = buildings_gdf.geometry.area
    total = areas.sum()
    if total == 0:
        return buildings_gdf.union_all().centroid
    xs = buildings_gdf.geometry.centroid.x
    ys = buildings_gdf.geometry.centroid.y
    return Point((xs * areas).sum() / total, (ys * areas).sum() / total)


def _road_bearing_at_point(point: Point, backbone_tree: STRtree, backbone_geoms: list) -> float:
    """Compute the bearing (degrees) of the nearest road segment to a point.

    Returns angle in degrees (0 = north/south aligned, 90 = east/west aligned).
    """
    nearest_idx = backbone_tree.nearest(point)
    nearest_edge = backbone_geoms[nearest_idx]

    # Project point onto road and get the local tangent
    proj_dist = nearest_edge.project(point)
    # Sample two points along the road near the projection to get bearing
    epsilon = 1.0  # 1 meter
    d1 = max(0, proj_dist - epsilon)
    d2 = min(nearest_edge.length, proj_dist + epsilon)
    p1 = nearest_edge.interpolate(d1)
    p2 = nearest_edge.interpolate(d2)

    dx = p2.x - p1.x
    dy = p2.y - p1.y
    bearing_rad = math.atan2(dx, dy)  # atan2(dx,dy) gives bearing from north
    return math.degrees(bearing_rad) % 180  # Normalize to 0-180 (direction doesn't matter)


def _generate_candidate_rectangles(
    inset_boundary: Polygon,
    required_area: float,
    road_tree: STRtree | None = None,
    road_geoms: list | None = None,
) -> list[Polygon]:
    """Generate candidate rectangles that fit inside the inset boundary.

    When road data is available, rectangles are aligned to the bearing of the
    nearest road segment (one side parallel to the road). Two orientations are
    tried: aligned and perpendicular.

    When no road data is available, falls back to 4 fixed rotation angles
    (0, 45, 90, 135 degrees).

    Slides templates across the bounding box on a grid. Only candidates fully
    contained in the inset boundary survive.

    Adaptively increases step size when the grid would exceed
    _MAX_RAW_CANDIDATES to prevent OOM on large boundaries with small kWp.
    """
    fallback_angles = [0.0, 45.0, 90.0, 135.0]
    use_road_alignment = road_tree is not None and road_geoms

    prep_boundary = shapely_prepared.prep(inset_boundary)
    minx, miny, maxx, maxy = inset_boundary.bounds
    candidates: list[Polygon] = []

    # Orientations per grid point: 2 if road-aligned (parallel + perpendicular),
    # else 4 fixed angles
    n_orientations = 2 if use_road_alignment else len(fallback_angles)

    for aspect in _ASPECT_RATIOS:
        # width <= height (aspect = height / width)
        width = np.sqrt(required_area / aspect)
        height = width * aspect
        step = width / 2.0

        if step < 1.0:
            continue

        # Estimate grid size and increase step if too many candidates
        nx_est = max(1, int((maxx - minx - width) / step) + 1)
        ny_est = max(1, int((maxy - miny - height) / step) + 1)
        grid_est = nx_est * ny_est * n_orientations
        if grid_est > _MAX_RAW_CANDIDATES:
            scale = np.sqrt(grid_est / _MAX_RAW_CANDIDATES)
            step = step * scale
            logger.info(
                f"Adaptive step increase: {width / 2:.1f}m → {step:.1f}m "
                f"(grid would be {grid_est} cells)"
            )

        # Template rectangle centered at origin
        template = box(-width / 2, -height / 2, width / 2, height / 2)

        xs = np.arange(minx + width / 2, maxx - width / 2 + step, step)
        ys = np.arange(miny + height / 2, maxy - height / 2 + step, step)

        # Coarse-grid bearing cache — nearby points share the same nearest road
        bearing_cache: dict[tuple[int, int], float] = {}
        cache_res = 50.0  # meters

        for x in xs:
            for y in ys:
                center = Point(float(x), float(y))

                if use_road_alignment:
                    cache_key = (int(x / cache_res), int(y / cache_res))
                    bearing = bearing_cache.get(cache_key)
                    if bearing is None:
                        bearing = _road_bearing_at_point(center, road_tree, road_geoms)
                        bearing_cache[cache_key] = bearing
                    angles = [bearing, bearing + 90.0]
                else:
                    angles = fallback_angles

                for angle in angles:
                    rotated = rotate(template, angle, origin=(0.0, 0.0)) if angle else template
                    candidate = translate(rotated, xoff=float(x), yoff=float(y))
                    if prep_boundary.contains(candidate):
                        candidates.append(candidate)
                        if len(candidates) >= _MAX_RAW_CANDIDATES:
                            logger.warning(
                                f"Hit candidate cap ({_MAX_RAW_CANDIDATES}), stopping early"
                            )
                            return candidates

    return candidates


def _is_road_accessible(
    centroid: Point,
    nearest_all_edge,
    route_dist: float,
    max_road_distance: float,
    backbone_tree: STRtree | None,
    backbone_geoms: list | None,
    prep_backbone_reach,
) -> bool:
    """Check if a candidate centroid has valid road access.

    Valid if within 2 pole spans of a backbone road, or within 2 pole spans
    of a side road that intersects the backbone reach zone.
    """
    if backbone_tree is None:
        return True

    nearest_backbone_idx = backbone_tree.nearest(centroid)
    nearest_backbone_edge = backbone_geoms[nearest_backbone_idx]
    dist_to_backbone = centroid.distance(
        nearest_backbone_edge.interpolate(nearest_backbone_edge.project(centroid))
    )
    if dist_to_backbone <= max_road_distance:
        return True  # Directly near a backbone road

    # Side road — check it's reachable from backbone within 10 pole spans
    if prep_backbone_reach is None:
        return False
    return bool(prep_backbone_reach.intersects(nearest_all_edge))


def find_plant_sites(
    boundary_proj: Polygon,
    buildings_gdf: gpd.GeoDataFrame,
    kwp: float,
    edges_gdf: gpd.GeoDataFrame | None = None,
    spacing_m: float = 45.0,
    sqm_per_kwp: float = SQM_PER_KWP,
    boundary_wgs84: Polygon | None = None,
    buildings_geojson: dict | None = None,
    site_name: str = "",
    render_map: bool = False,
    canopy_height_threshold_m: float = _DEFAULT_CANOPY_THRESHOLD_M,
    building_setback_m: float = _BUILDING_BUFFER_M,
    title_suffix: str = "",
    max_candidates: int = _MAX_CANDIDATES,
    skip_exclusion_zones: bool = False,
) -> SiteSelectionResult:
    """Find candidate solar farm sites within the community boundary.

    Searches for clear rectangular areas that:
    - Have the required area (kwp * sqm_per_kwp)
    - Don't overlap any building (with building_setback_m buffer, default 15m)
    - Don't overlap tree canopy >= canopy_height_threshold_m (default 5m)
    - Are within 2 wire spans of a road (if roads available)
    - Are ranked by proximity to the load center (area-weighted building centroid)

    Returns a SiteSelectionResult with up to 3 candidates, sorted best-first.
    When render_map=True and boundary_wgs84/buildings_geojson are provided,
    also includes a base64-encoded PNG visualization of the candidates.

    Args:
        boundary_proj: Community boundary in projected UTM CRS.
        buildings_gdf: Building geometries in projected UTM CRS.
        kwp: Target system size in kWp.
        edges_gdf: Road edges in projected UTM CRS (optional).
        spacing_m: Pole spacing in meters (for road proximity check).
        sqm_per_kwp: Area per kWp in sqm.
        boundary_wgs84: Community boundary in EPSG:4326 (for rendering).
        buildings_geojson: Buildings GeoJSON FeatureCollection (for rendering).
        site_name: Site name for map title (for rendering).
        render_map: If True, generate a site map visualization.
        canopy_height_threshold_m: Trees >= this height are excluded (0 to disable).
        building_setback_m: Min distance from site border to nearest building.

    Returns:
        SiteSelectionResult with candidates list and optional site_map_b64.
    """
    if kwp <= 0 or sqm_per_kwp <= 0:
        return SiteSelectionResult(candidates=[])

    required_area = kwp * sqm_per_kwp
    logger.info(f"Site selection: {kwp} kWp × {sqm_per_kwp} sqm/kWp = {required_area:.0f} sqm")

    # Inset boundary for setback
    inset = boundary_proj.buffer(-_SITE_SETBACK_M)
    if inset.is_empty or not isinstance(inset, Polygon):
        logger.warning("Boundary too small after setback — no site candidates")
        return SiteSelectionResult(candidates=[])

    # Check if required area fits at all
    if required_area > inset.area:
        logger.warning(
            f"Required area {required_area:.0f} sqm exceeds boundary "
            f"inset area {inset.area:.0f} sqm"
        )
        return SiteSelectionResult(candidates=[])

    # Load center (falls back to boundary centroid if no buildings)
    load_center = _weighted_building_centroid(buildings_gdf, fallback=boundary_proj.centroid)
    logger.info(f"Load center at ({load_center.x:.1f}, {load_center.y:.1f})")

    # Building exclusion zone (prepared for fast intersection checks)
    if len(buildings_gdf) > 0:
        buildings_union = union_all(buildings_gdf.geometry.buffer(building_setback_m, resolution=4))
        prep_buildings = shapely_prepared.prep(buildings_union)
        # Separate corridor clearance geometry (10m buffer for route-to-road check)
        corridor_buildings_union = union_all(
            buildings_gdf.geometry.buffer(_CORRIDOR_CLEARANCE_M, resolution=4)
        )
        prep_corridor_buildings = shapely_prepared.prep(corridor_buildings_union)
    else:
        prep_buildings = None
        prep_corridor_buildings = None

    # Tree canopy exclusion (CHM >= threshold)
    prep_tree_exclusion = None
    tree_exclusion_utm = None
    utm_crs = buildings_gdf.crs if len(buildings_gdf) > 0 else None
    if utm_crs is None and edges_gdf is not None and len(edges_gdf) > 0:
        utm_crs = edges_gdf.crs
    if (
        not skip_exclusion_zones
        and boundary_wgs84 is not None
        and canopy_height_threshold_m > 0
        and utm_crs is not None
    ):
        tree_exclusion_utm = _fetch_chm_exclusion_zone(
            boundary_wgs84, canopy_height_threshold_m, str(utm_crs)
        )
        if tree_exclusion_utm is not None:
            prep_tree_exclusion = shapely_prepared.prep(tree_exclusion_utm)
            logger.info(f"Tree exclusion zone active: {tree_exclusion_utm.area:.0f} sqm")

    # Water body exclusion (ESA WorldCover) with buffer backoff
    prep_water_exclusion = None
    water_exclusion_utm = None
    if not skip_exclusion_zones and boundary_wgs84 is not None and utm_crs is not None:
        water_exclusion_utm = _fetch_water_exclusion_zone(boundary_wgs84, str(utm_crs))
        if water_exclusion_utm is not None:
            water_exclusion_utm = water_exclusion_utm.buffer(_WATERWAY_BUFFER_M)
            prep_water_exclusion = shapely_prepared.prep(water_exclusion_utm)
            logger.info(
                f"Water exclusion zone active (with {_WATERWAY_BUFFER_M:.0f}m buffer): "
                f"{water_exclusion_utm.area:.0f} sqm"
            )

    # Precompute road STRtrees for nearest-edge lookups
    max_road_distance = 2 * spacing_m
    max_side_road_to_backbone = 10 * spacing_m  # Side roads must connect within 10 pole spans
    backbone_edges = None
    backbone_tree = None
    backbone_geoms = None
    all_road_tree = None
    all_road_geoms = None
    prep_backbone_reach = None  # Backbone roads buffered by 10 pole spans
    prep_road_exclusion = None
    if edges_gdf is not None and len(edges_gdf) > 0:
        backbone_edges = filter_backbone_edges(edges_gdf)
        backbone_geoms = list(backbone_edges.geometry)
        if backbone_geoms:
            backbone_tree = STRtree(backbone_geoms)
            # Backbone "reach" zone — side roads within this zone can feed candidates
            backbone_reach = union_all(
                backbone_edges.geometry.buffer(max_side_road_to_backbone, resolution=1)
            )
            prep_backbone_reach = shapely_prepared.prep(backbone_reach)

        # All roads (backbone + side roads like tracks/paths) for candidate proximity
        all_road_geoms = list(edges_gdf.geometry)
        all_road_tree = STRtree(all_road_geoms)

        # Road exclusion zone — sites must not overlap the road surface
        road_exclusion = union_all(edges_gdf.geometry.buffer(_ROAD_SETBACK_M, resolution=4))
        prep_road_exclusion = shapely_prepared.prep(road_exclusion)

    # Generate raw candidates (aligned to nearest road — backbone or side road)
    raw_candidates = _generate_candidate_rectangles(
        inset, required_area, road_tree=all_road_tree, road_geoms=all_road_geoms
    )
    logger.info(f"Generated {len(raw_candidates)} raw candidate rectangles")

    # Filter candidates
    results: list[PlantSite] = []
    for candidate in raw_candidates:
        # No building overlap
        if prep_buildings is not None and prep_buildings.intersects(candidate):
            continue

        # No tree canopy overlap
        if prep_tree_exclusion is not None and prep_tree_exclusion.intersects(candidate):
            continue

        # No water body overlap
        if prep_water_exclusion is not None and prep_water_exclusion.intersects(candidate):
            continue

        # No road overlap — solar farms shouldn't sit on roads
        if prep_road_exclusion is not None and prep_road_exclusion.intersects(candidate):
            continue

        centroid = candidate.centroid

        # Road proximity check: near backbone or side road connected to backbone
        route_line = None
        route_dist = 0.0
        if all_road_tree is not None:
            nearest_all_idx = all_road_tree.nearest(centroid)
            nearest_all_edge = all_road_geoms[nearest_all_idx]
            nearest_road_pt = nearest_all_edge.interpolate(nearest_all_edge.project(centroid))
            route_dist = centroid.distance(nearest_road_pt)

            if route_dist > max_road_distance:
                continue

            if not _is_road_accessible(
                centroid,
                nearest_all_edge,
                route_dist,
                max_road_distance,
                backbone_tree,
                backbone_geoms,
                prep_backbone_reach,
            ):
                continue

            # Corridor must be clear of buildings (10m buffer around route line)
            route_line = LineString([centroid, nearest_road_pt])
            if prep_corridor_buildings is not None:
                corridor = route_line.buffer(_CORRIDOR_CLEARANCE_M)
                if prep_corridor_buildings.intersects(corridor):
                    continue

        dist_to_load = centroid.distance(load_center)

        # Compute entrance gate on the edge closest to the biggest nearby road
        gate_line, gate_road_type = _find_entrance_gate(candidate, edges_gdf, spacing_m)

        results.append(
            PlantSite(
                polygon=candidate,
                centroid=centroid,
                area_sqm=candidate.area,
                distance_to_load_center_m=dist_to_load,
                route_to_road=route_line,
                route_distance_m=route_dist,
                gate_line=gate_line,
                gate_road_type=gate_road_type,
            )
        )

    # Sort by load center proximity (primary), route distance (tiebreaker).
    # Corridor clearance is a pass/fail filter above, not a ranking criterion.
    results.sort(key=lambda s: (s.distance_to_load_center_m, s.route_distance_m))
    selected: list[PlantSite] = []
    for site in results:
        # Require minimum separation and no overlap — prevents abutting sites
        if any(
            site.polygon.intersects(s.polygon)
            or site.centroid.distance(s.centroid) < _MIN_CANDIDATE_SEPARATION_M
            for s in selected
        ):
            continue
        selected.append(site)
        if len(selected) >= max_candidates:
            break
    results = selected

    logger.info(
        f"Site selection: {len(results)} candidates found "
        f"(from {len(raw_candidates)} raw, {required_area:.0f} sqm target)"
    )
    for i, site in enumerate(results):
        logger.info(
            f"  #{i + 1}: area={site.area_sqm:.0f} sqm, "
            f"road_dist={site.route_distance_m:.0f}m, "
            f"load_center_dist={site.distance_to_load_center_m:.0f}m"
        )

    # Optional rendering
    site_map_b64 = None
    if render_map and boundary_wgs84 is not None and buildings_geojson is not None:
        # Reproject exclusion zones to WGS84 for rendering
        tree_exclusion_wgs84 = None
        if tree_exclusion_utm is not None and utm_crs is not None:
            try:
                tree_gdf = gpd.GeoDataFrame(geometry=[tree_exclusion_utm], crs=str(utm_crs))
                tree_exclusion_wgs84 = tree_gdf.to_crs("EPSG:4326").geometry.iloc[0]
            except Exception:
                logger.debug("Failed to reproject tree exclusion for rendering", exc_info=True)

        water_exclusion_wgs84 = None
        if water_exclusion_utm is not None and utm_crs is not None:
            try:
                water_gdf = gpd.GeoDataFrame(geometry=[water_exclusion_utm], crs=str(utm_crs))
                water_exclusion_wgs84 = water_gdf.to_crs("EPSG:4326").geometry.iloc[0]
            except Exception:
                logger.debug("Failed to reproject water exclusion for rendering", exc_info=True)

        try:
            site_map_b64 = render_site_options_b64(
                boundary_wgs84=boundary_wgs84,
                buildings_geojson=buildings_geojson,
                candidates=results,
                site_name=site_name,
                edges_gdf=edges_gdf,
                tree_exclusion_wgs84=tree_exclusion_wgs84,
                water_exclusion_wgs84=water_exclusion_wgs84,
                canopy_threshold_label=canopy_height_threshold_m,
                title_suffix=title_suffix,
            )
            logger.info("Generated site map image (base64)")
        except Exception:
            logger.exception("Failed to render site map image")

    return SiteSelectionResult(candidates=results, site_map_b64=site_map_b64)


# ---------------------------------------------------------------------------
# Building-path detection (augment road network with off-road paths)
# ---------------------------------------------------------------------------

# Minimum number of off-road buildings before building paths are generated.
_MIN_OFF_ROAD_BUILDINGS = 3


def detect_building_paths(
    edges_gdf: gpd.GeoDataFrame,
    nodes_gdf: gpd.GeoDataFrame,
    buildings_gdf: gpd.GeoDataFrame,
    boundary_wgs84: Polygon | None = None,
    target_crs: str | None = None,
    spacing_m: float = 45.0,
    max_drop_distance_m: float = 50.0,
    min_path_length_m: float = 50.0,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Detect inter-building paths for off-road clusters and return path edges/nodes.

    Identifies buildings that are too far from any existing road edge to be
    served by drop cables, then builds grid-cell paths connecting those
    clusters back to the road network.

    Algorithm (adapted from _build_cluster_backbone):
    1. Identify off-road buildings (> max_drop_distance_m from nearest road)
    2. If < 3 off-road buildings, return empty
    3. Grid-cell assignment (cell_size = spacing_m)
    4. Gap-cell detection (flanked by buildings on opposite sides)
    5. 8-connected adjacency graph
    6. Bridge disconnected components
    7. Find road-attachment cells (grid cells near road edges)
    8. SPT from road-attachment cells to off-road cluster terminals
    9. Merge degree-2 chains, simplify with Douglas-Peucker
    10. Filter: keep paths >= min_path_length_m
    11. Return path edges with edge_type="building_path"

    Args:
        edges_gdf: Existing road edges in projected CRS.
        nodes_gdf: Existing road nodes in projected CRS.
        buildings_gdf: Building centroids in projected CRS.
        boundary_wgs84: Community boundary in WGS84 (unused, reserved).
        target_crs: Target CRS string (unused, derived from edges_gdf).
        spacing_m: Grid cell size / pole spacing in meters.
        max_drop_distance_m: Max drop cable length — buildings farther than
            this from any road are considered "off-road".
        min_path_length_m: Minimum total path length to keep (shorter paths
            are filtered out to avoid trivial connections).

    Returns:
        Tuple of (path_edges_gdf, path_nodes_gdf) both in the same CRS as
        edges_gdf. path_edges_gdf has columns: geometry, length_meters,
        cable_type, edge_type. path_nodes_gdf has columns: geometry.
    """
    crs = edges_gdf.crs

    empty_edges = gpd.GeoDataFrame(
        columns=["geometry", "length_meters", "cable_type", "edge_type"], crs=crs
    )
    empty_nodes = gpd.GeoDataFrame(columns=["geometry"], crs=crs)

    if len(buildings_gdf) == 0 or len(edges_gdf) == 0:
        return empty_edges, empty_nodes

    # --- Step 1: Identify off-road buildings ---
    bldg_coords = np.array([(p.x, p.y) for p in buildings_gdf.geometry])

    # Build STRtree for road edges for fast nearest-neighbor queries — O(B·log(E))
    edge_geom_arr = edges_gdf.geometry.values
    road_tree = STRtree(edge_geom_arr)
    bldg_points = buildings_gdf.geometry.values
    nearest_idx = [road_tree.nearest(pt) for pt in bldg_points]
    nearest_dists = np.array(
        [edge_geom_arr[nearest_idx[i]].distance(bldg_points[i]) for i in range(len(bldg_points))]
    )
    off_road_mask = nearest_dists > max_drop_distance_m
    off_road_indices = np.where(off_road_mask)[0]

    if len(off_road_indices) < _MIN_OFF_ROAD_BUILDINGS:
        logger.info(
            f"Only {len(off_road_indices)} off-road buildings "
            f"(need >={_MIN_OFF_ROAD_BUILDINGS}) — skipping building path detection"
        )
        return empty_edges, empty_nodes

    logger.info(
        f"Building path detection: {len(off_road_indices)} off-road buildings "
        f"(>{max_drop_distance_m:.0f}m from roads)"
    )

    # Use off-road building coordinates for grid
    off_road_coords = bldg_coords[off_road_indices]

    # --- Step 1.5: Adaptive cell size based on building spacing ---
    bldg_tree_nn = KDTree(off_road_coords)
    nn_dists = bldg_tree_nn.query(off_road_coords, k=2)[0][:, 1]
    median_nn = float(np.median(nn_dists))
    cell_size = max(10.0, min(spacing_m, median_nn * 2.0))
    cluster_radius = max(20.0, min(50.0, median_nn * 2.5))
    logger.info(
        f"Building path detection: adaptive cell_size={cell_size:.1f}m "
        f"(median NN: {median_nn:.1f}m, cluster_radius: {cluster_radius:.1f}m)"
    )

    # --- Step 1.6: Detect linear building alignments as seed roads ---
    alignment_centerlines = detect_aligned_roads(
        off_road_coords, cluster_radius=cluster_radius, median_nn=median_nn
    )

    if alignment_centerlines:
        total_inferred = sum(cl.length for cl in alignment_centerlines)
        total_osm = edges_gdf.geometry.length.sum()
        logger.info(
            f"Building alignment: {len(alignment_centerlines)} centerlines, "
            f"{total_inferred:.0f}m total (OSM: {total_osm:.0f}m)"
        )
        if total_inferred > total_osm * _ALIGNMENT_TO_OSM_WARNING_RATIO:
            logger.warning(
                f"Building alignment inferred {total_inferred:.0f}m of roads "
                f"vs {total_osm:.0f}m OSM — may contain false positives"
            )

    # --- Step 2: Fine grid — assign off-road buildings to cells ---
    grid_x = np.floor(off_road_coords[:, 0] / cell_size).astype(int)
    grid_y = np.floor(off_road_coords[:, 1] / cell_size).astype(int)

    occupied: dict[tuple[int, int], list[int]] = {}
    for i, (gx, gy) in enumerate(zip(grid_x, grid_y)):
        occupied.setdefault((gx, gy), []).append(i)

    cell_centroids: dict[tuple[int, int], tuple[float, float]] = {}
    for cell_key, indices in occupied.items():
        cx = off_road_coords[indices, 0].mean()
        cy = off_road_coords[indices, 1].mean()
        cell_centroids[cell_key] = (cx, cy)

    # --- Step 3: Gap cells ---
    gap_cells: set[tuple[int, int]] = set()
    if occupied:
        occ_keys = np.array(list(occupied.keys()))
        min_gx, min_gy = occ_keys.min(axis=0) - 1
        max_gx, max_gy = occ_keys.max(axis=0) + 1
        occupied_set = set(occupied.keys())

        for gx in range(min_gx, max_gx + 1):
            for gy in range(min_gy, max_gy + 1):
                if (gx, gy) in occupied_set:
                    continue
                has_gap = False
                if (gx - 1, gy) in occupied_set and (gx + 1, gy) in occupied_set:
                    has_gap = True
                elif (gx, gy - 1) in occupied_set and (gx, gy + 1) in occupied_set:
                    has_gap = True
                elif (gx - 1, gy - 1) in occupied_set and (gx + 1, gy + 1) in occupied_set:
                    has_gap = True
                elif (gx - 1, gy + 1) in occupied_set and (gx + 1, gy - 1) in occupied_set:
                    has_gap = True

                if has_gap:
                    gap_cells.add((gx, gy))
                    cell_centroids[(gx, gy)] = (
                        (gx + 0.5) * cell_size,
                        (gy + 0.5) * cell_size,
                    )

    # --- Step 4: 8-connected adjacency graph ---
    all_cells = set(occupied.keys()) | gap_cells
    G = nx.Graph()

    for cell_key in all_cells:
        G.add_node(cell_key, pos=cell_centroids[cell_key])

    gap_weight_penalty = 1.3
    neighbors_8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    for cell_key in all_cells:
        gx, gy = cell_key
        cx, cy = cell_centroids[cell_key]
        for dx, dy in neighbors_8:
            neighbor = (gx + dx, gy + dy)
            if neighbor in all_cells and not G.has_edge(cell_key, neighbor):
                nx_, ny_ = cell_centroids[neighbor]
                dist = float(np.hypot(nx_ - cx, ny_ - cy))
                weight = dist
                if cell_key in gap_cells:
                    weight *= gap_weight_penalty
                if neighbor in gap_cells:
                    weight *= gap_weight_penalty
                G.add_edge(cell_key, neighbor, weight=weight)

    # --- Step 4.5: Inject alignment centerlines as seed edges ---
    if alignment_centerlines:
        alignment_weight_factor = _ALIGNMENT_WEIGHT_FACTOR
        for centerline in alignment_centerlines:
            total_len = centerline.length
            n_segments = max(1, int(total_len / cell_size))
            prev_cell = None
            for seg_i in range(n_segments + 1):
                frac = seg_i / n_segments
                pt = np.array(centerline.interpolate(frac, normalized=True).coords[0])
                gx = int(np.floor(pt[0] / cell_size))
                gy = int(np.floor(pt[1] / cell_size))
                cell_key = (gx, gy)

                if cell_key not in G:
                    G.add_node(cell_key, pos=(pt[0], pt[1]))
                    cell_centroids[cell_key] = (pt[0], pt[1])
                    all_cells.add(cell_key)

                if prev_cell is not None and prev_cell != cell_key:
                    dist = float(
                        np.hypot(
                            cell_centroids[prev_cell][0] - cell_centroids[cell_key][0],
                            cell_centroids[prev_cell][1] - cell_centroids[cell_key][1],
                        )
                    )
                    G.add_edge(prev_cell, cell_key, weight=dist * alignment_weight_factor)

                prev_cell = cell_key

        logger.info(
            f"Injected alignment edges: graph now has "
            f"{G.number_of_nodes()} nodes, {G.number_of_edges()} edges"
        )

    # --- Step 5: Bridge disconnected components ---
    components = list(nx.connected_components(G))
    if len(components) > 1:
        bridge_penalty = 2.0
        comp_data = []
        for comp_nodes in components:
            nodes_list = list(comp_nodes)
            comp_coords = np.array([cell_centroids[n] for n in nodes_list])
            comp_data.append((nodes_list, comp_coords))

        while len(comp_data) > 1:
            best_dist = float("inf")
            best_pair = None
            best_nodes = None

            for i in range(len(comp_data)):
                tree_i = KDTree(comp_data[i][1])
                for j in range(i + 1, len(comp_data)):
                    dists, idxs = tree_i.query(comp_data[j][1])
                    min_idx_j = int(np.argmin(dists))
                    min_idx_i = int(idxs[min_idx_j])
                    d = dists[min_idx_j]
                    if d < best_dist:
                        best_dist = d
                        best_pair = (i, j)
                        best_nodes = (
                            comp_data[i][0][min_idx_i],
                            comp_data[j][0][min_idx_j],
                        )

            if best_pair is None:
                break

            n1, n2 = best_nodes
            G.add_edge(n1, n2, weight=best_dist * bridge_penalty)

            i, j = best_pair
            merged_nodes = comp_data[i][0] + comp_data[j][0]
            merged_coords = np.vstack([comp_data[i][1], comp_data[j][1]])
            comp_data[i] = (merged_nodes, merged_coords)
            comp_data.pop(j)

    # --- Step 6: Find road-attachment cells (cells near road edges) ---
    all_cell_keys = list(all_cells)
    all_cell_coords = np.array([cell_centroids[k] for k in all_cell_keys])

    # For each cell, check distance to nearest road edge
    road_attachment_cells: set[tuple[int, int]] = set()
    for i, cell_key in enumerate(all_cell_keys):
        cx, cy = all_cell_coords[i]
        cell_pt = Point(cx, cy)
        dist_to_road = edges_gdf.geometry.distance(cell_pt).min()
        if dist_to_road <= max_drop_distance_m * 1.5:
            road_attachment_cells.add(cell_key)

    # If no cells are near roads, use the cell closest to any road as root
    if not road_attachment_cells:
        road_dists = np.array(
            [edges_gdf.geometry.distance(Point(cx, cy)).min() for cx, cy in all_cell_coords]
        )
        closest_cell_idx = int(np.argmin(road_dists))
        road_attachment_cells.add(all_cell_keys[closest_cell_idx])

    # Snap road-attachment cell centroids to the nearest point ON a road edge.
    # Without this, building path edges start at grid-cell centroids that can
    # be 50-300m from any road, leaving the path network disconnected from
    # the road network in the pole adjacency graph.
    edge_geoms = list(edges_gdf.geometry.values)
    edge_tree = STRtree(edge_geoms)
    for cell_key in road_attachment_cells:
        cx, cy = cell_centroids[cell_key]
        cell_pt = Point(cx, cy)
        edge_idx: int = edge_tree.nearest(cell_pt)  # type: ignore[assignment]
        nearest_edge = edge_geoms[edge_idx]
        proj_dist = nearest_edge.project(cell_pt)
        snapped_pt = nearest_edge.interpolate(proj_dist)
        cell_centroids[cell_key] = (snapped_pt.x, snapped_pt.y)

    # --- Step 7: Coarse-cluster terminal cells ---
    coarse_size = cell_size * 3
    coarse_grid_x = np.floor(off_road_coords[:, 0] / coarse_size).astype(int)
    coarse_grid_y = np.floor(off_road_coords[:, 1] / coarse_size).astype(int)

    coarse_cells: dict[tuple[int, int], list[int]] = {}
    for i, (gx, gy) in enumerate(zip(coarse_grid_x, coarse_grid_y)):
        coarse_cells.setdefault((gx, gy), []).append(i)

    terminal_cells: set[tuple[int, int]] = set()
    for coarse_key, indices in coarse_cells.items():
        centroid_x = off_road_coords[indices, 0].mean()
        centroid_y = off_road_coords[indices, 1].mean()
        dists = np.linalg.norm(all_cell_coords - np.array([centroid_x, centroid_y]), axis=1)
        terminal_cells.add(all_cell_keys[int(np.argmin(dists))])

    # Remove road-attachment cells from terminals (they're roots, not targets)
    terminal_cells -= road_attachment_cells

    if not terminal_cells:
        logger.info("No terminal cells for building paths — all off-road buildings near roads")
        return empty_edges, empty_nodes

    # --- Step 8: Multi-root SPT from road-attachment cells ---
    # Add a virtual root connected to all road-attachment cells with zero weight
    virtual_root = (-999999, -999999)
    G.add_node(virtual_root)
    for ra_cell in road_attachment_cells:
        if ra_cell in G:
            G.add_edge(virtual_root, ra_cell, weight=0.0)

    spt_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    try:
        paths = nx.single_source_dijkstra_path(G, virtual_root, weight="weight")
    except nx.NetworkXError:
        logger.warning("SPT computation failed for building paths")
        G.remove_node(virtual_root)
        return empty_edges, empty_nodes

    for target in terminal_cells:
        if target not in paths:
            continue
        path = paths[target]
        for a, b in zip(path[:-1], path[1:]):
            if a == virtual_root or b == virtual_root:
                continue
            edge_key = (min(a, b), max(a, b))
            spt_edges.add(edge_key)

    G.remove_node(virtual_root)

    if not spt_edges:
        logger.info("No SPT paths found for building paths")
        return empty_edges, empty_nodes

    # Build SPT subgraph
    spt_graph = nx.Graph()
    for a, b in spt_edges:
        spt_graph.add_node(a, pos=cell_centroids[a])
        spt_graph.add_node(b, pos=cell_centroids[b])
        spt_graph.add_edge(a, b)

    logger.info(
        f"Building path SPT: {spt_graph.number_of_edges()} edges, "
        f"{spt_graph.number_of_nodes()} nodes, "
        f"{len(road_attachment_cells)} road-attachment roots, "
        f"{len(terminal_cells)} terminals"
    )

    # --- Step 9: Merge degree-2 chains ---
    junction_nodes = set()
    for node in spt_graph.nodes():
        if spt_graph.degree(node) != 2 or node in road_attachment_cells or node in terminal_cells:
            junction_nodes.add(node)

    visited_edges: set[tuple] = set()
    lines: list[LineString] = []

    for start_node in junction_nodes:
        for neighbor in spt_graph.neighbors(start_node):
            edge_key = (min(start_node, neighbor), max(start_node, neighbor))
            if edge_key in visited_edges:
                continue
            chain = [start_node, neighbor]
            visited_edges.add(edge_key)
            current = neighbor
            prev = start_node
            while current not in junction_nodes:
                next_nodes = [n for n in spt_graph.neighbors(current) if n != prev]
                if not next_nodes:
                    break
                next_node = next_nodes[0]
                ek = (min(current, next_node), max(current, next_node))
                if ek in visited_edges:
                    break
                visited_edges.add(ek)
                chain.append(next_node)
                prev = current
                current = next_node
            if len(chain) >= 2:
                chain_coords = [cell_centroids[c] for c in chain]
                lines.append(LineString(chain_coords))

    # --- Step 10: Simplify with Douglas-Peucker ---
    simplify_tolerance = cell_size * 0.3
    simplified_lines = []
    for line in lines:
        simplified = line.simplify(simplify_tolerance)
        if not simplified.is_empty and simplified.length > 0:
            simplified_lines.append(simplified)

    if not simplified_lines:
        return empty_edges, empty_nodes

    # --- Step 11: Filter short paths ---
    kept_lines = [ln for ln in simplified_lines if ln.length >= min_path_length_m]
    if not kept_lines:
        # If all paths are short, keep the longest one as a fallback
        kept_lines = [max(simplified_lines, key=lambda ln: ln.length)]
        logger.info(
            f"All building paths < {min_path_length_m:.0f}m — keeping longest "
            f"({kept_lines[0].length:.0f}m)"
        )

    # --- Step 12: Build output GeoDataFrames ---
    path_cables = []
    path_node_points = []

    for line in kept_lines:
        path_cables.append(
            {
                "geometry": line,
                "length_meters": line.length,
                "cable_type": "backbone",
                "edge_type": "building_path",
            }
        )
        # Collect endpoint/vertex nodes
        for coord in line.coords:
            path_node_points.append({"geometry": Point(coord[0], coord[1])})

    path_edges_gdf = gpd.GeoDataFrame(path_cables, crs=crs)
    path_nodes_gdf = gpd.GeoDataFrame(path_node_points, crs=crs)

    total_length = sum(c["length_meters"] for c in path_cables)
    logger.info(
        f"Building paths: {len(path_cables)} segments, "
        f"{total_length:.0f}m total, {len(path_node_points)} nodes"
    )

    return path_edges_gdf, path_nodes_gdf


def _deduplicate_nodes_by_proximity(
    road_nodes_gdf: gpd.GeoDataFrame | None,
    path_nodes_gdf: gpd.GeoDataFrame,
    tolerance_m: float = _NODE_DEDUP_TOLERANCE_M,
) -> gpd.GeoDataFrame:
    """Drop path nodes that duplicate existing road nodes or each other.

    1. Remove any path node within *tolerance_m* of a road node.
    2. Among remaining path nodes, keep only one per proximity cluster.
    """
    if path_nodes_gdf.empty:
        return path_nodes_gdf

    path_coords = np.array([(g.x, g.y) for g in path_nodes_gdf.geometry])

    total_before = len(path_nodes_gdf)

    # Step 1 — drop path nodes near road nodes
    if road_nodes_gdf is not None and not road_nodes_gdf.empty:
        road_coords = np.array([(g.x, g.y) for g in road_nodes_gdf.geometry])
        road_tree = KDTree(road_coords)
        dists, _ = road_tree.query(path_coords)
        keep_mask = dists > tolerance_m
        path_nodes_gdf = path_nodes_gdf[keep_mask]
        path_coords = path_coords[keep_mask]

    if len(path_coords) == 0:
        logger.debug(f"Deduplicated all {total_before} path nodes (too close to road nodes)")
        return path_nodes_gdf

    # Step 2 — self-dedup among remaining path nodes
    path_tree = KDTree(path_coords)
    pairs = path_tree.query_pairs(r=tolerance_m)
    drop_indices: set[int] = set()
    for i, j in pairs:
        drop_indices.add(max(i, j))  # keep the lower index
    keep = [i for i in range(len(path_coords)) if i not in drop_indices]
    result = path_nodes_gdf.iloc[keep].copy()
    dropped = total_before - len(result)
    if dropped:
        logger.debug(f"Deduplicated {dropped} path nodes within {tolerance_m}m")
    return result


def _filter_redundant_path_edges(
    path_edges_gdf: gpd.GeoDataFrame,
    road_edges_gdf: gpd.GeoDataFrame,
    distance_m: float = _PATH_REDUNDANCY_DISTANCE_M,
) -> gpd.GeoDataFrame:
    """Remove building-path edges that run parallel to existing road edges.

    For each path edge, check whether its midpoint is within *distance_m* of
    the nearest road edge.  If so, the path is redundant (it would produce a
    parallel row of poles) and is dropped.

    Note: Only the edge midpoint is sampled, so very long curved paths may
    produce false negatives. This is acceptable for typical building paths
    which are short straight segments.
    """
    if path_edges_gdf.empty or road_edges_gdf.empty:
        return path_edges_gdf

    road_geoms = road_edges_gdf.geometry.values
    road_tree = STRtree(road_geoms)
    midpoints = path_edges_gdf.geometry.interpolate(0.5, normalized=True)
    nearest_idxs = road_tree.nearest(midpoints.values)
    nearest_geoms = road_geoms[nearest_idxs]
    dists = midpoints.distance(gpd.GeoSeries(nearest_geoms, crs=path_edges_gdf.crs))
    dropped = (dists <= distance_m).sum()
    if dropped:
        logger.debug(f"Dropped {dropped} redundant path edges parallel to roads")
    return path_edges_gdf[dists > distance_m].copy()


def _snap_edge_endpoints(
    path_edges_gdf: gpd.GeoDataFrame,
    all_nodes_gdf: gpd.GeoDataFrame,
    tolerance_m: float = _NODE_DEDUP_TOLERANCE_M,
) -> gpd.GeoDataFrame:
    """Snap building-path edge endpoints to the nearest node within tolerance.

    After node deduplication, edge geometries may reference removed coordinates.
    This function updates each edge's start/end vertex to the nearest surviving
    node so that ``place_poles_along_roads`` can match endpoints to intersection
    anchors correctly.
    """
    if path_edges_gdf.empty or all_nodes_gdf.empty:
        return path_edges_gdf

    node_coords = np.array([(g.x, g.y) for g in all_nodes_gdf.geometry])
    node_tree = KDTree(node_coords)

    updated_geoms = []
    snapped_count = 0
    for geom in path_edges_gdf.geometry:
        coords = list(geom.coords)
        changed = False

        # Snap start
        dist_s, idx_s = node_tree.query(coords[0][:2])
        if dist_s <= tolerance_m and dist_s > 0.01:
            coords[0] = tuple(node_coords[idx_s])
            changed = True

        # Snap end
        dist_e, idx_e = node_tree.query(coords[-1][:2])
        if dist_e <= tolerance_m and dist_e > 0.01:
            coords[-1] = tuple(node_coords[idx_e])
            changed = True

        if changed:
            snapped_count += 1
            new_line = LineString(coords)
            if new_line.is_empty or new_line.length < 0.5:
                updated_geoms.append(geom)  # keep original if snapping collapses
            else:
                updated_geoms.append(new_line)
        else:
            updated_geoms.append(geom)

    if snapped_count:
        logger.debug(f"Snapped endpoints on {snapped_count} path edges to nearest node")

    result = path_edges_gdf.copy()
    result["geometry"] = updated_geoms
    return result


def augment_road_network(
    road_result: "RoadNetworkResult",
    path_edges_gdf: gpd.GeoDataFrame,
    path_nodes_gdf: gpd.GeoDataFrame,
) -> "RoadNetworkResult":
    """Merge detected building paths into the road network.

    Before concatenation, applies four passes:
    1. Filter path edges that run parallel to existing road edges.
    2. Drop path nodes within tolerance of road nodes (or each other).
    3. Snap surviving path edge endpoints to the nearest node so that
       pole placement can match endpoints to intersection anchors.
    4. Concatenate the surviving edges and nodes.

    Args:
        road_result: Original RoadNetworkResult from extract_road_network().
        path_edges_gdf: Building path edges from detect_building_paths().
        path_nodes_gdf: Building path nodes from detect_building_paths().

    Returns:
        New RoadNetworkResult with augmented edges_gdf and nodes_gdf.
    """
    if path_edges_gdf.empty:
        return road_result

    # 1. Drop path edges that run parallel to road edges
    path_edges_gdf = _filter_redundant_path_edges(path_edges_gdf, road_result.edges_gdf)

    if path_edges_gdf.empty:
        return road_result

    # 2. Deduplicate path nodes against road nodes and each other
    path_nodes_gdf = _deduplicate_nodes_by_proximity(road_result.nodes_gdf, path_nodes_gdf)

    # 3. Build combined nodes and snap path edge endpoints
    if len(path_nodes_gdf) > 0 and road_result.nodes_gdf is not None:
        combined_nodes = gpd.GeoDataFrame(
            pd.concat([road_result.nodes_gdf, path_nodes_gdf], ignore_index=True),
            crs=road_result.nodes_gdf.crs,
        )
    elif len(path_nodes_gdf) > 0:
        combined_nodes = path_nodes_gdf
    else:
        combined_nodes = road_result.nodes_gdf

    path_edges_gdf = _snap_edge_endpoints(path_edges_gdf, combined_nodes)

    # 4. Merge edges
    combined_edges = gpd.GeoDataFrame(
        pd.concat([road_result.edges_gdf, path_edges_gdf], ignore_index=True),
        crs=road_result.edges_gdf.crs,
    )

    return RoadNetworkResult(
        graph=road_result.graph,
        nodes_gdf=combined_nodes,
        edges_gdf=combined_edges,
        crs=road_result.crs,
    )
