"""QGIS project builder from distribution layout GeoJSON.

Uses a PyQGIS-generated template .qgs file as the base (ensuring full QGIS
compatibility), and outputs a .qgs + .gpkg pair. Both files must live in the
same directory — the .qgs references ``./distribution_network.gpkg``.

The template is stored on Google Drive (``QGIS_TEMPLATE_FILE_ID``) so that
engineers can evolve it without code deploys. Falls back to the local copy
checked into ``shared/layout/qgis_template.qgs`` if the Drive fetch fails.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape

logger = logging.getLogger(__name__)

# Drive file ID for the QGIS template (primary source of truth)
QGIS_TEMPLATE_FILE_ID = os.environ.get("QGIS_TEMPLATE_FILE_ID")

# Local fallback template (checked into repo)
_LOCAL_TEMPLATE_PATH = Path(__file__).parent / "qgis_template.qgs"

GPKG_FILENAME = "distribution_network.gpkg"


def _fetch_template() -> str:
    """Fetch QGIS template XML from Google Drive, falling back to local file."""
    if QGIS_TEMPLATE_FILE_ID:
        try:
            from shared.utils.drive_upload import download_from_drive

            template_bytes = download_from_drive(QGIS_TEMPLATE_FILE_ID)
            logger.info("Loaded QGIS template from Google Drive (%s)", QGIS_TEMPLATE_FILE_ID)
            return template_bytes.decode("utf-8")
        except Exception as exc:
            logger.warning(
                "Failed to fetch QGIS template from Drive, using local fallback: %s", exc
            )

    return _LOCAL_TEMPLATE_PATH.read_text(encoding="utf-8")


def build_qgis_project(
    layout_result: dict[str, Any],
    site_name: str,
    number_of_phases: str,
    max_drop_distance_m: float,
    site_boundary_wgs84: dict | None = None,
    arrestors_gdf: gpd.GeoDataFrame | None = None,
    jumpers_gdf: gpd.GeoDataFrame | None = None,
) -> tuple[bytes, bytes]:
    """Build a QGIS project (.qgs) and GeoPackage (.gpkg) from layout data.

    Returns a (.qgs XML bytes, .gpkg bytes) tuple. Both files must be saved
    to the same directory — the .qgs references ``./distribution_network.gpkg``.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        gpkg_path = tmp / GPKG_FILENAME

        layers = _build_all_layers(
            layout_result,
            site_name,
            number_of_phases,
            max_drop_distance_m,
            site_boundary_wgs84,
            arrestors_gdf,
            jumpers_gdf,
        )

        _write_geopackage(gpkg_path, layers)

        # Fetch template from Drive (or local fallback)
        qgs_content = _fetch_template()

        # Update project title (escape XML special characters including quotes)
        safe_name = escape(site_name, entities={'"': "&quot;"})
        qgs_content = re.sub(
            r'projectname="[^"]*"',
            f'projectname="{safe_name} Distribution Design"',
            qgs_content,
            count=1,
        )

        # Update dynamic drop cable layer name (template has "Drop Cable - 40m")
        drop_name = f"Drop Cable - {max_drop_distance_m:.0f}m"
        if drop_name != "Drop Cable - 40m":
            qgs_content = qgs_content.replace("Drop Cable - 40m", drop_name)

        return qgs_content.encode("utf-8"), gpkg_path.read_bytes()


