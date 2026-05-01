"""Tests for site layout geometry engine and supporting utilities."""

import math

import pytest
from shapely.geometry import Polygon

from shared.site_layout import (
    ARRAY_FENCE_SETBACK_M,
    DEFAULT_BOX_HEIGHT_M,
    DEFAULT_BOX_WIDTH_M,
    DEFAULT_PANELS_PER_BOX,
    ESS_MAX_MODULES,
    FENCE_SETBACK_M,
    PanelArray,
    SiteLayout,
    generate_site_layout,
    parse_panel_config,
)


class TestParseConfig:
    def test_standard_configs(self):
        assert parse_panel_config("5S2P") == (5, 2)
        assert parse_panel_config("17S2P") == (17, 2)
        assert parse_panel_config("10S4P") == (10, 4)

    def test_case_insensitive(self):
        assert parse_panel_config("5s2p") == (5, 2)
        assert parse_panel_config("17s2p") == (17, 2)

    def test_whitespace_stripped(self):
        assert parse_panel_config("  5S2P  ") == (5, 2)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_panel_config("invalid")
        with pytest.raises(ValueError):
            parse_panel_config("5S")
        with pytest.raises(ValueError):
            parse_panel_config("S2P")
        with pytest.raises(ValueError):
            parse_panel_config("")


class TestPanelArray:
    def test_default_box_dimensions(self):
        arr = PanelArray(0, 0)
        assert arr.array_width == DEFAULT_BOX_WIDTH_M
        assert arr.array_height == DEFAULT_BOX_HEIGHT_M
        assert arr.panel_count == DEFAULT_PANELS_PER_BOX

    def test_custom_box_dimensions(self):
        arr = PanelArray(0, 0, panel_count=10, box_width=8.0, box_height=3.0)
        assert arr.array_width == 8.0
        assert arr.array_height == 3.0
        assert arr.panel_count == 10


