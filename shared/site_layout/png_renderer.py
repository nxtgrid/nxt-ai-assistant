"""PNG renderer for site layouts using matplotlib.

Generates a raster image matching the Draw.io output colors and layout.
Output is base64-encoded PNG for embedding in workflow state and Telegram.
"""

import base64
import io
import math

import matplotlib

matplotlib.use("Agg")  # Headless backend for Docker

from collections import defaultdict

import matplotlib.patches as mpatches
import matplotlib.path as mpath
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, PathPatch, Rectangle
from matplotlib.patches import Polygon as MplPolygon

from shared.site_layout import (
    COMMS_BOX_SIZE_M,
    EP_RADIUS_M,
    ESS_MODULE_DEPTH_M,
    ESS_MODULE_FRONT_M,
    FEEDER_PILLAR_SIZE_M,
    LIGHTNING_RADIUS_M,
    PLINTH_SIZE_M,
    SiteLayout,
)


def render_png(layout: SiteLayout, dpi: int = 150) -> str:
    """Render SiteLayout to base64-encoded PNG string."""
    minx, miny, maxx, maxy = layout.boundary.bounds
    site_w = maxx - minx
    site_h = maxy - miny

    # Figure size: aim for ~10 inches on the longer dimension
    aspect = site_w / site_h if site_h > 0 else 1.0
    fig_w: float
    fig_h: float
    if aspect >= 1:
        fig_w = 12
        fig_h = 12 / aspect + 2  # extra for title
    else:
        fig_h = 12
        fig_w = 12 * aspect + 1

    fig, ax = plt.subplots(1, 1, figsize=(fig_w, fig_h))
    try:
        return _render_to_axes(fig, ax, layout, dpi)
    finally:
        plt.close(fig)


