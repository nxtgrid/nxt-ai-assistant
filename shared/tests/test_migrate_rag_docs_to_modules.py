"""Pure helpers for the RAG-document -> knowledge_modules migration."""

import pytest

from scripts.migrate_rag_docs_to_modules import (
    CURATED,
    assemble_body,
    build_module_row,
    is_migration_candidate,
)


def test_candidate_selects_technical_but_not_cet_rules():
    assert is_migration_candidate(
        {"title": "Guidelines for Sizing PV to MPPT cables", "metadata": {"doc_type": "technical"}}
    )
    assert not is_migration_candidate(
        {"title": "CET-rules.pdf", "metadata": {"doc_type": "technical"}}
    )
    assert not is_migration_candidate(
        {"title": "NXT-3555-Procedure_1", "metadata": {"doc_type": "support_example"}}
    )


def test_assemble_body_joins_chunks_in_index_order():
    chunks = [
        {"chunk_index": 1, "content": "second"},
        {"chunk_index": 0, "content": "first"},
    ]
    assert assemble_body(chunks) == "first\n\nsecond"


def test_assemble_body_rejects_empty_chunks():
    with pytest.raises(ValueError, match="no chunks"):
        assemble_body([])


def test_curated_covers_exactly_fourteen_documents():
    assert len(CURATED) == 14
    assert len({entry["slug"] for entry in CURATED.values()}) == 14


def test_build_module_row_is_on_demand_and_traceable():
    doc = {
        "id": "doc-uuid-1",
        "title": "Technical: Azimuth Calculation Azimuth is defined as the angle... by Vaibhav Vaidya",
        "metadata": {"doc_type": "technical"},
    }
    row = build_module_row(doc, body="### Azimuth\n\nAngle from true north.", summary="How azimuth is measured.")
    assert row["slug"] == "azimuth-calculation"
    assert row["title"] == "Azimuth Calculation"
    assert row["mode"] == "on_demand"
    assert row["scope"] == "sector"
    assert row["tags"] == []
    assert row["source"] == "ingested"
    assert row["source_ref"] == "doc-uuid-1"
    assert row["body"] == "### Azimuth\n\nAngle from true north."
    assert row["summary"] == "How azimuth is measured."


def test_build_module_row_rejects_unknown_document():
    with pytest.raises(KeyError):
        build_module_row({"id": "x", "title": "Not In Curated List"}, body="b", summary="s")
