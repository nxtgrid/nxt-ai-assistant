"""Vega-Lite chart generation for equipment diagnostics.

Generates PNG charts for power timelines, battery status, outage events, etc.
Uses the shared Tableau10 theme for consistent styling.
"""

import base64
from typing import Any, Dict, List, Optional

try:
    import vl_convert as vlc

    VL_AVAILABLE = True
except ImportError:
    VL_AVAILABLE = False

from shared.charts import SEMANTIC_COLORS, apply_theme

from ..analyzers.grid_outage_analyzer import GridOutageEvent
from ..platforms.base_platform import HistoricalDataPoint


class ChartBuilder:
    """Builds Vega-Lite charts for equipment diagnostics."""

    # Default chart dimensions
    DEFAULT_WIDTH = 600
    DEFAULT_HEIGHT = 400

    # Use shared color scheme from theme
    COLORS = SEMANTIC_COLORS

    def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT):
        """Initialize chart builder.

        Args:
            width: Chart width in pixels
            height: Chart height in pixels
        """
        self.width = width
        self.height = height

        if not VL_AVAILABLE:
            raise ImportError(
                "vl-convert-python is required for chart generation. "
                "Install with: pip install vl-convert-python"
            )

    def _render_chart(self, spec: Dict[str, Any]) -> bytes:
        """Render a Vega-Lite spec to PNG bytes with theme applied."""
        themed_spec = apply_theme(spec)
        png_data: bytes = vlc.vegalite_to_png(themed_spec, scale=2)
        return png_data

    def render_to_base64(self, spec: Dict[str, Any]) -> str:
        """Render a Vega-Lite spec to base64-encoded PNG."""
        png_bytes = self._render_chart(spec)
        return base64.b64encode(png_bytes).decode("utf-8")

    def build_power_timeline(
        self,
        data_points: List[HistoricalDataPoint],
        title: str = "Power Timeline",
        outages: Optional[List[GridOutageEvent]] = None,
        show_phases: bool = True,
    ) -> Dict[str, Any]:
        """Build a power timeline chart with optional outage highlighting.

        Args:
            data_points: Historical power data
            title: Chart title
            outages: Optional list of outages to highlight
            show_phases: If True, show per-phase lines; if False, show total only

        Returns:
            Vega-Lite specification dict
        """
        # Transform data for Vega-Lite
        chart_data = []

        if show_phases:
            for point in data_points:
                label = f"{point.metric}"
                if point.phase:
                    label = f"{point.metric} {point.phase}"

                chart_data.append(
                    {
                        "timestamp": point.timestamp.isoformat(),
                        "power": point.value,
                        "series": label,
                    }
                )
        else:
            # Group by timestamp and sum
            timestamp_totals: Dict[str, float] = {}
            for point in data_points:
                ts = point.timestamp.isoformat()
                if ts not in timestamp_totals:
                    timestamp_totals[ts] = 0
                timestamp_totals[ts] += point.value

            for ts, total in timestamp_totals.items():
                chart_data.append(
                    {
                        "timestamp": ts,
                        "power": total,
                        "series": "Total Power",
                    }
                )

        layers = []

        # Main power line layer
        power_layer = {
            "data": {"values": chart_data},
            "mark": {
                "type": "line",
                "interpolate": "monotone",
                "strokeWidth": 2,
            },
            "encoding": {
                "x": {
                    "field": "timestamp",
                    "type": "temporal",
                    "title": "Time",
                    "axis": {"format": "%H:%M", "labelAngle": -45},
                },
                "y": {
                    "field": "power",
                    "type": "quantitative",
                    "title": "Power (W)",
                },
                "color": {
                    "field": "series",
                    "type": "nominal",
                    "legend": {"title": "Metric"},
                },
            },
        }
        layers.append(power_layer)

        # Add outage highlight rectangles
        if outages:
            outage_data = [
                {
                    "start": o.start_time.isoformat(),
                    "end": o.end_time.isoformat(),
                }
                for o in outages
            ]

            if outage_data:
                outage_layer = {
                    "data": {"values": outage_data},
                    "mark": {
                        "type": "rect",
                        "opacity": 0.2,
                        "color": self.COLORS["outage"],
                    },
                    "encoding": {
                        "x": {"field": "start", "type": "temporal"},
                        "x2": {"field": "end", "type": "temporal"},
                    },
                }
                layers.insert(0, outage_layer)  # Put behind the lines

        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": title,
            "width": self.width,
            "height": self.height,
            "layer": layers,
        }

        return spec

    def build_battery_soc_chart(
        self,
        data_points: List[HistoricalDataPoint],
        title: str = "Battery State of Charge",
    ) -> Dict[str, Any]:
        """Build a battery SOC area chart.

        Args:
            data_points: Historical battery SOC data
            title: Chart title

        Returns:
            Vega-Lite specification dict
        """
        # Filter for battery_soc metric
        soc_data = [
            {
                "timestamp": point.timestamp.isoformat(),
                "soc": point.value,
            }
            for point in data_points
            if point.metric == "battery_soc"
        ]

        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": title,
            "width": self.width,
            "height": self.height,
            "data": {"values": soc_data},
            "mark": {
                "type": "area",
                "interpolate": "monotone",
                "color": self.COLORS["battery"],
                "opacity": 0.7,
                "line": {"color": self.COLORS["battery"]},
            },
            "encoding": {
                "x": {
                    "field": "timestamp",
                    "type": "temporal",
                    "title": "Time",
                    "axis": {"format": "%H:%M", "labelAngle": -45},
                },
                "y": {
                    "field": "soc",
                    "type": "quantitative",
                    "title": "State of Charge (%)",
                    "scale": {"domain": [0, 100]},
                },
            },
        }

        return spec

    def build_grid_vs_inverter_chart(
        self,
        data_points: List[HistoricalDataPoint],
        title: str = "Grid vs Inverter Power",
    ) -> Dict[str, Any]:
        """Build a stacked area chart comparing grid and inverter power.

        Args:
            data_points: Historical power data
            title: Chart title

        Returns:
            Vega-Lite specification dict
        """
        # Separate and aggregate grid vs inverter data
        chart_data = []

        # Group by timestamp
        timestamp_data: Dict[str, Dict[str, float]] = {}

        for point in data_points:
            ts = point.timestamp.isoformat()
            if ts not in timestamp_data:
                timestamp_data[ts] = {"grid": 0, "inverter": 0}

            if "grid" in point.metric:
                timestamp_data[ts]["grid"] += point.value
            elif "inverter" in point.metric:
                timestamp_data[ts]["inverter"] += point.value

        for ts, values in timestamp_data.items():
            chart_data.append({"timestamp": ts, "power": values["grid"], "source": "Grid"})
            chart_data.append({"timestamp": ts, "power": values["inverter"], "source": "Inverter"})

        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": title,
            "width": self.width,
            "height": self.height,
            "data": {"values": chart_data},
            "mark": {
                "type": "area",
                "interpolate": "monotone",
                "opacity": 0.7,
            },
            "encoding": {
                "x": {
                    "field": "timestamp",
                    "type": "temporal",
                    "title": "Time",
                    "axis": {"format": "%H:%M", "labelAngle": -45},
                },
                "y": {
                    "field": "power",
                    "type": "quantitative",
                    "title": "Power (W)",
                    "stack": True,
                },
                "color": {
                    "field": "source",
                    "type": "nominal",
                    "scale": {
                        "domain": ["Grid", "Inverter"],
                        "range": [self.COLORS["grid"], self.COLORS["inverter"]],
                    },
                    "legend": {"title": "Source"},
                },
            },
        }

        return spec

    def build_load_distribution_chart(
        self,
        phase_loads: Dict[str, float],
        title: str = "Load Distribution by Phase",
    ) -> Dict[str, Any]:
        """Build a pie/donut chart showing load distribution by phase.

        Args:
            phase_loads: Dict with L1, L2, L3 power values
            title: Chart title

        Returns:
            Vega-Lite specification dict
        """
        total = sum(phase_loads.values())
        chart_data = []

        for phase, load in phase_loads.items():
            pct = (load / total * 100) if total > 0 else 0
            chart_data.append(
                {
                    "phase": phase,
                    "load": load,
                    "label": f"{load:.0f}W ({pct:.0f}%)",
                }
            )

        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": title,
            "width": self.width,
            "height": self.height,
            "data": {"values": chart_data},
            "layer": [
                {
                    "mark": {
                        "type": "arc",
                        "innerRadius": 60,
                        "outerRadius": 140,
                    },
                    "encoding": {
                        "theta": {"field": "load", "type": "quantitative"},
                        "color": {
                            "field": "phase",
                            "type": "nominal",
                            "scale": {
                                "domain": ["L1", "L2", "L3"],
                                "range": [
                                    self.COLORS["L1"],
                                    self.COLORS["L2"],
                                    self.COLORS["L3"],
                                ],
                            },
                            "legend": {"title": "Phase"},
                        },
                    },
                },
                {
                    "mark": {
                        "type": "text",
                        "radius": 110,
                        "fontSize": 14,
                        "fontWeight": "bold",
                    },
                    "encoding": {
                        "theta": {"field": "load", "type": "quantitative", "stack": True},
                        "text": {"field": "label", "type": "nominal"},
                    },
                },
            ],
        }

        return spec

    def build_outage_events_chart(
        self,
        data_points: List[HistoricalDataPoint],
        outages: List[GridOutageEvent],
        title: str = "Outage Events Timeline",
    ) -> Dict[str, Any]:
        """Build a chart highlighting outage events on a power timeline.

        Args:
            data_points: Historical power data
            outages: List of outage events
            title: Chart title

        Returns:
            Vega-Lite specification dict
        """
        # This is similar to power_timeline but focuses on outage visibility
        return self.build_power_timeline(
            data_points=data_points,
            title=title,
            outages=outages,
            show_phases=False,  # Show total only for clarity
        )

    def generate_chart(
        self,
        chart_type: str,
        data_points: List[HistoricalDataPoint],
        grid_name: str,
        outages: Optional[List[GridOutageEvent]] = None,
        phase_loads: Optional[Dict[str, float]] = None,
        time_range: str = "last_24h",
    ) -> str:
        """Generate a chart and return base64-encoded PNG.

        Args:
            chart_type: Type of chart to generate
            data_points: Historical data
            grid_name: Name of the grid (for title)
            outages: Optional outage events
            phase_loads: Optional phase load data for distribution chart
            time_range: Time range string for title

        Returns:
            Base64-encoded PNG string
        """
        title_suffix = f" - {grid_name} ({time_range})"

        if chart_type == "power_timeline":
            spec = self.build_power_timeline(
                data_points=data_points,
                title=f"Power Timeline{title_suffix}",
                outages=outages,
                show_phases=True,
            )

        elif chart_type == "battery_soc":
            spec = self.build_battery_soc_chart(
                data_points=data_points,
                title=f"Battery SOC{title_suffix}",
            )

        elif chart_type == "grid_vs_inverter":
            spec = self.build_grid_vs_inverter_chart(
                data_points=data_points,
                title=f"Grid vs Inverter{title_suffix}",
            )

        elif chart_type == "load_distribution":
            if not phase_loads:
                # Calculate from data points
                phase_loads = {"L1": 0, "L2": 0, "L3": 0}
                for point in data_points:
                    if point.phase and point.phase in phase_loads:
                        # Use latest value
                        phase_loads[point.phase] = point.value

            spec = self.build_load_distribution_chart(
                phase_loads=phase_loads,
                title=f"Load Distribution{title_suffix}",
            )

        elif chart_type == "outage_events":
            spec = self.build_outage_events_chart(
                data_points=data_points,
                outages=outages or [],
                title=f"Outage Events{title_suffix}",
            )

        else:
            raise ValueError(f"Unknown chart type: {chart_type}")

        return self.render_to_base64(spec)
