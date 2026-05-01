"""Tests for the distribution layout engine.

Tests the core layout algorithm modules: road_network, distribution,
output_formatter, and the top-level generate_layout orchestrator.

All tests use synthetic geometry to avoid OSMnx/Overpass API dependencies.
"""

import json
from unittest.mock import patch

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point, Polygon

from shared.layout.distribution import (
    POLE_DEDUP_DISTANCE_M,
    _bridge_disconnected_components,
    _compute_snapped_positions,
    build_pole_adjacency,
    connect_buildings,
    optimize_backbone,
    place_poles_along_roads,
)
from shared.layout.output_formatter import format_layout_output
from shared.layout.road_network import (
    PlantSite,
    RoadNetworkResult,
    SiteSelectionResult,
    _deduplicate_directed_edges,
    _deduplicate_nodes_by_proximity,
    _filter_redundant_path_edges,
    _snap_edge_endpoints,
    augment_road_network,
    detect_building_paths,
    find_plant_sites,
    locate_power_plant,
)
from shared.mapping.data_reader import extract_cables, extract_meta, extract_poles

# --- Fixtures ---

# A simple projected CRS for testing (UTM zone 33N)
TEST_CRS = "EPSG:32633"


@pytest.fixture
def simple_road_edges():
    """Two intersecting roads forming an L-shape, each ~200m long."""
    line1 = LineString([(500000, 1000000), (500200, 1000000)])  # horizontal
    line2 = LineString([(500100, 999900), (500100, 1000100)])  # vertical
    gdf = gpd.GeoDataFrame(
        {"geometry": [line1, line2]},
        crs=TEST_CRS,
    )
    return gdf


@pytest.fixture
def simple_road_nodes():
    """Intersection + endpoints for the L-shape road."""
    nodes = {
        0: {"geometry": Point(500000, 1000000)},
        1: {"geometry": Point(500200, 1000000)},
        2: {"geometry": Point(500100, 999900)},
        3: {"geometry": Point(500100, 1000100)},
        4: {"geometry": Point(500100, 1000000)},  # intersection
    }
    gdf = gpd.GeoDataFrame.from_dict(nodes, orient="index")
    gdf = gdf.set_geometry("geometry")
    gdf.crs = TEST_CRS
    return gdf


@pytest.fixture
def simple_road_graph():
    """NetworkX graph matching the L-shape roads with length weights."""
    G = nx.Graph()
    # OSMnx requires 'crs' on graph for ox.distance.nearest_nodes
    G.graph["crs"] = TEST_CRS
    G.add_node(0, x=500000, y=1000000)
    G.add_node(1, x=500200, y=1000000)
    G.add_node(2, x=500100, y=999900)
    G.add_node(3, x=500100, y=1000100)
    G.add_node(4, x=500100, y=1000000)
    G.add_edge(0, 4, length=100.0, geometry=LineString([(500000, 1000000), (500100, 1000000)]))
    G.add_edge(4, 1, length=100.0, geometry=LineString([(500100, 1000000), (500200, 1000000)]))
    G.add_edge(2, 4, length=100.0, geometry=LineString([(500100, 999900), (500100, 1000000)]))
    G.add_edge(4, 3, length=100.0, geometry=LineString([(500100, 1000000), (500100, 1000100)]))
    return G


@pytest.fixture
def four_buildings_gdf():
    """Four building centroids near the road intersection, within 40m."""
    buildings = [
        Point(500090, 1000030),  # 30m from road
        Point(500110, 999970),  # 30m from road
        Point(500050, 1000020),  # 20m from road
        Point(500150, 1000010),  # 10m from road
    ]
    return gpd.GeoDataFrame({"geometry": buildings}, crs=TEST_CRS)


@pytest.fixture
def four_buildings_geojson():
    """Four buildings as GeoJSON FeatureCollection (polygon form)."""
    features = []
    # Small square buildings near the road intersection
    offsets = [
        (500085, 1000025),
        (500105, 999965),
        (500045, 1000015),
        (500145, 1000005),
    ]
    for ox, oy in offsets:
        features.append(
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [ox, oy],
                            [ox + 10, oy],
                            [ox + 10, oy + 10],
                            [ox, oy + 10],
                            [ox, oy],
                        ]
                    ],
                },
                "properties": {},
            }
        )
    return {"type": "FeatureCollection", "features": features}


# --- Pole Placement Tests ---


class TestPlacePolesAlongRoads:
    def test_places_poles_at_spacing(self, simple_road_edges, simple_road_nodes):
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=50.0)
        assert len(poles) > 0
        assert "pole_type" in poles.columns
        assert "road_node_id" in poles.columns

    def test_includes_intersection_poles(self, simple_road_edges, simple_road_nodes):
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=50.0)
        intersection_poles = poles[poles["pole_type"] == "intersection"]
        assert len(intersection_poles) == len(simple_road_nodes)

    def test_deduplicates_nearby_poles(self, simple_road_edges, simple_road_nodes):
        # With very tight spacing, there will be many duplicates near intersections
        poles_tight = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=10.0)
        poles_wide = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=100.0)
        # Tighter spacing should produce more poles
        assert len(poles_tight) > len(poles_wide)

    def test_empty_roads_returns_empty(self):
        empty_edges = gpd.GeoDataFrame(columns=["geometry"], crs=TEST_CRS)
        empty_nodes = gpd.GeoDataFrame(columns=["geometry"], crs=TEST_CRS)
        empty_nodes.index.name = None
        poles = place_poles_along_roads(empty_edges, empty_nodes, spacing_m=45.0)
        assert len(poles) == 0


# --- Building Connection Tests ---


class TestConnectBuildings:
    def test_connects_nearby_buildings(
        self, four_buildings_gdf, simple_road_edges, simple_road_nodes
    ):
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=45.0)
        drop_cables, updated_bldgs, coverage = connect_buildings(
            four_buildings_gdf, poles, max_drop_distance_m=40.0
        )
        assert coverage > 0.0
        assert len(drop_cables) > 0
        assert "cable_type" in drop_cables.columns
        assert all(drop_cables["cable_type"] == "drop")

    def test_coverage_100_when_all_near(self, simple_road_edges, simple_road_nodes):
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=20.0)
        # Buildings very close to road
        close_buildings = gpd.GeoDataFrame(
            {"geometry": [Point(500100, 1000005), Point(500050, 1000002)]},
            crs=TEST_CRS,
        )
        _, _, coverage = connect_buildings(close_buildings, poles, max_drop_distance_m=40.0)
        assert coverage == 100.0

    def test_coverage_0_when_all_far(self, simple_road_edges, simple_road_nodes):
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=45.0)
        # Buildings very far from any road
        far_buildings = gpd.GeoDataFrame(
            {"geometry": [Point(501000, 1001000), Point(502000, 1002000)]},
            crs=TEST_CRS,
        )
        _, _, coverage = connect_buildings(far_buildings, poles, max_drop_distance_m=40.0)
        assert coverage == 0.0

    def test_drop_cable_lengths_are_distances(
        self, four_buildings_gdf, simple_road_edges, simple_road_nodes
    ):
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=20.0)
        drop_cables, _, _ = connect_buildings(four_buildings_gdf, poles, max_drop_distance_m=50.0)
        for _, cable in drop_cables.iterrows():
            assert cable["length_meters"] > 0
            assert cable["length_meters"] <= 50.0


# --- Backbone Optimization Tests ---


