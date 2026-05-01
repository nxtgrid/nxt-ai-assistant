"""Auto-placement algorithms for lightning arrestors and power jumpers.

Phase 2 of QGIS template generation: populates the empty annotation layers
from Phase 1 using network-distance algorithms per operator placement standards.
"""

from shared.layout.annotations._graph import build_backbone_graph
from shared.layout.annotations.lightning_arrestors import place_lightning_arrestors
from shared.layout.annotations.power_jumpers import place_power_jumpers

__all__ = ["build_backbone_graph", "place_lightning_arrestors", "place_power_jumpers"]
