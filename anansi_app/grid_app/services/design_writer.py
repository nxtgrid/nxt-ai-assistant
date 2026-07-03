"""Shim — engine moved to shared/grid_design (used by both anansi_app and the
grid_design MCP server). Import from shared.grid_design in new code."""

from shared.grid_design.design_writer import *  # noqa: F401,F403
