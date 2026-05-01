"""
Mapping utilities for generating site maps from pipeline data.

This module provides functions to:
- Read site pipeline data from database or raw data
- Extract and parse geographic features (boundaries, buildings, poles, cables)
- Generate site map visualizations with satellite basemaps
- Export maps as base64-encoded images

Example usage:
    from shared.mapping import generate_site_map, read_site_pipeline_row

    # From database
    result = generate_site_map(site_id=201, db_config=DB_CONFIG)

    # From pre-loaded data
    site_data = read_site_pipeline_row(row_dict)
    result = generate_site_map(site_data=site_data)
"""

from shared.mapping.data_reader import (
    extract_buildings,
    extract_cables,
    extract_meta,
    extract_poles,
    extract_site_boundary,
    fetch_site_pipeline_row,
    read_site_pipeline_row,
)
from shared.mapping.generator import generate_site_map, generate_site_map_sync
from shared.mapping.models import Building, Cable, Pole, SiteBoundary, SiteData, SiteMeta
from shared.mapping.plot_builder import (
    add_boundary,
    add_buildings,
    add_cables,
    add_poles,
    export_bytes,
    export_png,
    prepare_base_map,
    prepare_site_plot,
)

__all__ = [
    # Models
    "SiteData",
    "SiteBoundary",
    "Building",
    "Pole",
    "Cable",
    "SiteMeta",
    # Data reading
    "read_site_pipeline_row",
    "fetch_site_pipeline_row",
    "extract_site_boundary",
    "extract_buildings",
    "extract_poles",
    "extract_cables",
    "extract_meta",
    # Plot building
    "prepare_site_plot",
    "prepare_base_map",
    "add_buildings",
    "add_boundary",
    "add_poles",
    "add_cables",
    "export_png",
    "export_bytes",
    # High-level generator
    "generate_site_map",
    "generate_site_map_sync",
]
