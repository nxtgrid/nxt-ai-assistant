"""Tests for OSM community-name resolution in community_detector._get_osm_name.

Regression for rural anchors where OSM has no settlement-level node
(hamlet/village/town/city) and only an LGA/county name. Such anchors must
resolve to the county name (e.g. "Pankshin") rather than the literal "Unknown",
which would otherwise propagate as the site label downstream in the LPP flow.
"""

from types import SimpleNamespace

from shared.layout.community_detector import _get_osm_name


class _FakeGeolocator:
    """Minimal Nominatim stand-in returning a fixed address dict."""

    def __init__(self, address):
        self._address = address

    def reverse(self, *_args, **_kwargs):
        if self._address is None:
            return None
        return SimpleNamespace(raw={"address": self._address})


def test_county_only_resolves_to_county_not_unknown():
    # Real Nominatim response for (9.3947551, 9.3176320): only county is present.
    addr = {
        "county": "Pankshin",
        "state": "Plateau State",
        "ISO3166-2-lvl4": "NG-PL",
        "country": "Nigeria",
        "country_code": "ng",
    }
    assert _get_osm_name(_FakeGeolocator(addr), 9.3947551, 9.3176320) == "Pankshin"


def test_settlement_name_preferred_over_county():
    addr = {"village": "Garin Dadi", "county": "Pankshin", "state": "Plateau State"}
    assert _get_osm_name(_FakeGeolocator(addr), 9.0, 9.0) == "Garin Dadi"


def test_sub_settlement_preferred_over_admin():
    addr = {"suburb": "Tudun Wada", "county": "Pankshin", "state": "Plateau State"}
    assert _get_osm_name(_FakeGeolocator(addr), 9.0, 9.0) == "Tudun Wada"


def test_only_state_and_country_returns_state():
    # State is the last-resort fallback before "Unknown".
    addr = {"state": "Plateau State", "country": "Nigeria"}
    assert _get_osm_name(_FakeGeolocator(addr), 9.0, 9.0) == "Plateau State"


def test_no_address_returns_unknown():
    assert _get_osm_name(_FakeGeolocator(None), 0.0, 0.0) == "Unknown"


def test_truly_empty_address_returns_unknown():
    assert _get_osm_name(_FakeGeolocator({}), 9.0, 9.0) == "Unknown"
