"""process_doc_edits is the one handler already doing comment-driven editing,
and until it has a contract it is unreachable as a skill step tool."""

from orchestrator.experts.step_registry import get_step_contract

# See test_replace_file_image.py for why this is snapshotted at collection
# time rather than called fresh inside each test function.
_CONTRACT = get_step_contract("process_doc_edits")


def test_it_has_a_contract_at_all():
    assert _CONTRACT is not None


def test_it_declares_document_id_and_instruction_as_parameters():
    names = {p.name for p in _CONTRACT.params}
    assert {"document_id", "instruction"} <= names


def test_it_is_marked_mutating_with_a_mock():
    assert _CONTRACT.mutates is True
    assert _CONTRACT.mutation_kind == "external_write"
    assert _CONTRACT.mock is not None
