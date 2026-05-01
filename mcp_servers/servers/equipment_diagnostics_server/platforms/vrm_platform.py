"""VRM (Victron Remote Management) platform adapter.

Implements the BasePlatform interface for Victron Energy's VRM API.
"""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
from dotenv import load_dotenv

from shared.auth.auth_service import get_auth_service
from shared.utils.geo import parse_location_geom

from .base_platform import (
    Alarm,
    BasePlatform,
    BatteryStatus,
    EquipmentStatus,
    GridStatus,
    HistoricalDataPoint,
    PowerReading,
)

load_dotenv()


@dataclass
class InverterVoltage:
    """Current inverter output voltage and power reading for ON/OFF and HPS determination."""

    timestamp: datetime
    l1_voltage_v: Optional[float] = None
    l2_voltage_v: Optional[float] = None
    l3_voltage_v: Optional[float] = None
    is_producing: bool = False  # True if any voltage > threshold (grid is ON)
    # AC power output per phase (watts)
    l1_power_w: Optional[float] = None
    l2_power_w: Optional[float] = None
    l3_power_w: Optional[float] = None
    total_power_kw: Optional[float] = None  # Sum of all phases in kW
    power_source: str = "inverter_output"  # "ac_consumption" or "inverter_output"
    data_timestamp: Optional[datetime] = None  # When VRM last received data from gateway
    error: Optional[str] = None


@dataclass
class DowntimeSummary:
    """Summary of downtime for a grid over a time period."""

    grid_name: str
    total_downtime_minutes: int
    outage_count: int
    longest_outage_minutes: int
    # Outage causes with total minutes per cause: battery, overload, vebus_error, grid_fault, unknown
    causes: Dict[str, int] = field(default_factory=dict)  # e.g., {"battery": 45, "unknown": 12}
    last_outage_time: Optional[datetime] = None
    last_outage_end: Optional[datetime] = None  # End time of the most recent outage
    last_outage_ongoing: bool = False  # True if the most recent outage is still in progress
    error: Optional[str] = None  # Set if fetch failed
    # Grid fault details: phases affected by grid faults (e.g., ["L1", "L2"])
    fault_details: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: Dict[str, Any] = {
            "total_downtime_minutes": self.total_downtime_minutes,
            "outage_count": self.outage_count,
            "longest_outage_minutes": self.longest_outage_minutes,
            "causes": self.causes,  # e.g., {"battery": 45, "unknown": 12} (minutes per cause)
        }
        if self.last_outage_time:
            result["last_outage_time"] = self.last_outage_time.isoformat()
        if self.last_outage_end:
            result["last_outage_end"] = self.last_outage_end.isoformat()
        if self.last_outage_ongoing:
            result["last_outage_ongoing"] = True
        if self.error:
            result["error"] = self.error
        if self.fault_details:
            result["fault_details"] = self.fault_details

        # Add icon: ⚡️ for stable, 🔻 for downtime
        if self.total_downtime_minutes == 0:
            result["icon"] = "⚡️"  # Stable - no downtime
        else:
            result["icon"] = "🔻"  # Has downtime

        return result


@dataclass
class WeatherData:
    """Current weather for a site from Open-Meteo API."""

    temperature_c: Optional[float]
    weather_code: int  # WMO standard code
    weather_description: str  # "Clear", "Cloudy", "Rain", etc.
    wind_speed_kmh: Optional[float]
    icon: str  # Weather icon emoji
    high_temp_warning: bool = False  # True if >33°C
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: Dict[str, Any] = {
            "icon": self.icon,
            "description": self.weather_description,
            "temperature_c": self.temperature_c,
            "wind_speed_kmh": self.wind_speed_kmh,
            "high_temp_warning": self.high_temp_warning,
        }
        if self.error:
            result["error"] = self.error
        # Add combined display: icon + temp warning if hot
        if self.high_temp_warning and self.temperature_c:
            result["display"] = f"🌡️{self.icon} {self.temperature_c:.0f}°C"
        elif self.temperature_c:
            result["display"] = f"{self.icon} {self.temperature_c:.0f}°C"
        else:
            result["display"] = self.icon
        return result


# WMO Weather interpretation codes
# See: https://open-meteo.com/en/docs (WMO Weather interpretation codes)
WMO_WEATHER_CODES: Dict[int, tuple] = {
    0: ("Clear", "☀️"),
    1: ("Mainly Clear", "🌤️"),
    2: ("Partly Cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Foggy", "🌫️"),
    48: ("Fog", "🌫️"),
    51: ("Light Drizzle", "🌧️"),
    53: ("Drizzle", "🌧️"),
    55: ("Heavy Drizzle", "🌧️"),
    61: ("Light Rain", "🌧️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy Rain", "🌧️"),
    66: ("Freezing Rain", "🌧️"),
    67: ("Heavy Freezing Rain", "🌧️"),
    71: ("Light Snow", "❄️"),
    73: ("Snow", "❄️"),
    75: ("Heavy Snow", "❄️"),
    77: ("Snow Grains", "❄️"),
    80: ("Light Showers", "🌧️"),
    81: ("Showers", "🌧️"),
    82: ("Heavy Showers", "🌧️"),
    85: ("Light Snow Showers", "❄️"),
    86: ("Heavy Snow Showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm w/ Hail", "⛈️"),
    99: ("Severe Thunderstorm", "⛈️"),
}

# Open-Meteo API base URL (free, no auth required)
OPEN_METEO_API_BASE = "https://api.open-meteo.com/v1/forecast"

# High temperature warning threshold (Celsius)
HIGH_TEMP_THRESHOLD_C = 33.0


# VRM API configuration
VRM_API_BASE = "https://vrmapi.victronenergy.com/v2"

# VRM attribute codes for different metrics
VRM_ATTRIBUTE_CODES = {
    # Grid power per phase (input)
    "grid_l1": "a1",
    "grid_l2": "a2",
    "grid_l3": "a3",
    # Grid power per phase (output)
    "grid_out_l1": "o1",
    "grid_out_l2": "o2",
    "grid_out_l3": "o3",
    # Inverter output power (W)
    "inverter_l1": "OP1",
    "inverter_l2": "OP2",
    "inverter_l3": "OP3",
    # Inverter output voltage (V) - for smarter outage detection
    "voltage_out_l1": "OV1",
    "voltage_out_l2": "OV2",
    "voltage_out_l3": "OV3",
    # Inverter input voltage (V) - for grid input monitoring
    "voltage_in_l1": "IV1",
    "voltage_in_l2": "IV2",
    "voltage_in_l3": "IV3",
    # Battery
    "battery_soc": "bs",
    "battery_voltage": "bv",
    "battery_current": "bc",
    "battery_power": "P",
    "battery_state": "bst",
    # PV/Solar
    "pv_power_dc": "Pdc",
    "pv_power_mppt": "PVP",
    "pv_power_inverter": "pP1",
    # System
    "phase_count": "PC",
    "total_consumption": "total_consumption",
}

# Inverter instance defaults
DEFAULT_INVERTER_INSTANCE = 276
DEFAULT_BATTERY_INSTANCE = 512


