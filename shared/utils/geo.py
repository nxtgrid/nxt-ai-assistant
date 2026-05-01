"""Geospatial utilities for parsing WKB geometry data."""

import struct
from typing import Dict, Optional


def parse_location_geom(wkb_hex: Optional[str]) -> Optional[Dict[str, float]]:
    """Parse WKB hex string from location_geom::text into lat/lon dict.

    Handles both POINT (21 bytes) and POINT with SRID (25 bytes) WKB formats.

    Args:
        wkb_hex: Hex-encoded WKB string, e.g. from ``location_geom::text``.

    Returns:
        ``{"latitude": ..., "longitude": ...}`` or ``None`` if parsing fails.
    """
    if not wkb_hex:
        return None
    try:
        wkb = bytes.fromhex(wkb_hex)
        # WKB format: byte_order(1) + type(4) + [SRID(4)] + X(8) + Y(8)
        # POINT with SRID = 25 bytes, regular POINT = 21 bytes
        if len(wkb) >= 25:
            lon, lat = struct.unpack("<dd", wkb[9:25])
        elif len(wkb) >= 21:
            lon, lat = struct.unpack("<dd", wkb[5:21])
        else:
            return None
        return {"latitude": round(lat, 6), "longitude": round(lon, 6)}
    except Exception:
        return None
