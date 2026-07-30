"""Tests for .prompt file parsing."""

import pytest

from shared.prompts.components import UNCATEGORIZED
from shared.prompts.spec import AccessSpec, parse_prompt_file

VALID = """---
id: customer.system
description: Customer-mode instructions.
owner: ops
component: orchestrator_services
overridable: true
output: text
variables: [user_name]
sections: [system_instructions, examples]
knowledge_tags: [grid_ops]
access:
  view: [ops, eng]
  edit: [ops]
  publish: [eng]
---
# System Instructions

Hello {{user_name}}.
"""


def test_parses_frontmatter_fields():
    spec = parse_prompt_file(VALID, path="customer.system.prompt")
    assert spec.id == "customer.system"
    assert spec.owner == "ops"
    assert spec.component == "orchestrator_services"
    assert spec.overridable is True
    assert spec.variables == ["user_name"]
    assert spec.sections == ["system_instructions", "examples"]
    assert spec.knowledge_tags == ["grid_ops"]


def test_component_defaults_to_uncategorized():
    text = "---\nid: a.b\ndescription: d\n---\nbody"
    spec = parse_prompt_file(text, path="a.b.prompt")
    assert spec.component == UNCATEGORIZED


def test_body_excludes_frontmatter():
    spec = parse_prompt_file(VALID, path="x.prompt")
    assert spec.body.startswith("# System Instructions")
    assert "owner: ops" not in spec.body


def test_access_defaults_to_empty_lists():
    text = "---\nid: a.b\ndescription: d\n---\nbody"
    spec = parse_prompt_file(text, path="a.b.prompt")
    assert spec.access == AccessSpec(view=[], edit=[], publish=[])


def test_defaults_are_conservative():
    text = "---\nid: a.b\ndescription: d\n---\nbody"
    spec = parse_prompt_file(text, path="a.b.prompt")
    assert spec.overridable is False
    assert spec.output == "text"
    assert spec.owner == "eng"


def test_checksum_is_stable_and_body_only():
    a = parse_prompt_file(VALID, path="x.prompt")
    b = parse_prompt_file(VALID.replace("owner: ops", "owner: eng"), path="x.prompt")
    assert a.checksum == b.checksum


def test_missing_frontmatter_raises():
    with pytest.raises(ValueError, match="frontmatter"):
        parse_prompt_file("no frontmatter here", path="x.prompt")


def test_missing_id_raises():
    with pytest.raises(ValueError, match="id"):
        parse_prompt_file("---\ndescription: d\n---\nbody", path="x.prompt")


def test_missing_description_raises():
    with pytest.raises(ValueError, match="description"):
        parse_prompt_file("---\nid: a.b\n---\nbody", path="x.prompt")


def test_json_output_requires_schema():
    text = "---\nid: a.b\ndescription: d\noutput: json\n---\nbody"
    with pytest.raises(ValueError, match="schema"):
        parse_prompt_file(text, path="a.b.prompt")


def test_json_output_with_schema_is_accepted():
    text = (
        "---\nid: a.b\ndescription: d\noutput: json\n"
        "schema:\n  type: object\n---\nbody"
    )
    spec = parse_prompt_file(text, path="a.b.prompt")
    assert spec.output == "json"
    assert spec.schema == {"type": "object"}