class VRMPlatform(BasePlatform):
    """VRM platform implementation."""

    def __init__(self, token: Optional[str] = None, user_id: Optional[str] = None):
        """Initialize VRM platform.

        Args:
            token: VRM API token. If not provided, reads from VRM_TOKEN env var.
            user_id: VRM user ID. If not provided, will be auto-detected.
        """
        self.token = token or os.getenv("VRM_TOKEN")
        self.user_id = user_id or os.getenv("VRM_USER_ID")
        self.base_url = VRM_API_BASE
        self._headers: Dict[str, str] = {}
        self._site_cache: Dict[str, str] = {}  # grid_name -> site_id
        self._initialized = False

    @property
    def platform_name(self) -> str:
        return "VRM"

    async def initialize(self) -> None:
        """Initialize VRM connection and auto-detect user ID if needed."""
        if not self.token:
            raise ValueError("VRM_TOKEN not configured")

        self._headers = {
            "X-Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        }

        # Auto-detect user ID if not provided
        if not self.user_id:
            user_info = await self._api_get("/users/me")
            self.user_id = user_info.get("user", {}).get("id") or user_info.get("user", {}).get(
                "idUser"
            )

        self._initialized = True

    async def _api_get(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Make GET request to VRM API."""
        if not self._headers:
            await self.initialize()

        url = f"{self.base_url}{endpoint}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers, params=params) as response:
                if response.status == 200:
                    result: Dict[str, Any] = await response.json()
                    return result
                else:
                    error_text = await response.text()
                    raise Exception(f"VRM API error ({response.status}): {error_text}")

    async def get_site_id_for_grid(self, grid_name: str) -> tuple[Optional[str], bool]:
        """Resolve grid name to VRM site ID using auth service with fuzzy matching.

        Returns:
            Tuple of (site_id, is_generation_managed):
            - site_id: VRM site ID or None if not found
            - is_generation_managed: True if grid's generation is managed by the operator
        """
        # Use AuthService for fuzzy matching and grid-to-VRM ID lookup
        auth_service = get_auth_service()
        try:
            site_id, gateway_id, actual_name, is_managed = await auth_service.get_grid_vrm_ids(
                grid_name
            )
            if site_id:
                self._site_cache[grid_name] = str(site_id)
                return (str(site_id), is_managed)
            # Grid found but no VRM site_id - still return managed status
            if actual_name:
                return (None, is_managed)
        except Exception:
            pass

        # Fallback: try direct VRM API lookup (assume managed if found via VRM)
        try:
            installations = await self._get_installations()
            for installation in installations:
                if installation.get("name", "").lower() == grid_name.lower():
                    site_id = str(installation.get("idSite"))
                    self._site_cache[grid_name] = site_id
                    # Found directly in VRM - assume managed (can't check DB flag here)
                    return (site_id, True)
        except Exception:
            pass

        return (None, False)

    async def _get_installations(self) -> List[Dict[str, Any]]:
        """Get list of VRM installations."""
        if not self._initialized:
            await self.initialize()

        data = await self._api_get(f"/users/{self.user_id}/installations")
        records: List[Dict[str, Any]] = data.get("records", [])
        return records

    async def is_site_online(self, site_id: str) -> bool:
        """Check if VRM site is online by checking gateway connection."""
        try:
            # Use system-overview endpoint to check gateway lastConnection
            url = f"/installations/{site_id}/system-overview"
            data = await self._api_get(url)

            devices = data.get("records", {}).get("devices", [])
            for device in devices:
                if device.get("name") == "Gateway":
                    last_conn = device.get("lastConnection")
                    if last_conn:
                        # Check if within 15 minutes
                        last_conn_dt = datetime.fromtimestamp(last_conn)
                        return (datetime.now() - last_conn_dt).total_seconds() < 900
            return False
        except Exception:
            return False

    async def _get_widget_data(
        self, site_id: str, widget: str, instance: Optional[int] = None
    ) -> Dict[str, Any]:
        """Get widget data from VRM."""
        endpoint = f"/installations/{site_id}/widgets/{widget}"
        params = {"instance": instance} if instance is not None else None
        return await self._api_get(endpoint, params)

    async def _get_diagnostics(self, site_id: str) -> Dict[str, Any]:
        """Get diagnostics data from VRM."""
        return await self._api_get(f"/installations/{site_id}/diagnostics")

    def _extract_widget_value(
        self, records: Dict[str, Any], field_id: str = None, code: str = None
    ) -> Optional[float]:
        """Extract a value from widget records by field ID or attribute code."""
        data_dict = records.get("data", records) if "data" in records else records

        for fid, field_data in data_dict.items():
            value = None
            field_code = ""

            if isinstance(field_data, list) and len(field_data) > 0:
                field_data = field_data[0]

            if isinstance(field_data, dict):
                value = field_data.get("rawValue") or field_data.get("formattedValue")
                field_code = field_data.get("code", "")
            elif isinstance(field_data, (int, float)):
                value = field_data

            if field_id and fid == field_id:
                try:
                    return float(value) if value is not None else None
                except (ValueError, TypeError):
                    return None

            if code and field_code == code:
                try:
                    return float(value) if value is not None else None
                except (ValueError, TypeError):
                    return None

        return None

    def _extract_output_consumption_from_diagnostics(
        self, records: List[Dict[str, Any]]
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Extract system-level AC Output Consumption (o1/o2/o3) from diagnostics records.

        These represent total load-side consumption per phase in watts, including
        AC-coupled PV inverters. Always positive, unlike OP1/OP2/OP3 which go
        negative on AC-coupled PV sites.

        Args:
            records: List of diagnostics records from VRM API

        Returns:
            Tuple of (l1_w, l2_w, l3_w) or (None, None, None) if not found
        """
        l1 = l2 = l3 = None

        if not isinstance(records, list):
            return (None, None, None)

        for record in records:
            code = record.get("code", "")
            dbus_service_type = record.get("dbusServiceType", "")

            # Only match system-level consumption (not per-device)
            if dbus_service_type != "system":
                continue

            raw_value = record.get("rawValue")
            if raw_value is None:
                continue

            try:
                value = float(raw_value)
            except (ValueError, TypeError):
                continue

            if code == "o1":
                l1 = value
            elif code == "o2":
                l2 = value
            elif code == "o3":
                l3 = value

        return (l1, l2, l3)

    def _extract_diagnostics_timestamp(self, records: list) -> Optional[datetime]:
        """Extract the most recent timestamp from VRM diagnostics records."""
        latest = None
        if not isinstance(records, list):
            return None
        for record in records:
            ts = record.get("timestamp")
            if ts:
                try:
                    record_dt = datetime.fromtimestamp(int(ts))
                    if latest is None or record_dt > latest:
                        latest = record_dt
                except (ValueError, TypeError, OSError):
                    pass
        return latest

    async def _get_output_consumption(
        self, site_id: str
    ) -> Optional[tuple[Optional[float], Optional[float], Optional[float], Optional[datetime]]]:
        """Fetch AC Output Consumption (o1/o2/o3) for a site from VRM diagnostics.

        Returns:
            Tuple of (l1_w, l2_w, l3_w, data_timestamp) or None if diagnostics fetch fails.
            data_timestamp is the most recent timestamp from the diagnostics records.
        """
        try:
            diagnostics = await self._get_diagnostics(site_id)
            records = diagnostics.get("records", [])
            consumption = self._extract_output_consumption_from_diagnostics(records)
            data_ts = self._extract_diagnostics_timestamp(records)
            # Return None if all values are None (no consumption data available)
            if all(c is None for c in consumption):
                return None
            return (*consumption, data_ts)
        except Exception:
            return None

    async def get_current_inverter_power(self, site_id: str) -> PowerReading:
        """Get current grid consumption power.

        Prefers AC Output Consumption (o1/o2/o3) from diagnostics — total load-side
        consumption including AC-coupled PV. Falls back to inverter output
        (OP1/OP2/OP3) from Status widget if o1-o3 unavailable.
        """
        try:
            # Try output consumption first (preferred — includes AC-coupled PV)
            consumption = await self._get_output_consumption(site_id)
            if consumption:
                l1, l2, l3, _ts = consumption
                total = sum(p for p in [l1, l2, l3] if p is not None)
                return PowerReading(
                    timestamp=datetime.utcnow(),
                    l1_power_w=l1,
                    l2_power_w=l2,
                    l3_power_w=l3,
                    total_power_w=total,
                )

            # Fall back to inverter output power (OP1/OP2/OP3)
            status_data = await self._get_widget_data(site_id, "Status", DEFAULT_INVERTER_INSTANCE)
            records = status_data.get("records", {})

            l1 = self._extract_widget_value(records, "29") or self._extract_widget_value(
                records, code="OP1"
            )
            l2 = self._extract_widget_value(records, "30") or self._extract_widget_value(
                records, code="OP2"
            )
            l3 = self._extract_widget_value(records, "31") or self._extract_widget_value(
                records, code="OP3"
            )

            total = sum(p for p in [l1, l2, l3] if p is not None)

            return PowerReading(
                timestamp=datetime.utcnow(),
                l1_power_w=l1,
                l2_power_w=l2,
                l3_power_w=l3,
                total_power_w=total,
            )
        except Exception:
            return PowerReading(timestamp=datetime.utcnow())

    def _extract_widget_seconds_ago(self, records: Dict[str, Any], code: str) -> Optional[int]:
        """Extract the secondsAgo field from a Status widget record by attribute code.

        VRM Status widget records include a 'secondsAgo' field indicating how long
        since the gateway last reported this value. This is the reliable way to
        detect stale data (e.g. gateway offline for hours but values still cached).
        """
        data_dict = records.get("data", records) if "data" in records else records

        for fid, field_data in data_dict.items():
            if isinstance(field_data, list) and len(field_data) > 0:
                field_data = field_data[0]
            if isinstance(field_data, dict) and field_data.get("code") == code:
                val = field_data.get("secondsAgo")
                if val is not None:
                    try:
                        return int(val)
                    except (ValueError, TypeError):
                        return None
        return None

    async def get_current_inverter_voltage(self, site_id: str) -> InverterVoltage:
        """Get current inverter output voltage and power to determine grid ON/OFF and HPS status.

        A grid is considered ON if any phase has voltage > 100V.

        For power values, prefers AC Output Consumption (o1/o2/o3) from diagnostics —
        total load-side consumption including AC-coupled PV. Falls back to inverter
        output (OP1/OP2/OP3) from Status widget if o1-o3 unavailable.

        Uses OV1 secondsAgo from Status widget for staleness detection — this is the
        reliable indicator of when the gateway last reported (diagnostics timestamps
        are refreshed by VRM even when the gateway is offline).
        """
        VOLTAGE_ON_THRESHOLD = 100.0  # Volts - inverter is ON if above this

        try:
            status_data = await self._get_widget_data(site_id, "Status", DEFAULT_INVERTER_INSTANCE)
            records = status_data.get("records", {})

            # Extract per-phase voltage (codes OV1, OV2, OV3) — always from Status widget
            v1 = self._extract_widget_value(records, code="OV1")
            v2 = self._extract_widget_value(records, code="OV2")
            v3 = self._extract_widget_value(records, code="OV3")

            # Extract secondsAgo from OV1 for staleness detection
            ov1_seconds_ago = self._extract_widget_seconds_ago(records, "OV1")
            if ov1_seconds_ago is not None:
                data_ts = datetime.utcnow() - timedelta(seconds=ov1_seconds_ago)
            else:
                data_ts = None

            # Grid is ON if any voltage is above threshold
            voltages = [v for v in [v1, v2, v3] if v is not None]
            is_producing = any(v > VOLTAGE_ON_THRESHOLD for v in voltages) if voltages else False

            # Try output consumption from diagnostics (preferred — includes AC-coupled PV)
            power_source = "inverter_output"
            consumption = await self._get_output_consumption(site_id)
            if consumption:
                p1, p2, p3, _diag_ts = consumption
                power_source = "output_consumption"
            else:
                # Fall back to inverter output power (OP1/OP2/OP3) from Status widget
                p1 = self._extract_widget_value(records, "29") or self._extract_widget_value(
                    records, code="OP1"
                )
                p2 = self._extract_widget_value(records, "30") or self._extract_widget_value(
                    records, code="OP2"
                )
                p3 = self._extract_widget_value(records, "31") or self._extract_widget_value(
                    records, code="OP3"
                )

            # Calculate total power in kW (sum of all phases that have data)
            powers = [p for p in [p1, p2, p3] if p is not None]
            total_power_kw = sum(powers) / 1000.0 if powers else None

            return InverterVoltage(
                timestamp=datetime.utcnow(),
                l1_voltage_v=v1,
                l2_voltage_v=v2,
                l3_voltage_v=v3,
                is_producing=is_producing,
                l1_power_w=p1,
                l2_power_w=p2,
                l3_power_w=p3,
                total_power_kw=total_power_kw,
                power_source=power_source,
                data_timestamp=data_ts,
            )
        except Exception as e:
            return InverterVoltage(
                timestamp=datetime.utcnow(),
                error=str(e)[:100],
            )

    async def get_batch_inverter_voltage(
        self,
        site_ids: List[str],
        max_concurrent: int = 10,
        timeout_per_site: float = 3.0,
    ) -> Dict[str, InverterVoltage]:
        """Fetch current inverter voltage for multiple sites in parallel.

        Args:
            site_ids: List of VRM site IDs
            max_concurrent: Max parallel API calls
            timeout_per_site: Timeout per site in seconds

        Returns:
            Dict mapping site_id -> InverterVoltage
        """
        if not site_ids:
            return {}

        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_one(site_id: str) -> tuple:
            async with semaphore:
                try:
                    voltage = await asyncio.wait_for(
                        self.get_current_inverter_voltage(site_id),
                        timeout=timeout_per_site,
                    )
                    return site_id, voltage
                except asyncio.TimeoutError:
                    return site_id, InverterVoltage(
                        timestamp=datetime.utcnow(),
                        error="Timeout",
                    )

        results = await asyncio.gather(
            *[fetch_one(sid) for sid in site_ids],
            return_exceptions=True,
        )

        output: Dict[str, InverterVoltage] = {}
        for i, result in enumerate(results):
            site_id = site_ids[i]
            if isinstance(result, Exception):
                output[site_id] = InverterVoltage(
                    timestamp=datetime.utcnow(),
                    error=f"Fetch failed: {str(result)[:50]}",
                )
            elif isinstance(result, tuple):
                output[result[0]] = result[1]

        return output

    async def get_current_battery_status(self, site_id: str) -> BatteryStatus:
        """Get current battery status."""
        try:
            battery_data = await self._get_widget_data(
                site_id, "BatterySummary", DEFAULT_BATTERY_INSTANCE
            )
            records = battery_data.get("records", {})

            voltage = self._extract_widget_value(records, code="V")
            soc = self._extract_widget_value(records, code="SOC")
            power = self._extract_widget_value(records, code="P")
            current = self._extract_widget_value(records, code="I")

            return BatteryStatus(
                timestamp=datetime.utcnow(),
                soc_percent=soc,
                voltage_v=voltage,
                current_a=current,
                power_w=power,
                charging=power is not None and power < 0,  # Negative power = charging
            )
        except Exception:
            return BatteryStatus(timestamp=datetime.utcnow())

    async def get_current_grid_status(self, site_id: str) -> GridStatus:
        """Get current grid connection status."""
        try:
            diagnostics = await self._get_diagnostics(site_id)
            records = diagnostics.get("records", [])

            l1 = l2 = l3 = None
            connected = False

            if isinstance(records, list):
                for record in records:
                    code = record.get("code", "")
                    raw_value = record.get("rawValue")

                    if code == "a1":
                        l1 = float(raw_value) if raw_value is not None else None
                    elif code == "a2":
                        l2 = float(raw_value) if raw_value is not None else None
                    elif code == "a3":
                        l3 = float(raw_value) if raw_value is not None else None

            # Consider connected if any phase has power
            total = sum(p for p in [l1, l2, l3] if p is not None)
            connected = total > 50  # Threshold for "connected"

            return GridStatus(
                timestamp=datetime.utcnow(),
                connected=connected,
                l1_power_w=l1,
                l2_power_w=l2,
                l3_power_w=l3,
                total_power_w=total if total else None,
            )
        except Exception:
            return GridStatus(timestamp=datetime.utcnow(), connected=False)

    async def get_current_pv_power(self, site_id: str) -> PowerReading:
        """Get current PV/solar power output (total DC power = to battery + to grid)."""
        try:
            diagnostics = await self._get_diagnostics(site_id)
            records = diagnostics.get("records", [])

            pv_power = None

            if isinstance(records, list):
                for record in records:
                    if record.get("code") == "Pdc":
                        raw_value = record.get("rawValue")
                        if raw_value is not None:
                            try:
                                pv_power = float(raw_value)
                            except (ValueError, TypeError):
                                pass
                        break

            return PowerReading(
                timestamp=datetime.utcnow(),
                total_power_w=pv_power,
            )
        except Exception:
            return PowerReading(timestamp=datetime.utcnow())

    async def get_active_alarms(self, site_id: str) -> List[Alarm]:
        """Get list of currently active alarms."""
        try:
            diagnostics = await self._get_diagnostics(site_id)
            records = diagnostics.get("records", [])
            alarms = []

            if isinstance(records, list):
                for record in records:
                    description = record.get("description", "").lower()

                    if "alarm" in description:
                        raw_value = record.get("rawValue")
                        formatted_value = record.get("formattedValue", "")
                        name_enum = record.get("nameEnum", "")

                        severity = None

                        if name_enum and name_enum.lower() == "warning":
                            severity = "warning"
                        elif name_enum and name_enum.lower() == "alarm":
                            severity = "alarm"
                        elif formatted_value.lower() == "warning":
                            severity = "warning"
                        elif formatted_value.lower() in ["alarm", "active"]:
                            severity = "alarm"
                        elif isinstance(raw_value, int):
                            if raw_value == 1:
                                severity = "warning"
                            elif raw_value == 2:
                                severity = "alarm"

                        if severity:
                            alarms.append(
                                Alarm(
                                    code=record.get("code", ""),
                                    description=record.get("description", ""),
                                    device=record.get("Device"),
                                    instance=record.get("instance"),
                                    severity=severity,
                                    timestamp=(
                                        datetime.fromtimestamp(record.get("timestamp", 0))
                                        if record.get("timestamp")
                                        else None
                                    ),
                                )
                            )

            return alarms
        except Exception:
            return []

    async def get_equipment_status(
        self, site_id: str, metrics: Optional[List[str]] = None
    ) -> EquipmentStatus:
        """Get combined equipment status."""
        if metrics is None:
            metrics = ["inverter", "battery", "grid", "pv", "alarms"]

        status = EquipmentStatus(
            grid_name="",  # Will be filled by caller
            site_id=site_id,
            timestamp=datetime.utcnow(),
            is_online=await self.is_site_online(site_id),
        )

        if "inverter" in metrics:
            status.inverter = await self.get_current_inverter_power(site_id)

        if "battery" in metrics:
            status.battery = await self.get_current_battery_status(site_id)

        if "grid" in metrics:
            status.grid = await self.get_current_grid_status(site_id)

        if "pv" in metrics:
            status.pv = await self.get_current_pv_power(site_id)

        if "alarms" in metrics:
            status.alarms = await self.get_active_alarms(site_id)

        return status

    async def get_historical_power(
        self,
        site_id: str,
        start_time: datetime,
        end_time: datetime,
        metrics: List[str],
        aggregation: Optional[str] = None,
    ) -> List[HistoricalDataPoint]:
        """Get historical power data for charting.

        Args:
            site_id: VRM site ID
            start_time: Start of time range
            end_time: End of time range
            metrics: List of metric names to fetch
            aggregation: VRM aggregation mode - None (default/mean), "max", or "min".
                         Use "max" to capture momentary peaks (e.g., for peak load calculation).
        """
        # Map metrics to VRM attribute codes
        # - o1/o2/o3: AC Consumption on Output per phase — total load-side consumption
        #   including AC-coupled PV inverters. Preferred for grid consumption.
        # - OP1/OP2/OP3: Inverter Output Power only (can go negative on PV sites)
        # - a1/a2/a3: AC Consumption on Input per phase (generator/grid input side)
        attribute_codes = []
        metric_to_code = {
            "grid_consumption": ["o1", "o2", "o3"],
            "inverter_voltage": ["OV1", "OV2", "OV3"],  # Output voltage per phase
            "grid_power": ["a1", "a2", "a3"],  # Kept for backwards compat, same data
            "battery_soc": ["bs"],
            "battery_state": ["bst"],
            "battery_power": ["P"],
            "pv_power": ["Pdc"],
        }

        for metric in metrics:
            if metric in metric_to_code:
                attribute_codes.extend(metric_to_code[metric])

        if not attribute_codes:
            return []

        # Build Graph widget request
        params: Dict[str, Any] = {
            "start": int(start_time.timestamp()),
            "end": int(end_time.timestamp()),
        }
        if aggregation:
            params["type"] = aggregation
        # Add attribute codes as array params
        for i, code in enumerate(attribute_codes):
            params[f"attributeCodes[{i}]"] = code

        try:
            endpoint = f"/installations/{site_id}/widgets/Graph"
            data = await self._api_get(endpoint, params)

            data_points: List[HistoricalDataPoint] = []
            records = data.get("records", {})

            # VRM Graph widget response structure:
            # - records.data: dict with field IDs as keys, values are arrays of [timestamp, value]
            # - records.meta: dict mapping field IDs to {code, description}
            inner_data = records.get("data", {})
            meta = records.get("meta", {})

            if not isinstance(inner_data, dict):
                return data_points

            for field_id, values in inner_data.items():
                if not isinstance(values, list) or not values:
                    continue

                # Get attribute code from meta
                field_info = meta.get(field_id, {})
                attr_code = field_info.get("code", "")

                # Determine metric and phase from attribute code
                metric_name = "power"
                phase = None

                # Map VRM codes to phases and metrics
                # o1/o2/o3 = AC Consumption on Output L1/L2/L3 (total grid consumption)
                # a1/a2/a3 = AC Consumption on Input L1/L2/L3
                # OV1/OV2/OV3 = Inverter Output Voltage L1/L2/L3
                code_to_phase = {
                    "o1": "L1",
                    "o2": "L2",
                    "o3": "L3",
                    "a1": "L1",
                    "a2": "L2",
                    "a3": "L3",
                    "OV1": "L1",
                    "OV2": "L2",
                    "OV3": "L3",
                }
                phase = code_to_phase.get(attr_code)

                if attr_code in ("o1", "o2", "o3"):
                    metric_name = "grid_consumption"
                elif attr_code in ("a1", "a2", "a3"):
                    metric_name = "grid_power"
                elif attr_code.startswith("OV"):
                    metric_name = "inverter_voltage"
                elif attr_code == "bs":
                    metric_name = "battery_soc"
                elif attr_code == "bst":
                    metric_name = "battery_state"
                elif attr_code == "P":
                    metric_name = "battery_power"
                elif attr_code == "Pdc":
                    metric_name = "pv_power"

                # Parse values - format is [[timestamp, value], ...]
                for item in values:
                    ts = None
                    val = None

                    if isinstance(item, dict):
                        ts = item.get("timestamp") or item.get("t")
                        val = item.get("value") or item.get("v")
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        ts, val = item[0], item[1]

                    if ts is not None and val is not None:
                        try:
                            data_points.append(
                                HistoricalDataPoint(
                                    timestamp=datetime.fromtimestamp(ts),
                                    value=float(val),
                                    metric=metric_name,
                                    phase=phase,
                                )
                            )
                        except (ValueError, TypeError):
                            pass

            return data_points
        except Exception:
            return []

    async def _get_system_overview(self, site_id: str) -> Dict[str, Any]:
        """Get system-overview data which includes productName for all devices."""
        try:
            return await self._api_get(f"/installations/{site_id}/system-overview")
        except Exception:
            return {}

    def _parse_vebus_unit_count(self, vid_raw: Any) -> int:
        """Parse VE.Bus ShortIDs (vid code) to determine physical inverter unit count.

        The vid code encodes how many physical units are in a VE.Bus system:
        - 1 = Single unit
        - 3 = Two units (parallel)
        - 7 = Three phase system (one unit per phase)
        - 15 = Three phase system, 1.5 units per phase
        - 31 = Three phase system, one and a half unit per phase (alt)
        - 63 = Three phase system, two units per phase (6 total)
        - 127 = Three phase system, three units per phase (9 total)
        """
        vid_map = {1: 1, 3: 2, 7: 3, 15: 4, 31: 5, 63: 6, 127: 9}
        try:
            return vid_map.get(int(vid_raw), 1)
        except (ValueError, TypeError):
            return 1

    async def get_site_info(self, site_id: str) -> Dict[str, Any]:
        """Get general site information."""
        try:
            installations = await self._get_installations()

            for installation in installations:
                if str(installation.get("idSite")) == str(site_id):
                    # Use system-overview for product names + diagnostics for detail
                    overview = await self._get_system_overview(site_id)
                    diagnostics = await self._get_diagnostics(site_id)
                    records = diagnostics.get("records", [])
                    overview_devices = overview.get("records", {}).get("devices", [])

                    phase_count = None
                    num_grid_inverters = 0
                    num_local_supply_inverters = 0
                    num_pv_inverters = 0
                    num_mppts = 0
                    num_battery_modules = 0

                    # Build product name lookup from system-overview
                    product_names: Dict[str, str] = {}
                    for dev in overview_devices:
                        dev_name = dev.get("name", "")
                        inst = dev.get("instance")
                        if inst is not None:
                            product_names[f"{dev_name}:{inst}"] = dev.get("productName", "")

                    # Count MPPTs and PV Inverters from system-overview (most reliable)
                    for dev in overview_devices:
                        dev_name = dev.get("name", "")
                        if dev_name == "Solar Charger":
                            num_mppts += 1
                        elif dev_name == "PV Inverter":
                            num_pv_inverters += 1
                        elif dev_name == "Inverter":
                            num_local_supply_inverters += 1

                    # Parse diagnostics for VE.Bus unit count and battery modules
                    seen_devices: set = set()
                    if isinstance(records, list):
                        for record in records:
                            code = record.get("code", "")
                            device = record.get("Device", "")
                            instance = record.get("instance")
                            device_key = f"{device}:{instance}"

                            if code == "PC" and phase_count is None:
                                phase_count = record.get("rawValue")

                            if code == "mon":
                                num_battery_modules += int(record.get("rawValue", 0))
                            elif code == "mof":
                                num_battery_modules += int(record.get("rawValue", 0))

                            # Count VE.Bus physical units using vid code
                            if (
                                device == "VE.Bus System"
                                and code == "vid"
                                and device_key not in seen_devices
                            ):
                                seen_devices.add(device_key)
                                num_grid_inverters += self._parse_vebus_unit_count(
                                    record.get("rawValue")
                                )

                    # Build model name for grid inverters
                    vebus_model = product_names.get("VE.Bus System:276", "")

                    return {
                        "site_id": installation.get("idSite"),
                        "name": installation.get("name"),
                        "identifier": installation.get("identifier"),
                        "timezone": installation.get("timezone"),
                        "phase_count": phase_count,
                        "num_inverters": num_grid_inverters,
                        "inverter_model": vebus_model or None,
                        "num_local_supply_inverters": num_local_supply_inverters,
                        "num_pv_inverters": num_pv_inverters,
                        "num_mppts": num_mppts,
                        "num_battery_modules": num_battery_modules,
                        "is_online": await self.is_site_online(site_id),
                    }

            return {"error": "Site not found"}
        except Exception as e:
            return {"error": str(e)}

    async def get_equipment_details(self, site_id: str) -> Dict[str, Any]:
        """Get detailed equipment inventory with model names and accurate counts.

        Uses system-overview for product names and diagnostics for serial numbers,
        VE.Bus physical unit counts, and battery module counts.
        """
        try:
            overview = await self._get_system_overview(site_id)
            diagnostics = await self._get_diagnostics(site_id)
            records = diagnostics.get("records", [])
            overview_devices = overview.get("records", {}).get("devices", [])

            # Build product name + firmware lookup from system-overview
            product_info: Dict[str, Dict[str, Any]] = {}
            for dev in overview_devices:
                dev_name = dev.get("name", "")
                inst = dev.get("instance")
                if inst is not None:
                    product_info[f"{dev_name}:{inst}"] = {
                        "productName": dev.get("productName", ""),
                        "firmwareVersion": dev.get("firmwareVersion"),
                        "customName": dev.get("customName") or None,
                    }

            # Index diagnostics codes by device_key for efficient lookup
            diag_codes: Dict[str, Dict[str, Any]] = {}
            if isinstance(records, list):
                for record in records:
                    device = record.get("Device", "")
                    instance = record.get("instance")
                    key = f"{device}:{instance}"
                    if key not in diag_codes:
                        diag_codes[key] = {}
                    code = record.get("code", "")
                    diag_codes[key][code] = {
                        "raw": record.get("rawValue"),
                        "fmt": record.get("formattedValue"),
                    }

            grid_inverters = []
            local_supply_inverters = []
            pv_inverters = []
            mppts = []
            battery_modules = {"online": 0, "offline": 0, "total": 0}
            battery_info: Dict[str, Any] = {}

            # Process VE.Bus Systems (grid inverters)
            for key, codes in diag_codes.items():
                if key.startswith("VE.Bus System:"):
                    instance = key.split(":")[1]
                    info = product_info.get(key, {})
                    vid_raw = codes.get("vid", {}).get("raw")
                    vid_fmt = codes.get("vid", {}).get("fmt", "")
                    unit_count = self._parse_vebus_unit_count(vid_raw)
                    grid_inverters.append(
                        {
                            "product_name": info.get("productName", ""),
                            "instance": instance,
                            "serial": codes.get("vs0", {}).get("raw"),
                            "firmware": info.get("firmwareVersion"),
                            "physical_units": unit_count,
                            "unit_config": vid_fmt,
                            "custom_name": codes.get("vcn", {}).get("raw") or None,
                        }
                    )

            # Process standalone Inverters (Phoenix - local supply)
            for dev in overview_devices:
                if dev.get("name") == "Inverter":
                    inst = dev.get("instance")
                    key = f"Inverter:{inst}"
                    codes = diag_codes.get(key, {})
                    local_supply_inverters.append(
                        {
                            "product_name": dev.get("productName", ""),
                            "instance": inst,
                            "serial": codes.get("iSN", {}).get("raw"),
                            "firmware": dev.get("firmwareVersion"),
                            "custom_name": codes.get("icn", {}).get("raw") or None,
                        }
                    )

            # Process PV Inverters (Fronius etc.)
            for dev in overview_devices:
                if dev.get("name") == "PV Inverter":
                    inst = dev.get("instance")
                    key = f"PV Inverter:{inst}"
                    codes = diag_codes.get(key, {})
                    pv_inverters.append(
                        {
                            "product_name": dev.get("productName", ""),
                            "instance": inst,
                            "model_detail": codes.get("pF", {}).get("fmt"),
                            "custom_name": codes.get("pcn", {}).get("raw") or None,
                            "power_w": codes.get("pP1", {}).get("raw"),
                        }
                    )

            # Process Solar Chargers (MPPTs)
            for dev in overview_devices:
                if dev.get("name") == "Solar Charger":
                    inst = dev.get("instance")
                    key = f"Solar Charger:{inst}"
                    codes = diag_codes.get(key, {})
                    mppts.append(
                        {
                            "product_name": dev.get("productName", ""),
                            "instance": inst,
                            "serial": codes.get("ScSN", {}).get("raw"),
                            "firmware": dev.get("firmwareVersion"),
                            "custom_name": codes.get("Sccn", {}).get("raw") or None,
                        }
                    )

            # Process Battery Monitor for module counts and brand
            for key, codes in diag_codes.items():
                if key.startswith("Battery Monitor:"):
                    mon = int(codes.get("mon", {}).get("raw", 0) or 0)
                    mof = int(codes.get("mof", {}).get("raw", 0) or 0)
                    battery_modules["online"] += mon
                    battery_modules["offline"] += mof
                    info = product_info.get(key, {})
                    battery_info = {
                        "product_name": info.get("productName", ""),
                        "brand": codes.get("Bm", {}).get("raw") or None,
                        "family": codes.get("Bf", {}).get("raw") or None,
                        "custom_name": codes.get("Bcn", {}).get("raw") or None,
                    }

            battery_modules["total"] = battery_modules["online"] + battery_modules["offline"]

            # Sum physical grid inverter units across all VE.Bus systems
            total_physical_inverters = sum(inv.get("physical_units", 1) for inv in grid_inverters)

            return {
                "timestamp": datetime.utcnow().isoformat(),
                "grid_inverters": grid_inverters,
                "local_supply_inverters": local_supply_inverters,
                "pv_inverters": pv_inverters,
                "mppts": mppts,
                "battery_modules": battery_modules,
                "battery_info": battery_info,
                "summary": {
                    "num_grid_inverter_units": total_physical_inverters,
                    "num_local_supply_inverters": len(local_supply_inverters),
                    "num_pv_inverters": len(pv_inverters),
                    "num_mppts": len(mppts),
                    "num_battery_modules": battery_modules["total"],
                },
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_historical_alarms(
        self,
        site_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> List[Dict[str, Any]]:
        """Get historical VE.Bus alarms and errors for a time period.

        Returns alarm events that occurred within the time window,
        useful for correlating with outage events.

        Args:
            site_id: VRM site ID
            start_time: Start of search window
            end_time: End of search window

        Returns:
            List of alarm events with timestamp, code, description, severity
        """
        try:
            # VRM stores alarms in the alarm-log endpoint
            params = {
                "start": int(start_time.timestamp()),
                "end": int(end_time.timestamp()),
            }
            endpoint = f"/installations/{site_id}/alarm-log"
            data = await self._api_get(endpoint, params)

            alarms = []
            records = data.get("records", [])

            if isinstance(records, list):
                for record in records:
                    # VRM alarm-log uses "started" for the epoch timestamp
                    alarm_time = record.get("started")
                    if not alarm_time:
                        continue
                    try:
                        alarm_dt = datetime.fromtimestamp(alarm_time)
                    except (ValueError, TypeError):
                        continue

                    # Skip "VE.Bus state" enum records — these are state
                    # transitions (Off→Inverting etc.), not actionable alarms.
                    desc = record.get("description", "")
                    if desc == "VE.Bus state":
                        continue

                    name_enum = record.get("nameEnum", "")
                    alarms.append(
                        {
                            "timestamp": alarm_dt.isoformat(),
                            "description": desc,
                            "device": record.get("device", record.get("Device", "")),
                            "instance": record.get("instance"),
                            "severity": name_enum or record.get("severity", ""),
                            "cleared": record.get("cleared"),
                        }
                    )

            return sorted(alarms, key=lambda x: x["timestamp"])

        except Exception:
            # Alarm log may not be available on all installations
            return []

    async def get_battery_soc_at_time(
        self,
        site_id: str,
        target_time: datetime,
        window_minutes: int = 15,
    ) -> Optional[float]:
        """Get battery SOC around a specific time.

        Args:
            site_id: VRM site ID
            target_time: Time to check battery SOC
            window_minutes: Window to search around target time

        Returns:
            Battery SOC percentage or None if not available
        """
        start_time = target_time - timedelta(minutes=window_minutes)
        end_time = target_time + timedelta(minutes=window_minutes)

        data_points = await self.get_historical_power(
            site_id, start_time, end_time, ["battery_soc"]
        )

        if not data_points:
            return None

        # Find SOC closest to target time
        closest = min(
            data_points,
            key=lambda p: abs((p.timestamp - target_time).total_seconds()),
        )
        return closest.value if closest else None

    async def get_battery_state_at_time(
        self,
        site_id: str,
        target_time: datetime,
        window_minutes: int = 15,
    ) -> Optional[int]:
        """Get battery/VE.Bus state around a specific time.

        The battery state (bst) indicates the VE.Bus system state:
        - 0: Off
        - 1: Low power
        - 2: Fault
        - 3: Bulk charging
        - 4: Absorption charging
        - 5: Float charging
        - 6: Storage mode
        - 7: Equalize charging
        - 8: Passthru
        - 9: Inverting
        - 10: Power assist
        - 11: Power supply
        - 252: External control

        Args:
            site_id: VRM site ID
            target_time: Time to check battery state
            window_minutes: Window to search around target time

        Returns:
            Battery state code or None if not available
        """
        start_time = target_time - timedelta(minutes=window_minutes)
        end_time = target_time + timedelta(minutes=window_minutes)

        data_points = await self.get_historical_power(
            site_id, start_time, end_time, ["battery_state"]
        )

        if not data_points:
            return None

        # Find state closest to target time
        closest = min(
            data_points,
            key=lambda p: abs((p.timestamp - target_time).total_seconds()),
        )
        return int(closest.value) if closest else None

    async def get_downtime_summary(
        self,
        grid_name: str,
        hours: int = 24,
        timeout_seconds: float = 3.0,
    ) -> DowntimeSummary:
        """Get downtime summary for a single grid.

        Args:
            grid_name: Name of the grid
            hours: Number of hours to analyze (default 24)
            timeout_seconds: Timeout for VRM API call

        Returns:
            DowntimeSummary with outage statistics
        """
        # Import here to avoid circular imports
        from ..analyzers.grid_outage_analyzer import GridOutageAnalyzer

        try:
            # Resolve grid name to site ID
            site_id, is_managed = await self.get_site_id_for_grid(grid_name)
            if not site_id:
                error_msg = (
                    "Equipment data not available (generation not managed by the operator)"
                    if not is_managed
                    else "Grid not found in VRM"
                )
                return DowntimeSummary(
                    grid_name=grid_name,
                    total_downtime_minutes=0,
                    outage_count=0,
                    longest_outage_minutes=0,
                    error=error_msg,
                )

            # Fetch historical data and alarms in parallel with timeout
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(hours=hours)

            data_points, alarms = await asyncio.wait_for(
                asyncio.gather(
                    self.get_historical_power(
                        site_id,
                        start_time,
                        end_time,
                        ["grid_consumption", "inverter_voltage", "battery_soc"],
                    ),
                    self.get_historical_alarms(site_id, start_time, end_time),
                ),
                timeout=timeout_seconds,
            )

            if not data_points:
                return DowntimeSummary(
                    grid_name=grid_name,
                    total_downtime_minutes=0,
                    outage_count=0,
                    longest_outage_minutes=0,
                    error="No historical data available",
                )

            # Filter data by metric type
            inverter_points = [p for p in data_points if p.metric == "grid_consumption"]
            voltage_points = [p for p in data_points if p.metric == "inverter_voltage"]

            # Build battery SOC lookup for classification
            battery_points = [p for p in data_points if p.metric == "battery_soc"]
            battery_soc_by_time: List[tuple] = [
                (p.timestamp, p.value) for p in battery_points if p.value is not None
            ]
            battery_soc_by_time.sort(key=lambda x: x[0])

            # Analyze for outages - pass voltage data for smarter detection
            analyzer = GridOutageAnalyzer()
            result = analyzer.detect_outages(
                inverter_points, start_time, end_time, voltage_points=voltage_points
            )

            # Check if grid was already down at start of period (first outage starts within 5 min of start_time)
            # If so, look back 12h more to find the actual cause of that outage
            extended_battery_soc: List[tuple] = []
            if result.outages:
                first_outage = result.outages[0]
                time_from_start = (first_outage.start_time - start_time).total_seconds()
                if time_from_start < 300:  # Within 5 minutes of start = was already down
                    # Fetch additional 12h of data and alarms to find the cause
                    extended_start = start_time - timedelta(hours=12)
                    try:
                        extended_points, extended_alarms = await asyncio.wait_for(
                            asyncio.gather(
                                self.get_historical_power(
                                    site_id,
                                    extended_start,
                                    start_time,
                                    ["grid_consumption", "inverter_voltage", "battery_soc"],
                                ),
                                self.get_historical_alarms(site_id, extended_start, start_time),
                            ),
                            timeout=timeout_seconds,
                        )
                        if extended_alarms:
                            alarms = extended_alarms + alarms
                        if extended_points:
                            # Only use battery SOC from extended period for classification
                            extended_battery = [
                                p for p in extended_points if p.metric == "battery_soc"
                            ]
                            extended_battery_soc = [
                                (p.timestamp, p.value)
                                for p in extended_battery
                                if p.value is not None
                            ]
                            extended_battery_soc.sort(key=lambda x: x[0])
                    except asyncio.TimeoutError:
                        pass  # Continue without extended data

            # Combine battery SOC data (extended + main period)
            all_battery_soc = extended_battery_soc + battery_soc_by_time

            # Classify each outage by cause category
            causes: Dict[str, int] = {}
            fault_details: Dict[str, List[str]] = {}
            for outage in result.outages:
                # Find battery SOC closest to outage start (using combined data)
                soc_at_outage = None
                for ts, soc in all_battery_soc:
                    if ts <= outage.start_time:
                        soc_at_outage = soc
                    else:
                        break

                # Use classify_outage_cause with alarms for proper categorization
                analyzer.classify_outage_cause(outage, battery_soc=soc_at_outage, alarms=alarms)

                # Sum minutes by category (battery_depletion → "battery" for brevity)
                category = outage.cause_category or "unknown"
                if category == "battery_depletion":
                    category = "battery"
                outage_minutes = round(outage.duration_seconds / 60)
                causes[category] = causes.get(category, 0) + outage_minutes

                # Collect affected phases for grid faults
                if category == "grid_fault" and outage.affected_phases:
                    # Collect all unique phases affected across all grid fault events
                    if "affected_phases" not in fault_details:
                        fault_details["affected_phases"] = []
                    for phase in outage.affected_phases:
                        if phase not in fault_details["affected_phases"]:
                            fault_details["affected_phases"].append(phase)

            # Build summary
            total_minutes = round(result.total_downtime_seconds / 60)
            longest_minutes = round(result.longest_outage_seconds / 60)
            last_outage = result.outages[-1] if result.outages else None

            # Cross-check: VRM historical data lags up to ~5 min behind real time.
            # A brief voltage blip during a failed restart attempt can make the
            # analyzer think the outage ended when it actually continued.
            # If the last outage shows as "recovered" within the past 20 minutes,
            # do a live voltage check — if the system is still down, override
            # is_ongoing so the LLM knows the outage is still in progress.
            last_outage_ongoing = last_outage.is_ongoing if last_outage else False
            if (
                not last_outage_ongoing
                and last_outage is not None
                and last_outage.end_time is not None
                and (end_time - last_outage.end_time).total_seconds() < 1200  # within 20 min
            ):
                try:
                    live_voltage = await asyncio.wait_for(
                        self.get_current_inverter_voltage(grid_name),
                        timeout=3.0,
                    )
                    if live_voltage and not live_voltage.is_producing:
                        # System is currently down despite historical data showing recovery —
                        # the "recovery" was a transient blip; treat outage as ongoing.
                        last_outage_ongoing = True
                except (asyncio.TimeoutError, Exception):
                    pass  # Live check failed — keep historical result

            return DowntimeSummary(
                grid_name=grid_name,
                total_downtime_minutes=total_minutes,
                outage_count=result.total_outages,
                longest_outage_minutes=longest_minutes,
                causes=causes,
                last_outage_time=last_outage.start_time if last_outage else None,
                last_outage_end=last_outage.end_time if last_outage else None,
                last_outage_ongoing=last_outage_ongoing,
                fault_details=fault_details,
            )

        except asyncio.TimeoutError:
            return DowntimeSummary(
                grid_name=grid_name,
                total_downtime_minutes=0,
                outage_count=0,
                longest_outage_minutes=0,
                error="VRM API timeout",
            )
        except Exception as e:
            return DowntimeSummary(
                grid_name=grid_name,
                total_downtime_minutes=0,
                outage_count=0,
                longest_outage_minutes=0,
                error=f"Error: {str(e)[:50]}",
            )

    async def get_batch_downtime_summary(
        self,
        grid_names: List[str],
        hours: int = 24,
        max_concurrent: int = 5,
        timeout_per_grid: float = 3.0,
    ) -> Dict[str, DowntimeSummary]:
        """Fetch 24h downtime for multiple grids in parallel with concurrency limit.

        Args:
            grid_names: List of grid names to fetch
            hours: Number of hours to analyze (default 24)
            max_concurrent: Maximum parallel VRM API calls
            timeout_per_grid: Timeout per grid in seconds

        Returns:
            Dict mapping grid_name -> DowntimeSummary
        """
        if not grid_names:
            return {}

        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_one(grid_name: str) -> tuple:
            async with semaphore:
                summary = await self.get_downtime_summary(
                    grid_name, hours=hours, timeout_seconds=timeout_per_grid
                )
                return grid_name, summary

        # Fetch all in parallel (limited by semaphore)
        results = await asyncio.gather(
            *[fetch_one(g) for g in grid_names],
            return_exceptions=True,
        )

        # Build result dict, handling exceptions
        output: Dict[str, DowntimeSummary] = {}
        for i, result in enumerate(results):
            grid_name = grid_names[i]
            if isinstance(result, Exception):
                output[grid_name] = DowntimeSummary(
                    grid_name=grid_name,
                    total_downtime_minutes=0,
                    outage_count=0,
                    longest_outage_minutes=0,
                    error=f"Fetch failed: {str(result)[:50]}",
                )
            elif isinstance(result, tuple):
                output[result[0]] = result[1]

        return output

    async def _get_grid_gps(self, grid_name: str) -> Optional[Dict[str, float]]:
        """Get GPS coordinates for a grid from the database.

        Parses location_geom (WKB format) from the grids table.

        Returns:
            Dict with "latitude" and "longitude" or None if not available
        """
        try:
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT location_geom::text as location_wkb
                    FROM grids
                    WHERE LOWER(name) = LOWER($1)
                      AND deleted_at IS NULL
                      AND location_geom IS NOT NULL
                    LIMIT 1
                    """,
                    grid_name,
                )

                if not row or not row["location_wkb"]:
                    return None

                return parse_location_geom(row["location_wkb"])

        except Exception:
            return None

    async def get_site_weather(
        self,
        grid_name: str,
        timeout_seconds: float = 3.0,
    ) -> WeatherData:
        """Fetch current weather for a grid using Open-Meteo API.

        Args:
            grid_name: Name of the grid
            timeout_seconds: Timeout for API calls

        Returns:
            WeatherData with current weather conditions
        """
        try:
            # Get GPS coordinates from grids table
            gps = await self._get_grid_gps(grid_name)
            if not gps:
                return WeatherData(
                    temperature_c=None,
                    weather_code=0,
                    weather_description="Unknown",
                    wind_speed_kmh=None,
                    icon="❓",
                    error="No GPS coordinates",
                )

            # Call Open-Meteo API
            params = {
                "latitude": gps["latitude"],
                "longitude": gps["longitude"],
                "current_weather": "true",
            }

            async with aiohttp.ClientSession() as session:
                async with asyncio.timeout(timeout_seconds):
                    async with session.get(OPEN_METEO_API_BASE, params=params) as resp:
                        if resp.status != 200:
                            return WeatherData(
                                temperature_c=None,
                                weather_code=0,
                                weather_description="Unknown",
                                wind_speed_kmh=None,
                                icon="❓",
                                error=f"Weather API error: {resp.status}",
                            )
                        data = await resp.json()

            current = data.get("current_weather", {})
            weather_code = current.get("weathercode", 0)
            description, icon = WMO_WEATHER_CODES.get(weather_code, ("Unknown", "❓"))
            temperature = current.get("temperature")
            wind_speed = current.get("windspeed")

            # Check for high temperature warning
            high_temp = temperature is not None and temperature > HIGH_TEMP_THRESHOLD_C

            return WeatherData(
                temperature_c=temperature,
                weather_code=weather_code,
                weather_description=description,
                wind_speed_kmh=wind_speed,
                icon=icon,
                high_temp_warning=high_temp,
            )

        except asyncio.TimeoutError:
            return WeatherData(
                temperature_c=None,
                weather_code=0,
                weather_description="Unknown",
                wind_speed_kmh=None,
                icon="❓",
                error="Weather API timeout",
            )
        except Exception as e:
            return WeatherData(
                temperature_c=None,
                weather_code=0,
                weather_description="Unknown",
                wind_speed_kmh=None,
                icon="❓",
                error=f"Error: {str(e)[:50]}",
            )

    async def get_batch_weather(
        self,
        grid_names: List[str],
        max_concurrent: int = 10,
        timeout_per_grid: float = 3.0,
    ) -> Dict[str, WeatherData]:
        """Fetch weather for multiple grids in parallel with concurrency limit.

        Args:
            grid_names: List of grid names to fetch
            max_concurrent: Maximum parallel API calls (Open-Meteo allows more)
            timeout_per_grid: Timeout per grid in seconds

        Returns:
            Dict mapping grid_name -> WeatherData
        """
        if not grid_names:
            return {}

        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_one(grid_name: str) -> tuple:
            async with semaphore:
                weather = await self.get_site_weather(grid_name, timeout_seconds=timeout_per_grid)
                return grid_name, weather

        # Fetch all in parallel (limited by semaphore)
        results = await asyncio.gather(
            *[fetch_one(g) for g in grid_names],
            return_exceptions=True,
        )

        # Build result dict, handling exceptions
        output: Dict[str, WeatherData] = {}
        for i, result in enumerate(results):
            grid_name = grid_names[i]
            if isinstance(result, Exception):
                output[grid_name] = WeatherData(
                    temperature_c=None,
                    weather_code=0,
                    weather_description="Unknown",
                    wind_speed_kmh=None,
                    icon="❓",
                    error=f"Fetch failed: {str(result)[:50]}",
                )
            elif isinstance(result, tuple):
                output[result[0]] = result[1]

        return output
