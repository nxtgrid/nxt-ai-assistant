"""Tests for scripts/seed_knowledge_modules.py -- proposing knowledge modules
from an existing instructions document's '## ' sections.

`scripts` has no `__init__.py` but is importable as a namespace package given
the repo root is on PYTHONPATH (see test_backfill_design_artifacts.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts.seed_knowledge_modules import _write, propose_modules

DOC = """## Power and Comms

Meters lose GSM when the grid is down.

## Site ABC Notes

The DCU sits in the container.
"""


def test_proposes_one_module_per_heading():
    assert [m["slug"] for m in propose_modules(DOC)] == ["power-and-comms", "site-abc-notes"]


def test_body_excludes_the_heading():
    modules = propose_modules(DOC)
    assert modules[0]["body"].strip() == "Meters lose GSM when the grid is down."


def test_summary_is_the_first_sentence():
    assert propose_modules(DOC)[0]["summary"] == "Meters lose GSM when the grid is down."


def test_site_headings_are_proposed_as_site_scoped():
    assert propose_modules(DOC)[1]["scope"] == "site:ABC"


def test_non_site_headings_default_to_sector():
    assert propose_modules(DOC)[0]["scope"] == "sector"


def test_empty_document_proposes_nothing():
    assert propose_modules("") == []


def test_heading_with_no_body_is_skipped():
    assert propose_modules("## Empty Heading\n\n## Real Heading\n\nSome text.") == [
        {
            "slug": "real-heading",
            "title": "Real Heading",
            "summary": "Some text.",
            "body": "Some text.",
            "tags": [],
            "scope": "sector",
            "mode": "pinned",
        }
    ]


# ── --write path ─────────────────────────────────────────────────────────────


def test_write_inserts_each_module_with_the_given_actor():
    modules = propose_modules(DOC)
    fake_store = MagicMock()
    fake_store._client = MagicMock()
    with patch(
        "shared.prompts.knowledge.KnowledgeStore.from_env", return_value=fake_store
    ):
        _write(modules, actor="ops@example.com")

    insert_calls = fake_store._client.table.return_value.insert.call_args_list
    assert len(insert_calls) == len(modules)
    inserted_rows = [call.args[0] for call in insert_calls]
    assert all(row["updated_by"] == "ops@example.com" for row in inserted_rows)
    assert [row["slug"] for row in inserted_rows] == [m["slug"] for m in modules]


def test_write_exits_when_storage_not_configured():
    import pytest

    fake_store = MagicMock()
    fake_store._client = None
    with patch(
        "shared.prompts.knowledge.KnowledgeStore.from_env", return_value=fake_store
    ):
        with pytest.raises(SystemExit):
            _write(propose_modules(DOC), actor="ops@example.com")
