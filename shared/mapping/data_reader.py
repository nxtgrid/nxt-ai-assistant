"""
Data reading and extraction functions for site pipeline data.

This module handles:
- Fetching data from the database
- Parsing WKB geometry data
- Extracting GeoJSON features into typed dataclasses
"""

import json
import logging
from typing import Any, Optional, Union

from shapely import wkb
from shapely.geometry import Polygon

from shared.mapping.models import Building, Cable, Pole, SiteBoundary, SiteData, SiteMeta

logger = logging.getLogger(__name__)


def _ensure_dict(value: Any, default: Optional[dict] = None) -> dict:
    """Convert value to dict, handling various database return types.

    Handles:
    - None → default
    - dict → as-is
    - str → json.loads
    - bytes → decode then json.loads
    - list → wrap in {"features": list} for GeoJSON compatibility
    - other → try str() then json.loads, warn on failure
    """
    if value is None:
        return default or {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else (default or {})
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON string: {e}")
            return default or {}
    if isinstance(value, bytes):
        try:
            result = json.loads(value.decode("utf-8"))
            return result if isinstance(result, dict) else (default or {})
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"Failed to parse JSON bytes: {e}")
            return default or {}
    if isinstance(value, list):
        # Likely a GeoJSON features array without the wrapper
        logger.debug(f"Got list instead of dict, wrapping as features (len={len(value)})")
        return {"features": value}

    # Last resort: try to convert to string and parse
    # This handles psycopg3 Json/Jsonb wrapper types
    logger.warning(
        f"Unexpected type for JSON field: {type(value).__name__}. Attempting string conversion."
    )
    try:
        # Some DB adapters have a .obj attribute with the actual data
        if hasattr(value, "obj"):
            return _ensure_dict(value.obj, default)
        # Try string representation
        str_value = str(value)
        result = json.loads(str_value)
        return result if isinstance(result, dict) else (default or {})
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(
            f"Could not convert {type(value).__name__} to dict: {e}. "
            f"Value preview: {str(value)[:200]}"
        )
        return default or {}


def _parse_wkb(wkb_data: Union[bytes, memoryview, str]) -> Polygon:
    """Parse WKB data to shapely Polygon, handling various input formats."""
    if isinstance(wkb_data, memoryview):
        wkb_data = bytes(wkb_data)
    elif isinstance(wkb_data, str):
        # Handle hex string with optional SRID prefix
        if wkb_data.startswith("0103000020E6100000"):
            # Extended WKB with SRID (EPSG:4326) - reconstruct without SRID
            wkb_data = bytes.fromhex("0103000000" + wkb_data[18:])
        else:
            wkb_data = bytes.fromhex(wkb_data)
    return wkb.loads(wkb_data)


def extract_site_boundary(outline_geom: Union[bytes, memoryview, str]) -> SiteBoundary:
    """
    Extract site boundary from WKB geometry data.

    Args:
        outline_geom: WKB geometry data (bytes, memoryview, or hex string)

    Returns:
        SiteBoundary with polygon and bounds
    """
    polygon = _parse_wkb(outline_geom)
    return SiteBoundary.from_polygon(polygon)


def extract_buildings(buildings_data: Union[dict, str, None]) -> list[Building]:
    """
    Extract buildings from GeoJSON FeatureCollection.

    Args:
        buildings_data: GeoJSON dict or string with building features

    Returns:
        List of Building objects with coordinates and connection status
    """
    data = _ensure_dict(buildings_data, {"features": []})
    features = data.get("features", [])

    logger.debug(
        f"extract_buildings: input type={type(buildings_data).__name__}, "
        f"features count={len(features)}"
    )

    buildings = []
    skipped_empty_coords = 0
    connected_count = 0

    for feature in features:
        geometry = feature.get("geometry", {})
        properties = feature.get("properties", {})

        # Get outer ring coordinates (handle Polygon and MultiPolygon nesting)
        geom_type = geometry.get("type", "Polygon")
        raw_coords = geometry.get("coordinates", [[]])
        if geom_type == "MultiPolygon" and raw_coords:
            # MultiPolygon: [[[[lon,lat]...]]] → take first polygon's outer ring
            coords = raw_coords[0][0] if raw_coords[0] else []
        else:
            # Polygon: [[[lon,lat]...]] → take outer ring
            coords = raw_coords[0] if raw_coords else []
        # Guard against extra nesting (e.g. [[[lon,lat]]] instead of [[lon,lat]])
        if coords and isinstance(coords[0], list) and isinstance(coords[0][0], list):
            coords = coords[0]
        if not coords:
            skipped_empty_coords += 1
            continue

        # Check connection status
        connected = properties.get("connected", True)
        if connected:
            connected_count += 1

        # Get closest connection point if available
        closest_point = None
        if "closest_point" in properties:
            cp = properties["closest_point"]
            if cp and "coordinates" in cp:
                closest_point = tuple(cp["coordinates"])

        buildings.append(
            Building(
                coordinates=coords,
                connected=connected,
                closest_point=closest_point,
            )
        )

    logger.info(
        f"extract_buildings: {len(buildings)} buildings extracted "
        f"({connected_count} connected, {len(buildings) - connected_count} unconnected), "
        f"{skipped_empty_coords} skipped (empty coords)"
    )

    return buildings


