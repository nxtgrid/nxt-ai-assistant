"""Detect road centerlines from linear building arrangements.

When OpenStreetMap road data is sparse, building arrangements reveal the
underlying street network. Buildings in rural communities are typically
arranged in rows along roads. This module detects those linear patterns
and produces centerline LineStrings that can be injected into the road
network for pole placement.

Algorithm:
1. Score each building centroid for local linearity (PCA on k-NN)
2. Cluster high-linearity points spatially (connected components)
3. Validate directional coherence per cluster (PCA)
4. Convert validated clusters to LineString centerlines
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.spatial import KDTree
from shapely.geometry import LineString

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants — implementation details, not caller-configurable
# ---------------------------------------------------------------------------

# Neighborhood radius multiplier: radius = median_nn * this factor.
# At 2×, a 13m-NN site uses ~26m radius (stays within one building row).
_NEIGHBORHOOD_RADIUS_FACTOR = 2.0

# Minimum / maximum neighbors for PCA (need >= 3 for meaningful SVD)
_MIN_NEIGHBORS = 3
_MAX_NEIGHBORS = 10

# Minimum linearity score for a point to be considered "on a linear feature"
# (sigma1 - sigma2) / sigma1;  1.0 = perfectly collinear, 0.0 = isotropic
_LINEARITY_THRESHOLD = 0.50

# Minimum cluster linearity (PCA on the whole cluster) to keep it
_CLUSTER_LINEARITY_THRESHOLD = 0.6

# Minimum points in a cluster to form a valid segment
_MIN_BUILDINGS_PER_SEGMENT = 4

# Minimum segment length to keep (meters)
_MIN_SEGMENT_LENGTH_M = 50.0


# ---------------------------------------------------------------------------
# Sub-functions
# ---------------------------------------------------------------------------


def _score_linearity(
    coords: np.ndarray,
    median_nn: float,
) -> np.ndarray:
    """Per-point linearity score via batched k-NN covariance eigendecomposition.

    For each point, gathers the k nearest neighbors (k = min(_MAX_NEIGHBORS, N)),
    computes a 2x2 covariance matrix from the centered neighbor coordinates,
    and derives linearity from the eigenvalues.
    Linearity = (sqrt(lam_max) - sqrt(lam_min)) / sqrt(lam_max).

    Args:
        coords: (N, 2) building centroids in projected CRS.
        median_nn: Median nearest-neighbor distance (meters). Retained for
            API compatibility; the vectorized version uses fixed-k neighbors.

    Returns:
        Array of linearity scores, shape (N,).
    """
    n = len(coords)
    if n < _MIN_NEIGHBORS:
        return np.zeros(n)

    k = min(_MAX_NEIGHBORS, n)
    tree = KDTree(coords)

    # Query k nearest neighbors for ALL points at once — shape (N, k)
    _, idx_all = tree.query(coords, k=k)

    # Build neighbor coordinate array — shape (N, k, 2)
    neighbors_all = coords[idx_all]

    # Center each neighborhood — shape (N, k, 2)
    centered = neighbors_all - neighbors_all.mean(axis=1, keepdims=True)

    # 2x2 covariance matrices — shape (N, 2, 2)
    cov = np.einsum("nki,nkj->nij", centered, centered)

    # Eigenvalues sorted ascending — shape (N, 2)
    eigenvalues = np.linalg.eigvalsh(cov)

    # Singular values in descending order — shape (N, 2)
    S = np.sqrt(np.maximum(eigenvalues[:, ::-1], 0.0))

    # Linearity: (S0 - S1) / S0, guarded against division by zero
    linearity = np.where(S[:, 0] > 1e-9, (S[:, 0] - S[:, 1]) / S[:, 0], 0.0)

    return linearity


def _cluster_linear_points(
    linear_points: np.ndarray,
    cluster_radius: float,
) -> list[LineString]:
    """Cluster linear points by spatial proximity and validate direction coherence.

    Uses KDTree connected-components clustering (equivalent to DBSCAN with
    min_samples=1, then filters by minimum cluster size). For each cluster,
    validates directional coherence via PCA and produces a centerline.

    Args:
        linear_points: (N, 2) points that passed linearity threshold.
        cluster_radius: Maximum distance between points in the same cluster.

    Returns:
        List of LineString centerlines for clusters with coherent direction
        and length >= _MIN_SEGMENT_LENGTH_M.
    """
    if len(linear_points) < _MIN_BUILDINGS_PER_SEGMENT:
        return []

    # Connected-components clustering via KDTree pairs
    tree = KDTree(linear_points)
    pairs = tree.query_pairs(r=cluster_radius)

    # Build adjacency and find connected components via union-find
    n = len(linear_points)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        union(a, b)

    # Group points by component
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        clusters.setdefault(root, []).append(i)

    centerlines = []
    for indices in clusters.values():
        if len(indices) < _MIN_BUILDINGS_PER_SEGMENT:
            continue

        cluster_pts = linear_points[indices]
        centroid = cluster_pts.mean(axis=0)
        centered = cluster_pts - centroid
        _, S, Vt = np.linalg.svd(centered, full_matrices=False)

        # Check directional coherence
        if S[0] < 1e-9:
            continue
        cluster_linearity = (S[0] - S[1]) / S[0]
        if cluster_linearity < _CLUSTER_LINEARITY_THRESHOLD:
            continue

        direction = Vt[0]
        projections = centered @ direction
        start = centroid + projections.min() * direction
        end = centroid + projections.max() * direction

        seg_length = float(np.linalg.norm(end - start))
        if seg_length < _MIN_SEGMENT_LENGTH_M:
            continue

        centerlines.append(LineString([start.tolist(), end.tolist()]))

    return centerlines


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def detect_aligned_roads(
    coords: np.ndarray,
    cluster_radius: float = 30.0,
    median_nn: float | None = None,
) -> list[LineString]:
    """Detect road centerlines from building alignment patterns.

    Main entry point. Runs local PCA linearity scoring, clusters high-linearity
    points, validates directional coherence, and returns centerlines.

    Args:
        coords: (N, 2) UTM building centroid coordinates.
        cluster_radius: DBSCAN-equivalent clustering distance (adaptive from
            median nearest-neighbor distance in the caller).
        median_nn: Median nearest-neighbor distance. If None, computed here.

    Returns:
        List of LineString road centerlines in projected CRS.
        Empty list if too few points or no patterns detected.
    """
    if len(coords) < _MIN_BUILDINGS_PER_SEGMENT:
        return []

    # Compute median_nn if not provided
    if median_nn is None:
        tree = KDTree(coords)
        nn_dists = tree.query(coords, k=2)[0][:, 1]
        median_nn = float(np.median(nn_dists))

    # Step 1: Score each point for local linearity
    linearity = _score_linearity(coords, median_nn=median_nn)
    linear_mask = linearity > _LINEARITY_THRESHOLD
    linear_points = coords[linear_mask]

    if len(linear_points) < _MIN_BUILDINGS_PER_SEGMENT:
        logger.info(
            f"Building alignment: only {len(linear_points)} linear points "
            f"(need >= {_MIN_BUILDINGS_PER_SEGMENT}) — skipping"
        )
        return []

    logger.info(
        f"Building alignment: {linear_mask.sum()}/{len(coords)} points "
        f"have linearity > {_LINEARITY_THRESHOLD}"
    )

    # Step 2: Cluster, validate, and produce centerlines
    centerlines = _cluster_linear_points(linear_points, cluster_radius)

    if not centerlines:
        logger.info("Building alignment: no valid segments found")
        return []

    return centerlines