class TestOptimizeBackbone:
    def test_produces_backbone_cables(
        self, simple_road_graph, simple_road_edges, simple_road_nodes, four_buildings_gdf
    ):
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=45.0)
        drop_cables, _, _ = connect_buildings(four_buildings_gdf, poles, max_drop_distance_m=40.0)
        if len(drop_cables) == 0:
            pytest.skip("No drop cables — buildings too far for this geometry")

        plant = Point(500000, 1000000)
        backbone, _ = optimize_backbone(
            simple_road_graph, poles, drop_cables, plant, edges_gdf=simple_road_edges
        )
        assert len(backbone) > 0
        assert all(backbone["cable_type"] == "backbone")

    def test_empty_drop_cables_returns_empty(
        self, simple_road_graph, simple_road_edges, simple_road_nodes
    ):
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=45.0)
        empty_drops = gpd.GeoDataFrame(
            columns=["geometry", "length_meters", "cable_type", "pole_idx"],
            crs=TEST_CRS,
        )
        plant = Point(500000, 1000000)
        backbone, _ = optimize_backbone(
            simple_road_graph, poles, empty_drops, plant, edges_gdf=simple_road_edges
        )
        assert len(backbone) == 0

    def test_radial_property_shortest_paths(self, simple_road_graph, simple_road_edges):
        """Every terminal's backbone path should follow a connected tree from plant."""
        plant = Point(500000, 1000000)

        # Create poles at all graph nodes and fake drop cables to terminals
        all_node_ids = list(simple_road_graph.nodes)
        poles_data = []
        for nid in all_node_ids:
            nd = simple_road_graph.nodes[nid]
            poles_data.append(
                {"geometry": Point(nd["x"], nd["y"]), "pole_type": "test", "road_node_id": nid}
            )
        poles_gdf = gpd.GeoDataFrame(poles_data, crs=TEST_CRS)

        # Create drop cables referencing pole indices (all except plant)
        drop_rows = [
            {
                "geometry": LineString([(0, 0), (1, 1)]),
                "length_meters": 1.0,
                "cable_type": "drop",
                "pole_idx": i,
            }
            for i in range(len(poles_data))
            if i != 0
        ]
        drop_cables = gpd.GeoDataFrame(drop_rows, crs=TEST_CRS)

        backbone, updated_poles = optimize_backbone(
            simple_road_graph, poles_gdf, drop_cables, plant, edges_gdf=simple_road_edges
        )
        assert len(backbone) > 0

        # Verify all backbone segments are straight 2-point lines
        for _, row in backbone.iterrows():
            coords = list(row.geometry.coords)
            assert len(coords) == 2, "Backbone cables must be straight 2-point lines"

        # Verify from_pole_idx is assigned
        assert "from_pole_idx" in updated_poles.columns

        # Reconstruct backbone graph from edges
        backbone_graph = nx.Graph()
        for _, row in backbone.iterrows():
            coords = list(row.geometry.coords)
            start, end = coords[0], coords[-1]
            backbone_graph.add_edge(start, end, length=row.length_meters)

        # The backbone is a tree — verify it's connected
        assert nx.is_connected(backbone_graph), "Backbone should be a connected tree"


# --- Coverage Iteration Tests ---


# --- Output Formatter Tests ---


