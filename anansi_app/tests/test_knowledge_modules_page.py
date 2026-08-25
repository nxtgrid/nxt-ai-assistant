"""Knowledge Modules page view-model.

The Prompts page's original knowledge-tab tests (KnowledgeTabRow/
build_knowledge_tab/filter_module_rows) moved to test_knowledge_picker.py
when that logic was generalized into knowledge_picker.py.
"""

import pytest
from nicegui_app.pages.knowledge_modules import (
    SCOPE_OPTIONS,
    ModuleRow,
    body_is_editable,
    build_module_rows,
    describe_audience,
    describe_usage,
    draft_gdoc_module,
    extract_drive_id,
    filter_context_rows,
    group_label,
    group_module_rows,
    module_is_deletable,
    next_auto_slug,
    preview_module_body,
    prompt_option_label,
    singleton_creation_warnings,
    slugify,
    validate_module,
)

from shared.prompts.knowledge import KnowledgeModule


def _module(slug, tags=("grid_ops",), summary="s", body="b" * 40, source="manual"):
    return KnowledgeModule(
        id=slug,
        slug=slug,
        title=slug.title(),
        summary=summary,
        body=body,
        tags=list(tags),
        scope="sector",
        source=source,
    )


def test_build_module_rows_reports_size():
    rows = build_module_rows([_module("comms")])
    assert rows == [
        ModuleRow(
            slug="comms", title="Comms", tags=["grid_ops"], scope="sector",
            chars=40, source="manual", size_label="40 chars",
            summary="s", body="b" * 40, used_by=[],
        )
    ]


def test_build_module_rows_carries_the_prompts_using_each_module():
    rows = build_module_rows([_module("comms")], {"comms": ["customer.system", "staff.system"]})
    assert rows[0].used_by == ["customer.system", "staff.system"]


def test_a_module_no_pin_row_mentions_is_unused():
    rows = build_module_rows([_module("comms")], {"other-id": ["staff.system"]})
    assert rows[0].used_by == []


def test_build_module_rows_resolves_a_skill_pin_to_its_title():
    rows = build_module_rows(
        [_module("comms")],
        {"comms": ["customer.system", "skill:11111111-1111-1111-1111-111111111111"]},
        skill_titles={"11111111-1111-1111-1111-111111111111": "Find Tickets"},
    )
    assert rows[0].used_by == ["customer.system", "🎬 Find Tickets"]


def test_build_module_rows_falls_back_to_the_raw_id_for_an_unknown_skill():
    """E.g. a skill deleted after the pin was made -- prompt_knowledge_overrides
    has no FK on skill ids, so a stale pin can outlive its skill."""
    rows = build_module_rows(
        [_module("comms")],
        {"comms": ["skill:22222222-2222-2222-2222-222222222222"]},
        skill_titles={},
    )
    assert rows[0].used_by == ["🎬 22222222-2222-2222-2222-222222222222"]


def test_describe_usage_names_the_prompts():
    assert describe_usage(["staff.system"]) == "used by: staff.system"


def test_describe_usage_calls_out_an_unattached_module():
    """A module attached to nothing reaches no conversation -- say so.

    Blank would read as "no information"; the built-in modules sat unattached
    unnoticed for exactly that reason.
    """
    text = describe_usage([])
    assert "not used by any prompt" in text


def test_filter_context_rows_matches_slug_title_summary_and_body():
    modules = [
        _module(
            "azimuth-calc",
            summary="How PV azimuth is measured.",
            body="Uses the sun's position and panel tilt.",
        ),
        _module(
            "victron-led",
            summary="Decoding inverter LED error states.",
            body="Flash codes and their fault meanings.",
        ),
    ]
    rows = build_module_rows(modules)

    # slug
    assert [r.slug for r in filter_context_rows(rows, "azimuth-calc")] == ["azimuth-calc"]
    # title (slug.title() -> "Victron-Led")
    assert [r.slug for r in filter_context_rows(rows, "Victron")] == ["victron-led"]
    # summary
    assert [r.slug for r in filter_context_rows(rows, "inverter LED")] == ["victron-led"]
    # body
    assert [r.slug for r in filter_context_rows(rows, "panel tilt")] == ["azimuth-calc"]


