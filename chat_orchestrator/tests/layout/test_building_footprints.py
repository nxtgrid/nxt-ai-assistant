import time

from shapely.geometry import Polygon

from shared.layout.building_footprints import FootprintResult, _quadkeys_for_boundary


def test_quadkeys_for_boundary_covers_bbox():
    boundary = Polygon([(4.58, 6.81), (4.60, 6.81), (4.60, 6.83), (4.58, 6.83)])
    keys = _quadkeys_for_boundary(boundary)
    assert len(keys) >= 1
    assert all(len(k) == 9 for k in keys)


def test_footprint_result_count_matches_features():
    fc = {"type": "FeatureCollection", "features": [{"x": 1}, {"x": 2}]}
    r = FootprintResult(buildings_geojson=fc, source="microsoft", ms_count=2, google_count=0)
    assert r.count == 2


import json as _json  # noqa: E402
from unittest.mock import patch  # noqa: E402

from shapely.geometry import Polygon as _Polygon  # noqa: E402


def _ms_line(lon, lat):
    # Microsoft tiles are newline-delimited GeoJSON Features (one per line).
    return _json.dumps(
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [lon, lat],
                        [lon + 0.0001, lat],
                        [lon + 0.0001, lat + 0.0001],
                        [lon, lat],
                    ]
                ],
            },
            "properties": {},
        }
    )


def test_fetch_ms_clips_to_boundary():
    from shared.layout import building_footprints as bf

    boundary = _Polygon([(4.58, 6.81), (4.60, 6.81), (4.60, 6.83), (4.58, 6.83)])
    inside_feat = _json.loads(_ms_line(4.59, 6.82))  # inside boundary
    outside_feat = _json.loads(_ms_line(4.70, 6.95))  # outside boundary

    # _stream_ndjson_features already clips to boundary — return only the inside feature.
    def fake_stream(url, boundary):
        from shapely.geometry import shape

        return [f for f in [inside_feat, outside_feat] if shape(f["geometry"]).intersects(boundary)]

    with (
        patch.object(bf, "_quadkeys_for_boundary", return_value=["122220330"]),
        patch.object(bf, "_ms_tile_url_for_quadkey", return_value="http://x/tile"),
        patch.object(bf, "_stream_ndjson_features", side_effect=fake_stream),
    ):
        fc = bf.fetch_ms_footprints(boundary)

    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1  # only the inside building survives the clip


import io as _io  # noqa: E402


class _FakeRaw(_io.BytesIO):
    """BytesIO subclass so requests' `.raw.decode_content = False` can be set.

    The plain BytesIO C type rejects arbitrary attributes; subclassing gives it
    a __dict__. TextIOWrapper reads from it like a real streamed response body.
    """


class _FakeStreamResponse:
    """Minimal stand-in for `requests.get(url, stream=True)` (a context manager)."""

    def __init__(self, body: bytes):
        self.raw = _FakeRaw(body)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def raise_for_status(self):
        pass


def test_fetch_google_filters_confidence_and_clips():
    from shared.layout import building_footprints as bf

    boundary = _Polygon([(4.58, 6.81), (4.60, 6.81), (4.60, 6.83), (4.58, 6.83)])
    # Google CSV columns: latitude,longitude,area_in_meters,confidence,geometry,full_plus_code
    poly_in = "POLYGON((4.59 6.82, 4.5901 6.82, 4.5901 6.8201, 4.59 6.82))"
    poly_low_conf = "POLYGON((4.591 6.821, 4.5911 6.821, 4.5911 6.8211, 4.591 6.821))"
    csv_text = (
        '6.82,4.59,40,0.92,"%s",abc\n' % poly_in + '6.821,4.591,40,0.30,"%s",def\n' % poly_low_conf
    )
    # Non-.gz URL → _stream_google_features reads resp.raw directly (no gzip layer).
    with (
        patch.object(bf, "_google_tile_urls_for_boundary", return_value=["http://x/tile.csv"]),
        patch.object(bf.requests, "get", return_value=_FakeStreamResponse(csv_text.encode())),
    ):
        fc = bf.fetch_google_open_buildings(boundary, min_confidence=0.70)

    assert len(fc["features"]) == 1  # low-confidence dropped, in-boundary kept


