"""MCP Solar Server - Solar potential assessment using Global Solar Atlas API.

Provides tools for fetching yearly solar generation potential (kWh/kWp) for any
geographic location using the Global Solar Atlas free API.

Tools (prefixed with 'solar_' when exposed via bridge):
- get_solar_potential: Get solar potential data for a latitude/longitude location
"""

import asyncio
import concurrent.futures
import json
import logging
import math
import os
import sys
from typing import Any, Dict, List, Optional

import httpx
import mcp.server.stdio
import mcp.types as types
import numpy as np
import rasterio
import rasterio.errors
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities
from rasterio.mask import mask as rasterio_mask

from shared.utils.response_formatters import compose_error_response, compose_json_response

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("solar-server")

# Startup message
print("🚀 Solar MCP Server starting...", file=sys.stderr)

# Initialize MCP server
server = Server("solar-server")

# Configuration
SOLAR_ACTIONS_ENABLED = os.getenv("SOLAR_ACTIONS_ENABLED", "true").lower() == "true"
GSA_API_BASE_URL = "https://api.globalsolaratlas.info/data/lta"

# WRI Aqueduct Floods v2
_FLOOD_BASE_URL = "https://wri-projects.s3.amazonaws.com/AqueductFloodTool/download/v2"
_FLOOD_RIVERINE_RP1000_URL = (
    f"{_FLOOD_BASE_URL}/inunriver_historical_000000000WATCH_1980_rp01000.tif"
)
_FLOOD_COASTAL_RP1000_URL = f"{_FLOOD_BASE_URL}/inuncoast_historical_nosub_hist_rp1000_0.tif"
_FLOOD_RIVERINE_RP100_URL = (
    f"{_FLOOD_BASE_URL}/inunriver_historical_000000000WATCH_1980_rp00100.tif"
)
# RCP8.5 2050 RP100 — 5 CMIP5 models. Sampled to get median and max across ensemble.
_FLOOD_RCP85_2050_RP100_MODELS = [
    f"{_FLOOD_BASE_URL}/inunriver_rcp8p5_00000NorESM1-M_2050_rp00100.tif",
    f"{_FLOOD_BASE_URL}/inunriver_rcp8p5_0000GFDL-ESM2M_2050_rp00100.tif",
    f"{_FLOOD_BASE_URL}/inunriver_rcp8p5_0000HadGEM2-ES_2050_rp00100.tif",
    f"{_FLOOD_BASE_URL}/inunriver_rcp8p5_00IPSL-CM5A-LR_2050_rp00100.tif",
    f"{_FLOOD_BASE_URL}/inunriver_rcp8p5_MIROC-ESM-CHEM_2050_rp00100.tif",
]

# Copernicus DEM GLO-30 (30m resolution, public S3)
_COP_DEM_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


def _cop_dem_tile_url(lat: float, lng: float) -> str:
    lat_prefix = "N" if lat >= 0 else "S"
    lng_prefix = "E" if lng >= 0 else "W"
    lat_tile = int(math.floor(abs(lat)))
    lng_tile = int(math.floor(abs(lng)))
    name = f"Copernicus_DSM_COG_10_{lat_prefix}{lat_tile:02d}_00_{lng_prefix}{lng_tile:03d}_00_DEM"
    return f"{_COP_DEM_BASE}/{name}/{name}.tif"


def _clamp_flood(value: float, nodata: Optional[float]) -> float:
    """Clamp nodata (-9999) and negative values to 0.0 before comparisons."""
    if nodata is not None and abs(value - nodata) < 1.0:
        return 0.0
    return max(0.0, float(value))


def _sample(url: str, lng: float, lat: float) -> float:
    """Sample a single rasterio COG at a point, clamping nodata to 0.0."""
    with rasterio.open(url) as src:
        val = float(list(src.sample([(lng, lat)]))[0][0])
        return _clamp_flood(val, src.nodata)


