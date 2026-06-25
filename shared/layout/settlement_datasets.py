"""Settlement-dataset registry: map a GPS anchor to the right country GeoPackage.

The community-detection pipeline (community_detector.py) was originally hardwired
to a single Nigeria GeoPackage via the ``GRID3_GPKG_PATH`` env var. This module
generalises that: a *location* (``SETTLEMENT_DATA_DIR``) holds one or more country
datasets plus a ``manifest.json`` describing them, and an anchor's country is
resolved by reverse-geocoding so the matching dataset can be looked up.

Resolution flow:
    1. Reverse-geocode the anchor (lat/lon) -> ISO 3166-1 alpha-2 country code.
    2. Load the manifest from the data location.
    3. Find the manifest entry whose ``iso2`` matches the anchor's country.
    4. Return a DatasetRef (path + layer + building-count column); raise
       SettlementDataUnavailable with a human-readable message when no dataset
       covers that country.

Manifest format (``{SETTLEMENT_DATA_DIR}/manifest.json``):
    {
      "datasets": [
        {
          "iso2": "NG",
          "iso3": "NGA",
          "country_name": "Nigeria",
          "file": "GRID3_NGA_settlement_extents_v04_3.gpkg",
          "layer": "main_GRID3_NGA_settlement_extents_v4_0",
          "building_count_col": "building_count"
        }
      ]
    }

``file`` may be a bare filename (resolved relative to ``SETTLEMENT_DATA_DIR``),
an absolute local path, or an ``s3://`` URI (DigitalOcean Spaces). This lets a
small local manifest point at large remote GeoPackages.

This module is fully synchronous (it does blocking Nominatim HTTP and file I/O).
Async callers must wrap ``resolve_dataset_for_anchor`` in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from geopy.geocoders import Nominatim

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Default layer / column for the legacy Nigeria GeoPackage, used when falling
# back to GRID3_GPKG_PATH (single-file mode) without a manifest.
_LEGACY_NGA_LAYER = "main_GRID3_NGA_settlement_extents_v4_0"
_LEGACY_NGA_COUNT_COL = "building_count"
_DEFAULT_BUILDING_COUNT_COL = "building_count"

_MANIFEST_FILENAME = "manifest.json"
_NOMINATIM_USER_AGENT = "anansi-settlement-datasets/1.0"


@dataclass(frozen=True)
class DatasetRef:
    """A resolved country dataset ready for community_detector.detect_communities."""

    path: str  # local path or s3:// URI to the GeoPackage
    layer: str  # GeoPackage layer holding settlement blocks
    building_count_col: str  # column with per-block building counts
    country_name: str  # human-readable name (e.g. "Nigeria")
    iso3: str  # ISO 3166-1 alpha-3 (e.g. "NGA"), for labelling outputs


class SettlementDataUnavailable(Exception):
    """Raised when no settlement dataset covers the anchor's country.

    Carries the resolved country (may be None if reverse-geocoding failed) and
    the list of countries that *are* available, so callers can render a clear,
    actionable message.
    """

    def __init__(self, country: Optional[str], available: list[str]) -> None:
        self.country = country
        self.available = available
        super().__init__(self.user_message())

    def user_message(self) -> str:
        supported = ", ".join(self.available) if self.available else "none configured"
        if not self.country:
            return (
                "Couldn't determine which country this location is in "
                "(reverse geocoding failed). Please try again, or check the "
                "coordinates. "
                f"Community detection is currently available for: {supported}."
            )
        return (
            f"Community detection data isn't available for {self.country} yet. "
            f"Currently supported: {supported}."
        )


class SettlementDataNotConfigured(Exception):
    """Raised when neither SETTLEMENT_DATA_DIR nor GRID3_GPKG_PATH is set."""


def _manifest_from_legacy_env() -> Optional[dict]:
    """Synthesise a single-country (Nigeria) manifest from GRID3_GPKG_PATH.

    Keeps existing deployments working before they migrate to SETTLEMENT_DATA_DIR.
    """
    legacy_path = os.environ.get("GRID3_GPKG_PATH")
    if not legacy_path:
        return None
    LOGGER.info("Using legacy GRID3_GPKG_PATH single-file mode (Nigeria only).")
    return {
        "datasets": [
            {
                "iso2": "NG",
                "iso3": "NGA",
                "country_name": "Nigeria",
                "file": legacy_path,
                "layer": _LEGACY_NGA_LAYER,
                "building_count_col": _LEGACY_NGA_COUNT_COL,
            }
        ]
    }


def _load_manifest() -> dict:
    """Load the settlement-dataset manifest.

    Precedence:
        1. SETTLEMENT_MANIFEST_JSON env var (inline JSON) — convenient for
           container deploys where the data lives on S3 and dropping a file is
           awkward.
        2. {SETTLEMENT_DATA_DIR}/manifest.json on the local filesystem.
        3. Legacy GRID3_GPKG_PATH single-file fallback (Nigeria only).

    Raises:
        SettlementDataNotConfigured: nothing configured.
        ValueError: manifest present but malformed.
    """
    inline = os.environ.get("SETTLEMENT_MANIFEST_JSON")
    if inline:
        try:
            parsed: dict = json.loads(inline)
            return parsed
        except json.JSONDecodeError as exc:
            raise ValueError(f"SETTLEMENT_MANIFEST_JSON is not valid JSON: {exc}") from exc

    data_dir = os.environ.get("SETTLEMENT_DATA_DIR")
    if data_dir:
        manifest_path = os.path.join(data_dir, _MANIFEST_FILENAME)
        if os.path.exists(manifest_path):
            with open(manifest_path, encoding="utf-8") as fh:
                try:
                    parsed = json.load(fh)
                    return parsed
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{manifest_path} is not valid JSON: {exc}") from exc
        # Directory set but no manifest — fall through to legacy before erroring.

    legacy = _manifest_from_legacy_env()
    if legacy is not None:
        return legacy

    raise SettlementDataNotConfigured(
        "No settlement data configured. Set SETTLEMENT_DATA_DIR (with a "
        "manifest.json) or the legacy GRID3_GPKG_PATH."
    )


def _resolve_file_path(file_ref: str) -> str:
    """Resolve a manifest ``file`` value to a usable path.

    Absolute paths and s3:// URIs are returned unchanged; bare filenames are
    joined onto SETTLEMENT_DATA_DIR.
    """
    if file_ref.startswith("s3://") or os.path.isabs(file_ref):
        return file_ref
    data_dir = os.environ.get("SETTLEMENT_DATA_DIR", "")
    return os.path.join(data_dir, file_ref)


def _available_country_names(manifest: dict) -> list[str]:
    names = [
        str(d.get("country_name") or d.get("iso3") or d.get("iso2") or "?")
        for d in manifest.get("datasets", [])
    ]
    return sorted(set(names))


def reverse_country(lat: float, lon: float) -> tuple[Optional[str], Optional[str]]:
    """Reverse-geocode an anchor to (alpha-2 country code, country name).

    Returns (None, None) when the lookup fails or yields no country (e.g. a
    point in the ocean). Lowercased alpha-2 code (e.g. "ng") on success.
    """
    try:
        geolocator = Nominatim(user_agent=_NOMINATIM_USER_AGENT, timeout=10)
        location = geolocator.reverse((lat, lon), language="en", zoom=3)
        if location is None:
            return None, None
        addr = location.raw.get("address", {})
        code = addr.get("country_code")
        name = addr.get("country")
        return (code.lower() if code else None), name
    except Exception as exc:  # network / parse failures are non-fatal here
        LOGGER.warning(f"Country reverse-geocode failed for ({lat}, {lon}): {exc}")
        return None, None


def resolve_dataset_for_anchor(lat: float, lon: float) -> DatasetRef:
    """Resolve the settlement dataset covering the anchor's country.

    Raises:
        SettlementDataNotConfigured: no data location/manifest configured.
        SettlementDataUnavailable: anchor's country has no dataset (or country
            could not be determined).
    """
    manifest = _load_manifest()
    datasets = manifest.get("datasets", [])
    available = _available_country_names(manifest)

    iso2, country_name = reverse_country(lat, lon)
    if not iso2:
        raise SettlementDataUnavailable(country=None, available=available)

    for entry in datasets:
        if str(entry.get("iso2", "")).lower() == iso2:
            return DatasetRef(
                path=_resolve_file_path(str(entry["file"])),
                layer=str(entry.get("layer") or _LEGACY_NGA_LAYER),
                building_count_col=str(
                    entry.get("building_count_col") or _DEFAULT_BUILDING_COUNT_COL
                ),
                country_name=str(entry.get("country_name") or country_name or iso2.upper()),
                iso3=str(entry.get("iso3") or "").upper(),
            )

    raise SettlementDataUnavailable(country=country_name or iso2.upper(), available=available)
