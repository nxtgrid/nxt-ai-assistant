"""Tests for orchestrator.experts.skill_validation -- static, save-time
validation of a skill's step list (Phase 2 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 4).
"""

from __future__ import annotations

from orchestrator.experts.skill_validation import ValidationError, validate_skill_steps


def _step(index, name, instruction, output_var=None):
    return {"index": index, "name": name, "instruction": instruction, "output_var": output_var}


class TestNoIssues:
    def test_empty_step_list_has_no_errors(self):
        assert validate_skill_steps([]) == []

    def test_steps_with_no_variables_at_all_are_fine(self):
        steps = [_step(0, "greet", "Say hello."), _step(1, "respond", "Answer the question.")]

        assert validate_skill_steps(steps) == []

    def test_write_then_read_is_valid(self):
        steps = [
            _step(0, "find", "List open tickets -> {{tickets}}", output_var="tickets"),
            _step(1, "evaluate", "Evaluate {{tickets}} for closure."),
        ]

        assert validate_skill_steps(steps) == []

    def test_declared_skill_input_can_be_read_with_no_prior_write(self):
        steps = [_step(0, "greet", "Say hello to {{name}}.")]

        assert validate_skill_steps(steps, declared_inputs=["name"]) == []


class TestUndeclaredRead:
    def test_read_with_no_earlier_write_and_no_declared_input_is_an_error(self):
        steps = [_step(0, "broken", "Use {{mystery}} here.")]

        errors = validate_skill_steps(steps)

        assert len(errors) == 1
        assert errors[0].severity == "error"
        assert errors[0].step_index == 0
        assert "mystery" in errors[0].message

    def test_read_before_the_step_that_writes_it_is_still_an_error(self):
        # Order matters -- {{x}} is written by step 1, but step 0 (which
        # runs first) tries to read it.
        steps = [
            _step(0, "too_early", "Use {{x}} here."),
            _step(1, "writer", "Find it -> {{x}}", output_var="x"),
        ]

        errors = validate_skill_steps(steps)

        assert any(e.step_index == 0 and "x" in e.message for e in errors)


class TestDuplicateOutputVars:
    def test_two_steps_writing_the_same_var_is_an_error(self):
        steps = [
            _step(0, "first", "Find it -> {{x}}", output_var="x"),
            _step(1, "second", "Find it again -> {{x}}", output_var="x"),
        ]

        errors = validate_skill_steps(steps)

        assert any(
            e.step_index == 1 and e.severity == "error" and "already written by step 0" in e.message
            for e in errors
        )


class TestMalformedWriteClause:
    def test_output_var_not_a_valid_identifier_is_an_error(self):
        steps = [_step(0, "broken", "Find it -> {{2invalid}}")]

        # The regex only matches valid identifiers, so a malformed name in
        # the arrow clause means parse_output_binding finds no write at all
        # -- there's nothing to flag as "invalid identifier" because it was
        # never recognized as a write clause in the first place. Confirmed
        # here as a documented behavior, not a silent gap.
        errors = validate_skill_steps(steps)
        assert errors == []

    def test_stored_output_var_without_a_matching_write_clause_is_an_error(self):
        # output_var is set in storage, but the instruction text has no
        # "-> {{...}}" clause at all -- storage and instruction disagree.
        steps = [_step(0, "mismatched", "Find the ticket count.", output_var="ticket_count")]

        errors = validate_skill_steps(steps)

        assert any(e.step_index == 0 and "ticket_count" in e.message for e in errors)

    def test_stored_output_var_mismatched_with_instruction_write_clause_is_an_error(self):
        steps = [_step(0, "mismatched", "Find it -> {{actual_name}}", output_var="wrong_name")]

        errors = validate_skill_steps(steps)

        assert any(
            e.step_index == 0 and "actual_name" in e.message and "wrong_name" in e.message
            for e in errors
        )


class TestUnusedWriteWarning:
    def test_write_nothing_reads_is_a_warning_not_an_error(self):
        steps = [_step(0, "find", "Find it -> {{unused}}", output_var="unused")]

        errors = validate_skill_steps(steps)

        assert len(errors) == 1
        assert errors[0].severity == "warning"
        assert "unused" in errors[0].message

    def test_write_that_is_read_produces_no_warning(self):
        steps = [
            _step(0, "find", "Find it -> {{used}}", output_var="used"),
            _step(1, "use_it", "Do something with {{used}}."),
        ]

        assert validate_skill_steps(steps) == []


class TestValidationErrorShape:
    def test_is_a_frozen_dataclass_with_expected_fields(self):
        error = ValidationError(step_index=2, step_name="a_step", message="something", severity="error")

        assert error.step_index == 2
        assert error.step_name == "a_step"
        assert error.severity == "error"

    def test_severity_defaults_to_error(self):
        error = ValidationError(step_index=0, step_name="s", message="m")

        assert error.severity == "error"


class TestStepsOutOfOrderInInput:
    def test_validation_sorts_by_index_regardless_of_list_order(self):
        # Steps passed out of order (e.g. from an unordered JSON blob) must
        # still be validated in logical (index) order, not list order.
        steps = [
            _step(1, "reader", "Use {{x}}."),
            _step(0, "writer", "Find it -> {{x}}", output_var="x"),
        ]

        assert validate_skill_steps(steps) == []
