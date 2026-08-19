"""Tests for orchestrator.experts.skill_step_bindings -- the regex-driven
{{var}} write-clause parsing, output extraction, and read-only tool
filtering shared by WorkflowExecutor and skill_validation.py.
"""

from __future__ import annotations

from orchestrator.experts.skill_step_bindings import (
    extract_output_value,
    filter_tools_for_step,
    is_read_only_tool_name,
    parse_output_binding,
    strip_result_line,
)


class TestParseOutputBinding:
    def test_no_write_clause_returns_instruction_unchanged_and_none(self):
        read_text, output_var = parse_output_binding("Just do the thing.")

        assert read_text == "Just do the thing."
        assert output_var is None

    def test_ascii_arrow(self):
        read_text, output_var = parse_output_binding("Find tickets -> {{tickets}}")

        assert read_text == "Find tickets"
        assert output_var == "tickets"

    def test_unicode_arrow(self):
        read_text, output_var = parse_output_binding("Find tickets → {{tickets}}")

        assert read_text == "Find tickets"
        assert output_var == "tickets"

    def test_tolerates_extra_whitespace_around_arrow_and_braces(self):
        read_text, output_var = parse_output_binding("Find tickets   ->   {{  tickets  }}  ")

        assert read_text == "Find tickets"
        assert output_var == "tickets"

    def test_write_clause_must_be_at_the_end(self):
        # A {{var}} -> style pattern in the MIDDLE of the text isn't a write
        # clause -- only a trailing one is. This one has no trailing clause
        # at all, so it's just a read.
        read_text, output_var = parse_output_binding("Use {{x}} -> not a real clause, then continue.")

        assert output_var is None
        assert read_text == "Use {{x}} -> not a real clause, then continue."

    def test_reads_before_the_write_clause_are_preserved_in_read_text(self):
        read_text, output_var = parse_output_binding(
            "Cross-reference {{grid}} and {{date}} -> {{result}}"
        )

        assert read_text == "Cross-reference {{grid}} and {{date}}"
        assert output_var == "result"

    def test_underscore_and_digit_in_var_name(self):
        _read_text, output_var = parse_output_binding("Do it -> {{open_ticket_count_2}}")

        assert output_var == "open_ticket_count_2"

    def test_var_name_cannot_start_with_a_digit(self):
        # Not a valid identifier -- the regex requires a letter/underscore
        # first, so this isn't recognized as a write clause at all.
        read_text, output_var = parse_output_binding("Do it -> {{2invalid}}")

        assert output_var is None
        assert read_text == "Do it -> {{2invalid}}"


class TestExtractOutputValue:
    def test_no_result_line_returns_none(self):
        assert extract_output_value("Just some text with no marker.") is None

    def test_extracts_simple_value(self):
        assert extract_output_value("Here's my answer.\n\nRESULT: 42") == "42"

    def test_takes_the_last_result_line_when_multiple_present(self):
        text = "RESULT: draft one\nMore reasoning here.\nRESULT: final answer"

        assert extract_output_value(text) == "final answer"

    def test_strips_surrounding_whitespace_from_the_value(self):
        assert extract_output_value("RESULT:    42   ") == "42"

    def test_empty_result_value_returns_none_not_empty_string(self):
        # "declared a write but produced nothing" must be distinguishable
        # from "wrote an empty string" -- both collapse to None here.
        assert extract_output_value("RESULT: ") is None

    def test_none_input_returns_none(self):
        assert extract_output_value(None) is None

    def test_multiword_value(self):
        assert extract_output_value("RESULT: none found") == "none found"


class TestStripResultLine:
    def test_removes_the_result_line(self):
        assert strip_result_line("Here's my answer.\n\nRESULT: 42") == "Here's my answer."

    def test_no_result_line_is_a_no_op(self):
        assert strip_result_line("Just some text.") == "Just some text."

    def test_none_input_returns_none(self):
        assert strip_result_line(None) is None

    def test_idempotent(self):
        once = strip_result_line("Text.\nRESULT: 42")
        twice = strip_result_line(once)

        assert once == twice == "Text."


class TestReadOnlyToolFiltering:
    def test_get_prefix_is_read_only(self):
        assert is_read_only_tool_name("get_grid_status") is True

    def test_list_search_check_fetch_prefixes_are_read_only(self):
        for name in ("list_tickets", "search_issues", "check_balance", "fetch_report"):
            assert is_read_only_tool_name(name) is True, name

    def test_update_create_delete_are_not_read_only(self):
        for name in ("update_ticket_status", "create_invoice", "delete_record", "send_message"):
            assert is_read_only_tool_name(name) is False, name

    def test_prefix_match_is_exact_not_substring(self):
        # A tool merely *containing* "get_" isn't read-only -- must start
        # with the prefix.
        assert is_read_only_tool_name("bulk_get_status") is False

    def test_filter_keeps_only_read_only_by_default(self):
        tools = [{"name": "get_status"}, {"name": "update_status"}]

        assert filter_tools_for_step(tools, allow_write=False) == [{"name": "get_status"}]

    def test_filter_returns_everything_when_allow_write(self):
        tools = [{"name": "get_status"}, {"name": "update_status"}]

        assert filter_tools_for_step(tools, allow_write=True) == tools

    def test_tool_missing_name_key_is_dropped_not_matched(self):
        tools = [{"description": "no name field"}, {"name": "get_status"}]

        assert filter_tools_for_step(tools, allow_write=False) == [{"name": "get_status"}]

    def test_empty_tools_list_returns_empty(self):
        assert filter_tools_for_step([], allow_write=False) == []
        assert filter_tools_for_step([], allow_write=True) == []


def test_a_step_without_a_kind_is_an_llm_step():
    """Every existing skills.steps row omits `kind`; none may change behaviour."""
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([{"index": 0, "name": "a", "instruction": "do a"}])

    assert parsed[0].step_type == "llm"
    assert parsed[0].is_skill_step is True


def test_a_function_step_becomes_a_function_parsed_step():
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis",
         "output_var": "kpis"},
    ])

    assert parsed[0].step_type == "function"
    assert parsed[0].name == "fetch_grafana_kpis"


def test_a_function_step_is_not_marked_as_a_skill_step():
    """is_skill_step unlocks {{var}} binding for [llm] steps and is
    meaningless for [function] steps, which have their own tool access."""
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis"},
    ])

    assert parsed[0].is_skill_step is False


def test_mixed_steps_keep_their_order():
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([
        {"index": 1, "name": "reason", "instruction": "analyse {{kpis}}"},
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis",
         "output_var": "kpis"},
    ])

    assert [s.step_type for s in parsed] == ["function", "llm"]
    assert [s.index for s in parsed] == [0, 1]


def test_the_final_step_is_still_forced_to_be_a_response_step():
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis"},
        {"index": 1, "name": "reply", "instruction": "summarise"},
    ])

    assert parsed[-1].is_response_step is True
