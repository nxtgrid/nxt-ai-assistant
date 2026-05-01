"""Distribution layout engine for auto-generating pole/cable placement."""

from shared.layout.pipeline import generate_layout
from shared.layout.road_network import PlantSite, SiteSelectionResult, find_plant_sites

__all__ = ["generate_layout", "find_plant_sites", "PlantSite", "SiteSelectionResult"]
