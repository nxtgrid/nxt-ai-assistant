"""
High-level site map generator.

This module provides the main entry point for generating site maps,
returning results as JSON with base64-encoded images.
"""

import base64
from typing import Any, Optional

from shared.mapping.data_reader import fetch_site_pipeline_row, read_site_pipeline_row
from shared.mapping.models import SiteData
from shared.mapping.plot_builder import export_bytes, prepare_site_plot


def generate_site_map(
    site_id: Optional[int] = None,
    site_data: Optional[SiteData] = None,
    row_data: Optional[dict] = None,
    db_pool=None,
    db_config: Optional[dict] = None,
    format: str = "png",
    dpi: int = 150,
    figsize: tuple = (16, 12),
    add_satellite: bool = True,
    zoom: Optional[int] = None,
) -> dict[str, Any]:
    """
    Generate a site map and return as JSON with base64-encoded image.

    This is the main entry point for map generation. It accepts data from
    multiple sources:
    - site_id + db_pool/db_config: Fetch from database
    - site_data: Pre-parsed SiteData object
    - row_data: Raw database row dict

    Args:
        site_id: Site submission ID to fetch from database
        site_data: Pre-parsed SiteData object
        row_data: Raw database row dict to parse
        db_pool: Async database connection pool
        db_config: Database config dict (host, port, user, password, dbname)
        format: Output format (png, jpeg, svg, pdf)
        dpi: Resolution for raster formats
        figsize: Figure size as (width, height) in inches
        add_satellite: Whether to add satellite basemap
        zoom: Zoom level for satellite tiles

    Returns:
        dict with:
            - success: bool
            - image: base64-encoded image string (if success)
            - format: image format
            - metadata: dict with site info and statistics
            - error: error message (if not success)

    Example:
        # From database
        result = generate_site_map(site_id=201, db_config=config)

        # From pre-loaded data
        result = generate_site_map(site_data=my_site_data)

        # From raw row
        result = generate_site_map(row_data=db_row)
    """
    try:
        # Resolve site data from the appropriate source
        if site_data is not None:
            # Use provided SiteData directly
            data = site_data

        elif row_data is not None:
            # Parse from raw row dict
            data = read_site_pipeline_row(row_data)

        elif site_id is not None:
            # Fetch from database
            if db_pool is not None or db_config is not None:
                import asyncio

                # Handle async fetch
                async def _fetch():
                    return await fetch_site_pipeline_row(
                        site_id=site_id,
                        db_pool=db_pool,
                        db_config=db_config,
                    )

                # Check if we're in an async context
                try:
                    asyncio.get_running_loop()
                    # We're in async context - this shouldn't happen in MCP context
                    # but handle gracefully
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        row = executor.submit(asyncio.run, _fetch()).result()
                except RuntimeError:
                    # No running loop - safe to use asyncio.run
                    row = asyncio.run(_fetch())

                data = read_site_pipeline_row(row)
            else:
                return {
                    "success": False,
                    "error": "site_id provided but no database connection (db_pool or db_config)",
                    "format": format,
                    "metadata": {"site_id": site_id},
                }
        else:
            return {
                "success": False,
                "error": "No data source provided. Specify site_id, site_data, or row_data.",
                "format": format,
                "metadata": {},
            }

        # Generate the plot
        fig, ax = prepare_site_plot(
            site_data=data,
            figsize=figsize,
            add_satellite=add_satellite,
            zoom=zoom,
        )

        # Export to bytes
        image_bytes = export_bytes(fig, format=format, dpi=dpi)

        # Encode to base64
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        # Build statistics dict
        statistics: dict[str, Any] = {
            "total_buildings": len(data.buildings),
            "served_buildings": len(data.served_buildings),
            "unserved_buildings": len(data.unserved_buildings),
            "poles": len(data.poles),
            "coverage_radius_m": data.coverage_radius,
        }

        # Add cable length from meta (if available)
        if data.meta and data.meta.distribution_line_total_length:
            statistics["cable_length_m"] = data.meta.distribution_line_total_length

        # Build metadata
        metadata: dict[str, Any] = {
            "site_id": data.site_id,
            "site_name": data.site_name,
            "bounds": {
                "minx": data.boundary.minx,
                "miny": data.boundary.miny,
                "maxx": data.boundary.maxx,
                "maxy": data.boundary.maxy,
            },
            "center": {
                "lat": data.boundary.center_lat,
                "lon": data.boundary.center_lon,
            },
            "statistics": statistics,
        }

        return {
            "success": True,
            "image": image_b64,
            "format": format,
            "metadata": metadata,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "format": format,
            "metadata": {"site_id": site_id} if site_id else {},
        }


def generate_site_map_sync(
    site_id: int,
    db_config: dict,
    **kwargs,
) -> dict[str, Any]:
    """
    Synchronous version of generate_site_map for non-async contexts.

    This is a convenience wrapper that handles the database fetch
    synchronously using psycopg (v3).

    Args:
        site_id: Site submission ID to fetch
        db_config: Database config dict
        **kwargs: Additional arguments passed to generate_site_map

    Returns:
        Same as generate_site_map
    """
    import psycopg

    try:
        conninfo = (
            f"host={db_config['host']} "
            f"port={db_config.get('port', 5432)} "
            f"dbname={db_config.get('dbname', 'postgres')} "
            f"user={db_config['user']} "
            f"password={db_config['password']}"
        )

        with psycopg.connect(conninfo) as conn:
            cur = conn.cursor()
            query = """
                SELECT
                    id, site_name,
                    outline_geom,
                    buildings_geo_flat,
                    distribution_geo_flat,
                    poles_geo_flat,
                    meta_geo_flat
                FROM pd_site_submissions
                WHERE id = %s
            """
            cur.execute(query, (site_id,))
            row = cur.fetchone()

        if not row:
            return {
                "success": False,
                "error": f"Site ID {site_id} not found",
                "format": kwargs.get("format", "png"),
                "metadata": {"site_id": site_id},
            }

        columns = [
            "id",
            "site_name",
            "outline_geom",
            "buildings_geo_flat",
            "distribution_geo_flat",
            "poles_geo_flat",
            "meta_geo_flat",
        ]
        row_data = dict(zip(columns, row))

        return generate_site_map(row_data=row_data, **kwargs)

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "format": kwargs.get("format", "png"),
            "metadata": {"site_id": site_id},
        }
