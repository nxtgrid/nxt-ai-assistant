"""Graph tools: formatting, permission scoping, and error quality.

An agent self-corrects from a good error and gives up on a bad one, so the
error text is part of the interface and is tested as such.
"""

import pytest
from servers.knowledge_server.knowledge_mcp_server import (
    STAFF_ORG_ID,
    format_entity_results,
    format_neighbors,
    org_ids_for_request,
    suggest_near_matches,
)


def test_org_ids_are_null_for_staff():
    assert org_ids_for_request(STAFF_ORG_ID) is None


def test_org_ids_are_the_callers_org_otherwise():
    # organization_id is a single int -- ToolExecutor's _execute_direct_tool/
    # _execute_bridge_tool inject exactly one, never an is_staff flag or a
    # list of orgs (see the real-permission-model memory's MCP addendum, or
    # tool_executor.py's enriched_arguments). The graph RPCs still take
    # integer[], so this wraps the one org id in a list.
    assert org_ids_for_request(7) == [7]


def test_a_request_with_no_organization_id_raises_rather_than_widening():
    """NULL means unrestricted; reaching it by accident hands over the graph."""
    with pytest.raises(ValueError, match="organization_id"):
        org_ids_for_request(None)


def test_entity_results_are_formatted_with_ids():
    rows = [{"id": "abc-123", "name": "DCU-7721", "type": "DCU", "description": "A DCU."}]
    text = format_entity_results(rows, query="DCU")
    assert "DCU-7721" in text
    assert "abc-123" in text


def test_no_matches_suggests_near_names_from_the_permitted_set_only():
    """A suggestion built from unfiltered names leaks entities the caller cannot see."""
    text = suggest_near_matches("DCU-7712", permitted_names=["DCU-7721", "DCU-7112"])
    assert "DCU-7721" in text
    assert "DCU-7112" in text


def test_no_matches_and_no_permitted_names_says_so_without_naming_anything():
    text = suggest_near_matches("DCU-7712", permitted_names=[])
    assert "DCU-7712" in text
    assert "no" in text.lower()


def test_empty_entity_results_never_return_a_bare_empty_list():
    text = format_entity_results([], query="Autor")
    assert text.strip()
    assert "Autor" in text


def test_neighbors_include_the_relationship_and_direction():
    rows = [
        {"neighbor_id": "m-1", "neighbor_name": "M-001", "neighbor_type": "Meter",
         "relationship_type": "connected_to", "description": "on the DCU",
         "direction": "outgoing"},
    ]
    text = format_neighbors(rows, entity_id="d-1")
    assert "M-001" in text
    assert "connected_to" in text
    assert "outgoing" in text


def test_no_neighbors_is_stated_not_returned_empty():
    text = format_neighbors([], entity_id="d-1")
    assert "d-1" in text
    assert text.strip()