class TestComputeSiteLayout:
    def _make_boundary(self, w: float, h: float) -> Polygon:
        """Create a rectangular boundary."""
        return Polygon([(0, 0), (w, 0), (w, h), (0, h)])

    def test_victron_basic(self):
        boundary = self._make_boundary(46, 43)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=50,
            site_type="victron",
            latitude=9.0,
            site_name="TestVictron",
        )
        assert isinstance(layout, SiteLayout)
        assert layout.energy_system_type == "victron"
        assert layout.total_modules > 0
        assert layout.achieved_kwp > 0
        assert len(layout.arrays) > 0
        assert layout.site_name == "TestVictron"

    def test_ess_basic(self):
        boundary = self._make_boundary(60, 50)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=100,
            site_type="ess",
            latitude=-2.0,
            site_name="TestESS",
            ess_module_count=3,
        )
        assert layout.energy_system_type == "ess"
        assert len(layout.ess_modules) > 0
        assert len(layout.ess_modules) <= ESS_MAX_MODULES
        assert layout.ess_plinth_rect is not None

    def test_arrays_have_plinths(self):
        boundary = self._make_boundary(46, 43)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=50,
            site_type="victron",
            latitude=9.0,
            site_name="Test",
        )
        for arr in layout.arrays:
            assert len(arr.plinths) > 0
            expected_plinths = math.ceil(arr.panel_count / 3)
            assert len(arr.plinths) == expected_plinths

    def test_plinth_count(self):
        boundary = self._make_boundary(80, 60)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=100,
            site_type="ess",
            latitude=5.0,
            site_name="TestPlinths",
            ess_module_count=3,
        )
        if layout.arrays:
            arr = layout.arrays[0]
            expected_plinths = math.ceil(arr.panel_count / 3)
            assert len(arr.plinths) == expected_plinths

    def test_lightning_covers_assets(self):
        boundary = self._make_boundary(46, 43)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=50,
            site_type="victron",
            latitude=9.0,
            site_name="Test",
        )
        assert len(layout.lightning_positions) > 0

    def test_fence_created(self):
        boundary = self._make_boundary(46, 43)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=50,
            site_type="victron",
            latitude=9.0,
            site_name="Test",
        )
        assert layout.fence is not None
        assert layout.fence.area < boundary.area

    def test_entrance_on_longest_edge(self):
        boundary = self._make_boundary(46, 43)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=50,
            site_type="victron",
            latitude=9.0,
            site_name="Test",
        )
        # Longest edge is 46m (the bottom), midpoint should be at x=23
        assert abs(layout.entrance_pos[0] - 23) < 1.0

    def test_small_boundary_warning(self):
        boundary = self._make_boundary(10, 10)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=500,
            site_type="ess",
            latitude=5.0,
            site_name="TinyTest",
            ess_module_count=3,
        )
        assert len(layout.warnings) > 0

    def test_invalid_site_type(self):
        boundary = self._make_boundary(46, 43)
        with pytest.raises(ValueError, match="site_type"):
            generate_site_layout(
                boundary=boundary,
                target_kwp=50,
                site_type="invalid",
                latitude=9.0,
                site_name="Test",
            )

    def test_arrays_within_boundary(self):
        boundary = self._make_boundary(46, 43)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=50,
            site_type="victron",
            latitude=9.0,
            site_name="Test",
        )
        setback = FENCE_SETBACK_M + ARRAY_FENCE_SETBACK_M
        for arr in layout.arrays:
            assert arr.origin_x >= setback - 0.01
            assert arr.origin_y >= setback - 0.01
            assert arr.origin_x + arr.array_width <= 46 - setback + 0.01
            assert arr.origin_y + arr.array_height <= 43 - setback + 0.01

    def test_irregular_boundary(self):
        """Test with an L-shaped boundary."""
        boundary = Polygon(
            [
                (0, 0),
                (30, 0),
                (30, 20),
                (15, 20),
                (15, 40),
                (0, 40),
            ]
        )
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=30,
            site_type="victron",
            latitude=9.0,
            site_name="LShape",
        )
        assert isinstance(layout, SiteLayout)
        assert layout.total_modules >= 0

    def test_custom_box_dimensions(self):
        """Test with custom box size."""
        boundary = self._make_boundary(60, 50)
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=50,
            site_type="victron",
            latitude=9.0,
            site_name="CustomBox",
            panels_per_box=10,
            box_width=8.0,
            box_height=3.0,
        )
        assert isinstance(layout, SiteLayout)
        for arr in layout.arrays:
            assert arr.box_width == 8.0
            assert arr.box_height == 3.0
            assert arr.panel_count == 10


