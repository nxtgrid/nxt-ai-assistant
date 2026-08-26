"""The SURROUNDING CONTEXT block and its per-caller truncation budget."""

from shared.utils.doc_editing import build_context_block


def test_no_context_produces_no_block():
    assert build_context_block("") == ""


def test_a_block_is_labelled_for_the_model():
    assert build_context_block("the whole doc") == "\nSURROUNDING CONTEXT:\nthe whole doc"


def test_the_default_budget_matches_the_single_edit_path():
    """Mode 2 passed markdown[:1500]; the default must not change its prompt."""
    assert build_context_block("x" * 5000).endswith("x" * 1500)
    assert len(build_context_block("x" * 5000)) == len("\nSURROUNDING CONTEXT:\n") + 1500


def test_a_caller_can_ask_for_a_larger_budget():
    block = build_context_block("y" * 20000, context_limit=12000)
    assert len(block) == len("\nSURROUNDING CONTEXT:\n") + 12000


def test_context_shorter_than_the_budget_is_passed_whole():
    assert build_context_block("short", context_limit=12000).endswith("short")
