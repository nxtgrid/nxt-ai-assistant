"""Canonical component taxonomy for grouping prompts in the admin UI.

Each ``.prompt`` file declares which of these it belongs to via the
``component`` frontmatter field, based on which service actually calls
``PROMPTS.render``/``PROMPTS.text`` for it -- not on naming convention or
who owns the content (see ``owner``).
"""

from __future__ import annotations

UNCATEGORIZED = "uncategorized"

# Order here is display order in the admin UI.
COMPONENT_LABELS: "dict[str, str]" = {
    "orchestrator_services": "Orchestrator — Core services",
    "orchestrator_ticketing": "Orchestrator — Ticketing",
    "orchestrator_experts": "Orchestrator — Expert handlers",
    "mcp_servers": "MCP Servers",
    "anansi_app": "Anansi App / Scripts",
    "shared": "Shared / Cross-cutting",
}

COMPONENT_ORDER: "list[str]" = list(COMPONENT_LABELS)