class TestCableRouting:
    """Tests for diagonal cable routing with obstacle avoidance and bunching."""

    def _make_layout(self, w=60, h=50, target_kwp=100, site_type="ess") -> SiteLayout:
        boundary = Polygon([(0, 0), (w, 0), (w, h), (0, h)])
        return generate_site_layout(
            boundary=boundary,
            target_kwp=target_kwp,
            site_type=site_type,
            latitude=9.0,
            site_name="CableTest",
            ess_module_count=3,
        )

    def test_cables_avoid_plinths(self):
        """No cable segment passes within 0.5m of same-row neighbor plinths."""
        from shared.site_layout.geometry import _point_to_segment_dist

        layout = self._make_layout()

        # Group arrays by row
        row_map: dict[float, list[int]] = {}
        for i, arr in enumerate(layout.arrays):
            key = round(arr.origin_y, 1)
            row_map.setdefault(key, []).append(i)

        dc_routes = [r for r in layout.cable_routes if r.cable_type == "dc"]
        for route_idx, route in enumerate(dc_routes):
            arr = layout.arrays[route_idx]
            row_key = round(arr.origin_y, 1)

            # Collect same-row neighbor plinths, excluding exit-adjacent ones
            neighbor_plinths = []
            for ni in row_map.get(row_key, []):
                if ni == route_idx:
                    continue
                for px, py in layout.arrays[ni].plinths:
                    cx, cy = px + 0.15, py + 0.15
                    if math.hypot(cx - route.start[0], cy - route.start[1]) > 0.8:
                        neighbor_plinths.append((cx, cy))

            pts = [route.start] + route.waypoints + [route.end]
            for i in range(len(pts) - 1):
                x1, y1 = pts[i]
                x2, y2 = pts[i + 1]
                for pcx, pcy in neighbor_plinths:
                    dist = _point_to_segment_dist(pcx, pcy, x1, y1, x2, y2)
                    assert dist >= 0.49, (
                        f"Cable {route.label} segment ({x1:.1f},{y1:.1f})-({x2:.1f},{y2:.1f}) "
                        f"passes {dist:.2f}m from plinth at ({pcx:.1f},{pcy:.1f})"
                    )

    def test_bunch_max_4(self):
        """No bunch has more than 4 cables."""
        layout = self._make_layout(w=80, h=60, target_kwp=200)
        from collections import Counter

        bunch_counts = Counter(r.bunch_id for r in layout.cable_routes if r.cable_type == "dc")
        for bunch_id, count in bunch_counts.items():
            assert count <= 4, f"Bunch {bunch_id} has {count} cables (max 4)"

    def test_cable_routes_per_array(self):
        """One DC route per array, one AC route total."""
        layout = self._make_layout()
        dc_routes = [r for r in layout.cable_routes if r.cable_type == "dc"]
        ac_routes = [r for r in layout.cable_routes if r.cable_type == "ac"]
        assert len(dc_routes) == len(layout.arrays)
        assert len(ac_routes) == 1

    def test_cable_no_obstacles(self):
        """Route through clear space has no detour waypoints."""
        # Single array far from ESS — direct diagonal path likely clear
        layout = self._make_layout(w=60, h=50, target_kwp=9)
        dc_routes = [r for r in layout.cable_routes if r.cable_type == "dc"]
        # At least one DC route should exist
        assert len(dc_routes) >= 1
        # AC route should always be direct (no waypoints)
        ac_routes = [r for r in layout.cable_routes if r.cable_type == "ac"]
        assert len(ac_routes[0].waypoints) == 0

    def test_cable_nearest_edge(self):
        """Route starts from the closer short edge to ESS."""
        layout = self._make_layout()
        ex, ey, ew, eh = layout.energy_system_rect
        ess_cx = ex + ew / 2
        ess_cy = ey + eh / 2

        dc_routes = [r for r in layout.cable_routes if r.cable_type == "dc"]
        # Routes are in same order as arrays
        for idx, route in enumerate(dc_routes):
            arr = layout.arrays[idx]
            arr_cy = arr.origin_y + arr.array_height / 2
            left_edge = (arr.origin_x, arr_cy)
            right_edge = (arr.origin_x + arr.array_width, arr_cy)
            dist_left = math.hypot(left_edge[0] - ess_cx, left_edge[1] - ess_cy)
            dist_right = math.hypot(right_edge[0] - ess_cx, right_edge[1] - ess_cy)
            if dist_left <= dist_right:
                assert abs(route.start[0] - arr.origin_x) < 0.01
            else:
                assert abs(route.start[0] - (arr.origin_x + arr.array_width)) < 0.01


