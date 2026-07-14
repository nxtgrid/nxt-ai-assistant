"""
Plot building functions for site maps.

This module handles matplotlib-based visualization:
- Creating figure and axes with satellite basemap
- Adding geographic features (buildings, poles, cables, boundaries)
- Exporting to various image formats
"""

import io
from typing import Optional, Tuple

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from shared.mapping.models import Building, Cable, Pole, SiteBoundary, SiteData

# Try to import contextily for satellite basemap
try:
    import contextily as ctx

    HAS_CONTEXTILY = True
except ImportError:
    HAS_CONTEXTILY = False

# Color scheme
COLORS = {
    "boundary": "#FFFF00",  # Yellow
    "served_building": "#4169E1",  # Royal blue
    "unserved_building": "#DC143C",  # Crimson red
    "pole_coverage": "#228B22",  # Forest green
    "pole_coverage_center": "#145214",  # Darker green for center
    "pole_marker": "#000000",  # Black
    "plant_marker": "#FF8C00",  # Dark orange for power plant
    "cable_backbone": "#1E90FF",  # Dodger blue for distribution/backbone
    "cable_building_path": "#FFA500",  # Orange for building path lines
    "cable_drop": "#999999",  # Grey for drop cables
    "fallback_background": "#2d5016",  # Dark green when no basemap
}


def _meters_to_degrees(meters: float, latitude: float) -> float:
    """Convert meters to approximate degrees at given latitude."""
    meters_per_degree: float = 111320 * float(np.cos(np.radians(latitude)))
    return meters / meters_per_degree


def _compute_adaptive_zoom(
    boundary: SiteBoundary,
    max_pixels: int = 2048,
    min_zoom: int = 12,
    max_zoom: int = 16,
) -> int:
    """Choose a satellite tile zoom from the site extent.

    Fixed zoom 16 is unnecessarily expensive for larger communities because it
    requests many more tiles than the output image can use. This mirrors the
    community-map approach: preserve detail for small extents, reduce zoom as
    the bounds grow.
    """
    width_m = (
        (boundary.maxx - boundary.minx) * 111_320.0 * float(np.cos(np.radians(boundary.center_lat)))
    )
    height_m = (boundary.maxy - boundary.miny) * 111_320.0
    max_extent_m = max(width_m, height_m, 1.0)
    zoom = int(np.log2(max_pixels * 40_075_016 / (max_extent_m * 256)))
    return max(min_zoom, min(max_zoom, zoom))


def prepare_base_map(
    boundary: SiteBoundary,
    figsize: Tuple[int, int] = (16, 12),
    padding_pct: float = 0.05,
    add_satellite: bool = True,
    zoom: Optional[int] = None,
) -> Tuple[Figure, Axes]:
    """
    Create a figure and axes with optional satellite basemap.

    Args:
        boundary: Site boundary for determining map extent
        figsize: Figure size in inches (width, height)
        padding_pct: Padding as percentage of extent (default 5%)
        add_satellite: Whether to add satellite imagery basemap
        zoom: Zoom level for satellite tiles (higher = more detail)

    Returns:
        Tuple of (Figure, Axes) ready for adding features
    """
    fig, ax = plt.subplots(1, 1, figsize=figsize)

    # Calculate padding
    width = boundary.maxx - boundary.minx
    height = boundary.maxy - boundary.miny
    padding_x = width * padding_pct
    padding_y = height * padding_pct

    # Set extent
    ax.set_xlim(boundary.minx - padding_x, boundary.maxx + padding_x)
    ax.set_ylim(boundary.miny - padding_y, boundary.maxy + padding_y)

    # Add satellite basemap
    if add_satellite and HAS_CONTEXTILY:
        try:
            tile_zoom = zoom if zoom is not None else _compute_adaptive_zoom(boundary)
            ctx.add_basemap(
                ax,
                crs="EPSG:4326",
                source=ctx.providers.Esri.WorldImagery,
                zoom=tile_zoom,
            )
        except Exception:
            # Fallback to solid background if tiles fail
            ax.set_facecolor(COLORS["fallback_background"])
    else:
        ax.set_facecolor(COLORS["fallback_background"])

    return fig, ax


