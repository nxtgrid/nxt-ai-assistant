"""Context expert step handlers: module proposal, dedup, storage."""

import pytest

from orchestrator.experts.handlers.context_expert.propose_module import (
    normalize_slug,
    parse_proposal,
)


def test_normalize_slug_is_kebab_case():
    assert normalize_slug("Azimuth Calculation") == "azimuth-calculation"
    assert normalize_slug("BYD / Pylontech  Voltage!") == "byd-pylontech-voltage"
    assert normalize_slug("--already-kebab--") == "already-kebab"


def test_normalize_slug_rejects_empty():
    with pytest.raises(ValueError, match="empty slug"):
        normalize_slug("!!!")


def test_parse_proposal_reads_llm_json():
    raw = '{"slug": "Victron LED Codes", "title": "Victron LED Codes", "summary": "Decodes LED states."}'
    proposal = parse_proposal(raw)
    assert proposal == {
        "slug": "victron-led-codes",
        "title": "Victron LED Codes",
        "summary": "Decodes LED states.",
    }


def test_parse_proposal_rejects_missing_summary():
    with pytest.raises(ValueError, match="summary"):
        parse_proposal('{"slug": "a", "title": "A"}')


def test_parse_proposal_rejects_non_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_proposal("I think this should be called Azimuth.")
