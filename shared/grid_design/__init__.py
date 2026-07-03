"""Grid design & BOM compute engine (ported from AppSheet's Apps Script).

Shared between the anansi_app Streamlit UI (grid_app package) and the
grid_design MCP server. All data lives in the Chat DB ``gd_*`` tables
(see anansi_app/db/schema.sql); connection settings in ``settings.py``.
"""