class TestRenderers:
    """Quick smoke tests that renderers don't crash."""

    def _make_layout(self) -> SiteLayout:
        boundary = Polygon([(0, 0), (46, 0), (46, 43), (0, 43)])
        return generate_site_layout(
            boundary=boundary,
            target_kwp=50,
            site_type="victron",
            latitude=9.0,
            site_name="RendererTest",
        )

    def test_drawio_produces_xml(self):
        from shared.site_layout.drawio_renderer import render_drawio

        layout = self._make_layout()
        xml_str = render_drawio(layout)
        assert xml_str.startswith("<?xml")
        assert "mxGraphModel" in xml_str
        assert "mxCell" in xml_str
        assert "RendererTest" in xml_str
        assert "group" in xml_str

    def test_png_produces_base64(self):
        from shared.site_layout.png_renderer import render_png

        layout = self._make_layout()
        b64 = render_png(layout)
        assert len(b64) > 100
        import base64

        raw = base64.b64decode(b64)
        assert raw[:4] == b"\x89PNG"

    def test_ess_drawio(self):
        from shared.site_layout.drawio_renderer import render_drawio

        boundary = Polygon([(0, 0), (60, 0), (60, 50), (0, 50)])
        layout = generate_site_layout(
            boundary=boundary,
            target_kwp=100,
            site_type="ess",
            latitude=-5.0,
            site_name="ESSRenderer",
            ess_module_count=3,
        )
        xml_str = render_drawio(layout)
        assert "ESS" in xml_str


class TestMagneticTrenches:
    """Tests for proximity-based cable merging (magnetic trenches)."""

    def test_magnetic_trench_merges(self):
        """Two solo cables from adjacent rows that converge should merge into the same bunch."""
        from shared.site_layout.geometry import CableRoute, _apply_magnetic_trenches

        # Two DC cables from adjacent rows, converging toward the same endpoint
        routes = [
            CableRoute(
                start=(30.0, 46.0),
                end=(5.0, 5.0),
                waypoints=[],
                cable_type="dc",
                length_m=math.hypot(25, 41),
                label="DC-1",
            ),
            CableRoute(
                start=(30.0, 41.0),
                end=(5.0, 5.0),
                waypoints=[],
                cable_type="dc",
                length_m=math.hypot(25, 36),
                label="DC-2",
            ),
        ]
        routes[0].bunch_id = 1
        routes[1].bunch_id = 2

        _apply_magnetic_trenches(routes, merge_dist=6.0, min_end_dist=8.0)
        assert routes[0].bunch_id == routes[1].bunch_id, (
            "Converging cables should be merged into the same bunch"
        )

    def test_magnetic_trench_respects_max(self):
        """Cables don't merge if it would exceed 4 per bunch."""
        from shared.site_layout.geometry import CableRoute, _apply_magnetic_trenches

        # Create 4 cables already in bunch 1, plus 1 in bunch 2 that converges
        routes = []
        for i in range(4):
            r = CableRoute(
                start=(30.0 + i, 46.0),
                end=(5.0, 5.0),
                waypoints=[],
                cable_type="dc",
                length_m=40.0,
                label=f"DC-{i + 1}",
            )
            r.bunch_id = 1
            routes.append(r)

        joiner = CableRoute(
            start=(30.0, 41.0),
            end=(5.0, 5.0),
            waypoints=[],
            cable_type="dc",
            length_m=38.0,
            label="DC-5",
        )
        joiner.bunch_id = 2
        routes.append(joiner)

        _apply_magnetic_trenches(routes, merge_dist=6.0, min_end_dist=8.0)
        assert routes[4].bunch_id == 2, "Cable should not merge when it would exceed max_per_bunch"

    def test_magnetic_trench_min_end_dist(self):
        """Cables that only converge within 8m of the ESS endpoint are NOT merged."""
        from shared.site_layout.geometry import CableRoute, _apply_magnetic_trenches

        # Two short cables that only get close near their shared endpoint
        routes = [
            CableRoute(
                start=(8.0, 7.0),
                end=(5.0, 5.0),
                waypoints=[],
                cable_type="dc",
                length_m=math.hypot(3, 2),
                label="DC-1",
            ),
            CableRoute(
                start=(8.0, 4.0),
                end=(5.0, 5.0),
                waypoints=[],
                cable_type="dc",
                length_m=math.hypot(3, 1),
                label="DC-2",
            ),
        ]
        routes[0].bunch_id = 1
        routes[1].bunch_id = 2

        _apply_magnetic_trenches(routes, merge_dist=3.0, min_end_dist=8.0)
        assert routes[0].bunch_id != routes[1].bunch_id, (
            "Cables converging only near the endpoint should not be merged"
        )
