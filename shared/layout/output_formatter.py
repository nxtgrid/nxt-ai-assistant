"""
Output formatter for distribution layout.

Converts internal GeoDataFrame representations to GeoJSON dicts that match
the pd_site_submissions table schema exactly. This module is the contract
boundary with shared/mapping/data_reader.py.

CRITICAL: All cable lengths are computed from UTM-projected geometries
BEFORE reprojection to WGS84. Computing .length on WGS84 gives degrees.
"""

import logging
from typing import Any

import geopandas as gpd
import numpy as np
from pyproj import Transformer

logger = logging.getLogger(__name__)

# WGS84 CRS for GeoJSON output (RFC 7946)
WGS84 = "EPSG:4326"


def format_layout_output(
    poles_gdf: gpd.GeoDataFrame,
    backbone_gdf: gpd.GeoDataFrame,
    drop_cables_gdf: gpd.GeoDataFrame,
    buildings_gdf: gpd.GeoDataFrame,
    original_buildings_geojson: dict[str, Any],
    spacing_m: float,
    max_drop_distance_m: float,
    kw_per_household: float = 0.0,
    power_factor: float = 0.95,
) -> dict[str, Any]:
    """Format layout as GeoJSON dicts matching pd_site_submissions schema.

    Args:
        poles_gdf: Poles in projected UTM CRS.
        backbone_gdf: Backbone cables in projected UTM CRS with length_meters.
        drop_cables_gdf: Drop cables in projected UTM CRS with length_meters.
        buildings_gdf: Buildings in projected UTM CRS with 'connected' and
            'closest_pole_point' columns.
        original_buildings_geojson: Original buildings_geo_flat from database
            (used to preserve building polygon geometry — we only update properties).
        spacing_m: Pole spacing used (for metadata).
        max_drop_distance_m: Max drop cable distance used (for metadata).
        kw_per_household: Real power demand per household (kW). When > 0,
            a ``power_kw`` property is added to each cable feature for heatmap
            rendering. Set to 0 to skip power flow computation (default).
        power_factor: LV distribution power factor (default 0.95). Only used
            when kw_per_household > 0.

    Returns:
        Dict with keys: poles_geo_flat, distribution_geo_flat,
        buildings_geo_flat, meta_geo_flat — all matching data_reader.py schema.
    """
    # --- Compute all lengths in UTM BEFORE reprojection ---
    backbone_lengths = (
        backbone_gdf["length_meters"].values if len(backbone_gdf) > 0 else np.array([])
    )
    drop_lengths = (
        drop_cables_gdf["length_meters"].values if len(drop_cables_gdf) > 0 else np.array([])
    )

    backbone_total_m = float(np.sum(backbone_lengths))
    drop_total_m = float(np.sum(drop_lengths))
    total_cable_m = backbone_total_m + drop_total_m

    # --- Power flow heatmap (optional) ---
    # Annotate each cable with power_kw (real power flowing through it).
    # Done in UTM before reprojection; power_kw column carries through to_crs().
    if kw_per_household > 0:
        from shared.layout.distribution import compute_power_flows

        backbone_gdf, drop_cables_gdf = compute_power_flows(
            backbone_gdf=backbone_gdf,
            drop_cables_gdf=drop_cables_gdf,
            poles_gdf=poles_gdf,
            kw_per_household=kw_per_household,
            power_factor=power_factor,
        )

    # --- Reproject to WGS84 for GeoJSON output ---
    poles_wgs = poles_gdf.to_crs(WGS84) if len(poles_gdf) > 0 else poles_gdf
    backbone_wgs = backbone_gdf.to_crs(WGS84) if len(backbone_gdf) > 0 else backbone_gdf
    drop_wgs = drop_cables_gdf.to_crs(WGS84) if len(drop_cables_gdf) > 0 else drop_cables_gdf

    # --- Format poles_geo_flat ---
    poles_features = []
    for _, pole in poles_wgs.iterrows():
        from_pole = pole.get("from_pole_idx")
        props = {
            "pole_type": pole.get("pole_type", "intermediate"),
            "from_pole_idx": (
                int(from_pole) if from_pole is not None and from_pole == from_pole else None
            ),
        }
        poles_features.append(
            {
                "geometry": {
                    "type": "Point",
                    "coordinates": [pole.geometry.x, pole.geometry.y],
                },
                "properties": props,
            }
        )

    poles_geo_flat = {"type": "FeatureCollection", "features": poles_features}

    # --- Format distribution_geo_flat (backbone + drop cables merged) ---
    cable_features = []

    for _, cable in backbone_wgs.iterrows():
        coords = list(cable.geometry.coords)
        props = {
            "length_meters": float(cable["length_meters"]),
            "cable_type": "backbone",
            "edge_type": cable.get("edge_type", "road"),
        }
        if "from_pole_idx" in cable.index:
            fpidx = cable["from_pole_idx"]
            props["from_pole_idx"] = int(fpidx) if fpidx is not None and fpidx == fpidx else None
        if "to_pole_idx" in cable.index:
            tpidx = cable["to_pole_idx"]
            props["to_pole_idx"] = int(tpidx) if tpidx is not None and tpidx == tpidx else None
        if "power_kw" in cable.index:
            props["power_kw"] = round(float(cable["power_kw"]), 3)
        cable_features.append(
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[c[0], c[1]] for c in coords],
                },
                "properties": props,
            }
        )

    for _, cable in drop_wgs.iterrows():
        coords = list(cable.geometry.coords)
        drop_props: dict[str, Any] = {
            "length_meters": float(cable["length_meters"]),
            "cable_type": "drop",
        }
        if "power_kw" in cable.index:
            drop_props["power_kw"] = round(float(cable["power_kw"]), 3)
        cable_features.append(
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[c[0], c[1]] for c in coords],
                },
                "properties": drop_props,
            }
        )

    distribution_geo_flat = {"type": "FeatureCollection", "features": cable_features}

    # --- Format buildings_geo_flat (update original with connected/closest_point) ---
    buildings_geo_flat = _update_buildings_geojson(
        original_buildings_geojson, buildings_gdf, poles_gdf.crs
    )

    # --- Compute meta_geo_flat ---
    connected_count = (
        int(buildings_gdf["connected"].sum()) if "connected" in buildings_gdf.columns else 0
    )
    total_count = len(buildings_gdf)
    unconnected_count = total_count - connected_count

    span_lengths = backbone_lengths if len(backbone_lengths) > 0 else np.array([0.0])
    drop_lengths_arr = drop_lengths if len(drop_lengths) > 0 else np.array([0.0])

    meta_geo_flat = {
        "pole_count": len(poles_gdf),
        "pole_coverage_radius": max_drop_distance_m,
        "minimum_building_area": 30.0,
        "served_building_count": connected_count,
        "unserved_building_count": unconnected_count,
        "distribution_line_total_length": total_cable_m,
        "backbone_cable_length_m": backbone_total_m,
        "drop_cable_length_m": drop_total_m,
        "backbone_cable_count": len(backbone_gdf),
        "drop_cable_count": len(drop_cables_gdf),
        "coverage_percentage": (connected_count / total_count * 100) if total_count > 0 else 0.0,
        "average_span_length_m": float(np.mean(span_lengths)) if len(span_lengths) > 0 else 0.0,
        "max_drop_cable_length_m": (
            float(np.max(drop_lengths_arr)) if len(drop_lengths_arr) > 0 else 0.0
        ),
    }

    logger.info(
        f"Layout output: {meta_geo_flat['pole_count']} poles, "
        f"{meta_geo_flat['backbone_cable_count']} backbone segments ({backbone_total_m:.0f}m), "
        f"{meta_geo_flat['drop_cable_count']} drop cables ({drop_total_m:.0f}m), "
        f"{meta_geo_flat['coverage_percentage']:.1f}% coverage"
    )

    return {
        "poles_geo_flat": poles_geo_flat,
        "distribution_geo_flat": distribution_geo_flat,
        "buildings_geo_flat": buildings_geo_flat,
        "meta_geo_flat": meta_geo_flat,
    }


