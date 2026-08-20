"""Function steps are validated before a skill can be saved."""

from orchestrator.experts.skill_validation import validate_skill_steps


def _errors(steps, exposed=("fetch_grafana_kpis",)):
    return validate_skill_steps(steps, exposed_handlers=list(exposed))


def test_a_known_exposed_handler_validates():
    assert _errors([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis",
         "output_var": "kpis"},
        {"index": 1, "name": "reply", "instruction": "summarise {{kpis}}"},
    ]) == []


def test_an_unknown_handler_is_rejected():
    errors = _errors([{"index": 0, "kind": "function", "handler": "no_such_handler"}])
    assert len(errors) == 1
    assert "no_such_handler" in errors[0].message


def test_a_registered_but_unexposed_handler_is_rejected():
    errors = _errors(
        [{"index": 0, "kind": "function", "handler": "copy_lpp_template"}],
        exposed=("fetch_grafana_kpis",),
    )
    assert len(errors) == 1
    assert errors[0].severity == "error"


def test_a_function_step_without_a_handler_is_rejected():
    errors = _errors([{"index": 0, "kind": "function"}])
    assert len(errors) == 1
    assert "handler" in errors[0].message


def test_an_unknown_kind_is_rejected():
    errors = _errors([{"index": 0, "kind": "webhook", "handler": "x"}])
    assert len(errors) == 1
    assert "webhook" in errors[0].message


def test_a_function_step_output_var_is_readable_downstream():
    """The write comes from the handler, not a '-> {{var}}' clause."""
    assert _errors([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis",
         "output_var": "kpis"},
        {"index": 1, "name": "reply", "instruction": "summarise {{kpis}}"},
    ]) == []


def test_omitting_exposed_handlers_skips_handler_checks():
    """Back-compat: existing callers pass no handler list at all."""
    assert validate_skill_steps(
        [{"index": 0, "name": "a", "instruction": "do a"}]
    ) == []


# Task 5.4 (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md):
# a mutating step with no MockSpec cannot be saved mock-enabled.


def test_mock_enabled_step_naming_an_unmockable_handler_is_rejected():
    errors = validate_skill_steps(
        [{"index": 0, "kind": "function", "handler": "write_review_section", "mock": True}],
        exposed_handlers=["write_review_section"],
        unmockable_handlers={"write_review_section"},
    )
    assert len(errors) == 1
    assert "write_review_section" in errors[0].message
    assert errors[0].severity == "error"


def test_mock_disabled_step_naming_an_unmockable_handler_is_fine():
    """Only an EXPLICIT mock:true is checked -- a step that doesn't opt into
    mock mode at all isn't blocked by a handler having no MockSpec."""
    errors = validate_skill_steps(
        [{"index": 0, "kind": "function", "handler": "write_review_section", "mock": False}],
        exposed_handlers=["write_review_section"],
        unmockable_handlers={"write_review_section"},
    )
    assert errors == []


def test_mock_field_absent_naming_an_unmockable_handler_is_fine():
    """Same as above -- absence (None, deferring to the run's baseline) is
    not an explicit mock:true, so it isn't flagged either. The runtime
    backstop (WorkflowExecutor._mock_step_result) is what catches this case
    if a future mock run's baseline happens to make it mocked anyway."""
    errors = validate_skill_steps(
        [{"index": 0, "kind": "function", "handler": "write_review_section"}],
        exposed_handlers=["write_review_section"],
        unmockable_handlers={"write_review_section"},
    )
    assert errors == []


def test_mock_enabled_step_naming_a_mockable_handler_is_fine():
    errors = validate_skill_steps(
        [{"index": 0, "kind": "function", "handler": "copy_lpp_template", "mock": True}],
        exposed_handlers=["copy_lpp_template"],
        unmockable_handlers={"write_review_section"},  # a DIFFERENT handler is unmockable
    )
    assert errors == []


def test_omitting_unmockable_handlers_skips_the_mock_check_entirely():
    """Back-compat: existing callers (and every call site before Task 5.4)
    pass no unmockable_handlers set at all."""
    errors = validate_skill_steps(
        [{"index": 0, "kind": "function", "handler": "write_review_section", "mock": True}],
        exposed_handlers=["write_review_section"],
    )
    assert errors == []


def test_llm_step_is_unaffected_by_the_mock_check():
    """The mock check only runs in Pass 0's kind=='function' branch -- an
    [llm] step's own 'mock' field (Phase 5's ParsedStep.mock override for
    its tool-call loop) is never checked against unmockable_handlers, since
    an llm step names no single handler to check it against."""
    errors = validate_skill_steps(
        [{"index": 0, "name": "a", "instruction": "do a", "mock": True}],
        unmockable_handlers={"write_review_section"},
    )
    assert errors == []