class TestFormatLayoutOutput:
    def _run_pipeline(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        """Run a full pipeline to get inputs for format_layout_output."""
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=45.0)
        drops, updated_bldgs, coverage = connect_buildings(
            four_buildings_gdf,
            poles,
            max_drop_distance_m=40.0,
        )
        plant = Point(500000, 1000000)
        backbone, poles_final = optimize_backbone(
            simple_road_graph, poles, drops, plant, edges_gdf=simple_road_edges
        )
        return format_layout_output(
            poles_gdf=poles_final,
            backbone_gdf=backbone,
            drop_cables_gdf=drops,
            buildings_gdf=updated_bldgs,
            original_buildings_geojson=four_buildings_geojson,
            spacing_m=45.0,
            max_drop_distance_m=40.0,
        )

    def test_output_has_all_keys(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        result = self._run_pipeline(
            simple_road_edges,
            simple_road_nodes,
            simple_road_graph,
            four_buildings_gdf,
            four_buildings_geojson,
        )
        assert "poles_geo_flat" in result
        assert "distribution_geo_flat" in result
        assert "buildings_geo_flat" in result
        assert "meta_geo_flat" in result

    def test_poles_geojson_format(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        result = self._run_pipeline(
            simple_road_edges,
            simple_road_nodes,
            simple_road_graph,
            four_buildings_gdf,
            four_buildings_geojson,
        )
        poles = result["poles_geo_flat"]
        assert poles["type"] == "FeatureCollection"
        assert len(poles["features"]) > 0
        for feat in poles["features"]:
            assert feat["geometry"]["type"] == "Point"
            assert len(feat["geometry"]["coordinates"]) == 2

    def test_cable_type_separation(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        result = self._run_pipeline(
            simple_road_edges,
            simple_road_nodes,
            simple_road_graph,
            four_buildings_gdf,
            four_buildings_geojson,
        )
        cables = result["distribution_geo_flat"]
        types = {f["properties"]["cable_type"] for f in cables["features"]}
        # Should have at least drop cables
        assert "drop" in types or len(cables["features"]) == 0

    def test_meta_consistency(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        """backbone + drop cable length should equal total cable length."""
        result = self._run_pipeline(
            simple_road_edges,
            simple_road_nodes,
            simple_road_graph,
            four_buildings_gdf,
            four_buildings_geojson,
        )
        meta = result["meta_geo_flat"]
        total = meta["distribution_line_total_length"]
        backbone_plus_drop = meta["backbone_cable_length_m"] + meta["drop_cable_length_m"]
        assert abs(total - backbone_plus_drop) < 0.01, (
            f"Total ({total}) != backbone ({meta['backbone_cable_length_m']}) + "
            f"drop ({meta['drop_cable_length_m']})"
        )

    def test_meta_counts_consistent(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        result = self._run_pipeline(
            simple_road_edges,
            simple_road_nodes,
            simple_road_graph,
            four_buildings_gdf,
            four_buildings_geojson,
        )
        meta = result["meta_geo_flat"]
        assert meta["served_building_count"] + meta["unserved_building_count"] == 4
        assert meta["pole_count"] > 0
        assert meta["coverage_percentage"] >= 0.0
        assert meta["coverage_percentage"] <= 100.0


# --- Round-trip through data_reader.py ---


class TestRoundTripDataReader:
    """Verify that output format is parseable by shared/mapping/data_reader.py."""

    def _get_layout_result(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=45.0)
        drops, updated_bldgs, _ = connect_buildings(
            four_buildings_gdf,
            poles,
            max_drop_distance_m=40.0,
        )
        plant = Point(500000, 1000000)
        backbone, poles_final = optimize_backbone(
            simple_road_graph, poles, drops, plant, edges_gdf=simple_road_edges
        )
        return format_layout_output(
            poles_gdf=poles_final,
            backbone_gdf=backbone,
            drop_cables_gdf=drops,
            buildings_gdf=updated_bldgs,
            original_buildings_geojson=four_buildings_geojson,
            spacing_m=45.0,
            max_drop_distance_m=40.0,
        )

    def test_extract_poles_parses_output(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        result = self._get_layout_result(
            simple_road_edges,
            simple_road_nodes,
            simple_road_graph,
            four_buildings_gdf,
            four_buildings_geojson,
        )
        poles = extract_poles(result["poles_geo_flat"])
        assert len(poles) > 0
        for p in poles:
            assert isinstance(p.lon, float)
            assert isinstance(p.lat, float)

    def test_extract_cables_parses_output(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        result = self._get_layout_result(
            simple_road_edges,
            simple_road_nodes,
            simple_road_graph,
            four_buildings_gdf,
            four_buildings_geojson,
        )
        cables = extract_cables(result["distribution_geo_flat"])
        assert len(cables) > 0
        for c in cables:
            assert c.length_meters is not None or c.properties.get("length_meters") is not None

    def test_extract_meta_parses_output(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        result = self._get_layout_result(
            simple_road_edges,
            simple_road_nodes,
            simple_road_graph,
            four_buildings_gdf,
            four_buildings_geojson,
        )
        meta = extract_meta(result["meta_geo_flat"])
        assert meta.pole_count > 0
        assert meta.backbone_cable_length_m >= 0.0
        assert meta.drop_cable_length_m >= 0.0
        assert meta.coverage_percentage >= 0.0

    def test_output_serializable_as_json(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        result = self._get_layout_result(
            simple_road_edges,
            simple_road_nodes,
            simple_road_graph,
            four_buildings_gdf,
            four_buildings_geojson,
        )
        # Must be JSON-serializable (going into JSONB columns)
        for key in (
            "poles_geo_flat",
            "distribution_geo_flat",
            "buildings_geo_flat",
            "meta_geo_flat",
        ):
            serialized = json.dumps(result[key])
            assert isinstance(serialized, str)


# --- Power Plant Location Tests ---


class TestLocatePowerPlant:
    def test_returns_point_on_road(self, simple_road_nodes, simple_road_edges):
        boundary = Polygon(
            [
                (499900, 999900),
                (500300, 999900),
                (500300, 1000100),
                (499900, 1000100),
                (499900, 999900),
            ]
        )
        plant = locate_power_plant(boundary, simple_road_nodes)
        assert isinstance(plant, Point)
        # Plant should be on or very near a road node
        from shapely.ops import unary_union

        road_union = unary_union(simple_road_edges.geometry)
        assert plant.distance(road_union) < 1.0  # within 1m


# --- RoadNetworkResult Tests ---


class TestRoadNetworkResult:
    def test_is_empty_with_no_edges(self):
        G = nx.Graph()
        G.add_node(0, x=0, y=0)
        result = RoadNetworkResult(graph=G, nodes_gdf=None, edges_gdf=None, crs=TEST_CRS)
        assert result.is_empty is True

    def test_is_empty_false_with_edges(self, simple_road_graph):
        result = RoadNetworkResult(
            graph=simple_road_graph, nodes_gdf=None, edges_gdf=None, crs=TEST_CRS
        )
        assert result.is_empty is False


# --- Fishbone Backbone Tests ---


class TestDetectBuildingPaths:
    """Test building path detection for off-road clusters."""

    @pytest.fixture
    def road_with_off_road_buildings(self):
        """Road edges + buildings where some are on-road and some off-road.

        Road runs horizontally. 4 buildings near road (within 40m),
        10 buildings far from road (200m+ away, forming off-road cluster).
        """
        road = gpd.GeoDataFrame(
            {
                "geometry": [LineString([(500000, 1000000), (500400, 1000000)])],
                "edge_type": ["road"],
            },
            crs=TEST_CRS,
        )
        nodes = gpd.GeoDataFrame(
            {"geometry": [Point(500000, 1000000), Point(500400, 1000000)]},
            crs=TEST_CRS,
        )
        # Near-road buildings (within 40m)
        near_buildings = [
            Point(500050, 1000030),
            Point(500150, 1000020),
            Point(500250, 1000025),
            Point(500350, 1000015),
        ]
        # Off-road cluster (200m north, well beyond max_drop_distance)
        rng = np.random.RandomState(42)
        off_road_buildings = [
            Point(500100 + rng.normal(0, 20), 1000250 + rng.normal(0, 30)) for _ in range(10)
        ]
        buildings = gpd.GeoDataFrame(
            {"geometry": near_buildings + off_road_buildings},
            crs=TEST_CRS,
        )
        return road, nodes, buildings

    def test_detects_off_road_cluster_paths(self, road_with_off_road_buildings):
        """Should generate building_path edges for off-road cluster."""
        edges, nodes, buildings = road_with_off_road_buildings
        path_edges, path_nodes = detect_building_paths(
            edges_gdf=edges,
            nodes_gdf=nodes,
            buildings_gdf=buildings,
            spacing_m=45.0,
            max_drop_distance_m=50.0,
            min_path_length_m=0.0,  # No minimum for test
        )
        assert len(path_edges) > 0, "Should produce building path edges"
        assert all(path_edges["edge_type"] == "building_path")
        assert all(path_edges["cable_type"] == "backbone")

    def test_no_paths_when_all_near_road(self):
        """Should return empty when all buildings are near road."""
        road = gpd.GeoDataFrame(
            {
                "geometry": [LineString([(500000, 1000000), (500400, 1000000)])],
                "edge_type": ["road"],
            },
            crs=TEST_CRS,
        )
        nodes = gpd.GeoDataFrame(
            {"geometry": [Point(500000, 1000000), Point(500400, 1000000)]},
            crs=TEST_CRS,
        )
        buildings = gpd.GeoDataFrame(
            {
                "geometry": [
                    Point(500050, 1000020),
                    Point(500150, 1000015),
                    Point(500250, 1000010),
                ]
            },
            crs=TEST_CRS,
        )
        path_edges, _ = detect_building_paths(
            edges_gdf=road,
            nodes_gdf=nodes,
            buildings_gdf=buildings,
            spacing_m=45.0,
            max_drop_distance_m=50.0,
        )
        assert len(path_edges) == 0

    def test_short_paths_filtered(self, road_with_off_road_buildings):
        """Short paths should be filtered with min_path_length_m."""
        edges, nodes, buildings = road_with_off_road_buildings
        path_edges_no_filter, _ = detect_building_paths(
            edges_gdf=edges,
            nodes_gdf=nodes,
            buildings_gdf=buildings,
            spacing_m=45.0,
            max_drop_distance_m=50.0,
            min_path_length_m=0.0,
        )
        path_edges_filtered, _ = detect_building_paths(
            edges_gdf=edges,
            nodes_gdf=nodes,
            buildings_gdf=buildings,
            spacing_m=45.0,
            max_drop_distance_m=50.0,
            min_path_length_m=5000.0,
        )
        # With a very high min length, most/all paths should still be kept
        # (fallback keeps longest), but count may differ
        assert len(path_edges_no_filter) >= len(path_edges_filtered)


class TestAugmentRoadNetwork:
    """Test merging building paths into road network."""

    def test_augments_edges(self, simple_road_edges, simple_road_nodes, simple_road_graph):
        """Augmented result should have more edges than original."""
        original = RoadNetworkResult(
            graph=simple_road_graph,
            nodes_gdf=simple_road_nodes,
            edges_gdf=simple_road_edges,
            crs=TEST_CRS,
        )
        path_edges = gpd.GeoDataFrame(
            [
                {
                    "geometry": LineString([(500100, 1000200), (500100, 1000400)]),
                    "length_meters": 200.0,
                    "cable_type": "backbone",
                    "edge_type": "building_path",
                }
            ],
            crs=TEST_CRS,
        )
        path_nodes = gpd.GeoDataFrame(
            {"geometry": [Point(500100, 1000200), Point(500100, 1000400)]},
            crs=TEST_CRS,
        )
        result = augment_road_network(original, path_edges, path_nodes)
        assert len(result.edges_gdf) == len(simple_road_edges) + 1
        assert "building_path" in result.edges_gdf["edge_type"].values

    def test_empty_paths_returns_original(
        self, simple_road_edges, simple_road_nodes, simple_road_graph
    ):
        """Empty path edges should return original unchanged."""
        original = RoadNetworkResult(
            graph=simple_road_graph,
            nodes_gdf=simple_road_nodes,
            edges_gdf=simple_road_edges,
            crs=TEST_CRS,
        )
        empty_edges = gpd.GeoDataFrame(
            columns=["geometry", "length_meters", "cable_type", "edge_type"],
            crs=TEST_CRS,
        )
        empty_nodes = gpd.GeoDataFrame(columns=["geometry"], crs=TEST_CRS)
        result = augment_road_network(original, empty_edges, empty_nodes)
        assert len(result.edges_gdf) == len(simple_road_edges)


class TestEdgeTypeInOutput:
    """Test that edge_type propagates to GeoJSON output."""

    def test_backbone_has_edge_type(
        self,
        simple_road_edges,
        simple_road_nodes,
        simple_road_graph,
        four_buildings_gdf,
        four_buildings_geojson,
    ):
        """Backbone cables in output should have edge_type property."""
        poles = place_poles_along_roads(simple_road_edges, simple_road_nodes, spacing_m=45.0)
        drops, updated_bldgs, _ = connect_buildings(
            four_buildings_gdf,
            poles,
            max_drop_distance_m=40.0,
        )
        plant = Point(500000, 1000000)
        backbone, poles_final = optimize_backbone(
            simple_road_graph,
            poles,
            drops,
            plant,
            edges_gdf=simple_road_edges,
        )
        # Add edge_type column manually (normally done by _tag_backbone_edge_type)
        if "edge_type" not in backbone.columns:
            backbone["edge_type"] = "road"

        result = format_layout_output(
            poles_gdf=poles_final,
            backbone_gdf=backbone,
            drop_cables_gdf=drops,
            buildings_gdf=updated_bldgs,
            original_buildings_geojson=four_buildings_geojson,
            spacing_m=45.0,
            max_drop_distance_m=40.0,
        )
        cables = result["distribution_geo_flat"]
        backbone_cables = [
            f for f in cables["features"] if f["properties"].get("cable_type") == "backbone"
        ]
        for cable in backbone_cables:
            assert "edge_type" in cable["properties"]
            assert cable["properties"]["edge_type"] in ("road", "building_path")


# --- Plant Site Selection Tests ---


class TestFindPlantSites:
    """Test the intelligent solar farm site selection algorithm."""

    @pytest.fixture
    def large_boundary(self):
        """500m x 500m boundary in UTM coordinates."""
        return Polygon(
            [
                (500000, 1000000),
                (500500, 1000000),
                (500500, 1000500),
                (500000, 1000500),
                (500000, 1000000),
            ]
        )

    @pytest.fixture
    def buildings_in_boundary(self):
        """20 buildings scattered within the 500x500m boundary."""
        rng = np.random.default_rng(42)
        polys = []
        for _ in range(20):
            x = 500050 + rng.uniform(0, 400)
            y = 1000050 + rng.uniform(0, 400)
            size = rng.uniform(5, 15)
            polys.append(
                Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)])
            )
        gdf = gpd.GeoDataFrame(geometry=polys, crs=TEST_CRS)
        return gdf

    @pytest.fixture
    def road_edges_in_boundary(self):
        """Road edges crossing through the 500x500m boundary."""
        lines = [
            LineString([(500000, 1000250), (500500, 1000250)]),  # horizontal
            LineString([(500250, 1000000), (500250, 1000500)]),  # vertical
        ]
        return gpd.GeoDataFrame(geometry=lines, crs=TEST_CRS)

    def test_happy_path(self, large_boundary, buildings_in_boundary, road_edges_in_boundary):
        """30 kWp in a 500x500m boundary with roads should find candidates."""
        result = find_plant_sites(
            boundary_proj=large_boundary,
            buildings_gdf=buildings_in_boundary,
            kwp=30.0,
            edges_gdf=road_edges_in_boundary,
        )
        assert isinstance(result, SiteSelectionResult)
        results = result.candidates
        assert len(results) > 0
        assert len(results) <= 3
        for site in results:
            assert isinstance(site, PlantSite)
            assert isinstance(site.polygon, Polygon)
            assert isinstance(site.centroid, Point)
            # Area within 5% of target (30 * 15.5 = 465 sqm)
            assert 440.0 <= site.area_sqm <= 490.0
            # Polygon has vertices (4 corners + closing = 5 coords)
            assert len(site.polygon.exterior.coords) >= 5

    def test_boundary_too_small(self, buildings_in_boundary):
        """Tiny boundary that can't fit the required area returns empty."""
        tiny_boundary = Polygon(
            [
                (500000, 1000000),
                (500020, 1000000),
                (500020, 1000020),
                (500000, 1000020),
                (500000, 1000000),
            ]
        )
        result = find_plant_sites(
            boundary_proj=tiny_boundary,
            buildings_gdf=buildings_in_boundary,
            kwp=50.0,  # 500 sqm needed, boundary is only 400 sqm
        )
        assert result.candidates == []

    def test_no_building_overlap(
        self, large_boundary, buildings_in_boundary, road_edges_in_boundary
    ):
        """No returned site polygon should overlap any building (with buffer)."""
        from shapely import union_all

        result = find_plant_sites(
            boundary_proj=large_boundary,
            buildings_gdf=buildings_in_boundary,
            kwp=20.0,
            edges_gdf=road_edges_in_boundary,
        )
        assert len(result.candidates) > 0, "Expected candidates in 500m boundary with 20 kWp"

        buildings_buffered = union_all(buildings_in_boundary.geometry.buffer(3.0))
        for site in result.candidates:
            assert not site.polygon.intersects(buildings_buffered), (
                "Site polygon overlaps buffered buildings"
            )

    def test_generate_layout_with_plant_location(self):
        """_road_based_layout with plant_location skips locate_power_plant."""
        from shared.layout.pipeline import _road_based_layout

        # Synthetic road network
        mock_nodes = gpd.GeoDataFrame(
            {"geometry": [Point(500100, 1000100), Point(500200, 1000100)]},
            crs=TEST_CRS,
        )
        mock_edges = gpd.GeoDataFrame(
            {
                "geometry": [LineString([(500100, 1000100), (500200, 1000100)])],
            },
            crs=TEST_CRS,
        )
        mock_graph = nx.Graph()
        mock_graph.graph["crs"] = TEST_CRS
        mock_graph.add_node(0, x=500100, y=1000100)
        mock_graph.add_node(1, x=500200, y=1000100)
        mock_graph.add_edge(
            0,
            1,
            length=100.0,
            geometry=LineString([(500100, 1000100), (500200, 1000100)]),
        )
        mock_road = RoadNetworkResult(
            graph=mock_graph, nodes_gdf=mock_nodes, edges_gdf=mock_edges, crs=TEST_CRS
        )

        boundary = Polygon(
            [
                (500000, 1000000),
                (500300, 1000000),
                (500300, 1000200),
                (500000, 1000200),
                (500000, 1000000),
            ]
        )
        # Buildings must be Point centroids (as _prepare_buildings produces)
        buildings = gpd.GeoDataFrame(
            geometry=[Point(500055, 1000055)],
            crs=TEST_CRS,
        )
        buildings_geojson = {"type": "FeatureCollection", "features": []}
        override_point = Point(500150, 1000100)

        with patch("shared.layout.pipeline.locate_power_plant") as mock_locate:
            _road_based_layout(
                road_result=mock_road,
                boundary_proj=boundary,
                buildings_gdf=buildings,
                buildings_geojson=buildings_geojson,
                spacing_m=45.0,
                max_drop_distance_m=40.0,
                plant_location=override_point,
            )
            mock_locate.assert_not_called()

    def test_corridor_blocked_by_building(self, large_boundary):
        """A building between the candidate site and the road should block that candidate."""
        # Road along the bottom edge of the boundary
        road = gpd.GeoDataFrame(
            geometry=[LineString([(500000, 1000050), (500500, 1000050)])],
            crs=TEST_CRS,
        )
        # Place a building directly between a candidate area and the road,
        # sitting in the corridor between y=1000050 (road) and y~1000100 (candidate)
        blocker = Polygon(
            [
                (500240, 1000060),
                (500260, 1000060),
                (500260, 1000090),
                (500240, 1000090),
                (500240, 1000060),
            ]
        )
        buildings = gpd.GeoDataFrame(geometry=[blocker], crs=TEST_CRS)
        result = find_plant_sites(
            boundary_proj=large_boundary,
            buildings_gdf=buildings,
            kwp=10.0,
            edges_gdf=road,
            spacing_m=45.0,
        )
        # Any returned candidate must NOT have a route passing through the blocker zone
        for site in result.candidates:
            # Candidate should not be in the blocked corridor (x ≈ 250, y > road)
            if 500230 <= site.centroid.x <= 500270:
                # If it's in the same x-band as the blocker it should have been rejected
                assert site.centroid.y <= 1000060 or site.centroid.y >= 1000200, (
                    f"Candidate at ({site.centroid.x:.0f}, {site.centroid.y:.0f}) "
                    "should have been blocked by corridor building"
                )

    def test_corridor_clear_when_offset(self, large_boundary):
        """A building offset from the route corridor should not block the candidate."""
        # Road along the bottom
        road = gpd.GeoDataFrame(
            geometry=[LineString([(500000, 1000050), (500500, 1000050)])],
            crs=TEST_CRS,
        )
        # Building far to the side — not in the corridor between candidate and road
        offset_building = Polygon(
            [
                (500050, 1000070),
                (500060, 1000070),
                (500060, 1000080),
                (500050, 1000080),
                (500050, 1000070),
            ]
        )
        buildings = gpd.GeoDataFrame(geometry=[offset_building], crs=TEST_CRS)
        result = find_plant_sites(
            boundary_proj=large_boundary,
            buildings_gdf=buildings,
            kwp=10.0,
            edges_gdf=road,
            spacing_m=45.0,
        )
        # Should find candidates since the building doesn't block the corridor
        assert len(result.candidates) > 0, "Offset building should not block candidates"

    def test_few_buildings(self, large_boundary, road_edges_in_boundary):
        """With < 5 buildings, should still work using building centroid."""
        few_buildings = gpd.GeoDataFrame(
            geometry=[
                Polygon(
                    [
                        (500100, 1000100),
                        (500110, 1000100),
                        (500110, 1000110),
                        (500100, 1000110),
                        (500100, 1000100),
                    ]
                ),
                Polygon(
                    [
                        (500300, 1000300),
                        (500310, 1000300),
                        (500310, 1000310),
                        (500300, 1000310),
                        (500300, 1000300),
                    ]
                ),
            ],
            crs=TEST_CRS,
        )
        result = find_plant_sites(
            boundary_proj=large_boundary,
            buildings_gdf=few_buildings,
            kwp=15.0,
            edges_gdf=road_edges_in_boundary,
        )
        # 500m boundary with 15 kWp (150 sqm) and roads should find candidates
        assert len(result.candidates) > 0, "Expected candidates in 500m boundary with 15 kWp"
        for site in result.candidates:
            assert isinstance(site, PlantSite)
            assert site.area_sqm > 0

    def test_render_map_produces_valid_png(
        self, large_boundary, buildings_in_boundary, road_edges_in_boundary
    ):
        """render_map=True should produce a valid base64 PNG in site_map_b64."""
        import base64

        # Create a synthetic WGS84 boundary (small area near equator)
        boundary_wgs84 = Polygon(
            [
                (6.0, 9.0),
                (6.005, 9.0),
                (6.005, 9.005),
                (6.0, 9.005),
                (6.0, 9.0),
            ]
        )
        buildings_geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "geometry": {"type": "Point", "coordinates": [6.002, 9.002]},
                    "properties": {},
                }
            ],
        }
        # Mock contextily to avoid network calls in tests
        with patch("contextily.add_basemap"):
            result = find_plant_sites(
                boundary_proj=large_boundary,
                buildings_gdf=buildings_in_boundary,
                kwp=30.0,
                edges_gdf=road_edges_in_boundary,
                boundary_wgs84=boundary_wgs84,
                buildings_geojson=buildings_geojson,
                site_name="Test Site",
                render_map=True,
            )
        assert isinstance(result, SiteSelectionResult)
        assert len(result.candidates) > 0
        assert result.site_map_b64 is not None
        # Verify it's valid base64 that decodes to a PNG
        png_bytes = base64.b64decode(result.site_map_b64)
        assert png_bytes[:4] == b"\x89PNG", "Decoded bytes should be a valid PNG"

    def test_render_map_false_no_image(
        self, large_boundary, buildings_in_boundary, road_edges_in_boundary
    ):
        """render_map=False (default) should not generate an image."""
        result = find_plant_sites(
            boundary_proj=large_boundary,
            buildings_gdf=buildings_in_boundary,
            kwp=30.0,
            edges_gdf=road_edges_in_boundary,
        )
        assert result.site_map_b64 is None


# --- Intersection-Aware Pole Snapping Tests ---


class TestComputeSnappedPositions:
    """Unit tests for _compute_snapped_positions() pure function."""

    def test_both_intersections_normal_edge(self):
        """90m edge with both ends as intersections: one intermediate at 45m."""
        positions = _compute_snapped_positions(90.0, 45.0, has_start=True, has_end=True)
        assert len(positions) == 1
        assert abs(positions[0] - 45.0) < 0.1

    def test_merge_gap_absorbed(self):
        """93m edge: gap=3m < 5m, last pole absorbed into end intersection."""
        positions = _compute_snapped_positions(93.0, 45.0, has_start=True, has_end=True)
        # Raw: [45, 90] → gap = 93-90 = 3 < 5 → remove 90 → [45]
        assert len(positions) == 1
        assert abs(positions[0] - 45.0) < 0.1

    def test_redistribute_gap(self):
        """97m edge: gap=7m (5-10m range), last two poles redistributed."""
        positions = _compute_snapped_positions(97.0, 45.0, has_start=True, has_end=True)
        # Raw: [45, 90] → gap = 97-90 = 7 → redistribute
        # [-1] += 3.5 → 93.5, [-2] += 1.75 → 46.75
        assert len(positions) == 2
        assert abs(positions[0] - 46.75) < 0.1
        assert abs(positions[1] - 93.5) < 0.1

    def test_no_adjustment_large_gap(self):
        """135m edge: gap=0 (3*45=135), no adjustment needed."""
        positions = _compute_snapped_positions(135.0, 45.0, has_start=True, has_end=True)
        # Raw: [45, 90] — 135 is removed (end intersection)
        assert len(positions) == 2
        assert abs(positions[0] - 45.0) < 0.1
        assert abs(positions[1] - 90.0) < 0.1

    def test_no_start_intersection_keeps_zero(self):
        """Position 0 kept when no start intersection."""
        positions = _compute_snapped_positions(90.0, 45.0, has_start=False, has_end=True)
        assert positions[0] < 0.1  # Position 0 is kept

    def test_no_end_intersection_keeps_last(self):
        """Last position kept when no end intersection."""
        positions = _compute_snapped_positions(90.0, 45.0, has_start=True, has_end=False)
        assert abs(positions[-1] - 90.0) < 0.1

    def test_no_intersections_at_all(self):
        """No intersections: full walk with 0 and length."""
        positions = _compute_snapped_positions(90.0, 45.0, has_start=False, has_end=False)
        assert abs(positions[0]) < 0.1
        assert abs(positions[-1] - 90.0) < 0.1
        assert len(positions) == 3  # 0, 45, 90

    def test_very_short_edge(self):
        """Edge shorter than spacing: only endpoints."""
        positions = _compute_snapped_positions(20.0, 45.0, has_start=True, has_end=True)
        # 20 < 45 → raw: [0, 20] → both removed → empty
        assert len(positions) == 0

    def test_single_spacing_edge(self):
        """Edge exactly equal to spacing with both intersections."""
        positions = _compute_snapped_positions(45.0, 45.0, has_start=True, has_end=True)
        # Raw: [0, 45] → both removed → empty
        assert len(positions) == 0


class TestIntersectionAwarePoleSnapping:
    """Integration tests for intersection-aware pole placement."""

    @pytest.fixture
    def t_junction_edges(self):
        """T-junction: horizontal road with vertical branch at midpoint."""
        h_road = LineString([(500000, 1000000), (500200, 1000000)])
        v_branch = LineString([(500100, 1000000), (500100, 1000100)])
        return gpd.GeoDataFrame({"geometry": [h_road, v_branch]}, crs=TEST_CRS)

    @pytest.fixture
    def t_junction_nodes(self):
        """Nodes for T-junction: 3 endpoints + 1 junction."""
        nodes = {
            0: {"geometry": Point(500000, 1000000)},
            1: {"geometry": Point(500200, 1000000)},
            2: {"geometry": Point(500100, 1000100)},
            3: {"geometry": Point(500100, 1000000)},  # junction
        }
        gdf = gpd.GeoDataFrame.from_dict(nodes, orient="index")
        gdf = gdf.set_geometry("geometry")
        gdf.crs = TEST_CRS
        return gdf

    def test_t_junction_exactly_one_pole_at_junction(self, t_junction_edges, t_junction_nodes):
        """At a T-junction, there should be exactly 1 pole within 5m of the junction point."""
        poles = place_poles_along_roads(t_junction_edges, t_junction_nodes, spacing_m=45.0)
        junction_pt = Point(500100, 1000000)
        nearby = [
            i
            for i in range(len(poles))
            if poles.iloc[i].geometry.distance(junction_pt) < POLE_DEDUP_DISTANCE_M
        ]
        assert len(nearby) == 1, f"Expected 1 pole near junction, got {len(nearby)}"

    def test_short_edge_between_intersections_no_intermediates(
        self, t_junction_edges, t_junction_nodes
    ):
        """A 30m edge between two intersection nodes should have 0 intermediates."""
        short_edge = LineString([(500100, 1000000), (500100, 1000030)])
        nodes = {
            0: {"geometry": Point(500100, 1000000)},
            1: {"geometry": Point(500100, 1000030)},
        }
        edges = gpd.GeoDataFrame({"geometry": [short_edge]}, crs=TEST_CRS)
        nodes_gdf = gpd.GeoDataFrame.from_dict(nodes, orient="index")
        nodes_gdf = nodes_gdf.set_geometry("geometry")
        nodes_gdf.crs = TEST_CRS

        poles = place_poles_along_roads(edges, nodes_gdf, spacing_m=45.0)
        intermediates = poles[poles["pole_type"] == "intermediate"]
        assert len(intermediates) == 0, "Short edge between intersections needs no intermediates"

    def test_edge_with_no_matching_nodes_normal_walk(self):
        """Edge whose endpoints don't match any node: falls back to normal walk."""
        edge = LineString([(500000, 1000000), (500200, 1000000)])
        # Nodes far from edge endpoints
        nodes = {0: {"geometry": Point(500500, 1000500)}}
        edges = gpd.GeoDataFrame({"geometry": [edge]}, crs=TEST_CRS)
        nodes_gdf = gpd.GeoDataFrame.from_dict(nodes, orient="index")
        nodes_gdf = nodes_gdf.set_geometry("geometry")
        nodes_gdf.crs = TEST_CRS

        poles = place_poles_along_roads(edges, nodes_gdf, spacing_m=45.0)
        intermediates = poles[poles["pole_type"] == "intermediate"]
        # 200m / 45m ≈ 4.4, so expect ~5 intermediates (0, 45, 90, 135, 180, 200)
        assert len(intermediates) >= 4

    def test_boundary_clipping_still_works(self, t_junction_edges, t_junction_nodes):
        """Boundary clipping should still exclude poles outside."""
        small_boundary = Polygon(
            [
                (499990, 999990),
                (500110, 999990),
                (500110, 1000010),
                (499990, 1000010),
                (499990, 999990),
            ]
        )
        poles = place_poles_along_roads(
            t_junction_edges, t_junction_nodes, spacing_m=45.0, boundary=small_boundary
        )
        for _, pole in poles.iterrows():
            assert small_boundary.contains(pole.geometry), (
                f"Pole at ({pole.geometry.x}, {pole.geometry.y}) is outside boundary"
            )

    def test_intersection_poles_have_road_node_id(self, t_junction_edges, t_junction_nodes):
        """Intersection poles should have road_node_id set."""
        poles = place_poles_along_roads(t_junction_edges, t_junction_nodes, spacing_m=45.0)
        intersection_poles = poles[poles["pole_type"] == "intersection"]
        for _, pole in intersection_poles.iterrows():
            assert pole["road_node_id"] is not None

    def test_fewer_poles_than_naive_at_junctions(self, t_junction_edges, t_junction_nodes):
        """Snapping should produce fewer poles than naive per-edge walk + dedup."""
        poles = place_poles_along_roads(t_junction_edges, t_junction_nodes, spacing_m=45.0)
        # With the T-junction (200m + 100m), a naive approach would produce more poles
        # from double-counting the junction area. We just check a reasonable count.
        # 200m → ~5 poles, 100m → ~3 poles, 4 intersection nodes = max ~12, dedup to ~10
        # With snapping: 4 intersections + clean intermediates, should be <= 10
        assert len(poles) <= 12, f"Too many poles ({len(poles)}), snapping may not be working"


# --- Deduplication Helper Tests ---


class TestDirectedEdgeDedup:
    """Tests for _deduplicate_directed_edges()."""

    def test_reverse_edges_removed(self):
        """A→B and B→A should collapse to one edge."""
        line = LineString([(0, 0), (100, 0)])
        gdf = gpd.GeoDataFrame(
            {"geometry": [line, line]},
            index=pd.MultiIndex.from_tuples([(1, 2, 0), (2, 1, 0)]),
            crs=TEST_CRS,
        )
        result = _deduplicate_directed_edges(gdf)
        assert len(result) == 1

    def test_parallel_keys_preserved(self):
        """Two edges between same nodes with different keys should both survive."""
        line1 = LineString([(0, 0), (100, 0)])
        line2 = LineString([(0, 0), (50, 10), (100, 0)])
        gdf = gpd.GeoDataFrame(
            {"geometry": [line1, line2]},
            index=pd.MultiIndex.from_tuples([(1, 2, 0), (1, 2, 1)]),
            crs=TEST_CRS,
        )
        result = _deduplicate_directed_edges(gdf)
        assert len(result) == 2

    def test_flat_index_passthrough(self):
        """GeoDataFrame with flat (non-Multi) index is returned unchanged."""
        line = LineString([(0, 0), (100, 0)])
        gdf = gpd.GeoDataFrame({"geometry": [line, line]}, crs=TEST_CRS)
        result = _deduplicate_directed_edges(gdf)
        assert len(result) == 2

    def test_empty_gdf(self):
        """Empty GeoDataFrame is returned as-is."""
        gdf = gpd.GeoDataFrame({"geometry": []}, crs=TEST_CRS)
        result = _deduplicate_directed_edges(gdf)
        assert len(result) == 0


class TestNodeDeduplication:
    """Tests for _deduplicate_nodes_by_proximity()."""

    def test_near_road_node_dropped(self):
        """Path node within tolerance of a road node should be dropped."""
        road_nodes = gpd.GeoDataFrame({"geometry": [Point(500000, 1000000)]}, crs=TEST_CRS)
        path_nodes = gpd.GeoDataFrame(
            {"geometry": [Point(500003, 1000000)]},
            crs=TEST_CRS,  # 3m away
        )
        result = _deduplicate_nodes_by_proximity(road_nodes, path_nodes, tolerance_m=5.0)
        assert len(result) == 0

    def test_distant_node_kept(self):
        """Path node far from any road node should survive."""
        road_nodes = gpd.GeoDataFrame({"geometry": [Point(500000, 1000000)]}, crs=TEST_CRS)
        path_nodes = gpd.GeoDataFrame(
            {"geometry": [Point(500100, 1000000)]},
            crs=TEST_CRS,  # 100m away
        )
        result = _deduplicate_nodes_by_proximity(road_nodes, path_nodes, tolerance_m=5.0)
        assert len(result) == 1

    def test_self_dedup(self):
        """Two path nodes near each other should collapse to one."""
        road_nodes = gpd.GeoDataFrame({"geometry": []}, crs=TEST_CRS)
        path_nodes = gpd.GeoDataFrame(
            {
                "geometry": [
                    Point(500000, 1000000),
                    Point(500002, 1000000),  # 2m away — within tolerance
                ]
            },
            crs=TEST_CRS,
        )
        result = _deduplicate_nodes_by_proximity(road_nodes, path_nodes, tolerance_m=5.0)
        assert len(result) == 1

    def test_empty_path_nodes(self):
        """Empty path nodes returns empty."""
        road_nodes = gpd.GeoDataFrame({"geometry": [Point(500000, 1000000)]}, crs=TEST_CRS)
        path_nodes = gpd.GeoDataFrame({"geometry": []}, crs=TEST_CRS)
        result = _deduplicate_nodes_by_proximity(road_nodes, path_nodes, tolerance_m=5.0)
        assert len(result) == 0

    def test_none_road_nodes(self):
        """None road_nodes_gdf should skip road-proximity check, only self-dedup."""
        path_nodes = gpd.GeoDataFrame(
            {
                "geometry": [
                    Point(500000, 1000000),
                    Point(500002, 1000000),  # 2m — within tolerance
                ]
            },
            crs=TEST_CRS,
        )
        result = _deduplicate_nodes_by_proximity(None, path_nodes, tolerance_m=5.0)
        assert len(result) == 1


class TestRedundantPathFiltering:
    """Tests for _filter_redundant_path_edges()."""

    def test_parallel_path_removed(self):
        """Path edge running parallel to a road within threshold is dropped."""
        road_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(500000, 1000000), (500200, 1000000)])]},
            crs=TEST_CRS,
        )
        # Path 10m north, parallel to road — within 22.5m threshold
        path_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(500000, 1000010), (500200, 1000010)])]},
            crs=TEST_CRS,
        )
        result = _filter_redundant_path_edges(path_edges, road_edges, distance_m=22.5)
        assert len(result) == 0

    def test_perpendicular_path_kept(self):
        """Path running perpendicular to road at some distance is kept."""
        road_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(500000, 1000000), (500200, 1000000)])]},
            crs=TEST_CRS,
        )
        # Path running north-south, midpoint 100m away from road
        path_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(500300, 999900), (500300, 1000100)])]},
            crs=TEST_CRS,
        )
        result = _filter_redundant_path_edges(path_edges, road_edges, distance_m=22.5)
        assert len(result) == 1

    def test_distant_path_kept(self):
        """Path edge far from any road is kept."""
        road_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(500000, 1000000), (500200, 1000000)])]},
            crs=TEST_CRS,
        )
        path_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(500000, 1000100), (500200, 1000100)])]},
            crs=TEST_CRS,
        )
        result = _filter_redundant_path_edges(path_edges, road_edges, distance_m=22.5)
        assert len(result) == 1

    def test_empty_path_edges(self):
        """Empty path edges returns empty."""
        road_edges = gpd.GeoDataFrame({"geometry": [LineString([(0, 0), (100, 0)])]}, crs=TEST_CRS)
        path_edges = gpd.GeoDataFrame({"geometry": []}, crs=TEST_CRS)
        result = _filter_redundant_path_edges(path_edges, road_edges)
        assert len(result) == 0

    def test_empty_road_edges(self):
        """Empty road edges returns all path edges unchanged."""
        road_edges = gpd.GeoDataFrame({"geometry": []}, crs=TEST_CRS)
        path_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(500000, 1000000), (500200, 1000000)])]},
            crs=TEST_CRS,
        )
        result = _filter_redundant_path_edges(path_edges, road_edges)
        assert len(result) == 1