def add_poles(
    ax: Axes,
    poles: list[Pole],
    coverage_radius_m: float = 50.0,
    center_lat: float = 7.0,
    show_coverage: bool = True,
    show_markers: bool = True,
) -> None:
    """
    Add poles to the map with coverage circles and markers.

    Draws gradient-style coverage circles (darker center, lighter edge)
    to match the reference styling.

    Args:
        ax: Matplotlib axes to draw on
        poles: List of Pole objects
        coverage_radius_m: Coverage radius in meters
        center_lat: Center latitude for degree conversion
        show_coverage: Whether to show coverage circles
        show_markers: Whether to show pole dot markers
    """
    if not poles:
        return

    radius_deg = _meters_to_degrees(coverage_radius_m, center_lat)

    if show_coverage:
        # Draw coverage circles with gradient effect (multiple layers).
        # PatchCollection keeps large sites from adding hundreds of artists one
        # by one to the axes.
        outer = []
        middle = []
        inner = []
        for pole in poles:
            outer.append(plt.Circle(pole.coords, radius_deg))
            middle.append(plt.Circle(pole.coords, radius_deg * 0.7))
            inner.append(plt.Circle(pole.coords, radius_deg * 0.4))

        ax.add_collection(
            PatchCollection(
                outer,
                facecolor=COLORS["pole_coverage"],
                edgecolor=COLORS["pole_coverage"],
                alpha=0.3,
                zorder=2,
            )
        )
        ax.add_collection(
            PatchCollection(
                middle,
                facecolor=COLORS["pole_coverage"],
                edgecolor=COLORS["pole_coverage"],
                alpha=0.3,
                zorder=2,
            )
        )
        ax.add_collection(
            PatchCollection(
                inner,
                facecolor=COLORS["pole_coverage_center"],
                edgecolor=COLORS["pole_coverage_center"],
                alpha=0.4,
                zorder=2,
            )
        )

    if show_markers:
        # Separate plant poles from regular poles
        regular_poles = [p for p in poles if p.properties.get("pole_type") != "plant"]
        plant_poles = [p for p in poles if p.properties.get("pole_type") == "plant"]

        # Draw regular pole markers
        if regular_poles:
            ax.scatter(
                [p.lon for p in regular_poles],
                [p.lat for p in regular_poles],
                c=COLORS["pole_marker"],
                s=30,
                zorder=5,
                edgecolors="white",
                linewidths=0.5,
            )

        # Draw power plant markers as stars
        if plant_poles:
            ax.scatter(
                [p.lon for p in plant_poles],
                [p.lat for p in plant_poles],
                c=COLORS["plant_marker"],
                s=250,
                marker="*",
                zorder=7,
                edgecolors="black",
                linewidths=0.8,
            )


def add_cables(ax: Axes, cables: list[Cable], linewidth: float = 1.5) -> None:
    """
    Add distribution cables/wires to the map.

    Backbone/distribution cables are drawn in blue, drop cables in thinner grey.

    Args:
        ax: Matplotlib axes to draw on
        cables: List of Cable objects
        linewidth: Line width for backbone cables (drops use 60% of this)
    """
    if not cables:
        return

    backbone_road = [
        c.coordinates
        for c in cables
        if c.properties.get("cable_type") != "drop"
        and c.properties.get("edge_type") != "building_path"
    ]
    backbone_path = [
        c.coordinates
        for c in cables
        if c.properties.get("cable_type") != "drop"
        and c.properties.get("edge_type") == "building_path"
    ]
    drops = [c.coordinates for c in cables if c.properties.get("cable_type") == "drop"]

    if backbone_road:
        ax.add_collection(
            LineCollection(
                backbone_road,
                colors=COLORS["cable_backbone"],
                linewidths=linewidth,
                zorder=3,
            )
        )

    if backbone_path:
        ax.add_collection(
            LineCollection(
                backbone_path,
                colors=COLORS["cable_building_path"],
                linewidths=linewidth,
                zorder=3,
            )
        )

    if drops:
        ax.add_collection(
            LineCollection(
                drops,
                colors=COLORS["cable_drop"],
                linewidths=linewidth * 0.6,
                zorder=3,
            )
        )


