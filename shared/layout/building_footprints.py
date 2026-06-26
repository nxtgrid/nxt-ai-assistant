"""Building-footprint sourcing for community-anchored planning.

Primary source: Microsoft Global ML Building Footprints (ODbL), indexed by
Bing zoom-9 QuadKey tiles. Cross-check / fallback: Google Open Buildings
(CC BY-4.0 / ODbL), indexed by S2 level-6 tiles.

Footprint counts are the single source of truth for downstream LPP planning
numbers. The GRID3 block-attribute count is used only to decide whether the
MS coverage looks thin enough to warrant the Google cross-check.

ODbL attribution: derived products must credit the source datasets. The caller
is responsible for surfacing attribution on outputs (see resolve_community_site).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import requests
from shapely.geometry import Polygon, shape

from shared.layout.quadkeys import bbox_to_quadkeys

logger = logging.getLogger(__name__)

_MS_ZOOM = 9
_HTTP_TIMEOUT_S = 60
_MS_LINKS_TIMEOUT_S = 120  # dataset-links.csv is ~25 MB — give it more time on first fetch

_ms_links_cache: "pd.DataFrame | None" = None


@dataclass
class FootprintResult:
    """Footprints clipped to a community boundary."""

    buildings_geojson: dict[str, Any]
    source: str  # "microsoft" | "google" | "microsoft+google"
    ms_count: int = 0
    google_count: int = 0
    grid3_estimate: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.buildings_geojson.get("features", []))


def _quadkeys_for_boundary(boundary: Polygon) -> list[str]:
    """Bing zoom-9 QuadKeys covering the boundary's bounding box."""
    minx, miny, maxx, maxy = boundary.bounds
    return bbox_to_quadkeys(minx, miny, maxx, maxy, _MS_ZOOM)


def _http_get_text(url: str) -> str:
    """GET a (possibly gzip) text resource and return decoded text."""
    resp = requests.get(url, timeout=_HTTP_TIMEOUT_S)
    resp.raise_for_status()
    content = resp.content
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
            return gz.read().decode("utf-8")
    return resp.text