def test_fetch_google_skips_wkt_parse_for_rows_outside_bbox():
    """Rows whose centroid is outside the boundary bbox must be rejected BEFORE
    the WKT parse — this cheap pre-filter is what keeps the huge S2 tiles from
    OOM-ing the instance (a single tile is 100MB+ and spans a far larger area
    than the community). Regression for the silent OOM restart on /lpp."""
    from shared.layout import building_footprints as bf

    boundary = _Polygon([(4.58, 6.81), (4.60, 6.81), (4.60, 6.83), (4.58, 6.83)])
    poly_in = "POLYGON((4.59 6.82, 4.5901 6.82, 4.5901 6.8201, 4.59 6.82))"
    # Centroid (lat=9.99, lon=9.99) is far outside the bbox; its geometry must
    # never reach wkt.loads. Use junk WKT so a parse attempt would surface.
    csv_text = (
        '6.82,4.59,40,0.95,"%s",in\n' % poly_in
        + '9.99,9.99,40,0.95,"NOT-WKT-SHOULD-NOT-PARSE",out\n'
    )
    import shapely.wkt

    parsed: list[str] = []
    real_loads = shapely.wkt.loads

    def tracking_loads(wkt_str):
        parsed.append(wkt_str)
        return real_loads(wkt_str)

    with (
        patch.object(bf, "_google_tile_urls_for_boundary", return_value=["http://x/tile.csv"]),
        patch.object(bf.requests, "get", return_value=_FakeStreamResponse(csv_text.encode())),
        patch.object(shapely.wkt, "loads", side_effect=tracking_loads),
    ):
        fc = bf.fetch_google_open_buildings(boundary, min_confidence=0.70)

    assert len(fc["features"]) == 1
    assert parsed == [poly_in]  # only the in-bbox row was ever WKT-parsed


def test_reconcile_keeps_ms_when_coverage_sufficient():
    from shared.layout import building_footprints as bf

    boundary = _Polygon([(4.58, 6.81), (4.60, 6.81), (4.60, 6.83), (4.58, 6.83)])
    ms_fc = {"type": "FeatureCollection", "features": [{"i": i} for i in range(90)]}
    with (
        patch.object(bf, "fetch_ms_footprints", return_value=ms_fc),
        patch.object(bf, "fetch_google_open_buildings") as google,
    ):
        res = bf.fetch_building_footprints(boundary, grid3_estimate=100, crosscheck_min_ratio=0.80)
    google.assert_not_called()  # 90/100 >= 0.80 → no cross-check
    assert res.source == "microsoft"
    assert res.count == 90


def test_reconcile_uses_google_when_ms_thin():
    from shared.layout import building_footprints as bf

    boundary = _Polygon([(4.58, 6.81), (4.60, 6.81), (4.60, 6.83), (4.58, 6.83)])
    ms_fc = {"type": "FeatureCollection", "features": [{"i": i} for i in range(40)]}
    google_fc = {"type": "FeatureCollection", "features": [{"j": j} for j in range(95)]}
    with (
        patch.object(bf, "fetch_ms_footprints", return_value=ms_fc),
        patch.object(bf, "fetch_google_open_buildings", return_value=google_fc),
    ):
        res = bf.fetch_building_footprints(boundary, grid3_estimate=100, crosscheck_min_ratio=0.80)
    assert res.source == "google"  # denser set wins
    assert res.count == 95
    assert res.ms_count == 40 and res.google_count == 95


def test_reconcile_runs_google_speculatively_for_large_estimates():
    from shared.layout import building_footprints as bf

    boundary = _Polygon([(4.58, 6.81), (4.60, 6.81), (4.60, 6.83), (4.58, 6.83)])
    ms_fc = {"type": "FeatureCollection", "features": [{"i": i} for i in range(100)]}
    google_fc = {"type": "FeatureCollection", "features": [{"j": j} for j in range(450)]}

    def slow_ms(_boundary):
        time.sleep(0.2)
        return ms_fc

    def slow_google(_boundary, min_confidence):
        time.sleep(0.2)
        return google_fc

    started = time.monotonic()
    with (
        patch.object(bf, "fetch_ms_footprints", side_effect=slow_ms),
        patch.object(bf, "fetch_google_open_buildings", side_effect=slow_google),
    ):
        res = bf.fetch_building_footprints(
            boundary,
            grid3_estimate=500,
            crosscheck_min_ratio=0.80,
        )
    elapsed = time.monotonic() - started

    assert res.source == "google"
    assert res.count == 450
    assert elapsed < 0.35