def test_filter_context_rows_is_case_insensitive():
    rows = build_module_rows([_module("azimuth-calc", body="Panel Tilt matters.")])
    assert [r.slug for r in filter_context_rows(rows, "PANEL tilt")] == ["azimuth-calc"]


def test_filter_context_rows_empty_query_returns_everything_unchanged():
    rows = build_module_rows([_module("a"), _module("b")])
    assert filter_context_rows(rows, "") == rows
    assert filter_context_rows(rows, "   ") == rows


def test_filter_context_rows_no_match_returns_empty():
    rows = build_module_rows([_module("azimuth-calc")])
    assert filter_context_rows(rows, "no such module") == []


def test_validate_module_requires_a_summary():
    """The summary is how a module is recognised in the picker."""
    with pytest.raises(ValueError, match="summary"):
        validate_module(slug="a", title="A", summary="", body="b")


def test_validate_module_rejects_a_bad_scope():
    with pytest.raises(ValueError, match="scope"):
        validate_module(slug="a", title="A", summary="s", body="b", scope="nonsense")


def test_validate_module_accepts_a_site_scope():
    validate_module(slug="a", title="A", summary="s", body="b", scope="site:ABC")


def test_validate_module_accepts_an_org_scope():
    validate_module(slug="a", title="A", summary="s", body="b", scope="org:2")


def test_validate_module_requires_slug_title_and_body():
    with pytest.raises(ValueError, match="required"):
        validate_module(slug="", title="A", summary="s", body="b")


def test_validate_module_no_longer_takes_a_mode():
    """The on-demand tier is gone; a caller still passing mode= must break loudly."""
    with pytest.raises(TypeError):
        validate_module(slug="a", title="A", summary="s", body="b", mode="on_demand")


def test_group_label_splits_built_in_curated_and_external():
    assert group_label("directory") == "Built-in"
    assert group_label("graph") == "Built-in"
    assert group_label("episodic") == "Built-in"
    # Typed directly into this admin UI -- this app is the source of truth.
    assert group_label("manual") == "Curated"
    # Attached from Google Drive -- content lives elsewhere, only mirrored
    # here, so it gets its own bucket rather than sharing Curated's "this
    # app is the source of truth" framing.
    assert group_label("gdoc") == "External"


def test_group_module_rows_orders_built_in_then_curated_then_external():
    rows = [
        _row_for_grouping("doc", source="gdoc"),
        _row_for_grouping("comms", source="manual"),
        _row_for_grouping("directory", source="directory"),
    ]
    groups = group_module_rows(rows)
    assert [label for label, _ in groups] == ["Built-in", "Curated", "External"]
    assert [r.slug for r in groups[0][1]] == ["directory"]
    assert [r.slug for r in groups[1][1]] == ["comms"]
    assert [r.slug for r in groups[2][1]] == ["doc"]


def test_group_module_rows_keeps_slug_order_within_a_group():
    rows = [_row_for_grouping("a"), _row_for_grouping("b")]
    groups = group_module_rows(rows)
    assert [r.slug for r in groups[0][1]] == ["a", "b"]


def test_group_module_rows_omits_empty_buckets():
    groups = group_module_rows([_row_for_grouping("a", source="manual")])
    assert [label for label, _ in groups] == ["Curated"]


def _row_for_grouping(slug: str, source: str = "manual") -> ModuleRow:
    return ModuleRow(
        slug=slug, title=slug.title(), tags=[], scope="sector", chars=0, source=source
    )


def test_prompt_option_label_combines_id_and_description():
    assert prompt_option_label("customer.system", "Customer-mode instructions.") == (
        "customer.system — Customer-mode instructions."
    )


def test_prompt_option_label_truncates_a_long_description():
    label = prompt_option_label("a.b", "x" * 100, max_len=10)
    assert label == "a.b — " + "x" * 9 + "…"


def test_prompt_option_label_falls_back_to_bare_id_when_no_description():
    assert prompt_option_label("a.b", "") == "a.b"


# ── Provider-backed modules (source badges, read-only bodies, preview) ─────