def _render_to_axes(fig, ax, layout: SiteLayout, dpi: int) -> str:
    """Render all layout elements to the axes and return base64 PNG."""
    minx, miny, maxx, maxy = layout.boundary.bounds
    site_w = maxx - minx
    site_h = maxy - miny

    # Boundary outline
    bcoords = list(layout.boundary.exterior.coords)
    bpoly = MplPolygon(bcoords, fill=False, edgecolor="black", linewidth=2)
    ax.add_patch(bpoly)

    # Boundary dimension labels
    for i in range(len(bcoords) - 1):
        p1, p2 = bcoords[i], bcoords[i + 1]
        edge_len = math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)
        mid_x = (p1[0] + p2[0]) / 2
        mid_y = (p1[1] + p2[1]) / 2
        ax.annotate(
            f"{edge_len:.1f}m",
            (mid_x, mid_y),
            fontsize=7,
            color="#666666",
            ha="center",
            va="center",
            bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.7},
        )

    # Fence (dashed)
    if layout.fence:
        fcoords = list(layout.fence.exterior.coords)
        fpoly = MplPolygon(fcoords, fill=False, edgecolor="#333333", linewidth=1, linestyle="--")
        ax.add_patch(fpoly)

    # Lightning arrester circles (draw early so they're behind other elements)
    for lx, ly in layout.lightning_positions:
        circle = Circle(
            (lx, ly),
            LIGHTNING_RADIUS_M,
            fill=True,
            facecolor="#FF000020",
            edgecolor="#CC0000",
            linewidth=0.5,
            linestyle="--",
        )
        ax.add_patch(circle)

    # Earth pit markers (filled brown circles, 1m radius symbol)
    for i, (epx, epy) in enumerate(layout.earth_pit_positions):
        ep_circle = Circle(
            (epx, epy),
            EP_RADIUS_M,
            fill=True,
            facecolor="#996633",
            edgecolor="#663300",
            linewidth=1.0,
            zorder=5,
        )
        ax.add_patch(ep_circle)
        ax.text(
            epx,
            epy + 1.5,
            f"EP{i + 1}",
            ha="center",
            va="bottom",
            fontsize=5,
            color="#663300",
            fontweight="bold",
            zorder=6,
        )

    # Solar arrays
    for idx, arr in enumerate(layout.arrays):
        # Array rectangle (pink)
        rect = Rectangle(
            (arr.origin_x, arr.origin_y),
            arr.array_width,
            arr.array_height,
            facecolor="#FF99CC",
            edgecolor="#CC0066",
            linewidth=0.8,
            alpha=0.8,
        )
        ax.add_patch(rect)

        # Array label
        config_label = f"{arr.panel_count}P"
        ax.text(
            arr.origin_x + arr.array_width / 2,
            arr.origin_y + arr.array_height / 2,
            config_label,
            fontsize=5,
            ha="center",
            va="center",
            color="#CC0066",
        )

        # Plinths (grey squares)
        for px, py in arr.plinths:
            plinth = Rectangle(
                (px, py),
                PLINTH_SIZE_M,
                PLINTH_SIZE_M,
                facecolor="#999999",
                edgecolor="#666666",
                linewidth=0.5,
            )
            ax.add_patch(plinth)

    # Energy system
    ex, ey, ew, eh = layout.energy_system_rect
    if layout.energy_system_type == "victron":
        cabin = Rectangle(
            (ex, ey),
            ew,
            eh,
            facecolor="#4472C4",
            edgecolor="#2F5496",
            linewidth=1.5,
        )
        ax.add_patch(cabin)
        ax.text(
            ex + ew / 2,
            ey + eh / 2,
            "Victron\nCabin",
            fontsize=6,
            ha="center",
            va="center",
            color="white",
            fontweight="bold",
        )
    else:
        # ESS combined plinth
        if layout.ess_plinth_rect:
            px, py, pw, ph = layout.ess_plinth_rect
            plinth = Rectangle(
                (px, py),
                pw,
                ph,
                facecolor="#D9D9D9",
                edgecolor="#999999",
                linewidth=0.8,
            )
            ax.add_patch(plinth)

        # Individual ESS modules
        for i, (mx, my) in enumerate(layout.ess_modules):
            mod = Rectangle(
                (mx, my),
                ESS_MODULE_FRONT_M,
                ESS_MODULE_DEPTH_M,
                facecolor="#70AD47",
                edgecolor="#507E32",
                linewidth=0.8,
            )
            ax.add_patch(mod)
            ax.text(
                mx + ESS_MODULE_FRONT_M / 2,
                my + ESS_MODULE_DEPTH_M / 2,
                f"E{i + 1}",
                fontsize=4,
                ha="center",
                va="center",
                color="white",
            )

    # Feeder pillar (orange)
    fpx, fpy = layout.feeder_pillar
    fp_rect = Rectangle(
        (fpx, fpy),
        FEEDER_PILLAR_SIZE_M,
        FEEDER_PILLAR_SIZE_M,
        facecolor="#ED7D31",
        edgecolor="#C55A11",
        linewidth=1.5,
    )
    ax.add_patch(fp_rect)
    ax.text(
        fpx + FEEDER_PILLAR_SIZE_M / 2,
        fpy + FEEDER_PILLAR_SIZE_M / 2,
        "FP",
        fontsize=5,
        ha="center",
        va="center",
        color="white",
        fontweight="bold",
    )

    # Comms box / VSAT (yellow)
    cbx, cby = layout.comms_box
    cb_rect = Rectangle(
        (cbx, cby),
        COMMS_BOX_SIZE_M,
        COMMS_BOX_SIZE_M,
        facecolor="#FFC000",
        edgecolor="#BF9000",
        linewidth=0.8,
    )
    ax.add_patch(cb_rect)
    ax.text(
        cbx + COMMS_BOX_SIZE_M / 2,
        cby + COMMS_BOX_SIZE_M / 2,
        "VSAT",
        fontsize=4,
        ha="center",
        va="center",
    )

    # Cable routes — smooth bezier curves with bunch grouping
    total_dc_length = 0.0
    total_ac_length = 0.0

    # Group routes by bunch_id for rendering
    bunches: dict[int, list] = defaultdict(list)
    for route in layout.cable_routes:
        bunches[route.bunch_id].append(route)

    def _draw_cable_path(ax, route, color, linewidth, alpha):
        """Draw a cable as a smooth bezier curve or straight line."""
        points = [route.start] + route.waypoints + [route.end]
        if len(points) == 2:
            # Straight diagonal
            ax.plot(
                [points[0][0], points[1][0]],
                [points[0][1], points[1][1]],
                color=color,
                linewidth=linewidth,
                alpha=alpha,
            )
        else:
            # Smooth quadratic bezier through waypoints using CURVE3
            verts = [points[0]]
            codes = [mpath.Path.MOVETO]
            for wp in points[1:-1]:
                verts.append(wp)
                codes.append(mpath.Path.CURVE3)
            verts.append(points[-1])
            codes.append(mpath.Path.CURVE3)
            path = mpath.Path(verts, codes)
            patch = PathPatch(
                path, facecolor="none", edgecolor=color, linewidth=linewidth, alpha=alpha
            )
            ax.add_patch(patch)

    for bunch_id, group in bunches.items():
        if group[0].cable_type == "ac":
            # AC route: draw individually
            for route in group:
                total_ac_length += route.length_m
                _draw_cable_path(ax, route, "#2980B9", 1.5, 0.8)
                pts = [route.start] + route.waypoints + [route.end]
                mid = pts[len(pts) // 2]
                ax.annotate(
                    f"AC {route.length_m:.1f}m",
                    mid,
                    fontsize=6,
                    color="#2980B9",
                    fontweight="bold",
                    ha="center",
                    va="bottom",
                    xytext=(0, 3),
                    textcoords="offset points",
                    bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.7},
                )
        elif len(group) > 1:
            # Bunched DC: single thick line with ×N label
            rep = group[0]
            bunch_length = sum(r.length_m for r in group)
            total_dc_length += bunch_length
            _draw_cable_path(ax, rep, "#E74C3C", 1.5, 0.7)
            pts = [rep.start] + rep.waypoints + [rep.end]
            mid = pts[len(pts) // 2]
            ax.annotate(
                f"\u00d7{len(group)} ({bunch_length:.0f}m)",
                mid,
                fontsize=5,
                color="#E74C3C",
                fontweight="bold",
                ha="center",
                va="bottom",
                xytext=(0, 3),
                textcoords="offset points",
                bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.7},
            )
        else:
            # Solo DC cable
            route = group[0]
            total_dc_length += route.length_m
            _draw_cable_path(ax, route, "#E74C3C", 0.7, 0.6)

    # Entrance gate marker
    enx, eny = layout.entrance_pos
    ax.plot(enx, eny, "s", color="#333333", markersize=6)
    ax.annotate(
        "Gate",
        (enx, eny),
        fontsize=6,
        ha="center",
        va="bottom",
        xytext=(0, 5),
        textcoords="offset points",
    )

    # Title with cable summary
    title_lines = [
        f"{layout.site_name} - Site Layout",
        f"Modules: {layout.total_modules} | "
        f"Achieved: {layout.achieved_kwp:.1f} kWp | "
        f"Target: {layout.target_kwp:.1f} kWp",
    ]
    if total_dc_length > 0 or total_ac_length > 0:
        contingency = 1.15
        title_lines.append(
            f"DC cable: {total_dc_length * contingency:.0f}m | "
            f"AC cable: {total_ac_length * contingency:.0f}m "
            f"(incl. 15% contingency)"
        )
    ax.set_title("\n".join(title_lines), fontsize=10, fontweight="bold")

    # North arrow
    arrow_x = maxx - site_w * 0.05
    arrow_y = maxy - site_h * 0.1
    ax.annotate(
        "N",
        xy=(arrow_x, arrow_y + site_h * 0.05),
        fontsize=12,
        fontweight="bold",
        ha="center",
    )
    ax.annotate(
        "",
        xy=(arrow_x, arrow_y + site_h * 0.04),
        xytext=(arrow_x, arrow_y),
        arrowprops={"arrowstyle": "->", "lw": 2, "color": "black"},
    )

    # Axis settings
    margin = max(site_w, site_h) * 0.05
    ax.set_xlim(minx - margin, maxx + margin)
    ax.set_ylim(miny - margin, maxy + margin)
    ax.set_aspect("equal")
    ax.set_xlabel("meters")
    ax.set_ylabel("meters")
    ax.grid(True, alpha=0.2)

    # Legend
    legend_patches = [
        mpatches.Patch(color="#FF99CC", label="PV Arrays"),
        mpatches.Patch(color="#999999", label="Plinths"),
        mpatches.Patch(color="#FF000020", label="Lightning Coverage"),
        mpatches.Patch(color="#ED7D31", label="Feeder Pillar"),
        mpatches.Patch(color="#FFC000", label="VSAT/Comms"),
    ]
    if layout.energy_system_type == "victron":
        legend_patches.insert(2, mpatches.Patch(color="#4472C4", label="Victron Cabin"))
    else:
        legend_patches.insert(2, mpatches.Patch(color="#70AD47", label="ESS Modules"))

    if layout.cable_routes:
        legend_patches.append(mpatches.Patch(color="#E74C3C", label="DC Cable (PV→ESS)"))
        legend_patches.append(mpatches.Patch(color="#2980B9", label="AC Cable (ESS→FP)"))

    ax.legend(handles=legend_patches, loc="lower right", fontsize=6, framealpha=0.8)

    # Warnings overlay
    if layout.warnings:
        ax.text(
            0.02,
            0.02,
            "\n".join(layout.warnings),
            transform=ax.transAxes,
            fontsize=7,
            color="red",
            va="bottom",
            bbox={"boxstyle": "round", "fc": "lightyellow", "ec": "red", "alpha": 0.8},
        )

    plt.tight_layout()

    # Render to bytes
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    buf.seek(0)

    return base64.b64encode(buf.read()).decode("ascii")
