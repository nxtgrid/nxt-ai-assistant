"""Draw.io XML renderer for site layouts.

Generates .drawio XML files with grouped container cells for human editability.
Uses meters as the drawing unit (1 drawing unit = 1 real meter).
3-level group hierarchy: site -> component groups -> elements.

Output is valid Draw.io XML that opens in diagrams.net with full group support.
"""

import base64
import math
import xml.etree.ElementTree as ET
from datetime import datetime

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

# Draw.io scale: 1 unit = 1 meter. Multiply by this for pixel positioning.
# Draw.io default is 1 unit = 1 pixel at 100% zoom. We use pageScale to set meters.
SCALE = 40  # 40 pixels per meter for readable diagrams


def render_drawio(layout: SiteLayout) -> str:
    """Render SiteLayout to Draw.io XML string."""
    root = ET.Element("mxGraphModel")
    root.set("dx", "0")
    root.set("dy", "0")
    root.set("grid", "1")
    root.set("gridSize", str(SCALE))  # 1m grid
    root.set("guides", "1")
    root.set("tooltips", "1")
    root.set("connect", "1")
    root.set("arrows", "1")
    root.set("fold", "1")
    root.set("page", "1")
    root.set("pageScale", "1")
    root.set("math", "0")
    root.set("shadow", "0")

    graph_root = ET.SubElement(root, "root")

    # Default parent cells (required by Draw.io)
    _cell(graph_root, id="0")
    _cell(graph_root, id="1", parent="0")

    cell_id = [10]  # mutable counter

    def next_id(prefix: str = "") -> str:
        cell_id[0] += 1
        return f"{prefix}{cell_id[0]}"

    # Compute offset so boundary starts near origin
    minx, miny, maxx, maxy = layout.boundary.bounds
    ox, oy = minx, miny  # offset to subtract

    def sx(x: float) -> float:
        return float((x - ox) * SCALE)

    def sy(y: float) -> float:
        # Flip Y axis (Draw.io Y increases downward)
        return float((maxy - miny - (y - oy)) * SCALE)

    # --- Top-level site group ---
    site_group_id = next_id("site-")
    _group(graph_root, id=site_group_id, parent="1", label=f"Site Layout: {layout.site_name}")

    # --- Boundary / Fence group ---
    boundary_group_id = next_id("boundary-")
    _group(graph_root, id=boundary_group_id, parent=site_group_id, label="Site Boundary")

    if layout.fence:
        coords = list(layout.fence.exterior.coords)
        _polygon(
            graph_root,
            id=next_id("fence-"),
            parent=boundary_group_id,
            coords=[(sx(x), sy(y)) for x, y in coords],
            style="strokeColor=#333333;fillColor=none;strokeWidth=2;dashed=1;",
            label="Fence",
        )

    # Boundary outline
    bcoords = list(layout.boundary.exterior.coords)
    _polygon(
        graph_root,
        id=next_id("bnd-"),
        parent=boundary_group_id,
        coords=[(sx(x), sy(y)) for x, y in bcoords],
        style="strokeColor=#000000;fillColor=none;strokeWidth=3;",
        label="Boundary",
    )

    # Boundary dimension labels on each edge
    for i in range(len(bcoords) - 1):
        p1, p2 = bcoords[i], bcoords[i + 1]
        edge_len = math.sqrt((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2)
        mid_x = (sx(p1[0]) + sx(p2[0])) / 2
        mid_y = (sy(p1[1]) + sy(p2[1])) / 2
        _text(
            graph_root,
            id=next_id("dim-"),
            parent=boundary_group_id,
            x=mid_x - 30,
            y=mid_y - 10,
            w=60,
            h=20,
            label=f"{edge_len:.1f}m",
            style="text;fontSize=10;fontColor=#666666;align=center;",
        )

    # --- Array groups ---
    for idx, arr in enumerate(layout.arrays):
        arr_group_id = next_id(f"array{idx}-")
        config_label = f"{arr.panel_count}P"
        _group(
            graph_root,
            id=arr_group_id,
            parent=site_group_id,
            label=f"Array {idx + 1} ({config_label})",
        )

        # Panel rectangle (entire array as one colored block)
        _rect(
            graph_root,
            id=next_id("panels-"),
            parent=arr_group_id,
            x=sx(arr.origin_x),
            y=sy(arr.origin_y + arr.array_height),
            w=arr.array_width * SCALE,
            h=arr.array_height * SCALE,
            style="fillColor=#FF99CC;strokeColor=#CC0066;strokeWidth=1;opacity=80;",
            label=config_label,
        )

        # Plinths within the array
        for px, py in arr.plinths:
            _rect(
                graph_root,
                id=next_id("plinth-"),
                parent=arr_group_id,
                x=sx(px),
                y=sy(py + PLINTH_SIZE_M),
                w=PLINTH_SIZE_M * SCALE,
                h=PLINTH_SIZE_M * SCALE,
                style="fillColor=#999999;strokeColor=#666666;strokeWidth=1;",
            )

        # Array dimension text
        _text(
            graph_root,
            id=next_id("arrdim-"),
            parent=arr_group_id,
            x=sx(arr.origin_x),
            y=sy(arr.origin_y) + 5,
            w=arr.array_width * SCALE,
            h=15,
            label=f"{arr.array_width:.1f}m x {arr.array_height:.1f}m",
            style="text;fontSize=8;fontColor=#CC0066;align=center;",
        )

    # --- Energy System group ---
    energy_group_id = next_id("energy-")
    _group(
        graph_root,
        id=energy_group_id,
        parent=site_group_id,
        label=f"Energy System ({layout.energy_system_type.upper()})",
    )

    ex, ey, ew, eh = layout.energy_system_rect
    if layout.energy_system_type == "victron":
        _rect(
            graph_root,
            id=next_id("cabin-"),
            parent=energy_group_id,
            x=sx(ex),
            y=sy(ey + eh),
            w=ew * SCALE,
            h=eh * SCALE,
            style="fillColor=#4472C4;strokeColor=#2F5496;strokeWidth=2;",
            label="Victron Cabin",
        )
    else:
        # ESS combined plinth
        if layout.ess_plinth_rect:
            px, py, pw, ph = layout.ess_plinth_rect
            _rect(
                graph_root,
                id=next_id("essplinth-"),
                parent=energy_group_id,
                x=sx(px),
                y=sy(py + ph),
                w=pw * SCALE,
                h=ph * SCALE,
                style="fillColor=#D9D9D9;strokeColor=#999999;strokeWidth=1;",
                label="ESS Plinth",
            )

        # Individual ESS modules
        for i, (mx, my) in enumerate(layout.ess_modules):
            _rect(
                graph_root,
                id=next_id("ess-"),
                parent=energy_group_id,
                x=sx(mx),
                y=sy(my + ESS_MODULE_DEPTH_M),
                w=ESS_MODULE_FRONT_M * SCALE,
                h=ESS_MODULE_DEPTH_M * SCALE,
                style="fillColor=#70AD47;strokeColor=#507E32;strokeWidth=1;",
                label=f"ESS{i + 1}",
            )

    # --- Lightning group ---
    if layout.lightning_positions:
        lightning_group_id = next_id("lightning-")
        _group(
            graph_root,
            id=lightning_group_id,
            parent=site_group_id,
            label="Lightning Arresters",
        )

        for i, (lx, ly) in enumerate(layout.lightning_positions):
            _ellipse(
                graph_root,
                id=next_id("la-"),
                parent=lightning_group_id,
                cx=sx(lx),
                cy=sy(ly),
                rx=LIGHTNING_RADIUS_M * SCALE,
                ry=LIGHTNING_RADIUS_M * SCALE,
                style="fillColor=#FF0000;strokeColor=#CC0000;strokeWidth=1;opacity=15;dashed=1;",
                label=f"LA{i + 1}",
            )

    # --- Earth pits group ---
    _EP_RADIUS = EP_RADIUS_M
    if layout.earth_pit_positions:
        ep_group_id = next_id("earthpit-")
        _group(
            graph_root,
            id=ep_group_id,
            parent=site_group_id,
            label="Earth Pits",
        )

        for i, (epx, epy) in enumerate(layout.earth_pit_positions):
            _ellipse(
                graph_root,
                id=next_id("ep-"),
                parent=ep_group_id,
                cx=sx(epx),
                cy=sy(epy),
                rx=_EP_RADIUS * SCALE,
                ry=_EP_RADIUS * SCALE,
                style=(
                    "fillColor=#996633;strokeColor=#663300;strokeWidth=2;"
                    "fontColor=#FFFFFF;fontSize=8;fontStyle=1;"
                ),
                label=f"EP{i + 1}",
            )

    # --- Infrastructure group ---
    infra_group_id = next_id("infra-")
    _group(graph_root, id=infra_group_id, parent=site_group_id, label="Infrastructure")

    # Feeder pillar
    fpx, fpy = layout.feeder_pillar
    _rect(
        graph_root,
        id=next_id("fp-"),
        parent=infra_group_id,
        x=sx(fpx),
        y=sy(fpy + FEEDER_PILLAR_SIZE_M),
        w=FEEDER_PILLAR_SIZE_M * SCALE,
        h=FEEDER_PILLAR_SIZE_M * SCALE,
        style="fillColor=#ED7D31;strokeColor=#C55A11;strokeWidth=2;",
        label="FP",
    )

    # Comms box (VSAT)
    cbx, cby = layout.comms_box
    _rect(
        graph_root,
        id=next_id("vsat-"),
        parent=infra_group_id,
        x=sx(cbx),
        y=sy(cby + COMMS_BOX_SIZE_M),
        w=COMMS_BOX_SIZE_M * SCALE,
        h=COMMS_BOX_SIZE_M * SCALE,
        style="fillColor=#FFC000;strokeColor=#BF9000;strokeWidth=1;",
        label="VSAT",
    )

    # Entrance marker
    enx, eny = layout.entrance_pos
    _rect(
        graph_root,
        id=next_id("gate-"),
        parent=infra_group_id,
        x=sx(enx) - 15,
        y=sy(eny) - 5,
        w=30,
        h=10,
        style="fillColor=#FFFFFF;strokeColor=#333333;strokeWidth=2;",
        label="Gate",
    )

    # --- Annotations group ---
    ann_group_id = next_id("ann-")
    _group(graph_root, id=ann_group_id, parent=site_group_id, label="Annotations")

    # Title
    site_w = (maxx - minx) * SCALE
    _text(
        graph_root,
        id=next_id("title-"),
        parent=ann_group_id,
        x=0,
        y=-40,
        w=site_w,
        h=30,
        label=f"{layout.site_name} - Site Layout ({datetime.now().strftime('%Y-%m-%d')})",
        style="text;fontSize=16;fontStyle=1;fontColor=#000000;align=center;",
    )

    # Module count note
    _text(
        graph_root,
        id=next_id("count-"),
        parent=ann_group_id,
        x=0,
        y=-20,
        w=site_w,
        h=15,
        label=(f"Installable modules: {layout.total_modules} ({layout.achieved_kwp:.1f} kWp)"),
        style="text;fontSize=11;fontColor=#333333;align=center;",
    )

    # North arrow (simple text)
    _text(
        graph_root,
        id=next_id("north-"),
        parent=ann_group_id,
        x=site_w - 50,
        y=-35,
        w=40,
        h=30,
        label="N\u2191",
        style="text;fontSize=18;fontStyle=1;fontColor=#000000;align=center;",
    )

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# ---------------------------------------------------------------------------
# Draw.io XML helpers
# ---------------------------------------------------------------------------
def _cell(parent_el: ET.Element, **attrs) -> ET.Element:
    """Create a basic mxCell element."""
    cell = ET.SubElement(parent_el, "mxCell")
    for k, v in attrs.items():
        cell.set(k, str(v))
    return cell


def _group(parent_el: ET.Element, id: str, parent: str, label: str = "") -> ET.Element:
    """Create a group container cell."""
    cell = ET.SubElement(parent_el, "mxCell")
    cell.set("id", id)
    cell.set("value", label)
    cell.set("style", "group")
    cell.set("vertex", "1")
    cell.set("connectable", "0")
    cell.set("parent", parent)
    geo = ET.SubElement(cell, "mxGeometry")
    geo.set("as", "geometry")
    return cell


def _rect(
    parent_el: ET.Element,
    id: str,
    parent: str,
    x: float,
    y: float,
    w: float,
    h: float,
    style: str,
    label: str = "",
) -> ET.Element:
    """Create a rectangle cell."""
    cell = ET.SubElement(parent_el, "mxCell")
    cell.set("id", id)
    cell.set("value", label)
    cell.set("style", style)
    cell.set("vertex", "1")
    cell.set("parent", parent)
    geo = ET.SubElement(cell, "mxGeometry")
    geo.set("x", f"{x:.1f}")
    geo.set("y", f"{y:.1f}")
    geo.set("width", f"{w:.1f}")
    geo.set("height", f"{h:.1f}")
    geo.set("as", "geometry")
    return cell


def _text(
    parent_el: ET.Element,
    id: str,
    parent: str,
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    style: str,
) -> ET.Element:
    """Create a text label cell."""
    full_style = f"{style}verticalAlign=middle;whiteSpace=wrap;overflow=hidden;"
    return _rect(parent_el, id=id, parent=parent, x=x, y=y, w=w, h=h, style=full_style, label=label)


def _ellipse(
    parent_el: ET.Element,
    id: str,
    parent: str,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    style: str,
    label: str = "",
) -> ET.Element:
    """Create an ellipse cell."""
    full_style = f"ellipse;{style}"
    return _rect(
        parent_el,
        id=id,
        parent=parent,
        x=cx - rx,
        y=cy - ry,
        w=rx * 2,
        h=ry * 2,
        style=full_style,
        label=label,
    )


def _polygon(
    parent_el: ET.Element,
    id: str,
    parent: str,
    coords: list[tuple[float, float]],
    style: str,
    label: str = "",
) -> ET.Element:
    """Create a polygon via a series of connected points.

    Draw.io doesn't have a native polygon cell, so we use a custom shape
    with the points encoded in the style.
    """
    if not coords:
        return _cell(parent_el, id=id, parent=parent)

    # Compute bounding box
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    bx, by = min(xs), min(ys)
    bw, bh = max(xs) - bx, max(ys) - by

    if bw == 0 or bh == 0:
        return _cell(parent_el, id=id, parent=parent)

    # Normalize coordinates to 0-1 range for stencil
    norm = [(f"{(x - bx) / bw:.4f}", f"{(y - by) / bh:.4f}") for x, y in coords]

    # Build custom shape stencil path
    moves = [f"M {norm[0][0]} {norm[0][1]}"]
    for nx, ny in norm[1:]:
        moves.append(f"L {nx} {ny}")
    moves.append("Z")
    path = " ".join(moves)

    shape_style = f"shape=stencil({_encode_stencil(path)});{style}"

    cell = ET.SubElement(parent_el, "mxCell")
    cell.set("id", id)
    cell.set("value", label)
    cell.set("style", shape_style)
    cell.set("vertex", "1")
    cell.set("parent", parent)
    geo = ET.SubElement(cell, "mxGeometry")
    geo.set("x", f"{bx:.1f}")
    geo.set("y", f"{by:.1f}")
    geo.set("width", f"{bw:.1f}")
    geo.set("height", f"{bh:.1f}")
    geo.set("as", "geometry")
    return cell


def _encode_stencil(path: str) -> str:
    """Encode a path into a Draw.io stencil XML, then base64 encode it.

    Draw.io stencil format:
    <shape><foreground><path>...</path></foreground></shape>
    """
    stencil = (
        '<shape name="custom" w="1" h="1" aspect="variable">'
        "<foreground>"
        f"<path>{path}</path>"
        "<fillstroke/>"
        "</foreground>"
        "</shape>"
    )
    return base64.b64encode(stencil.encode()).decode()
