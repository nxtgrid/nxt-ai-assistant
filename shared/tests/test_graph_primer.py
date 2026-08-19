"""The ontology primer formatter, shared between chat_orchestrator's
GraphProvider and mcp_servers' get_graph_schema tool -- see the module
docstring in shared/graph_primer.py for why it lives here."""

from shared.graph_primer import render_primer


def _rows():
    return [
        {"kind": "entity", "type_name": "Meter", "item_count": 120,
         "examples": ["M-001", "M-002", "M-003"]},
        {"kind": "entity", "type_name": "DCU", "item_count": 18,
         "examples": ["DCU-7721"]},
        {"kind": "relationship", "type_name": "connected_to", "item_count": 140,
         "examples": []},
    ]


def test_primer_lists_entity_types_with_counts():
    text = render_primer(_rows())
    assert "Meter" in text and "120" in text
    assert "DCU" in text and "18" in text


def test_primer_lists_relationship_types():
    text = render_primer(_rows())
    assert "connected_to" in text and "140" in text


def test_primer_includes_examples_for_entity_types():
    assert "M-001" in render_primer(_rows())


def test_primer_returns_none_for_no_rows():
    assert render_primer([]) is None


def test_primer_stays_compact():
    """It is pinned into every request that attaches the module."""
    assert len(render_primer(_rows())) < 1500
