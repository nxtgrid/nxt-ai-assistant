"""Community boundary detection from GRID3 settlement extents GeoPackage.

Given GPS anchor coordinates, finds the contiguous settlement block cluster,
dissolves it into a community boundary, reverse-geocodes an OSM name, and
returns structured results with a satellite-underlay map image.

This module is fully synchronous — mirrors pipeline.py. The async expert step
handler (orchestrator/experts/handlers/community_detector.py) wraps calls here
via asyncio.to_thread() to avoid blocking the FastAPI event loop.

Usage (standalone):
    cd /path/to/anansi
    source chat_orchestrator/.venv/bin/activate
    python shared/layout/community_detector.py
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time
from dataclasses import dataclass
from math import cos, log2, radians

import matplotlib

matplotlib.use("Agg")  # Must be set before pyplot is imported; safe to call at module level

import geopandas as gpd
import networkx as nx
import numpy as np
from geopy.geocoders import Nominatim
from shapely.geometry import Point, mapping
from shapely.ops import unary_union
from shapely.strtree import STRtree

logger = logging.getLogger(__name__)

# Nominatim result cache: keyed on (lat rounded to 0.01°, lon rounded to 0.01°)
# 0.01° ≈ 1km, so nearby anchors in the same village share one lookup.
_nominatim_cache: dict[tuple[float, float], str] = {}
_NOMINATIM_CACHE_MAXSIZE = 1_000  # clear all when limit hit (simple eviction)

# Known DigitalOcean Spaces regions
_DO_REGIONS = {"ams3", "sgp1", "nyc3", "sfo3", "fra1", "blr1", "syd1"}
_gdal_s3_configured = False  # set once by _configure_gdal_s3()

# GRID3 Nigeria v4 GeoPackage constants
_GPKG_LAYER = "main_GRID3_NGA_settlement_extents_v4_0"
_BUILDING_COUNT_COL = "building_count"
_NOMINATIM_USER_AGENT = "anansi-community-detector/1.0"
_NOMINATIM_RATE_LIMIT_S = 1.1  # Nominatim ToS: max 1 req/sec


@dataclass
class CommunityResult:
    """Detection result for a single anchor point."""

    anchor_name: str
    community_name: str  # from Nominatim reverse geocode, not GeoPackage
    building_count: int
    block_count: int
    boundary: dict  # GeoJSON Feature (WGS84); empty dict on error
    map_b64: str  # base64 PNG — all anchors + community boundaries on satellite basemap
    error: str = ""  # non-empty if this anchor failed; other fields are zero/empty


# Maximum anchors to include in a single combined map render.
# Beyond this, map rendering is skipped (map_b64 = "") to avoid OOM.
_MAP_BATCH_LIMIT = 50


def detect_communities(
    anchors: list[dict],
    gpkg_path: str,
    search_radius_m: float = 500.0,
    bbox_radius_m: float = 3000.0,
    skip_map: bool = False,
    max_blocks_in_bbox: int | None = None,
    max_buildings_in_bbox: int | None = None,
    layer: str | None = None,
    building_count_col: str | None = None,
    source_label: str | None = None,
) -> list[CommunityResult]:
    """Detect community boundaries for one or more GPS anchor points.

    Args:
        anchors: List of dicts with keys: lat, lon, name
                 e.g. [{"lat": 0.0, "lon": 0.0, "name": "EXAMPLE_SITE_001"}]
        gpkg_path: Local path to the GRID3 GeoPackage file.
        search_radius_m: Maximum distance (metres) to search for the nearest block
                         when the anchor falls outside all blocks. Default 500m.
        bbox_radius_m: Radius (metres) of the bbox used to load blocks from the
                       GeoPackage. Should be large enough to encompass the whole
                       community. Default 3000m. Increasing this costs more disk
                       I/O but ensures distant blocks are loaded.
        skip_map: If True, skip map rendering entirely (map_b64 = ""). Use for
                  large batches where rendering is impractical. Map is also
                  automatically skipped when len(anchors) > _MAP_BATCH_LIMIT.
        max_blocks_in_bbox: If set, anchors whose 3km bbox contains more than
                  this many GRID3 blocks are tagged "urban_skip". Checked after
                  the block load; only the flood fill and Nominatim are skipped.
                  Suggested value: 300 (skips ~77% of the IHS tower database).
        max_buildings_in_bbox: If set, anchors whose 3km bbox total building
                  count exceeds this are tagged "urban_skip". Better semantic
                  proxy than block count (measures actual settlement density).
                  Suggested value: 5000 (slightly more conservative than 300
                  blocks; no site with >300 blocks has <5000 buildings).
        layer: GeoPackage layer holding settlement blocks. Defaults to the
                  Nigeria GRID3 layer when None (single-country / batch callers).
        building_count_col: Column with per-block building counts. Defaults to
                  the GRID3 "building_count" column when None.
        source_label: Provenance string written into each boundary feature's
                  properties (e.g. "GRID3_NGA_v4"). Defaults to the layer name.

    Returns:
        List of CommunityResult, one per anchor (including failed anchors with
        error field set). Never raises for per-anchor failures; only raises for
        unrecoverable setup errors (missing GeoPackage file).

    Raises:
        FileNotFoundError: If gpkg_path does not exist.
    """
    layer = layer or _GPKG_LAYER
    count_col = building_count_col or _BUILDING_COUNT_COL
    source = source_label or layer

    gdal_path = _to_gdal_path(gpkg_path)
    if gdal_path == gpkg_path and not os.path.exists(gpkg_path):
        raise FileNotFoundError(f"GeoPackage not found: {gpkg_path}")

    partial_results: list[dict] = []  # successful anchors; used for map render
    final_results: list[CommunityResult] = []
    n = len(anchors)
    _first_nominatim_call = True

    geolocator = Nominatim(user_agent=_NOMINATIM_USER_AGENT, timeout=10)

    for i, anchor in enumerate(anchors):
        lat, lon, name = float(anchor["lat"]), float(anchor["lon"]), anchor["name"]
        if n > 1:
            logger.info(f"[{i + 1}/{n}] [{name}] Detecting community at ({lat}, {lon})")
        else:
            logger.info(f"[{name}] Detecting community boundary at ({lat}, {lon})")

        try:
            # 1. Bbox-filtered GeoPackage read — GDAL R-tree does the spatial filter
            #    Use bbox_radius_m (default 3km) to load enough blocks for the whole community.
            gdf = _load_blocks(gdal_path, lat, lon, bbox_radius_m, layer, count_col)
            if gdf.empty:
                raise ValueError(
                    f"No settlement blocks found within {bbox_radius_m}m of {name} ({lat}, {lon})"
                )

            # Urban skip: too many blocks OR buildings in bbox → city tower, not rural.
            # Building count is the better semantic proxy (settlement density).
            # Block count is kept as an alternative since it's free and pre-computed.
            if max_blocks_in_bbox is not None and len(gdf) > max_blocks_in_bbox:
                logger.info(
                    f"[{name}] Urban skip — {len(gdf)} blocks in bbox > threshold {max_blocks_in_bbox}"
                )
                final_results.append(
                    CommunityResult(
                        anchor_name=name,
                        community_name="",
                        building_count=0,
                        block_count=0,
                        boundary={},
                        map_b64="",
                        error=f"urban_skip:{len(gdf)}_blocks",
                    )
                )
                continue
            bbox_building_count = int(gdf[count_col].sum()) if count_col in gdf.columns else 0
            if max_buildings_in_bbox is not None and bbox_building_count > max_buildings_in_bbox:
                logger.info(
                    f"[{name}] Urban skip — {bbox_building_count:,} buildings in bbox > threshold {max_buildings_in_bbox}"
                )
                final_results.append(
                    CommunityResult(
                        anchor_name=name,
                        community_name="",
                        building_count=0,
                        block_count=0,
                        boundary={},
                        map_b64="",
                        error=f"urban_skip:{bbox_building_count}_buildings",
                    )
                )
                continue

            # 2. Project to UTM for all metric operations (buffer, distance)
            utm_crs = gdf.estimate_utm_crs()
            gdf_utm = gdf.to_crs(utm_crs)
            del gdf  # release WGS84 copy; gdf_utm is all that's needed from here
            anchor_utm = (
                gpd.GeoDataFrame(geometry=[Point(lon, lat)], crs="EPSG:4326")
                .to_crs(utm_crs)
                .geometry.iloc[0]
            )

            # 3. Find the seed block (contains anchor, or nearest within search_radius_m)
            seed_pos = _find_anchor_block(gdf_utm, anchor_utm, search_radius_m)
            logger.info(
                f"[{name}] Seed block positional index {seed_pos}, "
                f"block_id={gdf_utm.iloc[seed_pos].get('block_id', '?')}"
            )

            # 4. Flood-fill contiguous blocks via STRtree adjacency + networkx
            cluster_gdf_utm = _find_contiguous_blocks(gdf_utm, seed_pos)
            del gdf_utm  # release full bbox frame; only the cluster is needed downstream
            logger.info(f"[{name}] Community cluster: {len(cluster_gdf_utm)} blocks")

            # 5. Dissolve to single community polygon, reproject to WGS84
            boundary_geom_utm = unary_union(cluster_gdf_utm.geometry)
            if not boundary_geom_utm.is_valid:
                boundary_geom_utm = boundary_geom_utm.buffer(0)
            boundary_wgs84 = (
                gpd.GeoDataFrame(geometry=[boundary_geom_utm], crs=utm_crs)
                .to_crs("EPSG:4326")
                .geometry.iloc[0]
            )

            # 6. Building count from cluster
            building_count = int(cluster_gdf_utm[count_col].sum())
            block_count = len(cluster_gdf_utm)
            logger.info(f"[{name}] {building_count} buildings across {block_count} blocks")

            # 7. OSM community name (rate-limited; cached to avoid duplicate lookups
            #    for nearby anchors — important for large batches of 1000+ anchors)
            cache_key = (round(lat, 2), round(lon, 2))
            if cache_key not in _nominatim_cache:
                if len(_nominatim_cache) >= _NOMINATIM_CACHE_MAXSIZE:
                    _nominatim_cache.clear()
                if not _first_nominatim_call:
                    time.sleep(_NOMINATIM_RATE_LIMIT_S)
                _nominatim_cache[cache_key] = _get_osm_name(geolocator, lat, lon)
                _first_nominatim_call = False
            community_name = _nominatim_cache[cache_key]
            logger.info(f"[{name}] Community name from OSM: {community_name!r}")

            partial_results.append(
                {
                    "anchor_name": name,
                    "community_name": community_name,
                    "building_count": building_count,
                    "block_count": block_count,
                    "boundary_wgs84": boundary_wgs84,
                    "lat": lat,
                    "lon": lon,
                    "source_label": source,
                }
            )

        except Exception as exc:
            logger.warning(f"[{name}] Skipping — {exc}")
            final_results.append(
                CommunityResult(
                    anchor_name=name,
                    community_name="",
                    building_count=0,
                    block_count=0,
                    boundary={},
                    map_b64="",
                    error=str(exc),
                )
            )

    # 8. Render one combined map (skipped for large batches to avoid OOM)
    render = not skip_map and len(partial_results) <= _MAP_BATCH_LIMIT
    map_b64 = _render_map(partial_results) if render else ""
    if not render and partial_results:
        logger.info(
            f"Map rendering skipped ({len(partial_results)} successful anchors; "
            f"limit is {_MAP_BATCH_LIMIT}). Pass skip_map=False and a smaller batch to get maps."
        )

    for r in partial_results:
        final_results.append(
            CommunityResult(
                anchor_name=r["anchor_name"],
                community_name=r["community_name"],
                building_count=r["building_count"],
                block_count=r["block_count"],
                boundary=_make_boundary_feature(r),
                map_b64=map_b64,
                error="",
            )
        )

    # Preserve original order: slot successful results back by anchor index
    # (final_results currently has errors first, then successes — reorder)
    order = {a["name"]: i for i, a in enumerate(anchors)}
    final_results.sort(key=lambda r: order.get(r.anchor_name, len(anchors)))
    return final_results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _configure_gdal_s3() -> None:
    """Configure GDAL S3 credentials once from DO_SPACES_* environment variables.

    Called lazily from _to_gdal_path() on first S3 path encounter.
    Sets os.environ once rather than on every call (idempotent after first run).
    Raises ValueError if DO_SPACES_REGION is not a recognised DO Spaces region.
    """
    global _gdal_s3_configured
    if _gdal_s3_configured:
        return

    if not os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("DO_SPACES_KEY"):
        os.environ["AWS_ACCESS_KEY_ID"] = os.environ["DO_SPACES_KEY"]
    if not os.environ.get("AWS_SECRET_ACCESS_KEY") and os.environ.get("DO_SPACES_SECRET"):
        os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ["DO_SPACES_SECRET"]

    region = os.environ.get("DO_SPACES_REGION", "ams3")
    if region not in _DO_REGIONS:
        raise ValueError(
            f"DO_SPACES_REGION {region!r} is not a known DO Spaces region. "
            f"Valid regions: {sorted(_DO_REGIONS)}"
        )
    os.environ.setdefault("AWS_S3_ENDPOINT", f"{region}.digitaloceanspaces.com")
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "YES")
    os.environ.setdefault("AWS_VIRTUAL_HOSTING", "FALSE")
    _gdal_s3_configured = True


def _to_gdal_path(gpkg_path: str) -> str:
    """Convert an s3://bucket/key URI to a GDAL /vsis3/ virtual path.

    Calls _configure_gdal_s3() on first S3 path to set GDAL credentials once.
    Local paths are returned unchanged.
    """
    if not gpkg_path.startswith("s3://"):
        return gpkg_path
    _configure_gdal_s3()
    return "/vsis3/" + gpkg_path[5:]


def _load_blocks(
    gpkg_path: str,
    lat: float,
    lon: float,
    radius_m: float,
    layer: str = _GPKG_LAYER,
    building_count_col: str = _BUILDING_COUNT_COL,
) -> gpd.GeoDataFrame:
    """Load settlement blocks within a bounding box around (lat, lon).

    Uses pyogrio engine (default in geopandas 1.x) which delegates spatial
    filtering to the GeoPackage's built-in SQLite R-tree index. Never loads
    the full 3.4GB file into memory.

    bbox is in WGS84 (EPSG:4326) which matches the GeoPackage CRS.
    """
    # Convert radius from metres to approximate WGS84 degrees
    # lat degree is constant; lon degree shrinks at higher latitudes
    lat_delta = radius_m / 111_320.0
    lon_delta = radius_m / (111_320.0 * cos(radians(lat)))
    bbox = (lon - lon_delta, lat - lat_delta, lon + lon_delta, lat + lat_delta)

    gdf = gpd.read_file(
        gpkg_path,
        layer=layer,
        bbox=bbox,
        engine="pyogrio",
        columns=[building_count_col],  # geometry always included; skip ~26 unused columns
    )
    gdf = gdf.reset_index(drop=True)

    if building_count_col not in gdf.columns:
        logger.warning(
            f"Column {building_count_col!r} not found in GeoPackage. "
            f"Available: {list(gdf.columns)}. Building count will be 0."
        )
        gdf[building_count_col] = 0

    return gdf


def _find_anchor_block(gdf_utm: gpd.GeoDataFrame, anchor_utm: Point, max_dist_m: float) -> int:
    """Return positional index of the seed block.

    First tries containment; if the anchor falls outside all blocks, returns
    the nearest block within max_dist_m. Raises ValueError if none found.
    """
    # Containment check
    containing = gdf_utm[gdf_utm.geometry.contains(anchor_utm)]
    if not containing.empty:
        return int(gdf_utm.index.get_loc(containing.index[0]))

    # Nearest within max_dist_m
    distances = gdf_utm.geometry.distance(anchor_utm)
    nearest_label = distances.idxmin()
    if distances[nearest_label] > max_dist_m:
        raise ValueError(
            f"No settlement block found within {max_dist_m}m of anchor point. "
            f"Nearest block is {distances[nearest_label]:.0f}m away."
        )

    return int(gdf_utm.index.get_loc(nearest_label))


def _find_contiguous_blocks(gdf_utm: gpd.GeoDataFrame, seed_pos: int) -> gpd.GeoDataFrame:
    """Return all blocks contiguous with the seed block via connected components.

    Uses STRtree vectorised adjacency (Shapely 2.x) for efficiency.
    A 1m buffer handles GRID3 digitizing gaps between blocks that share
    a boundary but don't perfectly touch due to floating-point precision.

    Args:
        gdf_utm: GeoDataFrame in a metric CRS (UTM).
        seed_pos: Positional index (iloc) of the seed block.

    Returns:
        Subset GeoDataFrame of all blocks in the same connected component.
    """
    geoms = list(gdf_utm.geometry)
    tree = STRtree(geoms)

    # Primary: exact topology — blocks sharing an edge/vertex
    left, right = tree.query(geoms, predicate="touches")

    # Gap-tolerant: 1m buffer in UTM catches digitizing slivers.
    # shapely.buffer() is vectorised (Shapely 2.x) — avoids a slow Python loop.
    import shapely as _shapely

    geoms_arr = np.array(geoms)
    geoms_buf = _shapely.buffer(geoms_arr, 1.0)
    tree_buf = STRtree(geoms_buf)
    l2, r2 = tree_buf.query(geoms_buf, predicate="intersects")
    mask = l2 != r2  # exclude self-pairs
    l2, r2 = l2[mask], r2[mask]

    all_l = np.concatenate([left, l2])
    all_r = np.concatenate([right, r2])

    G = nx.Graph()
    G.add_nodes_from(range(len(geoms)))
    G.add_edges_from(zip(all_l.tolist(), all_r.tolist()))

    component = nx.node_connected_component(G, seed_pos)
    return gdf_utm.iloc[sorted(component)].copy()


def _get_osm_name(geolocator: Nominatim, lat: float, lon: float) -> str:
    """Reverse-geocode a settlement name from Nominatim OSM data.

    Uses zoom=14 (settlement-level resolution) and returns only town/village
    equivalent names. Explicitly excludes LGA/county-level names (which are
    too coarse) and sub-settlement names like ward/community designations
    (which are too granular and often administrative artefacts in Nigeria).

    Hierarchy tried (settlement-level first):
        hamlet → village → town → city
    Sub-settlement fallback (if no settlement found):
        suburb → neighbourhood → quarter → city_district

    Returns "Unknown" on any failure or if no suitable name is found.
    """
    import re

    # Settlement-level OSM keys — these represent named towns/villages
    _SETTLEMENT_KEYS = ("hamlet", "village", "town", "city")
    # Sub-settlement fallback — only used when no settlement-level name exists
    _SUB_KEYS = ("suburb", "neighbourhood", "quarter", "city_district")
    # Patterns that indicate administrative divisions rather than place names
    _ADMIN_PATTERN = re.compile(
        r"^(ward|community|district|zone|quarter)\s+\w",
        re.IGNORECASE,
    )

    try:
        location = geolocator.reverse((lat, lon), language="en", zoom=14)
        if location is None:
            return "Unknown"
        addr = location.raw.get("address", {})

        # Prefer direct settlement-level names
        for key in _SETTLEMENT_KEYS:
            val = addr.get(key)
            if val and not _ADMIN_PATTERN.match(val):
                return val

        # Fallback to sub-settlement names, filtering out "Ward X" / "Community N" patterns
        for key in _SUB_KEYS:
            val = addr.get(key)
            if val and not _ADMIN_PATTERN.match(val):
                return val

        return "Unknown"
    except Exception as exc:
        logger.warning(f"Nominatim reverse geocode failed for ({lat}, {lon}): {exc}")
        return "Unknown"


def _make_boundary_feature(r: dict) -> dict:
    """Build a GeoJSON Feature dict from a partial result."""
    return {
        "type": "Feature",
        "geometry": mapping(r["boundary_wgs84"]),
        "properties": {
            "anchor_name": r["anchor_name"],
            "community_name": r["community_name"],
            "building_count": r["building_count"],
            "block_count": r["block_count"],
            "source": r.get("source_label") or "GRID3_NGA_v4",
        },
    }


def _compute_map_zoom_and_figsize(
    bounds: tuple[float, float, float, float],
    max_pixels: int = 2048,
    dpi: int = 100,
) -> tuple[int, tuple[float, float]]:
    """Compute tile zoom level and figure size (inches) to fit within max_pixels.

    Zoom is capped at 13 to avoid fetching thousands of high-resolution tiles
    (zoom=14 needs ~28MB of tile RAM vs ~13MB at zoom=13 for a typical community).
    For single-community maps the extent is small so zoom will naturally be ≥ 12.

    Args:
        bounds: (minx, miny, maxx, maxy) in WGS84 degrees (after padding).
        max_pixels: Maximum pixels on the longest side of the output image.
        dpi: Figure DPI; figsize is derived as pixels / dpi.

    Returns:
        (zoom, (fig_width_in, fig_height_in))
    """
    lat_mid = (bounds[1] + bounds[3]) / 2
    width_m = (bounds[2] - bounds[0]) * 111_320.0 * cos(radians(lat_mid))
    height_m = (bounds[3] - bounds[1]) * 111_320.0
    max_extent_m = max(width_m, height_m, 1.0)

    # Tile at zoom z covers 40_075_016 / 2^z metres.
    # We want (max_extent_m / tile_size_m) * 256 ≈ max_pixels.
    zoom = max(6, min(13, int(log2(max_pixels * 40_075_016 / (max_extent_m * 256)))))

    # Figsize: maintain aspect ratio, cap longest side at max_pixels / dpi inches
    aspect = width_m / max(height_m, 1.0)
    max_in = max_pixels / dpi
    if aspect >= 1.0:
        fig_w, fig_h = max_in, max_in / aspect
    else:
        fig_w, fig_h = max_in * aspect, max_in

    return zoom, (fig_w, fig_h)


def _render_map(partial_results: list[dict]) -> str:
    """Render satellite-underlay map with all community boundaries and anchor points.

    Follows the road_network.py pattern: plot in WGS84, pass crs="EPSG:4326"
    to contextily, use Agg backend for headless rendering.

    Tile zoom is computed adaptively from the map extent so the output image
    never exceeds 2048px on its longest side and tile RAM stays reasonable.

    Returns:
        Base64-encoded PNG string.
    """
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    try:
        import contextily as ctx
    except ImportError:
        ctx = None
        logger.warning("contextily not available; map will have no satellite basemap")

    # Compute padded extent BEFORE creating the figure so we can derive zoom/figsize.
    all_geoms = [r["boundary_wgs84"] for r in partial_results]
    all_gdf = gpd.GeoDataFrame(geometry=all_geoms, crs="EPSG:4326")
    bounds = all_gdf.total_bounds  # (minx, miny, maxx, maxy)
    pad = max((bounds[2] - bounds[0]), (bounds[3] - bounds[1])) * 0.15 + 0.002
    padded_bounds = (bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad)

    _DPI = 100
    zoom, figsize = _compute_map_zoom_and_figsize(padded_bounds, max_pixels=2048, dpi=_DPI)
    logger.info(f"Map render: zoom={zoom}, figsize={figsize[0]:.1f}×{figsize[1]:.1f}in @{_DPI}dpi")

    fig, ax = plt.subplots(1, 1, figsize=figsize)
    ax.set_aspect("equal")
    ax.set_xlim(padded_bounds[0], padded_bounds[2])
    ax.set_ylim(padded_bounds[1], padded_bounds[3])

    colors = ["#ff4444", "#44aaff", "#ffcc00", "#88ff44", "#ff88cc"]
    legend_handles = []

    for i, r in enumerate(partial_results):
        color = colors[i % len(colors)]
        geom = r["boundary_wgs84"]

        # Fill community boundary (semi-transparent)
        comm_gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
        comm_gdf.plot(ax=ax, facecolor=color, edgecolor=color, alpha=0.25, linewidth=2.5, zorder=3)

        # Anchor point marker
        ax.plot(
            r["lon"],
            r["lat"],
            marker="*",
            color=color,
            markersize=18,
            markeredgecolor="white",
            markeredgewidth=1.5,
            zorder=6,
            linestyle="None",
        )

        # Label at anchor
        ax.annotate(
            r["anchor_name"],
            xy=(r["lon"], r["lat"]),
            xytext=(5, 8),
            textcoords="offset points",
            fontsize=8,
            color="white",
            fontweight="bold",
            zorder=7,
            bbox=dict(boxstyle="round,pad=0.2", facecolor="black", alpha=0.6, edgecolor=color),
        )

        legend_handles.append(
            Patch(
                facecolor=color,
                alpha=0.35,
                edgecolor=color,
                label=(
                    f"{r['anchor_name']}: {r['community_name']} "
                    f"({r['building_count']} bldgs, {r['block_count']} blocks)"
                ),
            )
        )

    # Satellite basemap — fetched once at the adaptive zoom level
    if ctx is not None:
        try:
            ctx.add_basemap(
                ax,
                crs="EPSG:4326",
                source=ctx.providers.Esri.WorldImagery,
                zoom=zoom,
                attribution_size=6,
            )
        except Exception as exc:
            logger.warning(f"contextily basemap failed: {exc}; rendering without basemap")
            ax.set_facecolor("#1a1a2e")
    else:
        ax.set_facecolor("#1a1a2e")

    if len(partial_results) == 1:
        r = partial_results[0]
        ax.set_title(
            f"{r['community_name']} — {r['anchor_name']} ({r['building_count']} buildings)",
            fontsize=13,
            color="white",
            pad=10,
        )
    else:
        ax.set_title("Community Boundaries", fontsize=13, color="white", pad=10)
    fig.patch.set_facecolor("#0d0d1a")
    ax.tick_params(colors="white")

    ax.legend(
        handles=legend_handles,
        loc="lower left",
        fontsize=8,
        framealpha=0.8,
        facecolor="#1a1a2e",
        edgecolor="#444",
        labelcolor="white",
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    gpkg_path = os.environ.get("GRID3_GPKG_PATH")
    if not gpkg_path:
        print("Error: GRID3_GPKG_PATH environment variable is required", file=sys.stderr)
        sys.exit(1)

    test_anchors = [
        {"lat": 0.0, "lon": 0.0, "name": "EXAMPLE_SITE_001"},
    ]

    print(f"GeoPackage: {gpkg_path}")
    print(f"Anchors: {json.dumps(test_anchors, indent=2)}")
    print()

    results = detect_communities(test_anchors, gpkg_path)

    for r in results:
        print(f"Anchor:          {r.anchor_name}")
        print(f"Community name:  {r.community_name}")
        print(f"Building count:  {r.building_count}")
        print(f"Block count:     {r.block_count}")
        geom_type = r.boundary["geometry"]["type"]
        coords_count = (
            len(r.boundary["geometry"]["coordinates"][0])
            if geom_type == "Polygon"
            else sum(len(ring[0]) for ring in r.boundary["geometry"]["coordinates"])
        )
        print(f"Boundary:        {geom_type} ({coords_count} coords)")
        print()

    # Save map
    map_path = "/tmp/community_map.png"
    map_bytes = base64.b64decode(results[0].map_b64)
    with open(map_path, "wb") as f:
        f.write(map_bytes)
    print(f"Map saved to: {map_path}")

    # Optionally save boundary GeoJSON
    geojson_path = "/tmp/community_boundary.geojson"
    with open(geojson_path, "w", encoding="utf-8") as fj:
        json.dump(results[0].boundary, fj, indent=2)
    print(f"Boundary GeoJSON saved to: {geojson_path}")

    sys.exit(0)