def test_row_reports_live_instead_of_a_char_count_for_jit_modules():
    modules = [
        KnowledgeModule(id="g", slug="entity-graph", title="Graph", summary="s",
                        body=None, source="graph"),
        KnowledgeModule(id="m", slug="comms", title="Comms", summary="s",
                        body="12345"),
    ]
    rows = {r.slug: r for r in build_module_rows(modules)}

    assert rows["entity-graph"].size_label == "live"
    assert rows["comms"].size_label == "5 chars"


def test_row_carries_the_source():
    rows = build_module_rows([
        KnowledgeModule(id="g", slug="entity-graph", title="G", summary="s",
                        body=None, source="graph")
    ])
    assert rows[0].source == "graph"


def test_body_is_editable_only_for_manual_modules():
    assert body_is_editable("manual") is True
    assert body_is_editable("ingested") is True
    for source in ("gdoc", "graph", "directory", "episodic"):
        assert body_is_editable(source) is False, source


def test_singleton_providers_cannot_be_deleted():
    assert module_is_deletable("graph") is False
    assert module_is_deletable("directory") is False
    # episodic is code-defined and bootstrapped the same as graph/directory
    # (see shared.prompts.knowledge.SINGLETON_SOURCES) -- it belongs in the
    # same non-deletable guard, not the gdoc/manual bucket below it.
    assert module_is_deletable("episodic") is False
    assert module_is_deletable("gdoc") is True
    assert module_is_deletable("manual") is True


def test_singleton_creation_warnings_are_empty_when_everything_succeeds():
    results = {"directory": "created", "graph": "exists", "episodic": "created"}
    assert singleton_creation_warnings(results) == []


def test_singleton_creation_warnings_surface_a_failed_source():
    results = {
        "directory": "created",
        "graph": "failed: violates check constraint",
        "episodic": "exists",
    }
    assert singleton_creation_warnings(results) == [
        "Couldn't create the 'graph' context module: failed: violates check constraint"
    ]


def test_singleton_creation_warnings_reports_every_failure():
    results = {"directory": "failed: boom", "graph": "failed: also boom", "episodic": "created"}
    assert singleton_creation_warnings(results) == [
        "Couldn't create the 'directory' context module: failed: boom",
        "Couldn't create the 'graph' context module: failed: also boom",
    ]


def test_validate_module_does_not_require_a_body_when_not_required():
    """A provider-backed module's body isn't stored -- the save path must not
    demand one just because the caller happened to leave the field empty."""
    validate_module(slug="a", title="A", summary="s", body="", require_body=False)


def test_validate_module_still_requires_a_body_by_default():
    with pytest.raises(ValueError, match="required"):
        validate_module(slug="a", title="A", summary="s", body="")


# ── Doc source, audience, and the honest scope dropdown ─────────────────────


def test_a_doc_module_body_is_not_editable():
    assert body_is_editable("gdoc") is False
    assert body_is_editable("manual") is True


def test_global_is_a_valid_scope():
    validate_module(slug="s", title="T", summary="x", body="b", scope="global")


def test_sector_is_still_a_valid_scope():
    validate_module(slug="s", title="T", summary="x", body="b", scope="sector")


def test_an_unknown_scope_is_rejected():
    with pytest.raises(ValueError, match="scope"):
        validate_module(slug="s", title="T", summary="x", body="b", scope="universe")


def test_scope_options_offer_global_and_org_and_a_disabled_site():
    """A free-text box accepted site:FOO and produced a module that never
    fired -- nothing populates RequestScope.grid anywhere."""
    labels = {opt["value"]: opt for opt in SCOPE_OPTIONS}

    assert "global" in labels
    assert "org:" in labels
    assert labels["site:"]["disabled"] is True


def test_a_doc_module_requires_a_source_ref():
    with pytest.raises(ValueError, match="Google Doc or Sheet"):
        validate_module(
            slug="s", title="T", summary="x", body="", scope="global",
            require_body=False, source="gdoc", source_ref="",
        )


