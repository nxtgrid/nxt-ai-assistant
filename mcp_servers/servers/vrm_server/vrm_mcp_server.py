"""MCP Victron VRM Server - Handles Victron Remote Management API operations.

This server is type-checked with mypy.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    Resource,
    ServerCapabilities,
    TextContent,
    Tool,
)

# Load environment variables from .env file BEFORE importing shared_code
load_dotenv()

from shared_code.config.settings import server_settings

from shared.utils.logging import get_logger
from shared.utils.response_formatters import compose_error_response, compose_json_response

logger = get_logger("vrm-server")

# Startup message to stderr
print("🚀 Victron VRM MCP Server starting...", file=sys.stderr)
print(f"📍 Python path: {sys.path}", file=sys.stderr)
print(f"📂 Working directory: {os.getcwd()}", file=sys.stderr)

# Initialize MCP server
server = Server("vrm-server")

# VRM API configuration
VRM_API_BASE = "https://vrmapi.victronenergy.com/v2"
VRM_TOKEN = os.getenv("VRM_TOKEN")
VRM_USER_ID = os.getenv("VRM_USER_ID")
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# Grid to Site ID mapping (from environment or config)
GRID_TO_SITE_MAP: dict[str, int] = {}


class VRMClient:
    """Client for interacting with Victron VRM API."""

    def __init__(self, token: str, user_id: str = None):
        self.token = token
        self.user_id = user_id
        self.base_url = VRM_API_BASE
        self.headers = {"X-Authorization": f"Token {token}", "Content-Type": "application/json"}

    def _log_api_call(
        self,
        method: str,
        url: str,
        params: dict = None,
        response_status: int = None,
        response_data: Any = None,
    ):
        """Log API calls and responses to servers_debug.log if DEBUG is enabled."""
        if DEBUG:
            log_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "servers_debug.log",
            )
            timestamp = datetime.now().isoformat()

            with open(log_file, "a") as f:
                f.write(f"\n{'=' * 80}\n")
                f.write(f"[{timestamp}] VRM API Call: {method} {url}\n")
                if params:
                    f.write(f"Params: {json.dumps(params, indent=2)}\n")
                if response_status is not None:
                    f.write(f"Response Status: {response_status}\n")
                f.write(f"{'=' * 80}\n\n")

    async def get_current_user(self) -> Dict[str, Any]:
        """Get current user information from VRM API."""
        url = f"{self.base_url}/users/me"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                status = response.status
                if status == 200:
                    data = await response.json()
                    self._log_api_call("GET", url, response_status=status, response_data=data)
                    return dict(data.get("user", {}))
                else:
                    error_text = await response.text()
                    self._log_api_call(
                        "GET", url, response_status=status, response_data={"error": error_text}
                    )
                    raise Exception(f"VRM API error ({status}): {error_text}")

    async def initialize(self):
        """Initialize the client and auto-detect user ID if not provided."""
        if not self.user_id:
            try:
                user_info = await self.get_current_user()
                self.user_id = user_info.get("id") or user_info.get("idUser")
                logger.info(f"Auto-detected VRM user ID: {self.user_id}")
            except Exception as e:
                logger.error(f"Failed to auto-detect user ID: {e}")
                raise

    async def get_installations(self) -> List[Dict[str, Any]]:
        """Get list of all installations for the user."""
        url = f"{self.base_url}/users/{self.user_id}/installations"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                status = response.status
                if status == 200:
                    data = await response.json()
                    self._log_api_call("GET", url, response_status=status, response_data=data)
                    return list(data.get("records", []))
                else:
                    error_text = await response.text()
                    self._log_api_call(
                        "GET", url, response_status=status, response_data={"error": error_text}
                    )
                    raise Exception(f"VRM API error ({status}): {error_text}")

    async def find_site_by_name(self, grid_name: str) -> Optional[Dict[str, Any]]:
        """Find installation by grid name."""
        installations = await self.get_installations()

        for installation in installations:
            if installation.get("name", "").lower() == grid_name.lower():
                return installation

        return None

    async def get_site_id_by_grid(self, grid_name: str) -> Optional[int]:
        """Get site ID from grid name."""
        if grid_name in GRID_TO_SITE_MAP:
            return int(GRID_TO_SITE_MAP[grid_name])

        site = await self.find_site_by_name(grid_name)
        if site:
            site_id = site.get("idSite")
            GRID_TO_SITE_MAP[grid_name] = site_id
            return int(site_id) if site_id else None

        return None

    async def get_available_attributes(self, site_id: int) -> Dict[str, Any]:
        """Get available data attributes from diagnostics endpoint."""
        try:
            url = f"{self.base_url}/installations/{site_id}/diagnostics"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as response:
                    status = response.status
                    if status == 200:
                        data = await response.json()
                        self._log_api_call("GET", url, response_status=status, response_data=data)

                        # Extract attribute mappings
                        # records is an array in diagnostics endpoint
                        attributes = {}
                        records = data.get("records", [])

                        # Check if records has attributes field (could be dict or array)
                        if isinstance(records, dict) and "attributes" in records:
                            for attr in records["attributes"]:
                                code = attr.get("code")
                                attr_id = attr.get("idDataAttribute")
                                if code and attr_id:
                                    attributes[code] = {
                                        "id": attr_id,
                                        "name": attr.get("description"),
                                        "instance": attr.get("instance"),
                                    }
                        elif isinstance(records, list):
                            # If records is an array, look for attributes in each record
                            for record in records:
                                if isinstance(record, dict) and "attributes" in record:
                                    for attr in record["attributes"]:
                                        code = attr.get("code")
                                        attr_id = attr.get("idDataAttribute")
                                        if code and attr_id:
                                            attributes[code] = {
                                                "id": attr_id,
                                                "name": attr.get("description"),
                                                "instance": attr.get("instance"),
                                            }

                        return attributes
                    else:
                        error_text = await response.text()
                        self._log_api_call(
                            "GET", url, response_status=status, response_data={"error": error_text}
                        )
                        logger.debug(f"Could not fetch attributes for site {site_id}")
                        return {}
        except Exception as e:
            logger.debug(f"Error getting attributes: {e}")
            return {}

    async def get_widget_data_with_attributes(
        self,
        site_id: int,
        attribute_codes: List[str],
        attribute_ids: List[int] = None,
        instance: int = None,
        start_time: int = None,
        end_time: int = None,
    ) -> Dict[str, Any]:
        """Get widget data using attribute codes and IDs with time filter.

        Format: /installations/:idSite/widgets/Graph?start=X&end=Y&attributeIds[]=Z&attributeCodes[]=W&instance=I
        """
        url = f"{self.base_url}/installations/{site_id}/widgets/Graph"

        params = {}

        # Add time range (default to last hour if not specified)
        if not start_time:
            start_time = int((datetime.now() - timedelta(hours=1)).timestamp())
        if not end_time:
            end_time = int(datetime.now().timestamp())

        params["start"] = start_time
        params["end"] = end_time

        # Add attribute codes
        for code in attribute_codes:
            params["attributeCodes[]"] = code  # type: ignore[assignment]

        # Add attribute IDs if provided
        if attribute_ids:
            for attr_id in attribute_ids:
                params["attributeIds[]"] = attr_id

        # Add instance if specified
        if instance is not None:
            params["instance"] = instance

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=self.headers) as response:
                status = response.status
                if status == 200:
                    data = await response.json()
                    self._log_api_call(
                        "GET", url, params=params, response_status=status, response_data=data
                    )
                    return dict(data)
                else:
                    error_text = await response.text()
                    self._log_api_call(
                        "GET",
                        url,
                        params=params,
                        response_status=status,
                        response_data={"error": error_text},
                    )
                    raise Exception(f"VRM API error ({status}): {error_text}")

    async def get_widget_data(
        self, site_id: int, widget: str, instance: int = None
    ) -> Dict[str, Any]:
        """Get widget data from VRM API (legacy method for simple widgets)."""
        url = f"{self.base_url}/installations/{site_id}/widgets/{widget}"
        params = {}
        if instance is not None:
            params["instance"] = instance

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params if params else None, headers=self.headers
            ) as response:
                status = response.status
                if status == 200:
                    data = await response.json()
                    self._log_api_call(
                        "GET", url, params=params, response_status=status, response_data=data
                    )
                    return dict(data)
                else:
                    error_text = await response.text()
                    self._log_api_call(
                        "GET",
                        url,
                        params=params,
                        response_status=status,
                        response_data={"error": error_text},
                    )
                    raise Exception(f"VRM API error ({status}): {error_text}")

    async def get_system_overview(self, site_id: int) -> Dict[str, Any]:
        """Get comprehensive system overview using diagnostics and stats endpoints."""
        try:
            # Use the diagnostics endpoint for system overview
            url = f"{self.base_url}/installations/{site_id}/diagnostics"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as response:
                    status = response.status
                    if status == 200:
                        data = await response.json()
                        self._log_api_call("GET", url, response_status=status, response_data=data)
                        return data  # type: ignore[no-any-return]
                    else:
                        error_text = await response.text()
                        self._log_api_call(
                            "GET", url, response_status=status, response_data={"error": error_text}
                        )

                        # Fallback to stats endpoint if diagnostics not available
                        url = f"{self.base_url}/installations/{site_id}/stats"
                        async with session.get(url, headers=self.headers) as stats_response:
                            stats_status = stats_response.status
                            if stats_status == 200:
                                stats_data = await stats_response.json()
                                self._log_api_call(
                                    "GET",
                                    url,
                                    response_status=stats_status,
                                    response_data=stats_data,
                                )
                                return stats_data  # type: ignore[no-any-return]
                            else:
                                stats_error = await stats_response.text()
                                self._log_api_call(
                                    "GET",
                                    url,
                                    response_status=stats_status,
                                    response_data={"error": stats_error},
                                )
                                logger.debug(f"System overview not available for site {site_id}")
                                return {}
        except Exception as e:
            logger.debug(f"System overview not available for site {site_id}: {e}")
            return {}

    async def get_inverter_power(self, site_id: int, instance: int = 276) -> Dict[str, Any]:
        """Get inverter power data using Status widget endpoint.

        Looks for output power in field IDs 29, 30, 31 or attribute codes OP1, OP2, OP3.
        """
        try:
            # Use the Status widget which provides real-time inverter data
            status_data = await self.get_widget_data(site_id, "Status", instance)

            power_data = {
                "timestamp": datetime.now().isoformat(),
                "instance": instance,
                "l1_power": None,
                "l2_power": None,
                "l3_power": None,
                "total_power": 0,
            }

            # Parse the status widget response
            records = status_data.get("records", {})

            # Extract 'data' dictionary if records has 'data' and 'meta' keys
            if "data" in records and "meta" in records:
                data_dict = records.get("data", {})
            else:
                data_dict = records

            # Filter for inverter output power using field IDs (29, 30, 31) or attribute codes (OP1, OP2, OP3)
            for field_id, field_data in data_dict.items():
                # Field data can be: int, float, string, dict, or list
                field_value = None
                value = None
                code = ""

                if isinstance(field_data, list) and len(field_data) > 0:
                    # If it's a list, take the first element
                    field_value = field_data[0]
                elif isinstance(field_data, dict):
                    # If it's a dict, use it directly
                    field_value = field_data
                elif isinstance(field_data, (int, float, str)):
                    # If it's a primitive value, use it directly
                    value = field_data
                    field_value = field_data
                else:
                    continue

                # Try to extract formatted or raw value from dict
                if isinstance(field_value, dict):
                    value = field_value.get("rawValue") or field_value.get("formattedValue")
                    code = field_value.get("code", "")
                elif value is None and isinstance(field_value, (int, float, str)):
                    # Already assigned above
                    value = field_value

                # Match power fields by field ID (29, 30, 31) or attribute codes (OP1, OP2, OP3)
                if field_id == "29" or code == "OP1":
                    if value is not None:
                        try:
                            power_data["l1_power"] = (
                                float(value) if isinstance(value, (int, float, str)) else value
                            )
                        except (ValueError, TypeError):
                            power_data["l1_power"] = value

                elif field_id == "30" or code == "OP2":
                    if value is not None:
                        try:
                            power_data["l2_power"] = (
                                float(value) if isinstance(value, (int, float, str)) else value
                            )
                        except (ValueError, TypeError):
                            power_data["l2_power"] = value

                elif field_id == "31" or code == "OP3":
                    if value is not None:
                        try:
                            power_data["l3_power"] = (
                                float(value) if isinstance(value, (int, float, str)) else value
                            )
                        except (ValueError, TypeError):
                            power_data["l3_power"] = value

            # Calculate total power
            total: float = 0.0
            for power in [power_data["l1_power"], power_data["l2_power"], power_data["l3_power"]]:
                if power is not None:
                    try:
                        power_val = float(power) if power else 0.0  # type: ignore[arg-type]
                        total += power_val
                    except (ValueError, TypeError):
                        pass

            power_data["total_power"] = total

            return power_data
        except Exception as e:
            logger.error(f"Error getting inverter power: {e}")
            return {"error": str(e)}

    async def get_battery_status(self, site_id: int, instance: int = 512) -> Dict[str, Any]:
        """Get battery status data using BatterySummary widget endpoint."""
        try:
            # Use the BatterySummary widget which provides real-time battery data
            battery_data = await self.get_widget_data(site_id, "BatterySummary", instance)

            status = {
                "timestamp": datetime.now().isoformat(),
                "instance": instance,
                "voltage": None,
                "soc": None,
                "power": None,
                "current": None,
            }

            # Parse the battery summary widget response
            records = battery_data.get("records", {})

            # Extract 'data' dictionary if records has 'data' and 'meta' keys
            if "data" in records:
                data_dict = records.get("data", {})
            else:
                data_dict = records

            # Log data dictionary if DEBUG is enabled
            if DEBUG:
                log_file = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "servers_debug.log",
                )
                timestamp = datetime.now().isoformat()
                with open(log_file, "a") as f:
                    f.write(f"\n{'=' * 80}\n")
                    f.write(f"[{timestamp}] get_battery_status - records.data dictionary:\n")
                    f.write(f"{json.dumps(data_dict, indent=2, default=str)}\n")
                    f.write(f"{'=' * 80}\n\n")

            # The BatterySummary widget returns data attributes by their numeric IDs
            # Battery attribute codes: V (Voltage), I (Current), SOC (State of charge), P (Power)
            for field_id, field_data in data_dict.items():
                # Field data can be: int, float, string, dict, or list
                field_value = None
                value = None
                code = ""

                if isinstance(field_data, list) and len(field_data) > 0:
                    # If it's a list, take the first element
                    field_value = field_data[0]
                elif isinstance(field_data, dict):
                    # If it's a dict, use it directly
                    field_value = field_data
                elif isinstance(field_data, (int, float, str)):
                    # If it's a primitive value, use it directly
                    value = field_data
                    field_value = field_data
                else:
                    continue

                # Try to extract formatted or raw value from dict
                if isinstance(field_value, dict):
                    value = (
                        field_value.get("rawValue")
                        or field_value.get("formattedValue")
                        or field_value.get("value")
                    )
                    code = field_value.get("code", "")
                elif value is None and isinstance(field_value, (int, float, str)):
                    # Already assigned above
                    value = field_value

                # Match battery fields by attribute codes
                if code == "V":
                    if value is not None:
                        try:
                            status["voltage"] = (
                                float(value) if isinstance(value, (int, float, str)) else value
                            )
                        except (ValueError, TypeError):
                            status["voltage"] = value

                elif code == "SOC":
                    if value is not None:
                        try:
                            status["soc"] = (
                                float(value) if isinstance(value, (int, float, str)) else value
                            )
                        except (ValueError, TypeError):
                            status["soc"] = value

                elif code == "P":
                    if value is not None:
                        try:
                            status["power"] = (
                                float(value) if isinstance(value, (int, float, str)) else value
                            )
                        except (ValueError, TypeError):
                            status["power"] = value

                elif code == "I":
                    if value is not None:
                        try:
                            status["current"] = (
                                float(value) if isinstance(value, (int, float, str)) else value
                            )
                        except (ValueError, TypeError):
                            status["current"] = value

            return status
        except Exception as e:
            logger.error(f"Error getting battery status: {e}")
            return {"error": str(e)}

    async def get_active_alarms(self, site_id: int) -> Dict[str, Any]:
        """Get active alarms for the site from diagnostics endpoint."""
        try:
            # Get diagnostics data which contains alarm information
            diagnostics = await self.get_system_overview(site_id)

            from typing import List

            active_alarms: List[Dict[str, Any]] = []

            # Parse diagnostics records for alarm fields
            records = diagnostics.get("records", [])

            if isinstance(records, list):
                for record in records:
                    description = record.get("description", "").lower()

                    # Check if this is an alarm field
                    if "alarm" in description:
                        raw_value = record.get("rawValue")
                        formatted_value = record.get("formattedValue", "")
                        name_enum = record.get("nameEnum", "")

                        # Determine severity based on enum or formatted value
                        severity = None

                        # Check for enum-based alarm states (Warning or Alarm)
                        if name_enum and name_enum.lower() == "warning":
                            severity = "warning"
                        elif name_enum and name_enum.lower() == "alarm":
                            severity = "alarm"
                        # Check formatted value for severity
                        elif formatted_value.lower() == "warning":
                            severity = "warning"
                        elif formatted_value.lower() in ["alarm", "active"]:
                            severity = "alarm"
                        # Check for numeric enum values: 0=No alarm, 1=Warning, 2=Alarm
                        elif isinstance(raw_value, int):
                            if raw_value == 1:
                                severity = "warning"
                            elif raw_value == 2:
                                severity = "alarm"

                        # Only report if we have a valid severity (warning or alarm)
                        if severity:
                            active_alarms.append(
                                {
                                    "code": record.get("code"),
                                    "description": record.get("description"),
                                    "device": record.get("Device"),
                                    "instance": record.get("instance"),
                                    "severity": severity,
                                    "timestamp": record.get("timestamp"),
                                }
                            )

            alarms = {
                "timestamp": datetime.now().isoformat(),
                "active_alarms": active_alarms,
                "alarm_count": len(active_alarms),
            }

            return alarms
        except Exception as e:
            logger.error(f"Error getting active alarms: {e}")
            return {"error": str(e), "alarm_count": 0, "active_alarms": []}

    async def get_current_weather(self, latitude: float, longitude: float) -> Dict[str, Any]:
        """Get current weather from Open-Meteo API (free, no API key required)."""
        try:
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": latitude,
                "longitude": longitude,
                "current_weather": "true",
                "temperature_unit": "celsius",
                "windspeed_unit": "kmh",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:  # type: ignore[arg-type]
                    status = response.status
                    if status == 200:
                        data = await response.json()
                        self._log_api_call(
                            "GET", url, params=params, response_status=status, response_data=data
                        )
                        current = data.get("current_weather", {})

                        # WMO Weather interpretation codes
                        wmo_codes = {
                            0: "Clear sky",
                            1: "Mainly clear",
                            2: "Partly cloudy",
                            3: "Overcast",
                            45: "Foggy",
                            48: "Depositing rime fog",
                            51: "Light drizzle",
                            53: "Moderate drizzle",
                            55: "Dense drizzle",
                            61: "Slight rain",
                            63: "Moderate rain",
                            65: "Heavy rain",
                            71: "Slight snow",
                            73: "Moderate snow",
                            75: "Heavy snow",
                            77: "Snow grains",
                            80: "Slight rain showers",
                            81: "Moderate rain showers",
                            82: "Violent rain showers",
                            85: "Slight snow showers",
                            86: "Heavy snow showers",
                            95: "Thunderstorm",
                            96: "Thunderstorm with slight hail",
                            99: "Thunderstorm with heavy hail",
                        }

                        weather_code = current.get("weathercode", 0)
                        weather_description = wmo_codes.get(weather_code, "Unknown")

                        return {
                            "temperature": current.get("temperature"),
                            "temperature_unit": "°C",
                            "windspeed": current.get("windspeed"),
                            "windspeed_unit": "km/h",
                            "wind_direction": current.get("winddirection"),
                            "weather_code": weather_code,
                            "weather_description": weather_description,
                            "timestamp": current.get("time"),
                        }
                    else:
                        error_text = await response.text()
                        self._log_api_call(
                            "GET",
                            url,
                            params=params,
                            response_status=status,
                            response_data={"error": error_text},
                        )
                        return {"error": f"Weather API error: {status}"}
        except Exception as e:
            logger.error(f"Error fetching weather: {e}")
            return {"error": str(e)}

    async def get_site_info(self, site_id: int) -> Dict[str, Any]:
        """Get basic site information including online/offline status."""
        try:
            installations = await self.get_installations()

            for installation in installations:
                if installation.get("idSite") == site_id:
                    # Determine online status by checking if we can get real-time data
                    # The installations endpoint doesn't always include last_connection or online status
                    is_online = False
                    last_connection = None

                    # Try to get real-time data from the Status widget to determine online status
                    try:
                        # Try to fetch a simple widget - if it returns valid data, site is online
                        status_check = await self.get_widget_data(site_id, "Status", instance=276)
                        if status_check and "records" in status_check:
                            records = status_check.get("records", {})
                            # Check if we have data or a data dict with actual values
                            if records:
                                if isinstance(records, dict) and "data" in records:
                                    data = records.get("data", {})
                                    is_online = len(data) > 0
                                else:
                                    is_online = len(records) > 0
                    except Exception:
                        # If we can't get status, assume offline
                        is_online = False

                    # Get equipment counts and other details from diagnostics if online
                    num_battery_modules = 0
                    num_inverters = 0
                    num_mppts = 0
                    latitude = None
                    longitude = None

                    if is_online:
                        try:
                            diagnostics = await self.get_system_overview(site_id)

                            # Try to extract device counts and phase info from diagnostics
                            # The diagnostics endpoint returns records as an array with Device field
                            records = diagnostics.get("records", [])
                            seen_devices = set()  # Track unique device instances
                            phase_count = None
                            modules_online = 0
                            modules_offline = 0

                            if isinstance(records, list):
                                for record in records:
                                    device_type = record.get("Device", "")
                                    instance = record.get("instance")
                                    device_key = f"{device_type}:{instance}"
                                    code = record.get("code", "")

                                    # Extract phase count if available
                                    if code == "PC" and phase_count is None:
                                        phase_count = record.get("rawValue")

                                    # Extract battery module counts
                                    if code == "mon":  # Number of modules online
                                        modules_online += int(record.get("rawValue", 0))
                                    elif code == "mof":  # Number of modules offline
                                        modules_offline += int(record.get("rawValue", 0))

                                    # Only count each unique device instance once
                                    if device_key not in seen_devices:
                                        seen_devices.add(device_key)

                                        if device_type in [
                                            "Inverter",
                                            "VE.Bus System",
                                            "PV Inverter",
                                        ]:
                                            num_inverters += 1
                                        elif device_type in ["Solar Charger", "PV Charger", "MPPT"]:
                                            num_mppts += 1

                            # Total battery modules is sum of online and offline modules
                            num_battery_modules = modules_online + modules_offline

                            # Check for location in diagnostics
                            if "latitude" in diagnostics:
                                latitude = diagnostics.get("latitude")
                            if "longitude" in diagnostics:
                                longitude = diagnostics.get("longitude")
                        except Exception:
                            pass

                    site_info = {
                        "site_id": installation.get("idSite"),
                        "name": installation.get("name"),
                        "identifier": installation.get("identifier"),
                        "online_status": "online" if is_online else "offline",
                        "num_battery_modules": num_battery_modules,
                        "num_inverters": num_inverters,
                        "num_mppts": num_mppts,
                        "last_connection": last_connection,
                        "phase_count": (
                            phase_count
                            if phase_count is not None
                            else ("unknown (site offline)" if not is_online else "unknown")
                        ),
                        "location": {
                            "latitude": latitude,
                            "longitude": longitude,
                            "city": None,  # Not available in API response
                            "country": None,  # Not available in API response
                            "timezone": installation.get("timezone"),
                        },
                    }

                    # Get current weather if location is available
                    if latitude and longitude:
                        try:
                            weather = await self.get_current_weather(latitude, longitude)
                            site_info["current_weather"] = weather
                        except Exception as weather_error:
                            logger.warning(f"Could not fetch weather: {weather_error}")
                            site_info["current_weather"] = {"error": "Weather data unavailable"}
                    else:
                        site_info["current_weather"] = {"error": "Location not available"}

                    return site_info

            return {"error": "Site not found"}
        except Exception as e:
            logger.error(f"Error getting site info: {e}")
            return {"error": str(e)}

    async def get_equipment_details(self, site_id: int) -> Dict[str, Any]:
        """Get detailed equipment information including serial numbers from diagnostics."""
        try:
            # Get diagnostics data which contains device information
            diagnostics = await self.get_system_overview(site_id)

            from typing import List

            inverters: List[Dict[str, Any]] = []
            mppts: List[Dict[str, Any]] = []
            batteries: List[Dict[str, Any]] = []
            battery_modules: Dict[str, int] = {"online": 0, "offline": 0, "total": 0}

            # Diagnostics API returns records as an array
            records = diagnostics.get("records", [])
            seen_devices = {}  # Track devices by type and instance

            if isinstance(records, list):
                for record in records:
                    device_type = record.get("Device", "")
                    instance = record.get("instance")
                    code = record.get("code", "")
                    device_key = f"{device_type}:{instance}"

                    # Track battery module counts
                    if code == "mon":  # Number of modules online
                        battery_modules["online"] += int(record.get("rawValue", 0))
                    elif code == "mof":  # Number of modules offline
                        battery_modules["offline"] += int(record.get("rawValue", 0))

                    # Collect device details - only once per device instance
                    if device_key not in seen_devices:
                        seen_devices[device_key] = True

                        if device_type in ["Inverter", "VE.Bus System", "PV Inverter"]:
                            # Look for serial number in subsequent records for this instance
                            serial_number = None
                            custom_name = None
                            model = None

                            for r in records:
                                if r.get("Device") == device_type and r.get("instance") == instance:
                                    if r.get("code") == "iSN":  # Inverter serial number
                                        serial_number = r.get("rawValue")
                                    elif r.get("code") in ["Bcn", "icn"]:  # Custom name codes
                                        custom_name = r.get("rawValue")
                                    elif r.get("code") == "iP":  # Inverter model
                                        model = r.get("rawValue")

                            inverters.append(
                                {
                                    "device_type": device_type,
                                    "instance": instance,
                                    "serial_number": serial_number,
                                    "model": model,
                                    "custom_name": custom_name if custom_name else None,
                                }
                            )

                        elif device_type in ["Solar Charger", "PV Charger", "MPPT"]:
                            # Look for additional details
                            custom_name = None
                            model = None
                            serial_number = None
                            firmware_version = None

                            for r in records:
                                if r.get("Device") == device_type and r.get("instance") == instance:
                                    if r.get("code") == "Sccn":  # Solar charger custom name
                                        custom_name = r.get("rawValue")
                                    elif r.get("code") == "ScM":  # Solar charger model
                                        model = r.get("rawValue")
                                    elif r.get("code") == "ScSN":  # Solar charger serial number
                                        serial_number = r.get("rawValue")
                                    elif r.get("code") == "ScVt":  # Solar charger firmware version
                                        firmware_version = r.get("rawValue")

                            mppts.append(
                                {
                                    "device_type": device_type,
                                    "instance": instance,
                                    "custom_name": custom_name if custom_name else None,
                                    "model": model,
                                    "serial_number": serial_number,
                                    "firmware_version": firmware_version,
                                }
                            )

                        elif device_type in ["Battery", "Battery Monitor"]:
                            # Look for battery details
                            manufacturer = None
                            model = None
                            custom_name = None

                            for r in records:
                                if r.get("Device") == device_type and r.get("instance") == instance:
                                    if r.get("code") == "Bm":  # Battery manufacturer
                                        manufacturer = r.get("rawValue")
                                    elif r.get("code") == "Bf":  # Battery family/model
                                        model = r.get("rawValue")
                                    elif r.get("code") == "Bcn":  # Battery custom name
                                        custom_name = r.get("rawValue")

                            batteries.append(
                                {
                                    "device_type": device_type,
                                    "instance": instance,
                                    "manufacturer": manufacturer,
                                    "model": model,
                                    "custom_name": custom_name if custom_name else None,
                                }
                            )

            # Calculate totals
            battery_modules["total"] = battery_modules["online"] + battery_modules["offline"]

            equipment = {
                "timestamp": datetime.now().isoformat(),
                "inverters": inverters,
                "mppts": mppts,
                "batteries": batteries,
                "battery_modules": battery_modules,
                "num_inverters": len(inverters),
                "num_mppts": len(mppts),
                "num_battery_systems": len(
                    batteries
                ),  # Number of battery systems/monitors, not modules
            }

            return equipment
        except Exception as e:
            logger.error(f"Error getting equipment details: {e}")
            return {"error": str(e)}


# Initialize VRM client
vrm_client = None
if VRM_TOKEN:
    # User ID is optional - will be auto-detected if not provided
    vrm_client = VRMClient(VRM_TOKEN, VRM_USER_ID if VRM_USER_ID else None)
else:
    logger.warning("VRM_TOKEN not configured")


@server.list_tools()
async def handle_list_tools() -> List[Tool]:
    """List available VRM tools."""
    tools = [
        Tool(
            name="get_site_info",
            description="Get current site information including online/offline status, phase configuration (1-phase or 3-phase), location, and current weather",
            inputSchema={
                "type": "object",
                "properties": {"grid": {"type": "string", "description": "Grid name"}},
                "required": ["grid"],
            },
            visible_to_customer=False,
        ),
        Tool(
            name="get_inverter_power",
            description="Get current inverter power output by phase",
            inputSchema={
                "type": "object",
                "properties": {
                    "grid": {"type": "string", "description": "Grid name"},
                    "instance": {
                        "type": "integer",
                        "description": "Inverter instance ID (optional, default: 276)",
                    },
                },
                "required": ["grid"],
            },
            visible_to_customer=False,
        ),
        Tool(
            name="get_battery_status",
            description="Get current battery level, voltage, and charging status",
            inputSchema={
                "type": "object",
                "properties": {
                    "grid": {"type": "string", "description": "Grid name"},
                    "instance": {
                        "type": "integer",
                        "description": "Battery instance ID (optional, default: 512)",
                    },
                },
                "required": ["grid"],
            },
            visible_to_customer=False,
        ),
        Tool(
            name="get_active_alarms",
            description="Get current list of active alarms for the site",
            inputSchema={
                "type": "object",
                "properties": {"grid": {"type": "string", "description": "Grid name"}},
                "required": ["grid"],
            },
            visible_to_customer=False,
        ),
        Tool(
            name="get_equipment_details",
            description="Get detailed equipment information including serial numbers and names",
            inputSchema={
                "type": "object",
                "properties": {"grid": {"type": "string", "description": "Grid name"}},
                "required": ["grid"],
            },
            visible_to_customer=False,
        ),
    ]

    logger.info(f"VRM server: {len(tools)} tools available")
    return tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """Handle tool calls."""
    if not vrm_client:
        return [TextContent(type="text", text="VRM client not configured. Please set VRM_TOKEN.")]

    # Initialize client if user_id not set (auto-detect)
    if not vrm_client.user_id:
        try:
            await vrm_client.initialize()
        except Exception as e:
            return [TextContent(type="text", text=f"Failed to initialize VRM client: {str(e)}")]

    grid_name = arguments.get("grid")
    if not grid_name:
        return [TextContent(type="text", text="Grid name is required")]

    try:
        # Get site ID from grid name
        site_id = await vrm_client.get_site_id_by_grid(grid_name)
        if not site_id:
            return [TextContent(type="text", text=f"Site not found for grid: {grid_name}")]

        # Route to appropriate handler
        if name == "get_site_info":
            result = await vrm_client.get_site_info(site_id)
        elif name == "get_inverter_power":
            instance = arguments.get("instance", 276)
            result = await vrm_client.get_inverter_power(site_id, instance)
        elif name == "get_battery_status":
            instance = arguments.get("instance", 512)
            result = await vrm_client.get_battery_status(site_id, instance)
        elif name == "get_active_alarms":
            result = await vrm_client.get_active_alarms(site_id)
        elif name == "get_system_overview":
            result = await vrm_client.get_system_overview(site_id)
        elif name == "get_equipment_details":
            result = await vrm_client.get_equipment_details(site_id)
        elif name == "get_available_attributes":
            result = await vrm_client.get_available_attributes(site_id)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        return list(compose_json_response(result, default=str))
    except Exception as e:
        logger.error(f"Error in tool {name}: {e}")
        return list(compose_error_response(e))


@server.list_resources()
async def handle_list_resources() -> List[Resource]:
    """List available resources."""
    return [
        Resource(
            uri="vrm://config",
            name="VRM Configuration",
            description="Current VRM server configuration",
            mimeType="application/json",
        ),
        Resource(
            uri="vrm://connection",
            name="Connection Status",
            description="VRM API connection status",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content."""
    if uri == "vrm://config":
        config = {
            "vrm_api_base": VRM_API_BASE,
            "user_id": VRM_USER_ID,
            "server_name": server_settings.server_name,
            "server_version": server_settings.server_version,
        }
        return json.dumps(config, indent=2)
    elif uri == "vrm://connection":
        status = {
            "connected": vrm_client is not None,
            "configured": VRM_TOKEN is not None and VRM_USER_ID is not None,
        }
        return json.dumps(status, indent=2)
    else:
        raise ValueError(f"Unknown resource: {uri}")


async def main():
    """Main server function."""
    try:
        logger.info("Starting Victron VRM MCP Server...")
        print("✅ VRM server initialized successfully", file=sys.stderr)

        # Initialize server
        options = InitializationOptions(
            server_name="vrm-server", server_version="1.0.0", capabilities=ServerCapabilities()
        )

        async with stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(read_stream, write_stream, options)
    except Exception as e:
        print(f"❌ Fatal error in VRM server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 VRM server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ VRM server crashed: {e}", file=sys.stderr)
        sys.exit(1)
