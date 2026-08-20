"""Knowledge Modules page view-model and the Prompts page knowledge tab."""

import pytest
from nicegui_app.pages.knowledge_modules import (
    SCOPE_OPTIONS,
    ModuleRow,
    body_is_editable,
    build_module_rows,
    describe_audience,
    extract_drive_id,
    filter_context_rows,
    group_module_rows,
    module_is_deletable,
    preview_module_body,
    prompt_option_label,
    singleton_creation_warnings,
    validate_module,
)
from nicegui_app.pages.prompts import KnowledgeTabRow, build_knowledge_tab, filter_module_rows

from shared.prompts.knowledge import KnowledgeModule


def _module(slug, tags=("grid_ops",), mode="pinned", summary="s", body="b" * 40):
    return KnowledgeModule(
        id=slug,
        slug=slug,
        title=slug.title(),
        summary=summary,
        body=body,
        tags=list(tags),
        scope="sector",
        mode=mode,
    )


def test_build_module_rows_reports_size():
    rows = build_module_rows([_module("comms")])
    assert rows == [
        ModuleRow(
            slug="comms", title="Comms", tags=["grid_ops"], scope="sector", mode="pinned",
            chars=40, source="manual", size_label="40 chars",
            summary="s", body="b" * 40,
        )
    ]


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


def test_knowledge_tab_lists_all_modules_with_pinned_state():
    rows = build_knowledge_tab([_module("beta"), _module("alpha", mode="on_demand")], {"alpha": True})
    assert rows == [
        KnowledgeTabRow(
            slug="alpha", title="Alpha", mode="on_demand", chars=40, checked=True, summary="s"
        ),
        KnowledgeTabRow(
            slug="beta", title="Beta", mode="pinned", chars=40, checked=False, summary="s"
        ),
    ]


def test_knowledge_tab_with_no_pins_checks_nothing():
    rows = build_knowledge_tab([_module("alpha")], {})
    assert [r.checked for r in rows] == [False]


def test_filter_modules_matches_slug_title_and_summary():
    rows = _knowledge_tab_rows_fixture()
    assert [r.slug for r in filter_module_rows(rows, "azimuth")] == ["azimuth-calculation"]
    assert [r.slug for r in filter_module_rows(rows, "LED")] == ["victron-led"]
    assert len(filter_module_rows(rows, "")) == 2


def _knowledge_tab_rows_fixture():
    return [
        KnowledgeTabRow(
            slug="azimuth-calculation", title="Azimuth Calculation",
            mode="on_demand", chars=318, checked=False,
            summary="How PV azimuth is measured.",
        ),
        KnowledgeTabRow(
            slug="victron-led", title="Victron Quattro Codes",
            mode="on_demand", chars=2438, checked=True,
            summary="Decoding inverter LED error states.",
        ),
    ]


def test_validate_module_requires_a_summary_for_on_demand():
    with pytest.raises(ValueError, match="summary"):
        validate_module(slug="a", title="A", summary="", body="b", mode="on_demand")


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


def test_validate_module_rejects_a_bad_mode():
    with pytest.raises(ValueError, match="mode"):
        validate_module(slug="a", title="A", summary="s", body="b", mode="sometimes")


def test_group_module_rows_orders_pinned_before_on_demand():
    rows = [
        _row_for_grouping("catalog", mode="on_demand"),
        _row_for_grouping("comms", mode="pinned"),
    ]
    groups = group_module_rows(rows)
    assert [label for label, _ in groups] == ["Pinned", "On-demand"]


def test_group_module_rows_keeps_slug_order_within_a_group():
    rows = [_row_for_grouping("a"), _row_for_grouping("b")]
    groups = group_module_rows(rows)
    assert [r.slug for r in groups[0][1]] == ["a", "b"]


def test_group_module_rows_omits_empty_buckets():
    groups = group_module_rows([_row_for_grouping("a", mode="pinned")])
    assert [label for label, _ in groups] == ["Pinned"]


def _row_for_grouping(slug: str, mode: str = "pinned") -> ModuleRow:
    return ModuleRow(slug=slug, title=slug.title(), tags=[], scope="sector", mode=mode, chars=0)


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


# ── The Prompts page's Context tab: the same null-body / JIT hazard ────────
# build_knowledge_tab lists every module (including provider-backed ones) so
# an operator can pin/unpin it to a prompt. It has the exact same
# len(module.body) crash build_module_rows had -- fixed alongside it here,
# since a JIT module now genuinely exists once the seed script runs.


def test_knowledge_tab_handles_a_jit_module_without_crashing():
    rows = build_knowledge_tab(
        [KnowledgeModule(id="g", slug="entity-graph", title="Graph", summary="s",
                         body=None, source="graph")],
        {},
    )
    assert rows[0].chars == 0
    assert rows[0].is_jit is True


def test_knowledge_tab_marks_manual_modules_as_not_jit():
    rows = build_knowledge_tab([_module("comms")], {})
    assert rows[0].is_jit is False


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