class TestWeightedBackbonePrefersRoads:
    """Verify that Dijkstra backbone prefers road-based routes over building paths."""

    def test_backbone_prefers_road_over_shortcut_path(self):
        """Given a road route (longer, more hops) and a building-path shortcut,
        the backbone should route via the road due to the weight penalty."""
        # Road: plant(0,0) -> A(45,0) -> B(90,0) -> C(135,0) -> D(180,0)
        # Building path shortcut: plant(0,0) -> D(180,0) directly
        road_line = LineString([(0, 0), (180, 0)])
        path_line = LineString([(0, 0), (0, 10), (180, 10), (180, 0)])

        edges_gdf = gpd.GeoDataFrame(
            {
                "geometry": [road_line, path_line],
                "edge_type": ["road", "building_path"],
            },
            crs=TEST_CRS,
        )

        # Place poles: plant at origin, intermediates along road, one at end
        poles_data = [
            {"geometry": Point(0, 0), "pole_type": "plant", "road_node_id": None},
            {"geometry": Point(45, 0), "pole_type": "intermediate", "road_node_id": None},
            {"geometry": Point(90, 0), "pole_type": "intermediate", "road_node_id": None},
            {"geometry": Point(135, 0), "pole_type": "intermediate", "road_node_id": None},
            {"geometry": Point(180, 0), "pole_type": "intermediate", "road_node_id": None},
        ]
        poles_gdf = gpd.GeoDataFrame(poles_data, crs=TEST_CRS)

        # Drop cable: building at (180, 5) connected to pole 4
        drop_cables = gpd.GeoDataFrame(
            [{"geometry": LineString([(180, 0), (180, 5)]), "pole_idx": 4}],
            crs=TEST_CRS,
        )

        G = nx.Graph()
        backbone_gdf, updated_poles = optimize_backbone(
            road_graph=G,
            poles_gdf=poles_gdf,
            drop_cables_gdf=drop_cables,
            plant_location=Point(0, 0),
            edges_gdf=edges_gdf,
        )

        # Pole 4 (D) should be reached via pole 3 (road), not via plant (path)
        parent_of_d = updated_poles.iloc[4]["from_pole_idx"]
        assert parent_of_d == 3, (
            f"Pole D should route via road (parent=3), got parent={parent_of_d}"
        )

    def test_adjacency_returns_weighted_dict(self):
        """build_pole_adjacency returns dict[int, dict[int, float]]."""
        line = LineString([(0, 0), (100, 0)])
        edges_gdf = gpd.GeoDataFrame(
            {"geometry": [line], "edge_type": ["road"]},
            crs=TEST_CRS,
        )
        poles_gdf = gpd.GeoDataFrame(
            [
                {"geometry": Point(0, 0), "pole_type": "intersection"},
                {"geometry": Point(50, 0), "pole_type": "intermediate"},
                {"geometry": Point(100, 0), "pole_type": "intersection"},
            ],
            crs=TEST_CRS,
        )
        adj = build_pole_adjacency(poles_gdf, edges_gdf)
        # Should return nested dicts with float weights
        assert isinstance(adj[0], dict)
        assert isinstance(adj[0][1], float)
        assert adj[0][1] == pytest.approx(50.0, abs=1.0)

    def test_building_path_adjacency_has_higher_weight(self):
        """Building-path edges produce higher weights than equivalent road edges."""
        line = LineString([(0, 0), (100, 0)])
        road_edges = gpd.GeoDataFrame({"geometry": [line], "edge_type": ["road"]}, crs=TEST_CRS)
        path_edges = gpd.GeoDataFrame(
            {"geometry": [line], "edge_type": ["building_path"]}, crs=TEST_CRS
        )
        poles_gdf = gpd.GeoDataFrame(
            [
                {"geometry": Point(0, 0), "pole_type": "intersection"},
                {"geometry": Point(100, 0), "pole_type": "intersection"},
            ],
            crs=TEST_CRS,
        )
        road_adj = build_pole_adjacency(poles_gdf, road_edges)
        path_adj = build_pole_adjacency(poles_gdf, path_edges)
        road_weight = road_adj[0][1]
        path_weight = path_adj[0][1]
        assert path_weight > road_weight


