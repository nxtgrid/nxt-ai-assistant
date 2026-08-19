"""The knowledge graph's ontology, as a context module.

Renders what P4's agentic graph tools need the model to already know: which
entity types exist, which relationship types connect them, and a few real
entity names so the model can pattern-match its own queries. Built here and
reused there -- do not render a second primer in the MCP layer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

MAX_TYPES = 20
EXAMPLES_PER_TYPE = 3


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


class GraphProvider:
    """Resolves the `graph` source."""

    source = "graph"

    def __init__(self, client: Any = None) -> None:
        self._client = client if client is not None else _default_client()

    async def resolve(
        self, module: KnowledgeModule, ctx: ResolutionContext
    ) -> Optional[str]:
        if self._client is None:
            LOGGER.warning("GraphProvider has no database client; skipping")
            return None

        # Staff see everything; everyone else is scoped to their orgs. A
        # caller with no orgs gets nothing -- never NULL, which the RPC
        # reads as unrestricted.
        if ctx.is_staff:
            org_ids = None
        elif ctx.organization_ids:
            org_ids = list(ctx.organization_ids)
        else:
            return None

        try:
            result = self._client.rpc(
                "summarize_entity_graph",
                {
                    "p_org_ids": org_ids,
                    "p_max_types": MAX_TYPES,
                    "p_examples": EXAMPLES_PER_TYPE,
                },
            ).execute()
        except Exception as e:
            LOGGER.warning(f"summarize_entity_graph failed: {e}")
            return None

        return render_primer(result.data or [])


def _default_client() -> Any:
    from shared.config.db_credentials import chat_db_service_key, chat_db_url

    url, key = chat_db_url(), chat_db_service_key()
    if not (url and key):
        return None
    try:
        from supabase import create_client

        return create_client(url, key)
    except Exception:
        LOGGER.warning("Could not build the graph provider client", exc_info=True)
        return None


__all__ = ["GraphProvider", "render_primer"]
