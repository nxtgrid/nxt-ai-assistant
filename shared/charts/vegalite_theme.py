"""Shared Vega-Lite theme configuration using Tableau10 color scheme.

This module provides a consistent theme for all Vega-Lite charts across the application.
"""

from typing import Any, Dict

# Tableau10 color palette
# https://vega.github.io/vega/docs/schemes/#tableau10
TABLEAU10 = [
    "#4e79a7",  # Blue
    "#f28e2b",  # Orange
    "#e15759",  # Red
    "#76b7b2",  # Teal
    "#59a14f",  # Green
    "#edc948",  # Yellow
    "#b07aa1",  # Purple
    "#ff9da7",  # Pink
    "#9c755f",  # Brown
    "#bab0ac",  # Gray
]

# Semantic color mappings for domain-specific use
SEMANTIC_COLORS = {
    # Power/Energy metrics
    "inverter": TABLEAU10[0],  # Blue
    "grid": TABLEAU10[4],  # Green
    "battery": TABLEAU10[1],  # Orange
    "pv": TABLEAU10[5],  # Yellow
    "load": TABLEAU10[3],  # Teal
    # Status indicators
    "outage": TABLEAU10[2],  # Red
    "warning": TABLEAU10[1],  # Orange
    "success": TABLEAU10[4],  # Green
    "info": TABLEAU10[0],  # Blue
    # Phase colors (for 3-phase power)
    "L1": TABLEAU10[0],  # Blue
    "L2": TABLEAU10[4],  # Green
    "L3": TABLEAU10[1],  # Orange
}

# Vega-Lite theme configuration
VEGALITE_THEME: Dict[str, Any] = {
    "background": "white",
    "title": {
        "font": "Inter, system-ui, sans-serif",
        "fontSize": 18,
        "fontWeight": 600,
        "color": "#333333",
        "anchor": "start",
    },
    "axis": {
        "labelFont": "Inter, system-ui, sans-serif",
        "labelFontSize": 14,
        "labelColor": "#666666",
        "titleFont": "Inter, system-ui, sans-serif",
        "titleFontSize": 15,
        "titleFontWeight": 500,
        "titleColor": "#333333",
        "gridColor": "#e0e0e0",
        "gridOpacity": 0.5,
        "domainColor": "#cccccc",
        "tickColor": "#cccccc",
    },
    "legend": {
        "labelFont": "Inter, system-ui, sans-serif",
        "labelFontSize": 14,
        "labelColor": "#666666",
        "titleFont": "Inter, system-ui, sans-serif",
        "titleFontSize": 15,
        "titleFontWeight": 500,
        "titleColor": "#333333",
        "symbolSize": 120,
        "orient": "bottom",
    },
    "view": {
        "stroke": "transparent",
    },
    "range": {
        "category": TABLEAU10,
    },
    "mark": {
        "tooltip": True,
    },
    "line": {
        "strokeWidth": 3,
    },
    "area": {
        "opacity": 0.7,
    },
    "bar": {
        "cornerRadiusEnd": 2,
    },
    "arc": {
        "stroke": "white",
        "strokeWidth": 1,
    },
}


def apply_theme(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the shared theme to a Vega-Lite specification.

    Args:
        spec: Vega-Lite specification dict

    Returns:
        Specification with theme config applied
    """
    # Don't modify the original
    themed_spec = dict(spec)

    # Add or merge config
    if "config" not in themed_spec:
        themed_spec["config"] = {}

    # Merge theme into config (existing config takes precedence)
    for key, value in VEGALITE_THEME.items():
        if key not in themed_spec["config"]:
            themed_spec["config"][key] = value
        elif isinstance(value, dict) and isinstance(themed_spec["config"][key], dict):
            # Merge nested dicts
            merged = dict(value)
            merged.update(themed_spec["config"][key])
            themed_spec["config"][key] = merged

    return themed_spec


def get_color(name: str, index: int = 0) -> str:
    """Get a color from the theme.

    Args:
        name: Semantic color name (e.g., 'inverter', 'battery') or 'palette'
        index: For 'palette', the index in Tableau10 (0-9)

    Returns:
        Hex color string
    """
    if name == "palette":
        return TABLEAU10[index % len(TABLEAU10)]
    return SEMANTIC_COLORS.get(name, TABLEAU10[index % len(TABLEAU10)])
