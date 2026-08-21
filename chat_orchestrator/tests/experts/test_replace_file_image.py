"""Generic image replacement, targeted by token or alt text."""

from orchestrator.experts.step_registry import get_step_contract

# Snapshot at module-import (collection) time, not inside the test function --
# tests/experts/test_parameter_confirmation.py::TestRegisterStepWithoutSchema.
# setup_method calls get_step_registry().clear() with no teardown, and since
# @register_step only runs once per process (later imports are sys.modules
# no-ops), a fresh get_step_contract() call inside a test body would see None
# if that clear() ran first -- purely an artifact of alphabetical test-file
# collection order ("test_replace_file_image" sorts after
# "test_parameter_confirmation"), not a real defect. Same fix already applied
# in test_contract_lint.py, test_package_generator_contracts.py,
# test_step_tool_schema.py and test_workflow_executor.py.
_CONTRACT = get_step_contract("replace_file_image")


def test_contract_exposes_file_target_and_image_source():
    assert _CONTRACT is not None
    names = {p.name for p in _CONTRACT.params}
    assert {"file_id", "target", "worksheet_name"} <= names
    assert _CONTRACT.mutates is True


def test_sizing_precedence_prefers_a_merged_range_over_a_fit_range():
    from orchestrator.experts.handlers.templates.replace_file_image import choose_fit_range

    assert choose_fit_range(merged_range="B6:F20", comment_fit_range="C1:D2") == "B6:F20"


def test_sizing_precedence_falls_back_to_the_comment_range():
    from orchestrator.experts.handlers.templates.replace_file_image import choose_fit_range

    assert choose_fit_range(merged_range=None, comment_fit_range="C1:D2") == "C1:D2"


def test_sizing_precedence_returns_none_when_neither_is_given():
    """None means: let Apps Script use the replaced image's own dimensions."""
    from orchestrator.experts.handlers.templates.replace_file_image import choose_fit_range

    assert choose_fit_range(merged_range=None, comment_fit_range=None) is None


def test_parses_a_fit_range_out_of_comment_text():
    from orchestrator.experts.handlers.templates.replace_file_image import parse_fit_range

    assert parse_fit_range("@anansi-chatbot {{site_map}} fit B6:F20") == "B6:F20"
    assert parse_fit_range("@anansi-chatbot {{site_map}}") is None
    assert parse_fit_range("fit A1:A1 please") == "A1:A1"
