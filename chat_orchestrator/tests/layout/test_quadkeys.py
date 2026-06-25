from shared.layout.quadkeys import bbox_to_quadkeys, latlon_to_tile, tile_to_quadkey


def test_tile_to_quadkey_known_value():
    # Lat/lon near Ikpoba (Nigeria) at zoom 9 — verified against pyquadkey2.
    tx, ty = latlon_to_tile(6.824423, 4.590881, 9)
    assert tile_to_quadkey(tx, ty, 9) == "122220330"


def test_bbox_to_quadkeys_returns_sorted_unique():
    keys = bbox_to_quadkeys(4.58, 6.81, 4.60, 6.83, 9)
    assert keys == sorted(set(keys))
    assert all(len(k) == 9 for k in keys)
    assert "122220330" in keys