class TestEdgeEndpointSnapping:
    """Verify that _snap_edge_endpoints corrects edge coordinates after node dedup."""

    def test_endpoint_snapped_to_nearby_node(self):
        """Edge endpoint 3m from a node gets snapped to it."""
        # Edge with endpoint at (100, 203) — road node at (100, 200), 3m away
        path_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(0, 0), (100, 203)])]},
            crs=TEST_CRS,
        )
        nodes = gpd.GeoDataFrame(
            {"geometry": [Point(0, 0), Point(100, 200)]},
            crs=TEST_CRS,
        )
        result = _snap_edge_endpoints(path_edges, nodes, tolerance_m=5.0)
        end_coord = list(result.geometry.iloc[0].coords)[-1]
        assert end_coord == pytest.approx((100.0, 200.0), abs=0.1)

    def test_distant_endpoint_not_snapped(self):
        """Edge endpoint 10m from a node (> tolerance) is NOT snapped."""
        path_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(0, 0), (100, 210)])]},
            crs=TEST_CRS,
        )
        nodes = gpd.GeoDataFrame(
            {"geometry": [Point(0, 0), Point(100, 200)]},
            crs=TEST_CRS,
        )
        result = _snap_edge_endpoints(path_edges, nodes, tolerance_m=5.0)
        end_coord = list(result.geometry.iloc[0].coords)[-1]
        assert end_coord == pytest.approx((100.0, 210.0), abs=0.1)

    def test_collapsed_edge_kept_as_original(self):
        """If snapping collapses an edge to zero length, the original is kept."""
        # Both endpoints snap to the same node
        path_edges = gpd.GeoDataFrame(
            {"geometry": [LineString([(0, 1), (0, -1)])]},
            crs=TEST_CRS,
        )
        nodes = gpd.GeoDataFrame(
            {"geometry": [Point(0, 0)]},
            crs=TEST_CRS,
        )
        result = _snap_edge_endpoints(path_edges, nodes, tolerance_m=5.0)
        assert result.geometry.iloc[0].length > 0


