"""The knowledge graph's ontology, as a context module.

Renders what P4's agentic graph tools need the model to already know: which
entity types exist, which relationship types connect them, and a few real
entity names so the model can pattern-match its own queries.
"""

from __future__ import annotations

from typing import Any, Optional

from shared.graph_primer import render_primer
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

MAX_TYPES = 20
EXAMPLES_PER_TYPE = 3

# render_primer moved to shared/graph_primer.py so mcp_servers' knowledge
# server (get_graph_schema, P4 Phase 3) can call the identical formatter --
# chat_orchestrator and mcp_servers are separate deployables with no shared
# Python path, so it couldn't stay here and be reused there. Re-exported
# under this name so existing imports (including this module's own tests)
# keep working unchanged.


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
        # reads as unrestricted. summarize_entity_graph's p_org_ids is
        # integer[] (0020 corrected this from an unworkable uuid[] -- see
        # the real-permission-model memory), so cast explicitly rather than
        # relying on PostgREST to coerce a numeric-looking string; a
        # genuinely non-numeric organization id is a data problem worth
        # surfacing, not silently swallowing into an empty/unrestricted query.
        if ctx.is_staff:
            org_ids = None
        elif ctx.organization_ids:
            try:
                org_ids = [int(o) for o in ctx.organization_ids]
            except (TypeError, ValueError) as e:
                LOGGER.warning(f"Non-integer organization id in {ctx.organization_ids}: {e}")
                return None
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