def _build_all_layers(
    layout_result: dict[str, Any],
    site_name: str,
    number_of_phases: str,
    max_drop_distance_m: float,
    site_boundary_wgs84: dict | None,
    arrestors_gdf: gpd.GeoDataFrame | None = None,
    jumpers_gdf: gpd.GeoDataFrame | None = None,
) -> dict[str, gpd.GeoDataFrame]:
    """Build all GeoDataFrames for the GeoPackage layers."""
    layers: dict[str, gpd.GeoDataFrame] = {}

    poles_geojson = layout_result.get("poles_geo_flat", {})
    dist_geojson = layout_result.get("distribution_geo_flat", {})
    buildings_geojson = layout_result.get("buildings_geo_flat", {})

    # Collect pole indices that have arrestors/jumpers for Poles Network update
    arrestor_pole_indices = set()
    jumper_pole_indices = set()
    if arrestors_gdf is not None and len(arrestors_gdf) > 0:
        arrestor_pole_indices = set(arrestors_gdf["pole_idx"].values)
    if jumpers_gdf is not None and len(jumpers_gdf) > 0:
        jumper_pole_indices = set(jumpers_gdf["pole_idx"].values)

    # --- Power Plant ---
    layers["Power Plant"] = _build_power_plant_layer(poles_geojson)

    # --- Poles Network (categorized on pole_status) ---
    layers["Poles Network"] = _build_poles_layer(
        poles_geojson, arrestor_pole_indices, jumper_pole_indices
    )

    # --- Poles Network copy (categorized on Phase Network — empty for designer) ---
    layers["Poles Network copy"] = _empty_poles_layer()

    # --- Power Lines (backbone cables) ---
    layers["power_lines"] = _build_power_lines_layer(
        dist_geojson,
        number_of_phases,
    )

    # --- Drop Cable layers ---
    drop_name = f"Drop Cable - {max_drop_distance_m:.0f}m"
    layers[drop_name] = _build_drop_cable_layer(
        dist_geojson,
        max_distance_m=max_drop_distance_m,
    )
    layers["Drop Cable - All"] = _build_drop_cable_layer(dist_geojson, max_distance_m=None)

    # --- Building ---
    layers["Building"] = _build_building_layer(buildings_geojson)

    # --- Distribution Network (boundary) ---
    layers["Distribution Network"] = _build_boundary_layer(
        site_boundary_wgs84,
        site_name,
    )

    # --- Cable Load Heatmap (added when power_kw computed by pipeline) ---
    heatmap_layer = _build_cable_load_layer(dist_geojson)
    if heatmap_layer is not None:
        layers["Cable Load (kW)"] = heatmap_layer

    # --- Lightning Arresters (populated by Phase 2 placement, or empty) ---
    layers["Lightning Arresters"] = _build_annotation_point_layer(arrestors_gdf, poles_geojson)

    # --- Power Jumper (populated by Phase 2 placement, or empty) ---
    layers["Power Jumper"] = _build_annotation_point_layer(jumpers_gdf, poles_geojson)

    layers["Transformer"] = _empty_point_layer(["name"])

    # --- Site plan layers (empty placeholders for designer) ---
    _add_site_plan_layers(layers)

    return layers