def extract_poles(poles_data: Union[dict, str, None]) -> list[Pole]:
    """
    Extract poles from GeoJSON FeatureCollection.

    Args:
        poles_data: GeoJSON dict or string with pole point features

    Returns:
        List of Pole objects with coordinates
    """
    data = _ensure_dict(poles_data, {"features": []})
    features = data.get("features", [])

    poles = []
    for feature in features:
        geometry = feature.get("geometry", {})
        properties = feature.get("properties", {})

        coords = geometry.get("coordinates", [])
        if len(coords) < 2:
            continue

        poles.append(
            Pole(
                lon=coords[0],
                lat=coords[1],
                properties=properties,
            )
        )

    return poles


def extract_cables(distribution_data: Union[dict, str, None]) -> list[Cable]:
    """
    Extract cables/distribution lines from GeoJSON FeatureCollection.

    Args:
        distribution_data: GeoJSON dict or string with LineString features

    Returns:
        List of Cable objects with coordinates and length
    """
    data = _ensure_dict(distribution_data, {"features": []})
    features = data.get("features", [])

    cables = []
    for feature in features:
        geometry = feature.get("geometry", {})
        properties = feature.get("properties", {})

        coords = geometry.get("coordinates", [])
        if not coords:
            continue

        cables.append(
            Cable(
                coordinates=coords,
                length_meters=properties.get("length_meters"),
                properties=properties,
            )
        )

    return cables


def extract_meta(meta_data: Union[dict, str, None]) -> SiteMeta:
    """
    Extract site metadata.

    Args:
        meta_data: Metadata dict or JSON string

    Returns:
        SiteMeta object with pole count, coverage radius, etc.
    """
    data = _ensure_dict(meta_data, {})

    return SiteMeta(
        pole_count=data.get("pole_count", 0),
        pole_coverage_radius=data.get("pole_coverage_radius", 50.0),
        minimum_building_area=data.get("minimum_building_area", 30.0),
        served_building_count=data.get("served_building_count", 0),
        unserved_building_count=data.get("unserved_building_count", 0),
        distribution_line_total_length=data.get("distribution_line_total_length", 0.0),
        backbone_cable_length_m=data.get("backbone_cable_length_m", 0.0),
        drop_cable_length_m=data.get("drop_cable_length_m", 0.0),
        backbone_cable_count=data.get("backbone_cable_count", 0),
        drop_cable_count=data.get("drop_cable_count", 0),
        coverage_percentage=data.get("coverage_percentage", 0.0),
        average_span_length_m=data.get("average_span_length_m", 0.0),
        max_drop_cable_length_m=data.get("max_drop_cable_length_m", 0.0),
    )


def read_site_pipeline_row(row: dict) -> SiteData:
    """
    Parse a site pipeline database row into SiteData.

    This function takes a raw database row (as dict) and extracts all
    geographic features into typed dataclasses.

    Args:
        row: Database row dict with keys:
            - id: Site ID
            - site_name: Site name
            - outline_geom: WKB boundary geometry
            - buildings_geo_flat: GeoJSON buildings
            - poles_geo_flat: GeoJSON poles
            - distribution_geo_flat: GeoJSON distribution lines
            - meta_geo_flat: Metadata dict

    Returns:
        SiteData object with all extracted features
    """
    site_id = row.get("id", 0)
    site_name = row.get("site_name", "Unknown Site")

    boundary = extract_site_boundary(row["outline_geom"])
    buildings = extract_buildings(row.get("buildings_geo_flat"))
    poles = extract_poles(row.get("poles_geo_flat"))
    cables = extract_cables(row.get("distribution_geo_flat"))
    meta = extract_meta(row.get("meta_geo_flat"))

    return SiteData(
        site_id=site_id,
        site_name=site_name,
        boundary=boundary,
        buildings=buildings,
        poles=poles,
        cables=cables,
        meta=meta,
    )


async def fetch_site_pipeline_row(
    site_id: int,
    db_pool=None,
    db_config: Optional[dict] = None,
) -> dict:
    """
    Fetch a site pipeline row from the database.

    This function can use either an existing asyncpg pool or create
    a sync connection from config.

    Args:
        site_id: The site submission ID to fetch
        db_pool: Optional asyncpg connection pool
        db_config: Optional dict with host, port, dbname, user, password

    Returns:
        Raw database row as dict

    Raises:
        ValueError: If site_id not found
        RuntimeError: If no database connection available
    """
    query = """
        SELECT
            id, site_name,
            outline_geom,
            buildings_geo_flat,
            distribution_geo_flat,
            poles_geo_flat,
            meta_geo_flat
        FROM pd_site_submissions
        WHERE id = $1 AND deleted_at IS NULL
    """

    if db_pool is not None:
        # Use async pool
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(query, site_id)
            if not row:
                raise ValueError(f"Site ID {site_id} not found")
            return dict(row)

    elif db_config is not None:
        # Use sync connection via psycopg (v3)
        import psycopg

        conninfo = (
            f"host={db_config['host']} "
            f"port={db_config.get('port', 5432)} "
            f"dbname={db_config.get('dbname', 'postgres')} "
            f"user={db_config['user']} "
            f"password={db_config['password']}"
        )

        with psycopg.connect(conninfo) as conn:
            cur = conn.cursor()
            # psycopg3 uses %s placeholders (same as psycopg2)
            cur.execute(query.replace("$1", "%s"), (site_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Site ID {site_id} not found")

            columns = [
                "id",
                "site_name",
                "outline_geom",
                "buildings_geo_flat",
                "distribution_geo_flat",
                "poles_geo_flat",
                "meta_geo_flat",
            ]
            return dict(zip(columns, row))

    else:
        raise RuntimeError("No database connection available (provide db_pool or db_config)")
