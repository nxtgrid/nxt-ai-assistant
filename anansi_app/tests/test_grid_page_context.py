"""Grid Design pages publish what they are showing to the chat widget.

Deliberately conservative about identifiers: the grid-design tables live in a
different database from the auth DB whose grid ids the bot's org-scoped tools
use, so a design row's `id` must NOT be published as EntityContext's typed
`grid_id`. The grid NAME is published instead -- the bot can resolve that
through its own tools without being handed an id that means something else.
"""

from __future__ import annotations

import ast
import os

from grid_app.entities import get_entity
from nicegui_app.page_context import to_entity_context
from nicegui_app.pages import grid as grid_page

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GRID_PATH = os.path.join(_REPO_ROOT, "anansi_app", "nicegui_app", "pages", "grid.py")


def _grids_spec():
    spec = get_entity("grids")
    assert spec is not None, "the 'grids' entity must exist in db/entities.json"
    return spec


def test_record_context_labels_by_the_entity_and_its_display_column():
    spec = _grids_spec()
    page = grid_page.build_grid_record_page_context(spec, {"id": "42", "name": "Alpha"})
    assert page.kind == "grid_record"
    assert page.label == "Grids: Alpha"


def test_record_context_publishes_the_entity_and_record_id():
    spec = _grids_spec()
    page = grid_page.build_grid_record_page_context(spec, {"id": "42", "name": "Alpha"})
    assert page.identifiers["entity"] == "grids"
    assert page.identifiers["record_id"] == "42"


def test_a_design_row_id_never_becomes_the_typed_grid_id():
    """The auth DB's grids.id is a different namespace. Publishing the design
    row's id as grid_id would point org-scoped tools at the wrong record."""
    spec = _grids_spec()
    page = grid_page.build_grid_record_page_context(spec, {"id": "42", "name": "Alpha"})
    assert "grid_id" not in to_entity_context(page)


def test_a_grids_row_publishes_its_name_so_the_bot_can_resolve_it():
    spec = _grids_spec()
    page = grid_page.build_grid_record_page_context(spec, {"id": "42", "name": "Alpha"})
    assert page.identifiers["grid_name"] == "Alpha"


def test_a_non_grid_entity_publishes_no_grid_name():
    spec = get_entity("components")
    assert spec is not None
    page = grid_page.build_grid_record_page_context(spec, {"id": "7", "name": "MPPT"})
    assert "grid_name" not in page.identifiers


def test_record_context_summarises_scalar_columns_only():
    spec = _grids_spec()
    # `stage` is a real list column; a dict value for it must be dropped, and a
    # string value for `name` kept -- proving the primitive filter, not just
    # that unknown keys are ignored.
    row = {"id": "42", "name": "Alpha", "stage": {"nested": "object"}}
    summary = grid_page.build_grid_record_page_context(spec, row).summary_text()
    assert "Name: Alpha" in summary
    assert "nested" not in summary
    assert "Stage:" not in summary


def test_record_context_falls_back_to_the_pk_when_there_is_no_display_value():
    spec = _grids_spec()
    page = grid_page.build_grid_record_page_context(spec, {"id": "42"})
    assert page.label == "Grids: 42"


def test_list_context_reports_the_count_and_a_capped_sample():
    spec = _grids_spec()
    rows = [{"id": str(i), "name": f"Grid {i}"} for i in range(25)]
    page = grid_page.build_grid_list_page_context(spec, rows)
    assert page.kind == "grid_list"
    assert page.label == "Grids (25)"
    summary = page.summary_text()
    assert "Grid 0" in summary
    assert "Grid 11" not in summary


def test_grid_page_publishes_context_on_both_views():
    tree = ast.parse(open(_GRID_PATH).read())
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "set_page_context"
            for inner in ast.walk(node)
        )
    }
    assert "_render_list" in callers
    assert "_render_detail" in callers