def _build_power_plant_layer(poles_geojson: dict) -> gpd.GeoDataFrame:
    """Extract power plant point(s) from poles GeoJSON."""
    features = poles_geojson.get("features", [])
    plants = [f for f in features if f.get("properties", {}).get("pole_type") == "plant"]

    if not plants:
        return gpd.GeoDataFrame(
            {
                "Landmark": pd.Series(dtype="str"),
                "x_coord": pd.Series(dtype="float"),
                "y_coord": pd.Series(dtype="float"),
            },
            geometry=[],
            crs="EPSG:4326",
        )

    rows = []
    for f in plants:
        geom = shape(f["geometry"])
        rows.append(
            {
                "geometry": geom,
                "Landmark": "Power Plant",
                "x_coord": geom.x,
                "y_coord": geom.y,
            }
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _build_poles_layer(
    poles_geojson: dict,
    arrestor_pole_indices: set[int] | None = None,
    jumper_pole_indices: set[int] | None = None,
) -> gpd.GeoDataFrame:
    """Build Poles Network layer with all schema fields.

    When arrestor_pole_indices or jumper_pole_indices are provided,
    sets Lightning Arrestor / LV Power Jumpers to "Yes" for those poles.
    """
    features = poles_geojson.get("features", [])
    if not features:
        return _empty_poles_layer()

    arrestor_set = arrestor_pole_indices or set()
    jumper_set = jumper_pole_indices or set()

    rows = []
    for i, f in enumerate(features):
        geom = shape(f["geometry"])
        props = f.get("properties", {})
        pole_type = props.get("pole_type", "intermediate")
        rows.append(
            {
                "geometry": geom,
                "Pole_ID": f"P-{i + 1:03d}",
                "pole_status": "New",
                "phase type": "",
                "Medium Voltage": "No",
                "Low Voltage": "Yes" if pole_type != "plant" else "No",
                "Lightning Arrestor": "Yes" if i in arrestor_set else "No",
                "LV Power Jumpers": "Yes" if i in jumper_set else "No",
                "x_coord": geom.x,
                "y_coord": geom.y,
            }
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _empty_poles_layer() -> gpd.GeoDataFrame:
    """Empty Poles Network layer with correct schema."""
    return gpd.GeoDataFrame(
        {
            "Pole_ID": pd.Series(dtype="str"),
            "pole_status": pd.Series(dtype="str"),
            "phase type": pd.Series(dtype="str"),
            "Medium Voltage": pd.Series(dtype="str"),
            "Low Voltage": pd.Series(dtype="str"),
            "Lightning Arrestor": pd.Series(dtype="str"),
            "LV Power Jumpers": pd.Series(dtype="str"),
            "x_coord": pd.Series(dtype="float"),
            "y_coord": pd.Series(dtype="float"),
        },
        geometry=[],
        crs="EPSG:4326",
    )


def _build_power_lines_layer(
    dist_geojson: dict,
    number_of_phases: str,
) -> gpd.GeoDataFrame:
    """Build Power Lines layer from backbone cables."""
    features = dist_geojson.get("features", [])
    backbone = [f for f in features if f.get("properties", {}).get("cable_type") == "backbone"]

    if not backbone:
        return _empty_power_lines()

    rows = []
    for f in backbone:
        geom = shape(f["geometry"])
        props = f.get("properties", {})
        length_m = props.get("length_meters", 0.0)

        if number_of_phases == "3":
            phase_class = "3-phase"
            voltage_phase = "LV-3"
        else:
            phase_class = "1-phase"
            voltage_phase = "LV-1"

        rows.append(
            {
                "geometry": geom,
                "length": length_m,
                "Phase class": phase_class,
                "Voltage-phase": voltage_phase,
                "voltage class": "LV",
                "Network status": "New",
            }
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _empty_power_lines() -> gpd.GeoDataFrame:
    """Empty Power Lines layer with correct schema."""
    return gpd.GeoDataFrame(
        {
            "length": pd.Series(dtype="float"),
            "Phase class": pd.Series(dtype="str"),
            "Voltage-phase": pd.Series(dtype="str"),
            "voltage class": pd.Series(dtype="str"),
            "Network status": pd.Series(dtype="str"),
        },
        geometry=[],
        crs="EPSG:4326",
    )


def _build_drop_cable_layer(
    dist_geojson: dict,
    max_distance_m: float | None = None,
) -> gpd.GeoDataFrame:
    """Build a drop cable layer, optionally filtered by max distance."""
    features = dist_geojson.get("features", [])
    drops = [f for f in features if f.get("properties", {}).get("cable_type") == "drop"]

    if max_distance_m is not None:
        drops = [
            f for f in drops if f.get("properties", {}).get("length_meters", 0) <= max_distance_m
        ]

    if not drops:
        return gpd.GeoDataFrame(
            {"length": pd.Series(dtype="float")},
            geometry=[],
            crs="EPSG:4326",
        )

    rows = []
    for f in drops:
        geom = shape(f["geometry"])
        rows.append(
            {
                "geometry": geom,
                "length": f.get("properties", {}).get("length_meters", 0.0),
            }
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _build_building_layer(buildings_geojson: dict) -> gpd.GeoDataFrame:
    """Build Building layer from buildings GeoJSON."""
    features = buildings_geojson.get("features", [])
    if not features:
        return gpd.GeoDataFrame(
            {
                "osm_id": pd.Series(dtype="str"),
                "building": pd.Series(dtype="str"),
                "area": pd.Series(dtype="float"),
                "connected": pd.Series(dtype="bool"),
            },
            geometry=[],
            crs="EPSG:4326",
        )

    rows = []
    for f in features:
        geom_dict = f.get("geometry")
        if not geom_dict:
            continue
        geom = shape(geom_dict)
        props = f.get("properties", {})
        rows.append(
            {
                "geometry": geom,
                "osm_id": str(props.get("osm_id", "")),
                "building": str(props.get("building", "yes")),
                "area": float(props.get("area", 0.0)),
                "connected": bool(props.get("connected", False)),
            }
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _build_cable_load_layer(dist_geojson: dict) -> gpd.GeoDataFrame | None:
    """Build Cable Load (kW) layer for power heatmap visualisation.

    Returns None when no ``power_kw`` property is present (i.e. power flows
    were not computed for this layout — keeps the layer out of older exports).

    The layer contains all backbone cables with ``power_kw``, ``cable_type``,
    and ``length_m`` fields.  Drop cables are intentionally excluded — they all
    carry the same single-household load and would clutter the heatmap.
    Apply a Graduated Color renderer on ``power_kw`` in QGIS for visualisation.
    """
    features = dist_geojson.get("features", [])
    backbone = [
        f
        for f in features
        if f.get("properties", {}).get("cable_type") == "backbone"
        and "power_kw" in f.get("properties", {})
    ]

    if not backbone:
        return None

    rows = []
    for f in backbone:
        geom = shape(f["geometry"])
        props = f.get("properties", {})
        rows.append(
            {
                "geometry": geom,
                "power_kw": float(props.get("power_kw", 0.0)),
                "cable_type": str(props.get("cable_type", "backbone")),
                "length_m": float(props.get("length_meters", 0.0)),
            }
        )

    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _build_annotation_point_layer(
    gdf: gpd.GeoDataFrame | None,
    poles_geojson: dict,
) -> gpd.GeoDataFrame:
    """Build a point layer from annotation placement results (arrestors or jumpers)."""
    if gdf is None or len(gdf) == 0:
        return _empty_point_layer(["pole ID", "x_coord", "y_coord"])

    features = poles_geojson.get("features", [])
    rows = []
    for _, row in gdf.iterrows():
        pole_idx = int(row["pole_idx"])
        geom = row.geometry
        pole_id = f"P-{pole_idx + 1:03d}" if pole_idx < len(features) else f"P-{pole_idx}"
        rows.append(
            {
                "geometry": geom,
                "pole ID": pole_id,
                "x_coord": geom.x,
                "y_coord": geom.y,
            }
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def _build_boundary_layer(
    boundary_wgs84: dict | None,
    site_name: str,
) -> gpd.GeoDataFrame:
    """Build Distribution Network boundary layer."""
    if not boundary_wgs84:
        return gpd.GeoDataFrame(
            {"name": pd.Series(dtype="str")},
            geometry=[],
            crs="EPSG:4326",
        )

    geom = shape(boundary_wgs84)
    return gpd.GeoDataFrame(
        [{"geometry": geom, "name": site_name}],
        crs="EPSG:4326",
    )


def _empty_point_layer(columns: list[str]) -> gpd.GeoDataFrame:
    """Create an empty point layer with the given columns."""
    data = {col: pd.Series(dtype="str") for col in columns}
    return gpd.GeoDataFrame(data, geometry=[], crs="EPSG:4326")


_SITE_PLAN_POINT_LAYERS = [
    "Solar Arrays",
    "Powerplant Footprint",
    "Site Border",
    "Gate",
    "Feeder Pillar",
    "Site Lightning Arresters",
]


def _add_site_plan_layers(
    layers: dict[str, gpd.GeoDataFrame],
) -> None:
    """Add empty site plan layers as placeholders for the designer."""
    for name in _SITE_PLAN_POINT_LAYERS:
        layers[name] = gpd.GeoDataFrame(
            {"name": pd.Series(dtype="str")},
            geometry=[],
            crs="EPSG:4326",
        )
    layers["Cable Routes"] = gpd.GeoDataFrame(
        {
            "cable_type": pd.Series(dtype="str"),
            "length_m": pd.Series(dtype="float"),
            "label": pd.Series(dtype="str"),
        },
        geometry=[],
        crs="EPSG:4326",
    )


def _write_geopackage(
    gpkg_path: Path,
    layers: dict[str, gpd.GeoDataFrame],
) -> None:
    """Write all layers to a single GeoPackage file."""
    for i, (layer_name, gdf) in enumerate(layers.items()):
        gdf.to_file(
            str(gpkg_path),
            layer=layer_name,
            driver="GPKG",
            mode="w" if i == 0 else "a",
        )
