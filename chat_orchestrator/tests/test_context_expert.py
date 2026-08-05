"""Context expert step handlers: module proposal, dedup, storage."""

import pytest

from orchestrator.experts.handlers.context_expert.detect_module_duplicates import (
    classify_collision,
    hash_body,
    unique_slug,
)
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


def test_hash_body_ignores_whitespace_and_case():
    assert hash_body("Hello   World") == hash_body("hello world")


def test_classify_collision_identical_body():
    existing = [{"slug": "azimuth", "title": "Azimuth", "body": "Angle from north."}]
    assert classify_collision("azimuth", "Azimuth", "angle from   NORTH.", existing) == "identical"


def test_classify_collision_same_slug_different_body():
    existing = [{"slug": "azimuth", "title": "Azimuth", "body": "Angle from north."}]
    assert classify_collision("azimuth", "Azimuth", "Completely new text.", existing) == "slug_taken"


def test_classify_collision_same_title_different_slug():
    existing = [{"slug": "azimuth-calc", "title": "Azimuth", "body": "Angle."}]
    assert classify_collision("azimuth-v2", "Azimuth", "Other text.", existing) == "title_taken"


def test_classify_collision_none():
    assert classify_collision("brand-new", "Brand New", "Text.", []) == "none"


def test_unique_slug_appends_suffix():
    assert unique_slug("azimuth", {"azimuth", "azimuth-2"}) == "azimuth-3"
    assert unique_slug("fresh", {"azimuth"}) == "fresh"
