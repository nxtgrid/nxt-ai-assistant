"""Bing Maps tile-system / QuadKey helpers.

Shared by road_network.py (Canopy Height Model tiles) and
building_footprints.py (Microsoft Global ML Building Footprints tiles).
Both datasets index tiles by zoom-9 Bing QuadKeys.
"""

from __future__ import annotations

import math


def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lon to tile XY at the given zoom level (Bing Maps tile system)."""
    sin_lat = math.sin(lat * math.pi / 180.0)
    sin_lat = max(-0.9999, min(0.9999, sin_lat))
    n = 1 << zoom
    tile_x = int((lon + 180.0) / 360.0 * n)
    tile_y = int((0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * n)
    tile_x = max(0, min(n - 1, tile_x))
    tile_y = max(0, min(n - 1, tile_y))
    return tile_x, tile_y


def tile_to_quadkey(tile_x: int, tile_y: int, zoom: int) -> str:
    """Convert tile XY to a Bing QuadKey string."""
    quadkey: list[str] = []
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if tile_x & mask:
            digit += 1
        if tile_y & mask:
            digit += 2
        quadkey.append(str(digit))
    return "".join(quadkey)


def bbox_to_quadkeys(minx: float, miny: float, maxx: float, maxy: float, zoom: int) -> list[str]:
    """Return unique QuadKeys covering a WGS84 bounding box at the given zoom."""
    tx_min, ty_min = latlon_to_tile(maxy, minx, zoom)  # NW corner
    tx_max, ty_max = latlon_to_tile(miny, maxx, zoom)  # SE corner
    keys = set()
    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            keys.add(tile_to_quadkey(tx, ty, zoom))
    return sorted(keys)
