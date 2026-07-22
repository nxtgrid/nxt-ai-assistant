"""Tool schemas for the Solar MCP server.

Extracted verbatim from ``handle_list_tools`` as part of migrating the server
onto ``shared_code.tool_registry.ToolRegistry`` — schema and handler are now
declared together at each ``@registry.tool(...)`` site instead of being kept
in sync by hand across a schema list and a separate dispatch chain.

Plain dicts rather than ``types.Tool`` objects: ``ToolRegistry.handle_list_tools``
constructs a fresh ``Tool`` per call, so sharing model instances across calls
would let one caller's mutation reach the next.

Both tools set ``visible_to_customer: False`` — solar assessment is staff-only.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "get_solar_potential",
        "description": (
            "[READ-ONLY] Get solar generation potential for a geographic location. "
            "Returns yearly and daily kWh/kWp values from Global Solar Atlas, "
            "along with irradiation data (GHI, DNI, GTI), optimal panel tilt angle, "
            "average temperature, and elevation. Useful for sizing solar installations "
            "and estimating energy production."
        ),
        "inputSchema": {
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
        "visible_to_customer": False,
    },
    {
        "name": "get_site_geo_hazard",
        "description": (
            "[READ-ONLY] Get flood hazard depth and terrain elevation statistics for a power plant site. "
            "Returns: (1) worst-case flood depth in metres above ground (WRI Aqueduct RP1000, structural design flood); "
            "(2) historical RP100 flood depth (1% annual chance, ~18% probability over 20-year asset life); "
            "(3) RCP8.5 2050 RP100 median and max across 5 CMIP5 models (climate-adjusted 20-year planning value); "
            "(4) site elevation from Copernicus DEM GLO-30 30m; "
            "(5) terrain min/max/range within the power plant boundary polygon (null if no boundary provided). "
            "All flood values are depth above ground surface — no DEM subtraction needed. "
            "Used during power plant planning to assess flood risk and determine panel mount height."
        ),
        "inputSchema": {
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
        "visible_to_customer": False,
    },
]