class TestBoundaryClippingContinue:
    """Verify that boundary clipping uses continue (not break)."""

    def test_road_reentering_boundary_keeps_far_poles(self):
        """A road that exits and re-enters the boundary should have poles
        on both the near and far segments inside the boundary."""
        # Boundary: a box from (0,0) to (200, 50)
        boundary = Polygon([(0, 0), (200, 0), (200, 50), (0, 50)])

        # Road that goes: inside → outside → inside
        # (0,25) → (80,25) inside, (80,25)→(120,80) outside, (120,80)→(200,25) inside
        road_line = LineString([(0, 25), (80, 25), (120, 80), (200, 25)])

        edges_gdf = gpd.GeoDataFrame({"geometry": [road_line]}, crs=TEST_CRS)
        nodes_gdf = gpd.GeoDataFrame(
            {"geometry": [Point(0, 25), Point(200, 25)]},
            crs=TEST_CRS,
        )

        poles = place_poles_along_roads(edges_gdf, nodes_gdf, spacing_m=30.0, boundary=boundary)

        # With continue: poles on both segments inside boundary
        # With break: only poles before the first exit
        pole_xs = [p.x for p in poles.geometry]
        has_near = any(x < 80 for x in pole_xs)
        has_far = any(x > 120 for x in pole_xs)
        assert has_near, "Should have poles on near segment"
        assert has_far, "Should have poles on far segment (road re-enters boundary)"