def _sample_or_none(
    url: str, lng: float, lat: float, label: str
) -> tuple[Optional[float], Optional[str]]:
    """Sample a COG at a point. Returns (value, None) on success or (None, reason) on
    failure — never raises, so one missing/unreachable data source doesn't take down
    the others. `reason` is a short, user-facing sentence naming the specific layer."""
    try:
        return _sample(url, lng, lat), None
    except rasterio.errors.RasterioIOError as e:
        logger.warning(f"{label} sample failed for ({lat}, {lng}): {e}")
        return None, f"{label} unavailable (data source unreachable)"
    except Exception as e:
        logger.warning(f"{label} sample failed for ({lat}, {lng}): {e}")
        return None, f"{label} unavailable ({e})"


def _query_flood(lat: float, lng: float) -> Dict[str, Any]:
    """
    Sample WRI Aqueduct flood depths at a point.
    Synchronous rasterio calls — invoke via asyncio.to_thread().
    RCP8.5 ensemble reads parallelised with ThreadPoolExecutor (different URLs, no shared VSI handles).
    Returns depth in metres above ground (no DEM subtraction needed).

    Each layer is sampled independently — a failure in one (e.g. an upstream data
    provider taking a file down) leaves the others intact instead of failing the
    whole query. Missing fields are returned as None with a reason in "flood_warnings".

    Fields:
    - flood_worst_case_depth_m: max(riverine RP1000, coastal RP1000) — structural design flood
    - flood_riverine_rp1000_m / flood_coastal_rp1000_m: components of worst case
    - flood_rp100_historical_m: historical RP100 (1% annual chance; ~18% over 20-year asset life)
    - flood_rp100_rcp85_2050_median_m: median across 5 CMIP5 models, RCP8.5 2050 RP100
    - flood_rp100_rcp85_2050_max_m: max across 5 CMIP5 models (conservative planning value)
    """
    warnings: List[str] = []

    riverine_rp1000, err = _sample_or_none(
        _FLOOD_RIVERINE_RP1000_URL, lng, lat, "Riverine RP1000 flood depth"
    )
    if err:
        warnings.append(err)
    coastal_rp1000, err = _sample_or_none(
        _FLOOD_COASTAL_RP1000_URL, lng, lat, "Coastal RP1000 flood depth"
    )
    if err:
        warnings.append(err)
    rp100_historical, err = _sample_or_none(
        _FLOOD_RIVERINE_RP100_URL, lng, lat, "Historical RP100 flood depth"
    )
    if err:
        warnings.append(err)

    # Read 5 RCP8.5 CMIP5 models in parallel — different URLs share no VSI handles.
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(_sample_or_none, url, lng, lat, "RCP8.5 2050 flood ensemble model")
            for url in _FLOOD_RCP85_2050_RP100_MODELS
        ]
        rcp85_results = [f.result() for f in futures]
    rcp85_vals = sorted(v for v, _ in rcp85_results if v is not None)
    n_available = len(rcp85_vals)
    if n_available == 0:
        rcp85_median: Optional[float] = None
        rcp85_max: Optional[float] = None
        warnings.append("RCP8.5 2050 flood ensemble unavailable (all 5 models failed)")
    else:
        rcp85_median = rcp85_vals[n_available // 2]
        rcp85_max = rcp85_vals[-1]
        if n_available < len(_FLOOD_RCP85_2050_RP100_MODELS):
            warnings.append(
                f"RCP8.5 2050 flood ensemble partial ({n_available}/"
                f"{len(_FLOOD_RCP85_2050_RP100_MODELS)} models available)"
            )

    worst_case: Optional[float] = None
    if riverine_rp1000 is not None or coastal_rp1000 is not None:
        worst_case = max(v for v in (riverine_rp1000, coastal_rp1000) if v is not None)

    return {
        "flood_worst_case_depth_m": round(worst_case, 3) if worst_case is not None else None,
        "flood_riverine_rp1000_m": round(riverine_rp1000, 3)
        if riverine_rp1000 is not None
        else None,
        "flood_coastal_rp1000_m": round(coastal_rp1000, 3) if coastal_rp1000 is not None else None,
        "flood_rp100_historical_m": (
            round(rp100_historical, 3) if rp100_historical is not None else None
        ),
        "flood_rp100_rcp85_2050_median_m": (
            round(rcp85_median, 3) if rcp85_median is not None else None
        ),
        "flood_rp100_rcp85_2050_max_m": round(rcp85_max, 3) if rcp85_max is not None else None,
        "flood_warnings": warnings,
    }


def _query_terrain(lat: float, lng: float, boundary_geometry: Optional[Dict]) -> Dict[str, Any]:
    """
    Sample Copernicus DEM GLO-30 for site elevation and boundary terrain stats.
    Synchronous rasterio call — invoke via asyncio.to_thread().

    boundary_geometry: parsed GeoJSON geometry dict (Polygon/MultiPolygon), or None.
    If None, boundary stats are returned as null — no radius fallback by design.
    A synthetic circle produces false precision for grading calculations.
    """
    tile_url = _cop_dem_tile_url(lat, lng)
    site_elevation_m = None
    boundary_min = None
    boundary_max = None
    boundary_range = None
    warnings: List[str] = []

    try:
        with rasterio.open(tile_url) as src:
            # Point elevation — always attempted
            val = float(list(src.sample([(lng, lat)]))[0][0])
            if src.nodata is None or abs(val - src.nodata) > 1.0:
                site_elevation_m = round(val, 1)
            else:
                warnings.append("Site elevation unavailable (no-data pixel at this location)")

            # Boundary terrain stats — only if polygon provided
            if boundary_geometry is not None:
                try:
                    masked_data, _ = rasterio_mask(src, [boundary_geometry], crop=True)
                    nodata_val = src.nodata
                    flat = masked_data.flatten().astype(float)
                    if nodata_val is not None:
                        valid = flat[np.abs(flat - nodata_val) > 1.0]
                    else:
                        valid = flat
                    valid = valid[np.isfinite(valid)]
                    if len(valid) > 0:
                        boundary_min = round(float(valid.min()), 1)
                        boundary_max = round(float(valid.max()), 1)
                        boundary_range = round(float(valid.max() - valid.min()), 1)
                    else:
                        logger.warning(
                            f"No valid DEM pixels within boundary polygon at ({lat}, {lng}) "
                            f"— polygon may be outside tile bounds"
                        )
                        warnings.append(
                            "Boundary terrain stats unavailable (no valid DEM pixels "
                            "within the site boundary)"
                        )
                except Exception as e:
                    logger.warning(f"DEM boundary mask failed at ({lat}, {lng}): {e}")
                    warnings.append(f"Boundary terrain stats unavailable ({e})")

    except rasterio.errors.RasterioIOError as e:
        logger.warning(f"DEM tile unavailable for ({lat}, {lng}): {e}")
        warnings.append("Terrain elevation unavailable (no DEM tile covers this location)")

    return {
        "site_elevation_m": site_elevation_m,
        "boundary_min_elevation_m": boundary_min,
        "boundary_max_elevation_m": boundary_max,
        "boundary_elevation_range_m": boundary_range,
        "terrain_warnings": warnings,
    }


# HTTP client for API requests
_http_client: httpx.AsyncClient | None = None


async def _get_http_client() -> httpx.AsyncClient:
    """Get or create HTTP client."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


async def _close_http_client():
    """Close HTTP client if open."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


async def fetch_solar_potential(latitude: float, longitude: float) -> Dict[str, Any]:
    """
    Fetch solar potential data from Global Solar Atlas API.

    Args:
        latitude: Latitude (-90 to 90)
        longitude: Longitude (-180 to 180)

    Returns:
        Dict with solar potential data including:
        - yearly_kwh_per_kwp: Annual PV output in kWh/kWp
        - daily_kwh_per_kwp: Average daily PV output in kWh/kWp
        - ghi_kwh_m2: Global Horizontal Irradiation
        - dni_kwh_m2: Direct Normal Irradiation
        - gti_kwh_m2: Global Tilted Irradiation (optimal angle)
        - optimal_tilt_deg: Optimal tilt angle in degrees
        - avg_temp_c: Average air temperature
        - elevation_m: Elevation in meters
    """
    client = await _get_http_client()

    url = f"{GSA_API_BASE_URL}?loc={latitude},{longitude}"
    logger.info(f"Fetching solar potential for ({latitude}, {longitude})")

    try:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"GSA API HTTP error: {e.response.status_code}")
        raise Exception(f"Global Solar Atlas API error: {e.response.status_code}")
    except httpx.RequestError as e:
        logger.error(f"GSA API request error: {e}")
        raise Exception(f"Failed to connect to Global Solar Atlas API: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"GSA API response parse error: {e}")
        raise Exception("Invalid response from Global Solar Atlas API")

    # Extract annual data
    annual = data.get("annual", {})
    annual_data = annual.get("data", {})

    if not annual_data:
        raise Exception("No solar data available for this location")

    # Calculate daily potential from annual PVOUT
    # API returns PVOUT_csi (PV output with crystalline silicon assumption)
    pvout = annual_data.get("PVOUT_csi") or annual_data.get("PVOUT")
    if pvout is None:
        raise Exception("PVOUT data not available for this location")

    daily_kwh = pvout / 365.25

    result = {
        "success": True,
        "yearly_kwh_per_kwp": round(pvout, 2),
        "daily_kwh_per_kwp": round(daily_kwh, 2),
        "ghi_kwh_m2": round(annual_data.get("GHI", 0), 2),
        "dni_kwh_m2": round(annual_data.get("DNI", 0), 2),
        "gti_kwh_m2": round(annual_data.get("GTI_opta", 0) or annual_data.get("GTI", 0), 2),
        "optimal_tilt_deg": annual_data.get("OPTA"),
        "avg_temp_c": round(annual_data.get("TEMP", 0), 2),
        "elevation_m": annual_data.get("ELE"),
        "location": {"lat": latitude, "lon": longitude},
        "data_source": "Global Solar Atlas",
    }

    logger.info(
        f"Solar potential for ({latitude}, {longitude}): "
        f"{result['daily_kwh_per_kwp']} kWh/kWp/day, "
        f"{result['yearly_kwh_per_kwp']} kWh/kWp/year"
    )

    return result


async def _handle_get_site_geo_hazard(args: Dict[str, Any]) -> List[types.TextContent]:
    """Handle get_site_geo_hazard tool call."""
    latitude = args.get("latitude")
    longitude = args.get("longitude")

    if latitude is None or longitude is None:
        return [
            types.TextContent(type="text", text="Error: Both latitude and longitude are required")
        ]

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return [
            types.TextContent(
                type="text", text="Error: latitude and longitude must be valid numbers"
            )
        ]

    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return [
            types.TextContent(
                type="text", text="Error: latitude must be -90..90 and longitude -180..180"
            )
        ]

    # Parse and validate power plant boundary GeoJSON if provided.
    # Cap at 512 KB to prevent oversized polygon allocations in rasterio_mask.
    boundary_geometry = None
    boundary_raw = args.get("power_plant_boundary_geojson")
    if boundary_raw:
        if len(boundary_raw) > 524_288:
            logger.warning(
                f"power_plant_boundary_geojson too large ({len(boundary_raw)} bytes) — ignoring"
            )
        else:
            try:
                geom = json.loads(boundary_raw)
                if geom.get("type") not in ("Polygon", "MultiPolygon"):
                    logger.warning(
                        f"power_plant_boundary_geojson has unsupported type '{geom.get('type')}' "
                        f"— boundary stats will be null"
                    )
                else:
                    boundary_geometry = geom
            except (json.JSONDecodeError, AttributeError) as e:
                logger.warning(
                    f"Invalid power_plant_boundary_geojson: {e} — boundary stats will be null"
                )

    # Run flood and terrain queries in parallel — different S3 buckets, no shared VSI handles.
    # Each query is internally resilient per-layer (see _query_flood/_query_terrain), so a
    # failure in one data source degrades gracefully instead of failing the whole lookup.
    # return_exceptions=True is a last-resort net for a genuinely unexpected bug in either
    # function — it should not normally trigger.
    flood_result: Any
    terrain_result: Any
    flood_result, terrain_result = await asyncio.gather(
        asyncio.to_thread(_query_flood, latitude, longitude),
        asyncio.to_thread(_query_terrain, latitude, longitude, boundary_geometry),
        return_exceptions=True,
    )

    data_warnings: List[str] = []

    if isinstance(flood_result, BaseException):
        logger.error(
            f"Flood query failed unexpectedly for ({latitude}, {longitude}): {flood_result}"
        )
        flood_data: Dict[str, Any] = {
            "flood_worst_case_depth_m": None,
            "flood_riverine_rp1000_m": None,
            "flood_coastal_rp1000_m": None,
            "flood_rp100_historical_m": None,
            "flood_rp100_rcp85_2050_median_m": None,
            "flood_rp100_rcp85_2050_max_m": None,
        }
        data_warnings.append("Flood hazard data unavailable due to an internal error.")
    else:
        flood_data = dict(flood_result)
        data_warnings.extend(flood_data.pop("flood_warnings", []))

    if isinstance(terrain_result, BaseException):
        logger.error(
            f"Terrain query failed unexpectedly for ({latitude}, {longitude}): {terrain_result}"
        )
        terrain_data: Dict[str, Any] = {
            "site_elevation_m": None,
            "boundary_min_elevation_m": None,
            "boundary_max_elevation_m": None,
            "boundary_elevation_range_m": None,
        }
        data_warnings.append("Terrain elevation data unavailable due to an internal error.")
    else:
        terrain_data = dict(terrain_result)
        data_warnings.extend(terrain_data.pop("terrain_warnings", []))

    result = {
        "latitude": latitude,
        "longitude": longitude,
        **flood_data,
        **terrain_data,
    }
    if data_warnings:
        result["data_warnings"] = data_warnings

    # Only a total loss (nothing at all could be determined) is a hard failure —
    # partial data (e.g. terrain succeeded but flood didn't) is returned as-is so
    # callers can proceed with what's available and see exactly what's missing and why.
    if all(
        v is None for k, v in result.items() if k not in ("latitude", "longitude", "data_warnings")
    ):
        logger.error(f"Geo hazard lookup returned no usable data for ({latitude}, {longitude})")
        return list(
            compose_error_response(
                Exception(
                    "Geo hazard lookup failed: "
                    + ("; ".join(data_warnings) if data_warnings else "no data sources available")
                )
            )
        )

    logger.info(
        f"Geo hazard for ({latitude}, {longitude}): "
        f"flood={result['flood_worst_case_depth_m']}m, "
        f"elevation={result['site_elevation_m']}m"
        + (f", warnings={data_warnings}" if data_warnings else "")
    )

    return list(compose_json_response(result))


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List available solar assessment tools."""
    tools = [
        types.Tool(
            name="get_solar_potential",
            description=(
                "[READ-ONLY] Get solar generation potential for a geographic location. "
                "Returns yearly and daily kWh/kWp values from Global Solar Atlas, "
                "along with irradiation data (GHI, DNI, GTI), optimal panel tilt angle, "
                "average temperature, and elevation. Useful for sizing solar installations "
                "and estimating energy production."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "latitude": {
                        "type": "number",
                        "description": "Latitude in decimal degrees (-90 to 90)",
                        "minimum": -90,
                        "maximum": 90,
                    },
                    "longitude": {
                        "type": "number",
                        "description": "Longitude in decimal degrees (-180 to 180)",
                        "minimum": -180,
                        "maximum": 180,
                    },
                },
                "required": ["latitude", "longitude"],
            },
            visible_to_customer=False,
        ),
        types.Tool(
            name="get_site_geo_hazard",
            description=(
                "[READ-ONLY] Get flood hazard depth and terrain elevation statistics for a power plant site. "
                "Returns: (1) worst-case flood depth in metres above ground (WRI Aqueduct RP1000, structural design flood); "
                "(2) historical RP100 flood depth (1% annual chance, ~18% probability over 20-year asset life); "
                "(3) RCP8.5 2050 RP100 median and max across 5 CMIP5 models (climate-adjusted 20-year planning value); "
                "(4) site elevation from Copernicus DEM GLO-30 30m; "
                "(5) terrain min/max/range within the power plant boundary polygon (null if no boundary provided). "
                "All flood values are depth above ground surface — no DEM subtraction needed. "
                "Used during power plant planning to assess flood risk and determine panel mount height."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "latitude": {
                        "type": "number",
                        "description": "Power plant site latitude in decimal degrees",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "Power plant site longitude in decimal degrees",
                    },
                    "power_plant_boundary_geojson": {
                        "type": "string",
                        "description": (
                            "Optional. GeoJSON polygon string of the power plant site boundary "
                            "(not the community boundary). If provided, terrain min/max/range are "
                            "computed within this polygon. If omitted, boundary terrain stats are "
                            "returned as null — no radius fallback."
                        ),
                    },
                },
                "required": ["latitude", "longitude"],
            },
            visible_to_customer=False,
        ),
    ]

    logger.info(f"Solar server: {len(tools)} tools available")
    return tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle tool calls."""
    try:
        if name == "get_solar_potential":
            return await _handle_get_solar_potential(arguments)
        elif name == "get_site_geo_hazard":
            return await _handle_get_site_geo_hazard(arguments)
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        logger.error(f"Error in tool {name}: {e}")
        return list(compose_error_response(e))


async def _handle_get_solar_potential(args: Dict[str, Any]) -> List[types.TextContent]:
    """
    Handle get_solar_potential tool call.

    Args:
        args: Tool arguments containing latitude and longitude

    Returns:
        Solar potential data as JSON
    """
    latitude = args.get("latitude")
    longitude = args.get("longitude")

    # Validate inputs
    if latitude is None or longitude is None:
        return [
            types.TextContent(
                type="text",
                text="Error: Both latitude and longitude are required",
            )
        ]

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return [
            types.TextContent(
                type="text",
                text="Error: latitude and longitude must be valid numbers",
            )
        ]

    # Validate ranges
    if not -90 <= latitude <= 90:
        return [
            types.TextContent(
                type="text",
                text="Error: latitude must be between -90 and 90",
            )
        ]

    if not -180 <= longitude <= 180:
        return [
            types.TextContent(
                type="text",
                text="Error: longitude must be between -180 and 180",
            )
        ]

    try:
        result = await fetch_solar_potential(latitude, longitude)
        return list(compose_json_response(result))
    except Exception as e:
        logger.error(f"Error fetching solar potential: {e}")
        return list(compose_error_response(Exception(f"Failed to fetch solar potential: {e}")))


@server.list_resources()
async def handle_list_resources() -> List[types.Resource]:
    """List available resources."""
    return [
        types.Resource(
            uri="solar://config",
            name="Solar Server Configuration",
            description="Current solar server configuration",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content."""
    if uri == "solar://config":
        config = {
            "api_url": GSA_API_BASE_URL,
            "actions_enabled": SOLAR_ACTIONS_ENABLED,
            "server_name": "solar-server",
            "server_version": "1.0.0",
            "data_source": "Global Solar Atlas (https://globalsolaratlas.info)",
        }
        return json.dumps(config, indent=2)
    else:
        raise ValueError(f"Unknown resource: {uri}")


async def main():
    """Main entry point."""
    try:
        logger.info("Starting Solar MCP Server...")
        print("✅ Solar server initialized successfully", file=sys.stderr)

        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="solar-server",
                    server_version="1.0.0",
                    capabilities=ServerCapabilities(),
                ),
            )
    except Exception as e:
        print(f"❌ Fatal error in solar server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        await _close_http_client()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Solar server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Solar server crashed: {e}", file=sys.stderr)
        sys.exit(1)
