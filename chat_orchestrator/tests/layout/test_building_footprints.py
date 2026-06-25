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


def test_fetch_google_filters_confidence_and_clips():
    from shared.layout import building_footprints as bf

    boundary = _Polygon([(4.58, 6.81), (4.60, 6.81), (4.60, 6.83), (4.58, 6.83)])
    # Google CSV columns: latitude,longitude,area_in_meters,confidence,geometry,full_plus_code
    poly_in = "POLYGON((4.59 6.82, 4.5901 6.82, 4.5901 6.8201, 4.59 6.82))"
    poly_low_conf = "POLYGON((4.591 6.821, 4.5911 6.821, 4.5911 6.8211, 4.591 6.821))"
    csv_text = (
        '6.82,4.59,40,0.92,"%s",abc\n' % poly_in + '6.821,4.591,40,0.30,"%s",def\n' % poly_low_conf
    )
    with (
        patch.object(bf, "_google_tile_urls_for_boundary", return_value=["http://x/tile.csv.gz"]),
        patch.object(bf, "_http_get_text", return_value=csv_text),
    ):
        fc = bf.fetch_google_open_buildings(boundary, min_confidence=0.70)

    assert len(fc["features"]) == 1  # low-confidence dropped, in-boundary kept


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