def test_a_doc_module_requires_an_audience():
    with pytest.raises(ValueError, match="audience"):
        validate_module(
            slug="s", title="T", summary="x", body="", scope="global",
            require_body=False, source="gdoc", source_ref="doc-1", doc_audience=None,
        )


def test_a_valid_doc_module_passes():
    validate_module(
        slug="s", title="T", summary="x", body="", scope="global",
        require_body=False, source="gdoc", source_ref="doc-1",
        doc_audience="acl_mirror",
    )


def test_describe_audience_warns_about_a_mirrored_customer_module():
    """It would provably resolve to nothing for a customer."""
    warning = describe_audience("acl_mirror", pinned_prompts=["customer.system"])

    assert warning is not None
    assert "customer" in warning.lower()


def test_describe_audience_is_quiet_for_a_staff_only_module():
    assert describe_audience("acl_mirror", pinned_prompts=["staff.system"]) is None


def test_describe_audience_is_quiet_for_a_published_module():
    assert describe_audience("published", pinned_prompts=["customer.system"]) is None


def test_extract_drive_id_reads_a_docs_url():
    assert extract_drive_id(
        "https://docs.google.com/document/d/1AbC_dEf-23456789012345678/edit"
    ) == "1AbC_dEf-23456789012345678"


def test_extract_drive_id_reads_a_sheets_url():
    assert extract_drive_id(
        "https://docs.google.com/spreadsheets/d/1AbC_dEf-23456789012345678/edit#gid=0"
    ) == "1AbC_dEf-23456789012345678"


def test_extract_drive_id_accepts_a_bare_id():
    assert extract_drive_id("1AbC_dEf-23456789012345678") == "1AbC_dEf-23456789012345678"


def test_extract_drive_id_rejects_nonsense():
    assert extract_drive_id("not a link") is None
    assert extract_drive_id("") is None


# ── Preview now resolves as the operator, not a hardcoded staff context ─────


def test_preview_resolves_against_the_viewing_operator():
    import asyncio

    module = KnowledgeModule(
        id="d", slug="directory", title="Directory", summary="s", body=None, source="directory"
    )

    class _Provider:
        source = "directory"

        async def resolve(self, m, ctx):
            return f"resolved for {ctx.user_email}"

    text = asyncio.run(
        preview_module_body(module, _Provider(), user_email="ops@example.com")
    )
    assert text == "resolved for ops@example.com"


def test_preview_reports_an_empty_resolution_clearly():
    import asyncio

    module = KnowledgeModule(
        id="e", slug="episodic", title="Episodic", summary="s", body=None, source="episodic"
    )

    class _Provider:
        source = "episodic"

        async def resolve(self, m, ctx):
            return None

    text = asyncio.run(
        preview_module_body(module, _Provider(), user_email="ops@example.com")
    )
    assert "nothing" in text.lower()


def test_preview_reports_a_provider_failure_rather_than_raising():
    import asyncio

    module = KnowledgeModule(
        id="g", slug="graph", title="Graph", summary="s", body=None, source="graph"
    )

    class _Provider:
        source = "graph"

        async def resolve(self, m, ctx):
            raise RuntimeError("RPC missing")

    text = asyncio.run(
        preview_module_body(module, _Provider(), user_email="ops@example.com")
    )
    assert "RPC missing" in text


# ── Slug autofill: same shape as the Skills editor and /learn's
# normalize_slug, applied live as an operator types a Title instead of at
# save time. Never raises -- it runs on every keystroke, where "nothing
# survived" just means "nothing to suggest yet." ────────────────────────────


def test_slugify_kebab_cases_a_title():
    assert slugify("Inverter Fault Codes and LED Error States") == (
        "inverter-fault-codes-and-led-error-states"
    )


def test_slugify_collapses_punctuation_to_single_hyphens():
    assert slugify("Victron -- Quattro!! (LED codes)") == "victron-quattro-led-codes"


def test_slugify_strips_leading_and_trailing_hyphens():
    assert slugify("  -- Wrapped --  ") == "wrapped"


def test_slugify_returns_empty_rather_than_raising_when_nothing_survives():
    assert slugify("🎉🎉🎉") == ""


