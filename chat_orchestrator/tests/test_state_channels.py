"""Regression tests for ConversationState channel declarations.

LangGraph only propagates keys that are declared as channels in the state
schema. A node that returns an *undeclared* key has that key silently dropped
before the next node runs. This bit us with ``expert_raw_request``: it was set
by ``expert_router`` and read by ``ask_about_duplicate`` / ``expert_handler``,
but was never declared in ``ConversationState`` — so the full NL request (and
the user parameters it carries, e.g. ``technology_family=deye``) was lost across
the very first node hop, and fresh packets fell back to the synthetic
``lpp <lat>,<lon>`` command → Victron/DEFAULT instead of Deye.
"""

import pytest
from langgraph.graph import END, START, StateGraph

from orchestrator.graphs.state import ConversationState


def test_expert_raw_request_is_a_declared_channel():
    """expert_raw_request must be declared so it survives node boundaries."""
    assert "expert_raw_request" in ConversationState.__annotations__


def test_expert_raw_request_survives_a_node_hop():
    """Prove the declared key actually propagates node -> node in LangGraph.

    node_a sets expert_raw_request; node_b (a separate node, mirroring the
    expert_router -> ask_about_duplicate edge) must still see it. Before the
    fix this returned None because the key had no channel.
    """

    def node_a(state):
        return {"expert_raw_request": "create an LPP ... using Deye technology"}

    def node_b(state):
        # Mirror what ask_about_duplicate does: read it back out of state.
        return {"expert_command": state.get("expert_raw_request")}

    builder = StateGraph(ConversationState)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    app = builder.compile()

    out = app.invoke({})

    assert out.get("expert_raw_request") == "create an LPP ... using Deye technology"
    # node_b observed the value across the hop (not None).
    assert out.get("expert_command") == "create an LPP ... using Deye technology"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