def _stream_ndjson_features(url: str, boundary: Polygon) -> list[dict[str, Any]]:
    """Stream an NDJSON tile line-by-line, filtering to features within boundary.

    Avoids loading the full (potentially 100MB+) tile into memory at once.
    Handles gzip-compressed tiles (URLs ending in .gz).
    """
    kept: list[dict[str, Any]] = []
    with requests.get(url, timeout=_HTTP_TIMEOUT_S, stream=True) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = False  # we manage decompression manually
        binary_stream: Any = gzip.GzipFile(fileobj=resp.raw) if url.endswith(".gz") else resp.raw
        for raw_line in io.TextIOWrapper(binary_stream, encoding="utf-8"):
            line = raw_line.strip()
            if not line:
                continue
            try:
                feat = json.loads(line)
                geom = shape(feat["geometry"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if geom.intersects(boundary):
                kept.append(feat)
    return kept


def _load_ms_links() -> "pd.DataFrame":
    """Download (and cache) the MS dataset-links.csv. Uses the enforced HTTP timeout."""
    global _ms_links_cache
    if _ms_links_cache is not None:
        return _ms_links_cache
    links_url = os.environ.get(
        "MS_BUILDINGS_DATASET_LINKS_URL",
        "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv",
    )
    logger.info(f"MS footprints: downloading dataset-links.csv (timeout={_MS_LINKS_TIMEOUT_S}s)")
    resp = requests.get(links_url, timeout=_MS_LINKS_TIMEOUT_S)
    resp.raise_for_status()
    _ms_links_cache = pd.read_csv(io.StringIO(resp.text), dtype=str)
    logger.info(f"MS footprints: dataset-links cached ({len(_ms_links_cache)} rows)")
    return _ms_links_cache


def _ms_tile_url_for_quadkey(quad_key: str) -> str | None:
    """Resolve the MS dataset-links.csv row for a QuadKey to its tile URL."""
    df = _load_ms_links()
    rows = df[df["QuadKey"] == quad_key]
    if len(rows) == 0:
        logger.info(f"MS footprints: QuadKey {quad_key} not in dataset-links (no coverage)")
        return None
    if len(rows) > 1:
        # Multiple vintages — take the most recent location (last row).
        rows = rows.iloc[[-1]]
    return str(rows.iloc[0]["Url"])


def _features_within(ndjson_text: str, boundary: Polygon) -> list[dict[str, Any]]:
    """Parse newline-delimited GeoJSON, keep features whose geometry intersects boundary."""
    kept: list[dict[str, Any]] = []
    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            feat = json.loads(line)
            geom = shape(feat["geometry"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if geom.intersects(boundary):
            kept.append(feat)
    return kept


def fetch_ms_footprints(boundary: Polygon) -> dict[str, Any]:
    """Microsoft Global ML Building Footprints clipped to the boundary (WGS84)."""
    features: list[dict[str, Any]] = []
    for quad_key in _quadkeys_for_boundary(boundary):
        url = _ms_tile_url_for_quadkey(quad_key)
        if not url:
            continue
        try:
            tile_features = _stream_ndjson_features(url, boundary)
        except requests.RequestException as e:
            logger.warning(f"MS footprints: tile {quad_key} download failed: {e}")
            continue
        logger.info(f"MS footprints: tile {quad_key} → {len(tile_features)} features")
        features.extend(tile_features)
    logger.info(f"MS footprints: {len(features)} buildings within boundary")
    return {"type": "FeatureCollection", "features": features}


def _google_tile_urls_for_boundary(boundary: Polygon) -> list[str]:
    """S2 level-6 tile CSV URLs covering the boundary bbox."""
    import s2sphere

    base = os.environ.get(
        "GOOGLE_OPEN_BUILDINGS_BASE_URL",
        "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_6_gzip_no_header",
    )
    minx, miny, maxx, maxy = boundary.bounds
    region = s2sphere.LatLngRect(
        s2sphere.LatLng.from_degrees(miny, minx),
        s2sphere.LatLng.from_degrees(maxy, maxx),
    )
    coverer = s2sphere.RegionCoverer()
    coverer.min_level = 6
    coverer.max_level = 6
    tokens = {c.parent(6).to_token() for c in coverer.get_covering(region)}
    return [f"{base}/{token}_buildings.csv.gz" for token in sorted(tokens)]


def _stream_google_features(
    url: str, boundary: Polygon, min_confidence: float
) -> list[dict[str, Any]]:
    """Stream a Google Open Buildings S2 tile (CSV.gz) line-by-line.

    A single S2 level-6 tile can be 100MB+ compressed and decompress to ~1GB —
    loading it whole (requests.get().text + pd.read_csv) OOM-kills the 1GB
    instance. We stream it instead and reject rows cheaply:

      columns (no header): latitude, longitude, area_in_meters, confidence,
                           geometry (WKT), full_plus_code

    The tile spans a far larger area than the community boundary, so a bbox
    test on the per-building centroid (lat/lon columns) discards the vast
    majority of rows before the expensive WKT parse. Only survivors are
    parsed and intersection-tested.
    """
    import csv

    from shapely import wkt

    minx, miny, maxx, maxy = boundary.bounds
    kept: list[dict[str, Any]] = []
    with requests.get(url, timeout=_HTTP_TIMEOUT_S, stream=True) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = False  # we manage decompression manually
        binary_stream: Any = gzip.GzipFile(fileobj=resp.raw) if url.endswith(".gz") else resp.raw
        reader = csv.reader(io.TextIOWrapper(binary_stream, encoding="utf-8"))
        for row in reader:
            if len(row) < 5:
                continue
            try:
                lat_c = float(row[0])
                lon_c = float(row[1])
                conf = float(row[3])
            except ValueError:
                continue
            if conf < min_confidence:
                continue
            # Cheap centroid bbox reject before the costly WKT parse.
            if not (minx <= lon_c <= maxx and miny <= lat_c <= maxy):
                continue
            try:
                geom = wkt.loads(row[4])
            except Exception:
                continue
            if geom.intersects(boundary):
                kept.append(
                    {
                        "type": "Feature",
                        "geometry": geom.__geo_interface__,
                        "properties": {"confidence": conf, "source": "google"},
                    }
                )
    return kept


def fetch_google_open_buildings(boundary: Polygon, min_confidence: float = 0.70) -> dict[str, Any]:
    """Google Open Buildings clipped to boundary, filtered by confidence.

    Streams each S2 tile to keep memory bounded (see _stream_google_features).
    """
    features: list[dict[str, Any]] = []
    for url in _google_tile_urls_for_boundary(boundary):
        try:
            tile_features = _stream_google_features(url, boundary, min_confidence)
        except requests.RequestException as e:
            logger.warning(f"Google OB: tile download failed: {e}")
            continue
        logger.info(f"Google OB: tile → {len(tile_features)} buildings within boundary")
        features.extend(tile_features)
    logger.info(f"Google OB: {len(features)} buildings within boundary")
    return {"type": "FeatureCollection", "features": features}


def fetch_building_footprints(
    boundary: Polygon,
    grid3_estimate: int = 0,
    min_confidence: float | None = None,
    crosscheck_min_ratio: float | None = None,
) -> FootprintResult:
    """Fetch footprints (MS primary), cross-check against Google when MS looks thin.

    Cross-check trigger: MS count < crosscheck_min_ratio * grid3_estimate.
    When triggered, the denser of the two sets wins (best coverage = best latest data).
    """
    if min_confidence is None:
        min_confidence = float(os.environ.get("GOOGLE_OPEN_BUILDINGS_MIN_CONFIDENCE", "0.70"))
    if crosscheck_min_ratio is None:
        crosscheck_min_ratio = float(os.environ.get("FOOTPRINT_CROSSCHECK_MIN_RATIO", "0.80"))

    ms_fc = fetch_ms_footprints(boundary)
    ms_count = len(ms_fc.get("features", []))
    notes: list[str] = [f"Microsoft footprints: {ms_count}"]

    thin = (
        crosscheck_min_ratio > 0
        and grid3_estimate > 0
        and ms_count < crosscheck_min_ratio * grid3_estimate
    )
    if not thin:
        notes.append("Coverage sufficient — Google cross-check skipped")
        return FootprintResult(
            buildings_geojson=ms_fc,
            source="microsoft",
            ms_count=ms_count,
            grid3_estimate=grid3_estimate,
            notes=notes,
        )

    notes.append(
        f"MS coverage thin ({ms_count} < {crosscheck_min_ratio:.0%} of GRID3 "
        f"estimate {grid3_estimate}) — running Google cross-check"
    )
    google_fc = fetch_google_open_buildings(boundary, min_confidence=min_confidence)
    google_count = len(google_fc.get("features", []))
    notes.append(f"Google footprints: {google_count}")

    if google_count > ms_count:
        return FootprintResult(
            buildings_geojson=google_fc,
            source="google",
            ms_count=ms_count,
            google_count=google_count,
            grid3_estimate=grid3_estimate,
            notes=notes,
        )
    return FootprintResult(
        buildings_geojson=ms_fc,
        source="microsoft",
        ms_count=ms_count,
        google_count=google_count,
        grid3_estimate=grid3_estimate,
        notes=notes,
    )
