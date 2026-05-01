"""Light Preliminary Package step handlers.

Handlers for the package_generator expert's workflow steps:
- generate_distribution_layout: Auto-generate distribution layout (poles, cables) for sites without QGIS data
- generate_qgis_project: Generate QGIS .qgs + .gpkg project files from layout output
- copy_lpp_template: Copy Google Slides/Docs template and register with document tracker
- generate_distribution_map: Generate site map from pd_site_submissions data
- send_lpp_map_to_telegram: Send generated map image to user via Telegram
- fetch_solar_potential: Fetch solar generation potential from Global Solar Atlas
- fetch_geo_hazard: Fetch flood depth (WRI Aqueduct RP1000) and terrain elevation (Copernicus DEM)
- generate_powerplant_design: Create design in AppSheet (no BOM yet)
- generate_site_layout: Generate to-scale power plant site layout (Draw.io + PNG)
- update_design_distances: Update AppSheet design with real cable distances
- generate_site_bom: Trigger BOM generation after distances are updated
- dump_lpp_values: Dump all values to columns E/F for reference
- populate_lpp_cells: Populate Main Input sheet with site data
- populate_bom_tab: Create Full BOM sheet with items grouped by Component Type
"""

from orchestrator.experts.handlers.package_generator.copy_template import copy_lpp_template
from orchestrator.experts.handlers.package_generator.create_site_folder import create_site_folder
from orchestrator.experts.handlers.package_generator.dump_values import dump_lpp_values
from orchestrator.experts.handlers.package_generator.fetch_geo_hazard import fetch_geo_hazard
from orchestrator.experts.handlers.package_generator.fetch_solar_potential import (
    fetch_solar_potential,
)
from orchestrator.experts.handlers.package_generator.generate_bom import generate_site_bom
from orchestrator.experts.handlers.package_generator.generate_design import (
    generate_powerplant_design,
)
from orchestrator.experts.handlers.package_generator.generate_distribution_layout import (
    generate_distribution_layout,
)
from orchestrator.experts.handlers.package_generator.generate_map import generate_distribution_map
from orchestrator.experts.handlers.package_generator.generate_qgis_project import (
    generate_qgis_project,
)
from orchestrator.experts.handlers.package_generator.generate_site_layout import (
    generate_site_layout,
)
from orchestrator.experts.handlers.package_generator.populate_bom_tab import populate_bom_tab
from orchestrator.experts.handlers.package_generator.populate_cells import populate_lpp_cells
from orchestrator.experts.handlers.package_generator.resolve_sites import resolve_sites
from orchestrator.experts.handlers.package_generator.send_map_to_telegram import (
    send_lpp_map_to_telegram,
)
from orchestrator.experts.handlers.package_generator.update_design_distances import (
    update_design_distances,
)

__all__ = [
    "resolve_sites",
    "create_site_folder",
    "generate_distribution_layout",
    "generate_qgis_project",
    "copy_lpp_template",
    "generate_distribution_map",
    "send_lpp_map_to_telegram",
    "fetch_solar_potential",
    "fetch_geo_hazard",
    "generate_powerplant_design",
    "generate_site_layout",
    "update_design_distances",
    "generate_site_bom",
    "dump_lpp_values",
    "populate_lpp_cells",
    "populate_bom_tab",
]
