"""Tests for prompt body rendering."""

import pytest

from shared.prompts.render import render_body, split_sections
from shared.prompts.types import PromptRenderError


def _no_partials(prompt_id: str) -> str:
    raise AssertionError(f"unexpected partial lookup: {prompt_id}")


def test_substitutes_declared_variable():
    out = render_body("Hi {{name}}.", {"name": "Ada"}, ["name"], _no_partials)
    assert out == "Hi Ada."


def test_substitutes_repeated_variable():
    out = render_body("{{n}}/{{n}}", {"n": "x"}, ["n"], _no_partials)
    assert out == "x/x"


def test_undeclared_placeholder_raises():
    with pytest.raises(PromptRenderError, match="not declared"):
        render_body("Hi {{name}}.", {"name": "Ada"}, [], _no_partials)


def test_missing_value_raises():
    with pytest.raises(PromptRenderError, match="no value"):
        render_body("Hi {{name}}.", {}, ["name"], _no_partials)


def test_none_value_raises():
    with pytest.raises(PromptRenderError, match="no value"):
        render_body("Hi {{name}}.", {"name": None}, ["name"], _no_partials)


def test_inlines_partial():
    def resolve(prompt_id):
        assert prompt_id == "partials.tone"
        return "Be brief."

    out = render_body("A. {{> partials.tone}} B.", {}, [], resolve)
    assert out == "A. Be brief. B."


def test_partial_may_contain_variables_of_the_host():
    def resolve(prompt_id):
        return "Grid {{grid}}."

    out = render_body("{{> partials.grid}}", {"grid": "ABC"}, ["grid"], resolve)
    assert out == "Grid ABC."


def test_partial_must_be_namespaced():
    with pytest.raises(PromptRenderError, match="partials\\."):
        render_body("{{> customer.system}}", {}, [], _no_partials)


def test_partial_cycle_raises():
    def resolve(prompt_id):
        return "{{> partials.a}}"

    with pytest.raises(PromptRenderError, match="cycle"):
        render_body("{{> partials.a}}", {}, [], resolve)


def test_partial_depth_cap():
    def resolve(prompt_id):
        depth = int(prompt_id.rsplit("p", 1)[-1])
        return f"{{{{> partials.p{depth + 1}}}}}"

    with pytest.raises(PromptRenderError, match="depth"):
        render_body("{{> partials.p0}}", {}, [], resolve)


def test_split_sections_routes_named_heading_to_system():
    body = "# System Instructions\n\nBe kind.\n\n# Examples\n\nQ then A.\n"
    system, context = split_sections(body, ["system_instructions"])
    assert system == "Be kind."
    assert context == "# Examples\n\nQ then A."


def test_split_sections_with_no_declared_sections_is_all_system():
    system, context = split_sections("Just text.", [])
    assert system == "Just text."
    assert context is None


def test_split_sections_missing_named_section_raises():
    with pytest.raises(PromptRenderError, match="System Instructions"):
        split_sections("# Examples\n\nx", ["system_instructions"])
