"""Context expert step handlers: module proposal, dedup, storage."""

import pytest

from orchestrator.experts.handlers.context_expert.detect_module_duplicates import (
    classify_collision,
    hash_body,
    unique_slug,
)
from orchestrator.experts.handlers.context_expert.prepare_module_approval import (
    build_approval_text,
)
from orchestrator.experts.handlers.context_expert.propose_module import (
    normalize_slug,
    parse_proposal,
)
from orchestrator.experts.handlers.context_expert.select_prompts import (
    format_prompt_choices,
    parse_prompt_selection,
)
from orchestrator.experts.handlers.context_expert.store_module import (
    build_module_payload,
    resolve_mode,
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


def test_format_prompt_choices_numbers_them():
    text = format_prompt_choices(
        [("staff.system", "Staff assistant"), ("customer.system", "Customer bot")]
    )
    assert "1. staff.system — Staff assistant" in text
    assert "2. customer.system — Customer bot" in text


def test_parse_prompt_selection_accepts_numbers():
    ids = ["staff.system", "customer.system", "troubleshooting.procedures"]
    assert parse_prompt_selection("1, 3", ids) == ["staff.system", "troubleshooting.procedures"]


def test_parse_prompt_selection_accepts_ids():
    ids = ["staff.system", "customer.system"]
    assert parse_prompt_selection("customer.system", ids) == ["customer.system"]


def test_parse_prompt_selection_none_means_empty():
    assert parse_prompt_selection("none", ["staff.system"]) == []


def test_parse_prompt_selection_ignores_out_of_range():
    assert parse_prompt_selection("9", ["staff.system"]) == []


def test_build_approval_text_shows_identity_and_targets():
    text = build_approval_text(
        slug="azimuth-calculation",
        title="Azimuth Calculation",
        summary="How PV azimuth is measured.",
        body="A" * 500,
        mode="on_demand",
        prompt_ids=["staff.system"],
    )
    assert "azimuth-calculation" in text
    assert "How PV azimuth is measured." in text
    assert "on_demand" in text
    assert "staff.system" in text
    assert "500 chars" in text


def test_build_approval_text_warns_when_unattached():
    text = build_approval_text(
        slug="x", title="X", summary="s", body="b", mode="on_demand", prompt_ids=[]
    )
    assert "not attached to any prompt" in text


def test_resolve_mode_defaults_to_on_demand():
    assert resolve_mode("A" * 100) == "on_demand"
    assert resolve_mode("A" * 5000) == "on_demand"


def test_build_module_payload_shape():
    payload = build_module_payload(
        slug="azimuth", title="Azimuth", summary="s", body="b",
        mode="on_demand", actor="ops@example.com",
    )
    assert payload == {
        "slug": "azimuth",
        "title": "Azimuth",
        "summary": "s",
        "body": "b",
        "tags": [],
        "scope": "global",
        "mode": "on_demand",
        "source": "manual",
        "updated_by": "ops@example.com",
    }
