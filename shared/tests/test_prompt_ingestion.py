"""Extracted ingestion prompts still render exactly what the literals produced."""

from shared.prompts import PROMPTS

LOCKED_IDS = [
    "ingestion.classify_document",
    "ingestion.detect_contradictions",
    "ingestion.extract_entities",
    "ingestion.improve_content.quality_eval",
    "ingestion.improve_content.modification",
    "ingestion.improve_content.naming",
]

# Not an LLM prompt — a static user-facing message. Safe for ops to edit.
OVERRIDABLE_IDS = [
    "ingestion.fetch_document.type_selection",
]


def test_all_ingestion_prompts_exist():
    for prompt_id in LOCKED_IDS + OVERRIDABLE_IDS:
        assert prompt_id in PROMPTS.ids()


def test_locked_ingestion_prompts_are_locked():
    for prompt_id in LOCKED_IDS:
        assert PROMPTS.spec(prompt_id).overridable is False


def test_type_selection_is_overridable_and_has_no_variables():
    spec = PROMPTS.spec("ingestion.fetch_document.type_selection")
    assert spec.overridable is True
    assert spec.variables == []


def test_json_prompts_declare_a_schema():
    for prompt_id in LOCKED_IDS:
        spec = PROMPTS.spec(prompt_id)
        if spec.output == "json":
            assert spec.schema, f"{prompt_id} declares json output but no schema"


def test_classify_document_renders_with_content_variable():
    text = PROMPTS.text("ingestion.classify_document", content="sample doc text")
    assert "sample doc text" in text
    assert "Classify this document" in text


def test_detect_contradictions_json_example_has_single_braces():
    """The original used {{ }} to escape literal JSON braces for .format().
    In the new template format, the body must already contain single braces —
    there's no escaping layer, so double braces would be misread as (invalid)
    variable placeholders and left untouched, corrupting the JSON example."""
    text = PROMPTS.text(
        "ingestion.detect_contradictions",
        existing_knowledge="EK",
        new_content="NC",
    )
    assert '{"contradictions": []}' in text
    assert "{{" not in text
    assert "EK" in text and "NC" in text


def test_quality_eval_json_example_has_single_braces():
    text = PROMPTS.text(
        "ingestion.improve_content.quality_eval", doc_type="sop", content="C"
    )
    assert '"is_good": true/false' in text
    assert "{{" not in text
    assert "sop" in text and "C" in text


def test_extract_entities_renders_with_content_variable():
    text = PROMPTS.text("ingestion.extract_entities", content="doc body")
    assert "doc body" in text


def test_modification_prompt_renders_all_four_variables():
    text = PROMPTS.text(
        "ingestion.improve_content.modification",
        doc_type="faq",
        original="orig text",
        suggestion="sugg text",
        user_instructions="make it shorter",
    )
    assert "faq" in text
    assert "orig text" in text
    assert "sugg text" in text
    assert "make it shorter" in text


def test_naming_prompt_renders_all_three_variables():
    text = PROMPTS.text(
        "ingestion.improve_content.naming",
        doc_type="technical",
        content_preview="preview text",
        uploader_name="Ada Lovelace",
    )
    assert "technical" in text
    assert "preview text" in text
    assert "Ada Lovelace" in text


def test_type_selection_renders_without_any_variables():
    text = PROMPTS.text("ingestion.fetch_document.type_selection")
    assert "What type of document are you adding?" in text