def _update_buildings_geojson(
    original: dict[str, Any],
    buildings_gdf: gpd.GeoDataFrame,
    poles_crs: Any,
) -> dict[str, Any]:
    """Update the original buildings GeoJSON with connected/closest_point properties.

    Preserves original building polygon geometry (we don't modify shapes).
    Only adds/updates properties.connected and properties.closest_point.

    The closest_point format must match data_reader.py:147:
        properties["closest_point"] = {"coordinates": [lon, lat]}

    Args:
        original: Original buildings GeoJSON FeatureCollection.
        buildings_gdf: Buildings GeoDataFrame in projected UTM CRS.
        poles_crs: CRS of the poles GeoDataFrame (used to build the
            UTM-to-WGS84 Transformer for closest_pole_point conversion).
    """
    features = original.get("features", [])

    if len(features) != len(buildings_gdf):
        logger.warning(
            f"Building count mismatch: original={len(features)}, "
            f"processed={len(buildings_gdf)}. Returning original."
        )
        return original

    # Build Transformer once for converting UTM closest_pole_point to WGS84
    transformer = (
        Transformer.from_crs(poles_crs, WGS84, always_xy=True) if poles_crs is not None else None
    )

    updated_features = []
    for i, feature in enumerate(features):
        updated_feature = {
            "geometry": feature.get("geometry", {}),
            "properties": dict(feature.get("properties", {})),
        }

        connected = bool(buildings_gdf.iloc[i].get("connected", False))
        updated_feature["properties"]["connected"] = connected

        closest_pole_pt = buildings_gdf.iloc[i].get("closest_pole_point")
        if connected and closest_pole_pt is not None:
            if transformer is not None:
                transformed = transformer.transform(closest_pole_pt[0], closest_pole_pt[1])
                lon, lat = transformed[0], transformed[1]
            else:
                lon, lat = closest_pole_pt[0], closest_pole_pt[1]

            updated_feature["properties"]["closest_point"] = {
                "coordinates": [lon, lat],
            }
        else:
            updated_feature["properties"]["closest_point"] = None

        updated_features.append(updated_feature)

    return {"type": "FeatureCollection", "features": updated_features}
