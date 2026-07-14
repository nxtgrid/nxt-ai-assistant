import matplotlib.pyplot as plt
from shapely.geometry import Polygon

from shared.mapping import plot_builder as pb
from shared.mapping.models import Building, SiteBoundary


def test_adaptive_zoom_reduces_zoom_for_large_bounds():
    small = SiteBoundary.from_polygon(
        Polygon([(4.58, 6.81), (4.59, 6.81), (4.59, 6.82), (4.58, 6.82)])
    )
    large = SiteBoundary.from_polygon(
        Polygon([(4.58, 6.81), (4.68, 6.81), (4.68, 6.91), (4.58, 6.91)])
    )

    assert pb._compute_adaptive_zoom(small) >= pb._compute_adaptive_zoom(large)
    assert pb._compute_adaptive_zoom(large) < 16


def test_add_buildings_uses_patch_collection():
    fig, ax = plt.subplots()
    buildings = [
        Building(
            coordinates=[
                [4.58, 6.81],
                [4.581, 6.81],
                [4.581, 6.811],
                [4.58, 6.81],
            ],
            connected=True,
        ),
        Building(
            coordinates=[
                [4.582, 6.812],
                [4.583, 6.812],
                [4.583, 6.813],
                [4.582, 6.812],
            ],
            connected=False,
        ),
    ]

    try:
        pb.add_buildings(ax, buildings)

        assert len(ax.collections) == 2
        assert len(ax.patches) == 0
    finally:
        plt.close(fig)
