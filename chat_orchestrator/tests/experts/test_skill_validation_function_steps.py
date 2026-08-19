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
