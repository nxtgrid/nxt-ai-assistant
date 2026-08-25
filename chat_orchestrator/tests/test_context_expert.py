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
        prompt_ids=["staff.system"],
    )
    assert "azimuth-calculation" in text
    assert "How PV azimuth is measured." in text
    assert "staff.system" in text
    assert "500 chars" in text
    # No tier to report: an attached module is inlined in full.
    assert "Mode" not in text


def test_build_approval_text_warns_when_unattached():
    text = build_approval_text(
        slug="x", title="X", summary="s", body="b", prompt_ids=[]
    )
    assert "not attached to any prompt" in text


def test_build_module_payload_shape():
    payload = build_module_payload(
        slug="azimuth", title="Azimuth", summary="s", body="b",
        actor="ops@example.com",
    )
    # No "mode" key: nothing reads that column any more, and it has a NOT
    # NULL DEFAULT, so omitting it works with or without migration 0029.
    assert payload == {
        "slug": "azimuth",
        "title": "Azimuth",
        "summary": "s",
        "body": "b",
        "tags": [],
        "scope": "global",
        "source": "manual",
        "updated_by": "ops@example.com",
    }


def test_a_typed_module_payload_is_unchanged():
    payload = build_module_payload(
        slug="s", title="T", summary="x", body="typed body",
        actor="tech@example.com",
    )

    assert payload["source"] == "manual"
    assert payload["body"] == "typed body"
    assert "source_ref" not in payload


def test_a_doc_linked_payload_stores_no_body():
    """The document is the source of truth; a stored copy would only drift."""
    payload = build_module_payload(
        slug="s", title="T", summary="x", body="ignored",
        actor="tech@example.com",
        source="gdoc", source_ref="doc-1", source_tab="Errors",
    )

    assert payload["source"] == "gdoc"
    assert payload["source_ref"] == "doc-1"
    assert payload["source_tab"] == "Errors"
    assert payload["doc_audience"] == "acl_mirror"
    assert payload["doc_audience_set_by"] == "tech@example.com"
    assert "body" not in payload


def test_a_doc_linked_payload_can_be_published():
    payload = build_module_payload(
        slug="s", title="T", summary="x", body="",
        actor="tech@example.com", source="gdoc", source_ref="doc-1",
        doc_audience="published",
    )

    assert payload["doc_audience"] == "published"


def test_a_spreadsheet_is_an_accepted_drive_type():
    from orchestrator.experts.handlers.ingestion_expert.fetch_document import (
        SUPPORTED_DRIVE_MIMES,
    )

    assert "application/vnd.google-apps.spreadsheet" in SUPPORTED_DRIVE_MIMES


class _Ctx:
    """Minimal StepContext stand-in for the link-mode question."""

    def __init__(self, state, user_input=None):
        self._state, self.user_input = state, user_input

    def get_state(self, key, default=None):
        return self._state.get(key, default)


@pytest.mark.asyncio
async def test_pasted_text_is_never_asked_about_linking():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(_Ctx({"source_type": "manual_input"}))

    assert result.needs_user_input is False


@pytest.mark.asyncio
async def test_a_drive_source_is_asked_once():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(_Ctx({"source_type": "gdrive"}))

    assert result.needs_user_input is True
    assert result.state_updates["awaiting_link_mode"] is True


@pytest.mark.asyncio
async def test_choosing_live_records_the_file_id_and_skips_the_rewrite():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(
        _Ctx({"source_type": "gdrive", "awaiting_link_mode": True, "source_id": "doc-9"}, "1")
    )

    assert result.state_updates["module_source"] == "gdoc"
    assert result.state_updates["module_source_ref"] == "doc-9"
    assert result.state_updates["module_doc_audience"] == "acl_mirror"
    assert result.state_updates["skip_improve_content"] is True


@pytest.mark.asyncio
async def test_choosing_a_snapshot_keeps_the_manual_source():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(
        _Ctx({"source_type": "gdrive", "awaiting_link_mode": True, "source_id": "doc-9"}, "2")
    )

    assert result.state_updates["module_source"] == "manual"
    assert "skip_improve_content" not in result.state_updates


@pytest.mark.asyncio
async def test_an_unclear_answer_asks_again():
    from orchestrator.experts.handlers.context_expert.choose_doc_link_mode import (
        choose_doc_link_mode,
    )

    result = await choose_doc_link_mode(
        _Ctx({"source_type": "gdrive", "awaiting_link_mode": True}, "maybe")
    )

    assert result.needs_user_input is True


@pytest.mark.asyncio
async def test_skip_improve_content_flag_short_circuits_the_step():
    from orchestrator.experts.handlers.ingestion_expert.improve_content import improve_content

    result = await improve_content(_Ctx({"skip_improve_content": True}))

    assert "as written" in (result.progress_message or "").lower()
