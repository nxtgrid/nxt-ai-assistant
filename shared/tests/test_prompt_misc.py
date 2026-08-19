"""The last prompts to leave code and env vars are in the library."""

from shared.config import flag_registry
from shared.prompts import PROMPTS

IDS = [
    "doc_editing.edit_highlighted",
    "doc_editor.locate_edits",
    "knowledge.summarize_topic",
    "ticketing.jira_issue_types",
    "gtr.analysis_conversation",
    "grafana.panel_description",
]


def test_all_remaining_prompts_exist():
    for prompt_id in IDS:
        assert prompt_id in PROMPTS.ids()


# Unlocked (was overridable: false) but with no ops/eng grant added -- only
# an admin can edit/publish these, via access.py's is_prompt_admin() bypass.
# See docs/superpowers/specs/2026-08-08-prompt-permissions-design.md.
ADMIN_ONLY_IDS = ["doc_editing.edit_highlighted", "doc_editor.locate_edits"]


def test_admin_only_misc_prompts_have_no_team_grants():
    for prompt_id in ADMIN_ONLY_IDS:
        spec = PROMPTS.spec(prompt_id)
        assert spec.overridable is True
        assert spec.access.edit == []
        assert spec.access.publish == []


def test_grafana_prompt_flag_is_retired():
    assert "GRAFANA_PANEL_DESCRIPTION_PROMPT" not in flag_registry.FLAGS


def test_grafana_prompt_renders_without_the_env_var(monkeypatch):
    monkeypatch.delenv("GRAFANA_PANEL_DESCRIPTION_PROMPT", raising=False)
    assert "MCP tool description" in PROMPTS.text("grafana.panel_description")


def test_no_prompt_text_remains_in_the_registry():
    for name in flag_registry.FLAGS:
        assert not name.endswith("_PROMPT"), f"{name} still holds prompt text"


def test_doc_editing_renders_with_optional_blocks_empty():
    text = PROMPTS.text(
        "doc_editing.edit_highlighted",
        instruction="make it formal",
        highlighted_text="hey there",
        context_block="",
        context_summary="",
        reference_block="",
    )
    assert "make it formal" in text
    assert "hey there" in text


def test_doc_editor_locate_edits_json_example_has_single_braces():
    text = PROMPTS.text(
        "doc_editor.locate_edits", instruction="fix the intro", markdown="# Title\n\nBody"
    )
    assert '"text": "the exact text' in text
    assert "{{" not in text
    assert "fix the intro" in text


def test_knowledge_summarize_topic_renders_all_variables():
    text = PROMPTS.text(
        "knowledge.summarize_topic",
        topic="grid outages",
        chunks_text="[1] chunk one",
        tools_text="",
        max_words=250,
    )
    assert "grid outages" in text
    assert "chunk one" in text
    assert "250-word" in text


def test_jira_issue_types_renders_without_operational_context():
    text = PROMPTS.text(
        "ticketing.jira_issue_types",
        catalogue_json="[]",
        summary="Grid down",
        description="No power",
        operational_context_block="",
    )
    assert "Alert summary: Grid down" in text
    assert "Alert details: No power" in text
    assert "Live grid telemetry" not in text


def test_jira_issue_types_renders_with_operational_context():
    text = PROMPTS.text(
        "ticketing.jira_issue_types",
        catalogue_json="[]",
        summary="Grid down",
        description="No power",
        operational_context_block="\nLive grid telemetry: {}",
    )
    assert "Alert details: No power\nLive grid telemetry: {}" in text


def test_gtr_analysis_conversation_is_fully_static():
    spec = PROMPTS.spec("gtr.analysis_conversation")
    assert spec.variables == []
    text = PROMPTS.text("gtr.analysis_conversation")
    assert "grid technical reviewer" in text
