"""
Data models for site mapping.

These dataclasses provide a clean interface for passing site data
between modules without coupling to database schemas or JSON formats.
"""

from dataclasses import dataclass, field
from typing import Optional

from shapely.geometry import Polygon


@dataclass
class SiteBoundary:
    """Site boundary polygon with bounds for map extent."""

    polygon: Polygon
    minx: float
    miny: float
    maxx: float
    maxy: float

    @classmethod
    def from_polygon(cls, polygon: Polygon) -> "SiteBoundary":
        """Create SiteBoundary from a shapely Polygon."""
        minx, miny, maxx, maxy = polygon.bounds
        return cls(polygon=polygon, minx=minx, miny=miny, maxx=maxx, maxy=maxy)

    @property
    def center_lat(self) -> float:
        """Center latitude for coordinate calculations."""
        return (self.miny + self.maxy) / 2

    @property
    def center_lon(self) -> float:
        """Center longitude for coordinate calculations."""
        return (self.minx + self.maxx) / 2

    @property
    def coords(self) -> list:
        """Exterior coordinates as a list for plotting."""
        return list(self.polygon.exterior.coords)


@dataclass
class Building:
    """A building polygon with connection status."""

    coordinates: list  # List of [lon, lat] coordinate pairs (outer ring)
    connected: bool = True  # Whether building is served by the distribution network
    closest_point: Optional[tuple] = None  # Closest connection point [lon, lat]

    @property
    def is_served(self) -> bool:
        """Alias for connected status."""
        return self.connected


@dataclass
class Pole:
    """A distribution pole location."""

    lon: float
    lat: float
    properties: dict = field(default_factory=dict)

    @property
    def coords(self) -> tuple:
        """Coordinates as (lon, lat) tuple."""
        return (self.lon, self.lat)


@dataclass
class Cable:
    """A distribution cable/wire segment."""

    coordinates: list  # List of [lon, lat] coordinate pairs
    length_meters: Optional[float] = None
    properties: dict = field(default_factory=dict)


@dataclass
class SiteMeta:
    """Site metadata from the pipeline."""

    pole_count: int = 0
    pole_coverage_radius: float = 50.0
    minimum_building_area: float = 30.0
    served_building_count: int = 0
    unserved_building_count: int = 0
    distribution_line_total_length: float = 0.0
    # Separated cable lengths for BoM accuracy (auto-designed layouts)
    backbone_cable_length_m: float = 0.0
    drop_cable_length_m: float = 0.0
    backbone_cable_count: int = 0
    drop_cable_count: int = 0
    coverage_percentage: float = 0.0
    average_span_length_m: float = 0.0
    max_drop_cable_length_m: float = 0.0

    @property
    def total_building_count(self) -> int:
        """Total number of buildings (served + unserved)."""
        return self.served_building_count + self.unserved_building_count


@dataclass
class SiteData:
    """Complete site data for map generation."""

    site_id: int
    site_name: str
    boundary: SiteBoundary
    buildings: list[Building] = field(default_factory=list)
    poles: list[Pole] = field(default_factory=list)
    cables: list[Cable] = field(default_factory=list)
    meta: Optional[SiteMeta] = None

    @property
    def served_buildings(self) -> list[Building]:
        """Buildings connected to the distribution network."""
        return [b for b in self.buildings if b.connected]

    @property
    def unserved_buildings(self) -> list[Building]:
        """Buildings not connected to the distribution network."""
        return [b for b in self.buildings if not b.connected]

    @property
    def coverage_radius(self) -> float:
        """Pole coverage radius in meters."""
        if self.meta:
            return self.meta.pole_coverage_radius
        return 50.0  # Default
