"""Formats summarize_entity_graph rows as a compact ontology primer.

Lives in shared/, not chat_orchestrator/, specifically so both
chat_orchestrator's GraphProvider (P1's `graph` context-module source,
rendered into a prompt) and mcp_servers' knowledge server (P4's
get_graph_schema tool, rendered into a tool result) can call the exact same
formatter without one importing the other's package -- they are separate
deployables with separate dependency sets and no shared Python path. Built
once, reused in both places rather than rendering a second primer in the
MCP layer (see P1's original comment on this, when it lived in
chat_orchestrator's graph_provider.py before this move).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def render_primer(rows: List[Dict[str, Any]]) -> Optional[str]:
    """Format summarize_entity_graph rows as a compact ontology primer."""
    entities = [r for r in rows if r.get("kind") == "entity"]
    relationships = [r for r in rows if r.get("kind") == "relationship"]
    if not entities and not relationships:
        return None

    lines: List[str] = []
    if entities:
        lines.append("Entity types in the knowledge graph:")
        for row in entities:
            examples = ", ".join(row.get("examples") or [])
            suffix = f" — e.g. {examples}" if examples else ""
            lines.append(f"- {row['type_name']} ({row['item_count']}){suffix}")
    if relationships:
        if lines:
            lines.append("")
        lines.append("Relationship types:")
        for row in relationships:
            lines.append(f"- {row['type_name']} ({row['item_count']})")
    return "\n".join(lines)


__all__ = ["render_primer"]