class TestBridgeDisconnectedComponents:
    """Tests for _bridge_disconnected_components."""

    @staticmethod
    def _all_reachable(adjacency, start):
        """Return all nodes reachable from *start* via adjacency DFS."""
        visited = set()
        stack = [start]
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            for nb in adjacency.get(n, {}):
                if nb not in visited:
                    stack.append(nb)
        return visited

    def test_two_components_bridged(self):
        """Two isolated clusters should be merged with one bridge edge."""
        adjacency = {
            0: {1: 10.0},
            1: {0: 10.0},
            2: {3: 10.0},
            3: {2: 10.0},
        }
        coords = np.array([[0.0, 0.0], [10.0, 0.0], [100.0, 0.0], [110.0, 0.0]])
        _bridge_disconnected_components(adjacency, coords)
        assert self._all_reachable(adjacency, 0) == {0, 1, 2, 3}

    def test_single_component_unchanged(self):
        """Already-connected graph should not be modified."""
        adjacency = {0: {1: 5.0}, 1: {0: 5.0, 2: 5.0}, 2: {1: 5.0}}
        coords = np.array([[0.0, 0.0], [5.0, 0.0], [10.0, 0.0]])
        _bridge_disconnected_components(adjacency, coords)
        assert len(adjacency[0]) == 1
        assert len(adjacency[2]) == 1

    def test_three_components_all_merged(self):
        """Three isolated components should all be merged."""
        # Use contiguous indices 0-5 with 3 components of 2 nodes each
        adjacency = {
            0: {1: 5.0},
            1: {0: 5.0},
            2: {3: 5.0},
            3: {2: 5.0},
            4: {5: 5.0},
            5: {4: 5.0},
        }
        coords = np.array(
            [
                [0.0, 0.0],
                [5.0, 0.0],
                [100.0, 0.0],
                [105.0, 0.0],
                [200.0, 0.0],
                [205.0, 0.0],
            ]
        )
        _bridge_disconnected_components(adjacency, coords)
        assert self._all_reachable(adjacency, 0) == {0, 1, 2, 3, 4, 5}

    def test_empty_adjacency(self):
        """Empty adjacency + empty coords should not crash."""
        adjacency: dict[int, dict[int, float]] = {}
        coords = np.array([]).reshape(0, 2)
        _bridge_disconnected_components(adjacency, coords)
        assert adjacency == {}

    def test_single_node(self):
        """Single node should be unchanged."""
        adjacency: dict[int, dict[int, float]] = {0: {}}
        coords = np.array([[0.0, 0.0]])
        _bridge_disconnected_components(adjacency, coords)
        assert adjacency == {0: {}}

    def test_bridge_respects_max_distance(self):
        """Components farther than _MAX_BRIDGE_DISTANCE_M should NOT be bridged."""
        from shared.layout.distribution import _MAX_BRIDGE_DISTANCE_M

        adjacency = {
            0: {1: 10.0},
            1: {0: 10.0},
            2: {3: 10.0},
            3: {2: 10.0},
        }
        # Place second component beyond the max bridge distance
        far = _MAX_BRIDGE_DISTANCE_M + 100.0
        coords = np.array([[0.0, 0.0], [10.0, 0.0], [far, 0.0], [far + 10, 0.0]])
        _bridge_disconnected_components(adjacency, coords)
        # Components should remain disconnected
        assert self._all_reachable(adjacency, 0) == {0, 1}
        assert self._all_reachable(adjacency, 2) == {2, 3}

    def test_bridge_weight_uses_penalty(self):
        """Bridge edges should use _BUILDING_PATH_WEIGHT_PENALTY."""
        from shared.layout.distribution import _BUILDING_PATH_WEIGHT_PENALTY

        adjacency = {
            0: {1: 10.0},
            1: {0: 10.0},
            2: {3: 10.0},
            3: {2: 10.0},
        }
        coords = np.array([[0.0, 0.0], [10.0, 0.0], [50.0, 0.0], [60.0, 0.0]])
        _bridge_disconnected_components(adjacency, coords)
        # Bridge should connect node 1 (at x=10) to node 2 (at x=50), distance=40
        assert 2 in adjacency[1]
        expected_weight = max(40.0, 1.0) * _BUILDING_PATH_WEIGHT_PENALTY
        assert abs(adjacency[1][2] - expected_weight) < 0.1