def add_buildings(
    ax: Axes,
    buildings: list[Building],
    alpha: float = 0.8,
) -> None:
    """
    Add buildings to the map with colors based on connection status.

    Served buildings are shown in blue, unserved in red.

    Args:
        ax: Matplotlib axes to draw on
        buildings: List of Building objects
        alpha: Opacity for building fill
    """
    if not buildings:
        return

    served_patches = []
    unserved_patches = []
    for building in buildings:
        polygon = mpatches.Polygon(building.coordinates)
        if building.connected:
            served_patches.append(polygon)
        else:
            unserved_patches.append(polygon)

    if served_patches:
        ax.add_collection(
            PatchCollection(
                served_patches,
                facecolor=COLORS["served_building"],
                edgecolor=COLORS["served_building"],
                alpha=alpha,
                zorder=4,
            )
        )
    if unserved_patches:
        ax.add_collection(
            PatchCollection(
                unserved_patches,
                facecolor=COLORS["unserved_building"],
                edgecolor=COLORS["unserved_building"],
                alpha=alpha,
                zorder=4,
            )
        )


def add_boundary(ax: Axes, boundary: SiteBoundary, linewidth: float = 3) -> None:
    """
    Add site boundary to the map.

    Args:
        ax: Matplotlib axes to draw on
        boundary: SiteBoundary object
        linewidth: Line width for boundary
    """
    boundary_patch = plt.Polygon(
        boundary.coords,
        fill=False,
        edgecolor=COLORS["boundary"],
        linewidth=linewidth,
        zorder=6,
    )
    ax.add_patch(boundary_patch)


def _add_legend(
    ax: Axes,
    site_data: SiteData,
    show_served_unserved: bool = True,
) -> None:
    """Add legend to the map."""
    coverage_radius = site_data.coverage_radius

    legend_elements = [
        mpatches.Patch(
            facecolor="none",
            edgecolor=COLORS["boundary"],
            linewidth=2,
            label="Site Boundary",
        ),
        mpatches.Patch(
            facecolor=COLORS["pole_coverage"],
            alpha=0.4,
            label=f"Pole Coverage ({coverage_radius:.0f}m)",
        ),
        Line2D(
            [0],
            [0],
            color=COLORS["cable_backbone"],
            linewidth=1.5,
            label="Distribution Lines",
        ),
        Line2D(
            [0],
            [0],
            color=COLORS["cable_building_path"],
            linewidth=1.5,
            label="Building Path Lines",
        ),
        Line2D(
            [0],
            [0],
            color=COLORS["cable_drop"],
            linewidth=0.9,
            label="Drop Cables",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            color="w",
            markerfacecolor=COLORS["pole_marker"],
            markersize=8,
            label=f"Poles ({len(site_data.poles)})",
        ),
        Line2D(
            [0],
            [0],
            marker="*",
            color="w",
            markerfacecolor=COLORS["plant_marker"],
            markeredgecolor="black",
            markersize=14,
            label="Power Plant",
        ),
    ]

    if show_served_unserved:
        served_count = len(site_data.served_buildings)
        unserved_count = len(site_data.unserved_buildings)
        legend_elements.extend(
            [
                mpatches.Patch(
                    facecolor=COLORS["served_building"],
                    alpha=0.8,
                    label=f"Served Buildings ({served_count})",
                ),
                mpatches.Patch(
                    facecolor=COLORS["unserved_building"],
                    alpha=0.8,
                    label=f"Unserved Buildings ({unserved_count})",
                ),
            ]
        )
    else:
        legend_elements.append(
            mpatches.Patch(
                facecolor=COLORS["served_building"],
                alpha=0.8,
                label=f"Buildings ({len(site_data.buildings)})",
            )
        )

    legend = ax.legend(
        handles=legend_elements,
        loc="upper right",
        fontsize=14,
        framealpha=0.85,
        facecolor="white",
        edgecolor="gray",
    )
    legend.set_zorder(10)  # Ensure legend is on top of all layers