def test_next_auto_slug_fills_in_from_a_blank_start():
    assert next_auto_slug(current_slug="", last_auto_slug="", title="Comms Plan") == (
        "comms-plan"
    )


def test_next_auto_slug_keeps_following_the_title_until_touched():
    """Still following: the slug field holds exactly what autofill wrote
    last time, so a further title edit is free to overwrite it again."""
    first = next_auto_slug(current_slug="", last_auto_slug="", title="Comms")
    second = next_auto_slug(current_slug=first, last_auto_slug=first, title="Comms Plan")
    assert second == "comms-plan"


def test_next_auto_slug_stops_once_the_operator_types_their_own():
    """The moment the slug field diverges from what autofill last wrote,
    it must never be overwritten again -- that would clobber a deliberate
    choice mid-keystroke."""
    result = next_auto_slug(
        current_slug="my-own-slug", last_auto_slug="comms-plan", title="Comms Plan V2"
    )
    assert result is None


def test_next_auto_slug_leaves_an_existing_modules_slug_alone():
    """Belt-and-suspenders: the dialog only wires this for a brand-new
    module (existing is None) in the first place, but the function itself
    should never suggest overwriting a slug that wasn't just its own last
    suggestion -- covered by the "stops once touched" case above using the
    same mechanism an already-saved slug would trigger."""
    result = next_auto_slug(current_slug="directory", last_auto_slug="", title="Directory")
    assert result is None


# ── Slug collisions are reported, not silently renamed: the field is
# visible and editable (unlike the Skills editor's hidden autofill), so a
# clash should read as "choose another," the same way an explicitly typed
# Skill name does (see SkillBuilderService.slug_taken) -- not disappear into
# a silently appended "-2" or a raw database UNIQUE-constraint error. ───────


def test_validate_module_accepts_a_free_slug():
    validate_module(
        slug="new-module", title="T", summary="s", body="b", taken_slugs=frozenset({"other"})
    )


def test_validate_module_rejects_a_taken_slug():
    with pytest.raises(ValueError, match="already used"):
        validate_module(
            slug="comms", title="T", summary="s", body="b",
            taken_slugs=frozenset({"comms", "other"}),
        )


def test_validate_module_skips_the_collision_check_when_taken_slugs_is_omitted():
    """An edit to an existing module never passes taken_slugs (its slug
    field is locked, so it can't newly collide) -- must not start rejecting
    every save because its own slug is technically "taken" by itself."""
    validate_module(slug="comms", title="T", summary="s", body="b")


# ── Live preview for an unsaved document module: resolving needs a
# KnowledgeModule to hand the provider, and a module being created doesn't
# have one yet. draft_gdoc_module builds a throwaway one from the current
# form values so Preview can run the real provider (and therefore the real
# access check) before Save, instead of a canned "save it first" message. ──


def test_draft_gdoc_module_carries_the_pasted_fields():
    module = draft_gdoc_module(
        slug="warranty-terms", title="Warranty Terms", summary="s",
        file_id="abc123", source_tab="Sheet2", doc_audience="acl_mirror",
    )
    assert module.source == "gdoc"
    assert module.source_ref == "abc123"
    assert module.source_tab == "Sheet2"
    assert module.doc_audience == "acl_mirror"
    assert module.slug == "warranty-terms"


def test_draft_gdoc_module_treats_a_blank_tab_as_the_first_tab():
    """Mirrors the save path's ``doc_tab_input.value.strip() or None`` --
    an empty string must resolve the same way an unset tab does, not be
    sent to the Sheets fetcher as a literal empty-string tab name."""
    module = draft_gdoc_module(
        slug="s", title="T", summary="", file_id="abc123", source_tab="", doc_audience="published"
    )
    assert module.source_tab is None


def test_draft_gdoc_module_falls_back_to_a_placeholder_slug_and_title():
    """Preview can run before the operator has typed a Slug or Title at
    all -- the draft needs *something* non-empty there since KnowledgeModule
    doesn't accept a blank slug/title, but this is never saved, so a
    placeholder is fine."""
    module = draft_gdoc_module(
        slug="", title="", summary="", file_id="abc123", source_tab=None, doc_audience="published"
    )
    assert module.slug
    assert module.title