# --- Tests for building alignment detection ---


class TestDetectAlignedRoads:
    """Test building alignment road detection."""

    def test_linear_input_returns_centerlines(self):
        """Two rows of 10 points should produce at least 1 centerline."""
        from shared.layout.building_alignment import detect_aligned_roads

        rng = np.random.RandomState(42)
        # Row 1: along y=0, spaced 15m apart
        row1 = np.column_stack([np.arange(0, 150, 15), np.zeros(10) + rng.normal(0, 1, 10)])
        # Row 2: along y=200, spaced 15m apart
        row2 = np.column_stack([np.arange(0, 150, 15), np.full(10, 200.0) + rng.normal(0, 1, 10)])
        coords = np.vstack([row1, row2])

        centerlines = detect_aligned_roads(coords, cluster_radius=30.0)
        assert len(centerlines) >= 1
        for cl in centerlines:
            assert cl.length >= 50.0  # _MIN_SEGMENT_LENGTH_M

    def test_random_scatter_returns_empty(self):
        """Random scatter of points should not produce centerlines."""
        from shared.layout.building_alignment import detect_aligned_roads

        rng = np.random.RandomState(99)
        coords = rng.uniform(0, 500, size=(30, 2))
        centerlines = detect_aligned_roads(coords, cluster_radius=30.0)
        assert len(centerlines) == 0

    def test_too_few_points_returns_empty(self):
        """Fewer than 4 points should return empty."""
        from shared.layout.building_alignment import detect_aligned_roads

        coords = np.array([[0, 0], [10, 0], [20, 0]])
        centerlines = detect_aligned_roads(coords, cluster_radius=30.0)
        assert len(centerlines) == 0


class TestAdaptiveCellSize:
    """Test that adaptive cell size is computed correctly."""

    def test_dense_buildings_reduce_cell_size(self):
        """With 13m NN distance, cell_size should be ~26m (< 45m spacing)."""
        from scipy.spatial import KDTree

        rng = np.random.RandomState(42)
        # Grid of points ~13m apart
        xs = np.arange(0, 200, 13)
        ys = np.arange(0, 200, 13)
        xx, yy = np.meshgrid(xs, ys)
        coords = np.column_stack([xx.ravel(), yy.ravel()])
        coords = coords.astype(float) + rng.normal(0, 1, coords.shape)

        tree = KDTree(coords)
        nn_dists = tree.query(coords, k=2)[0][:, 1]
        median_nn = float(np.median(nn_dists))
        spacing_m = 45.0
        cell_size = max(10.0, min(spacing_m, median_nn * 2.0))

        assert cell_size < spacing_m
        assert 20.0 < cell_size < 35.0  # ~26m expected

    def test_sparse_buildings_keep_spacing(self):
        """With 40m NN distance, cell_size should cap at spacing_m (45m)."""
        from scipy.spatial import KDTree

        coords = np.array([[0, 0], [40, 0], [80, 0], [120, 0], [160, 0]], dtype=float)
        tree = KDTree(coords)
        nn_dists = tree.query(coords, k=2)[0][:, 1]
        median_nn = float(np.median(nn_dists))
        spacing_m = 45.0
        cell_size = max(10.0, min(spacing_m, median_nn * 2.0))

        assert cell_size == spacing_m


class TestBuildingEnvelope:
    """Test building envelope computation."""

    def test_envelope_smaller_than_boundary(self):
        """Building envelope should be smaller than site boundary."""
        from shared.layout.distribution import compute_building_envelope

        boundary = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
        # Cluster of buildings in the center
        rng = np.random.RandomState(42)
        pts = rng.uniform(300, 700, size=(20, 2))
        buildings = gpd.GeoDataFrame(
            {"geometry": [Point(x, y) for x, y in pts]},
            crs=TEST_CRS,
        )
        envelope = compute_building_envelope(buildings, boundary, buffer_m=50.0)
        assert envelope.area < boundary.area

    def test_envelope_fallback_on_few_points(self):
        """With < 3 buildings, should return site boundary."""
        from shared.layout.distribution import compute_building_envelope

        boundary = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
        buildings = gpd.GeoDataFrame(
            {"geometry": [Point(500, 500), Point(510, 510)]},
            crs=TEST_CRS,
        )
        envelope = compute_building_envelope(buildings, boundary)
        assert envelope.equals(boundary)


class TestAlignmentIntegration:
    """Integration tests for building alignment features."""

    def test_detect_building_paths_with_aligned_offroad_buildings(self):
        """detect_building_paths should produce path edges for linearly arranged
        off-road buildings that are beyond max_drop_distance from roads."""
        # --- Road: horizontal at y=1000000, from x=500000 to x=500400 ---
        road_line = LineString([(500000, 1000000), (500400, 1000000)])
        edges = gpd.GeoDataFrame(
            {"geometry": [road_line]},
            crs=TEST_CRS,
        )

        nodes = gpd.GeoDataFrame(
            {"geometry": [Point(500000, 1000000), Point(500400, 1000000)]},
            crs=TEST_CRS,
        )

        # --- Buildings: two parallel rows 200m north of the road (y=1000200),
        #     spaced ~15m apart along x.  200m > 40m max_drop_distance,
        #     so all buildings are "off-road". ---
        building_pts = []
        for i in range(12):
            x = 500050 + i * 15
            building_pts.append(Point(x, 1000200))  # row 1
        for i in range(12):
            x = 500050 + i * 15
            building_pts.append(Point(x, 1000215))  # row 2

        buildings = gpd.GeoDataFrame(
            {"geometry": building_pts},
            crs=TEST_CRS,
        )

        path_edges, path_nodes = detect_building_paths(
            edges,
            nodes,
            buildings,
            spacing_m=45.0,
            max_drop_distance_m=40.0,
        )

        # All 24 buildings are > 40m from the road, so alignment detection
        # should fire and the function should produce building_path edges.
        assert len(path_edges) > 0, (
            "Expected non-empty path_edges for linearly aligned off-road buildings"
        )
        assert "edge_type" in path_edges.columns
        assert (path_edges["edge_type"] == "building_path").all()

    def test_compute_building_envelope_with_clustered_buildings(self):
        """compute_building_envelope with clustered buildings should produce an
        envelope smaller than the full site boundary."""
        from shared.layout.distribution import compute_building_envelope

        # Large site boundary
        boundary = Polygon(
            [
                (0, 0),
                (2000, 0),
                (2000, 2000),
                (0, 2000),
            ]
        )

        # Clustered buildings in one corner
        rng = np.random.RandomState(99)
        pts = rng.uniform(200, 600, size=(25, 2))
        buildings = gpd.GeoDataFrame(
            {"geometry": [Point(x, y) for x, y in pts]},
            crs=TEST_CRS,
        )

        envelope = compute_building_envelope(
            buildings,
            boundary,
            buffer_m=50.0,
        )

        assert envelope.area < boundary.area, (
            f"Envelope area ({envelope.area:.0f}) should be smaller than "
            f"boundary area ({boundary.area:.0f})"
        )