def _add_scale_bar(ax: Axes, center_lat: float) -> None:
    """Add a scale bar to the bottom-left corner of the map."""
    # Determine a nice scale bar length based on map extent
    xlim = ax.get_xlim()
    map_width_deg = xlim[1] - xlim[0]
    map_width_m = map_width_deg * 111320 * float(np.cos(np.radians(center_lat)))

    # Pick a round scale bar length (~20% of map width)
    target_m = map_width_m * 0.2
    nice_lengths = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000]
    bar_m = min(nice_lengths, key=lambda x: abs(x - target_m))

    bar_deg = bar_m / (111320 * float(np.cos(np.radians(center_lat))))

    # Position in bottom-left
    ylim = ax.get_ylim()
    x_start = xlim[0] + (xlim[1] - xlim[0]) * 0.03
    y_pos = ylim[0] + (ylim[1] - ylim[0]) * 0.04

    # Draw the bar
    ax.plot(
        [x_start, x_start + bar_deg],
        [y_pos, y_pos],
        color="white",
        linewidth=4,
        solid_capstyle="butt",
        zorder=9,
    )
    # Black outline for contrast
    ax.plot(
        [x_start, x_start + bar_deg],
        [y_pos, y_pos],
        color="black",
        linewidth=6,
        solid_capstyle="butt",
        zorder=8,
    )

    # Label
    label = f"{bar_m}m" if bar_m < 1000 else f"{bar_m // 1000}km"
    ax.text(
        x_start + bar_deg / 2,
        y_pos + (ylim[1] - ylim[0]) * 0.012,
        label,
        fontsize=10,
        fontweight="bold",
        color="white",
        ha="center",
        va="bottom",
        zorder=9,
        bbox=dict(boxstyle="round,pad=0.15", facecolor="black", alpha=0.5, edgecolor="none"),
    )


def _add_title_and_labels(
    ax: Axes,
    fig: Figure,
    site_data: SiteData,
) -> None:
    """Add title, axis labels, and small ID in corner."""
    # Main title (without ID)
    ax.set_title(
        f"{site_data.site_name} Site Map - Distribution Network",
        fontsize=14,
        fontweight="bold",
    )

    ax.set_xlabel("Longitude", fontsize=14)
    ax.set_ylabel("Latitude", fontsize=14)

    # Add site ID as tiny number in bottom-right corner of the plot area
    ax.text(
        0.995,
        0.005,
        f"#{site_data.site_id}",
        fontsize=7,
        color="white",
        alpha=0.7,
        ha="right",
        va="bottom",
        transform=ax.transAxes,
        bbox=dict(boxstyle="round,pad=0.2", facecolor="black", alpha=0.3, edgecolor="none"),
    )


def prepare_site_plot(
    site_data: SiteData,
    figsize: Tuple[int, int] = (16, 12),
    add_satellite: bool = True,
    zoom: Optional[int] = None,
    show_served_unserved: bool = True,
) -> Tuple[Figure, Axes]:
    """
    Create a complete site map plot.

    This is the main entry point that combines all plotting functions.

    Args:
        site_data: Complete site data
        figsize: Figure size in inches
        add_satellite: Whether to add satellite basemap
        zoom: Zoom level for satellite tiles
        show_served_unserved: Whether to differentiate served/unserved buildings

    Returns:
        Tuple of (Figure, Axes) with all features drawn
    """
    # Create base map
    fig, ax = prepare_base_map(
        boundary=site_data.boundary,
        figsize=figsize,
        add_satellite=add_satellite,
        zoom=zoom,
    )

    # Add features in correct z-order
    add_poles(
        ax,
        site_data.poles,
        coverage_radius_m=site_data.coverage_radius,
        center_lat=site_data.boundary.center_lat,
    )
    add_cables(ax, site_data.cables)
    add_buildings(ax, site_data.buildings)
    add_boundary(ax, site_data.boundary)

    # Add legend, labels, and scale bar
    _add_legend(ax, site_data, show_served_unserved=show_served_unserved)
    _add_title_and_labels(ax, fig, site_data)
    _add_scale_bar(ax, site_data.boundary.center_lat)

    # Set equal aspect ratio
    ax.set_aspect("equal")

    return fig, ax


def export_png(fig: Figure, dpi: int = 150) -> bytes:
    """
    Export figure to PNG bytes.

    Args:
        fig: Matplotlib figure
        dpi: Resolution in dots per inch

    Returns:
        PNG image as bytes
    """
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    buf.seek(0)
    png_bytes = buf.read()
    buf.close()
    plt.close(fig)
    return png_bytes


def export_bytes(fig: Figure, format: str = "png", dpi: int = 150) -> bytes:
    """
    Export figure to image bytes in specified format.

    Args:
        fig: Matplotlib figure
        format: Image format (png, jpeg, svg, pdf)
        dpi: Resolution in dots per inch

    Returns:
        Image as bytes
    """
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format=format, dpi=dpi, bbox_inches="tight")
    buf.seek(0)
    image_bytes = buf.read()
    buf.close()
    plt.close(fig)
    return image_bytes
