"""Abstract base class for equipment monitoring platforms.

This module defines the interface that all platform adapters must implement.
Currently implemented: VRM (Victron Remote Management)
Future: Deye, SolarEdge, etc.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class PowerReading:
    """Power reading for a single timestamp."""

    timestamp: datetime
    l1_power_w: Optional[float] = None
    l2_power_w: Optional[float] = None
    l3_power_w: Optional[float] = None
    total_power_w: Optional[float] = None


@dataclass
class BatteryStatus:
    """Battery status reading."""

    timestamp: datetime
    soc_percent: Optional[float] = None
    voltage_v: Optional[float] = None
    current_a: Optional[float] = None
    power_w: Optional[float] = None
    charging: Optional[bool] = None


@dataclass
class GridStatus:
    """Grid connection status."""

    timestamp: datetime
    connected: bool
    l1_power_w: Optional[float] = None
    l2_power_w: Optional[float] = None
    l3_power_w: Optional[float] = None
    total_power_w: Optional[float] = None


@dataclass
class Alarm:
    """Active alarm on the system."""

    code: str
    description: str
    device: Optional[str] = None
    instance: Optional[int] = None
    severity: str = "warning"  # "warning" or "alarm"
    timestamp: Optional[datetime] = None


@dataclass
class EquipmentStatus:
    """Combined equipment status response."""

    grid_name: str
    site_id: str
    timestamp: datetime
    is_online: bool
    inverter: Optional[PowerReading] = None
    battery: Optional[BatteryStatus] = None
    grid: Optional[GridStatus] = None
    pv: Optional[PowerReading] = None
    alarms: Optional[List[Alarm]] = None
    is_generation_managed: bool = True  # True if the operator manages generation for this grid


@dataclass
class HistoricalDataPoint:
    """Single data point in a time series."""

    timestamp: datetime
    value: float
    metric: str
    phase: Optional[str] = None  # L1, L2, L3, or None for total


class BasePlatform(ABC):
    """Abstract base class for equipment monitoring platforms.

    All platform adapters must implement these methods to provide
    a unified interface for equipment diagnostics.
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Return the name of the platform (e.g., 'VRM', 'Deye')."""
        pass

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the platform connection.

        This may include authentication, token refresh, etc.
        """
        pass

    @abstractmethod
    async def get_site_id_for_grid(self, grid_name: str) -> tuple[Optional[str], bool]:
        """Resolve a grid name to the platform-specific site ID.

        Args:
            grid_name: Human-readable grid name (supports fuzzy matching)

        Returns:
            Tuple of (site_id, is_generation_managed):
            - site_id: Platform-specific site identifier, or None if not found
            - is_generation_managed: True if grid's generation is managed by the operator
        """
        pass

    @abstractmethod
    async def is_site_online(self, site_id: str) -> bool:
        """Check if a site/gateway is currently online.

        Args:
            site_id: Platform-specific site identifier

        Returns:
            True if the site is online and responsive
        """
        pass

    @abstractmethod
    async def get_current_inverter_power(self, site_id: str) -> PowerReading:
        """Get current inverter output power.

        Args:
            site_id: Platform-specific site identifier

        Returns:
            PowerReading with per-phase and total power
        """
        pass

    @abstractmethod
    async def get_current_battery_status(self, site_id: str) -> BatteryStatus:
        """Get current battery status.

        Args:
            site_id: Platform-specific site identifier

        Returns:
            BatteryStatus with SOC, voltage, current, power
        """
        pass

    @abstractmethod
    async def get_current_grid_status(self, site_id: str) -> GridStatus:
        """Get current grid connection status.

        Args:
            site_id: Platform-specific site identifier

        Returns:
            GridStatus with connection state and per-phase power
        """
        pass

    @abstractmethod
    async def get_current_pv_power(self, site_id: str) -> PowerReading:
        """Get current PV/solar power output.

        Args:
            site_id: Platform-specific site identifier

        Returns:
            PowerReading with PV power
        """
        pass

    @abstractmethod
    async def get_active_alarms(self, site_id: str) -> List[Alarm]:
        """Get list of currently active alarms.

        Args:
            site_id: Platform-specific site identifier

        Returns:
            List of active Alarm objects
        """
        pass

    @abstractmethod
    async def get_equipment_status(
        self, site_id: str, metrics: Optional[List[str]] = None
    ) -> EquipmentStatus:
        """Get combined equipment status.

        Args:
            site_id: Platform-specific site identifier
            metrics: List of metrics to include ('inverter', 'battery', 'grid', 'pv', 'alarms')
                    If None, returns all metrics.

        Returns:
            EquipmentStatus with requested metrics
        """
        pass

    @abstractmethod
    async def get_historical_power(
        self,
        site_id: str,
        start_time: datetime,
        end_time: datetime,
        metrics: List[str],
    ) -> List[HistoricalDataPoint]:
        """Get historical power data for charting.

        Args:
            site_id: Platform-specific site identifier
            start_time: Start of time range (UTC)
            end_time: End of time range (UTC)
            metrics: List of metrics to retrieve:
                    - 'grid_consumption': AC output consumption per phase (o1-o3)
                    - 'grid_power': AC input power per phase (a1-a3)
                    - 'battery_soc': Battery state of charge
                    - 'battery_power': Battery charge/discharge power
                    - 'pv_power': PV/solar power
                    - 'consumption': Total consumption

        Returns:
            List of HistoricalDataPoint objects
        """
        pass

    @abstractmethod
    async def get_site_info(self, site_id: str) -> Dict[str, Any]:
        """Get general site information.

        Args:
            site_id: Platform-specific site identifier

        Returns:
            Dict with site details (name, location, phase count, equipment counts, etc.)
        """
        pass

    @abstractmethod
    async def get_equipment_details(self, site_id: str) -> Dict[str, Any]:
        """Get detailed equipment inventory.

        Args:
            site_id: Platform-specific site identifier

        Returns:
            Dict with equipment lists (inverters, batteries, MPPTs with serial numbers, etc.)
        """
        pass

    async def get_full_status(
        self, grid_name: str, metrics: Optional[List[str]] = None
    ) -> Optional[EquipmentStatus]:
        """Convenience method: resolve grid name and get status.

        Args:
            grid_name: Human-readable grid name
            metrics: Optional list of metrics to include

        Returns:
            EquipmentStatus or None if grid not found
        """
        site_id, is_managed = await self.get_site_id_for_grid(grid_name)
        if not site_id:
            return None

        status = await self.get_equipment_status(site_id, metrics)
        status.grid_name = grid_name
        status.is_generation_managed = is_managed
        return status
