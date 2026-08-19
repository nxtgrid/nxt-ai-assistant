"""Snapshot every prompt so accidental text drift fails CI.

Regenerate deliberately by deleting prompt_checksums.json and re-running --
review the diff before committing; a change here changes what the model sees.
"""

import hashlib
import json
import pathlib

import pytest

from shared.prompts import PromptLibrary

# A bare PromptLibrary(), not the shared.prompts.PROMPTS singleton: PROMPTS is
# built once at import time (_build_default_library) with its DB/GDoc lookups
# wired up whenever CHAT_DB_URL / GOOGLE_SERVICE_ACCOUNT_JSON happen to be
# configured -- a developer's local .env, copied in for unrelated reasons, is
# enough. This file exists specifically to catch accidental edits to the
# *committed bundled* prompt files (its own docstring: "review the diff
# before committing"), so it must never be able to instead start checksumming
# whatever's live in the real chat_db prompts table or a Google Doc at the
# moment it happens to run. A bare PromptLibrary() leaves db_body_for/
# gdoc_body_for unset, which resolves every prompt straight from the bundled
# file, deterministically, regardless of environment.
PROMPTS = PromptLibrary()

SNAPSHOT = pathlib.Path(__file__).parent / "prompt_checksums.json"

# Prompts whose bodies need variables to render; supply representative values
# so every prompt actually renders end-to-end, not just parses.
SAMPLE_VARS = {
    "context_filter.relevance": {
        "incoming_message": "sample incoming message",
        "formatted_candidates": "0: user - hello",
    },
    "conversation.summarize": {"messages": "user: hi\nassistant: hello"},
    "doc_editing.edit_highlighted": {
        "instruction": "make it formal",
        "highlighted_text": "hey there",
        "context_block": "",
        "context_summary": "",
        "reference_block": "",
    },
    "doc_editor.locate_edits": {"instruction": "fix the intro", "markdown": "# Title\n\nBody"},
    "episodic.distill": {
        "anchor_name": "Alpha",
        "messages_text": "- inverter tripped\n- replaced fuse",
        "target_words": 200,
    },
    "ingestion.classify_document": {"content": "sample document text"},
    "ingestion.detect_contradictions": {
        "existing_knowledge": "existing knowledge block",
        "new_content": "new document text",
    },
    "ingestion.extract_entities": {"content": "sample document text"},
    "ingestion.improve_content.modification": {
        "doc_type": "faq",
        "original": "original text",
        "suggestion": "suggested text",
        "user_instructions": "make it shorter",
    },
    "ingestion.improve_content.naming": {
        "doc_type": "technical",
        "content_preview": "preview text",
        "uploader_name": "Ada Lovelace",
    },
    "ingestion.improve_content.quality_eval": {"doc_type": "sop", "content": "sample content"},
    "intent_router.route": {
        "now_str": "2026-07-30 12:00 UTC",
        "user_input_repr": repr("build me an lpp for site X"),
    },
    "knowledge.summarize_topic": {
        "topic": "grid outages",
        "chunks_text": "[1] chunk one",
        "tools_text": "",
        "max_words": 250,
    },
    "procedure.match": {"procedure_descriptions": "PROCEDURE 1: Foo", "content": "support text"},
    "procedure.suggest": {"next_number": 3, "existing_list": "- Procedure 1: Foo", "content": "x"},
    "thread_assignment.classify": {"threads_text": "Thread t1:\n  user: hi", "user_input": "hi"},
    "ticketing.jira_issue_types": {
        "catalogue_json": "[]",
        "summary": "Grid down",
        "description": "No power",
        "operational_context_block": "",
    },
    "verification.sanitize": {"response_text": "internal step foo() failed", "context": "test"},
}


def _checksum(prompt_id: str) -> str:
    spec = PROMPTS.spec(prompt_id)
    variables = SAMPLE_VARS.get(prompt_id, {name: "x" for name in spec.variables})
    rendered = PROMPTS.render(prompt_id, vars=variables)
    payload = f"{rendered.system_text}\x00{rendered.context_text or ''}"
    return hashlib.sha256(payload.encode()).hexdigest()


def test_every_prompt_renders_without_error():
    for prompt_id in PROMPTS.ids():
        _checksum(prompt_id)


def test_every_declared_variable_has_a_sample_value():
    for prompt_id in PROMPTS.ids():
        spec = PROMPTS.spec(prompt_id)
        supplied = set(SAMPLE_VARS.get(prompt_id, {}))
        missing = set(spec.variables) - supplied
        assert not missing, f"{prompt_id} is missing sample values for {missing}"


def test_prompt_text_has_not_drifted():
    current = {prompt_id: _checksum(prompt_id) for prompt_id in sorted(PROMPTS.ids())}
    if not SNAPSHOT.exists():
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        pytest.skip("snapshot created; re-run to verify")
    expected = json.loads(SNAPSHOT.read_text())
    assert current == expected, (
        "Prompt text changed. If deliberate, delete "
        f"{SNAPSHOT.name}, re-run, and review the diff in the commit."
    )


def test_snapshot_covers_every_prompt():
    if SNAPSHOT.exists():
        assert set(json.loads(SNAPSHOT.read_text())) == set(PROMPTS.ids())
