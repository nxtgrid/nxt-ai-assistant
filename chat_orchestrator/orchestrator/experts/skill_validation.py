"""Static, save-time validation for a skill's step list.

Phase 2 of docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 4.

Operates on the stored step shape Phase 3 will persist in `skills.steps`
(jsonb) -- a plain list of dicts, not the runtime `ParsedStep` dataclass:

    {"index": 0, "name": "find_tickets", "instruction": "...",
     "output_var": "open_tickets", "allow_write": false,
     "is_response_step": false}

This is a pure function with no I/O and no LLM calls, meant to be called by
the builder UI at save time so an author sees errors before a single step
runs. Runtime keeps `render_body`'s strictness as the backstop for anything
this misses (see `skill_step_bindings.py` and
`WorkflowExecutor._execute_llm_step`) -- this function narrows the feedback
loop from "runtime pause three steps in" to "can't save."
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from orchestrator.experts.skill_step_bindings import parse_output_binding

# Matches the same {{name}} shape shared/prompts/render.py's renderer uses
# for reads. Deliberately not imported from there -- that module's pattern
# is private, and duplicating a two-line regex is cheaper than coupling
# skill validation to the prompt library's internals.
_READ_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_IDENTIFIER_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# P3's function steps (docs/superpowers/plans/2026-08-22-p3-skills-lifecycle-and-function-steps.md).
VALID_STEP_KINDS = ("llm", "function")


@dataclass(frozen=True)
class ValidationError:
    """One finding. `severity="warning"` findings don't block saving."""

    step_index: int
    step_name: str
    message: str
    severity: str = "error"  # "error" | "warning"


def _var_display(name: str) -> str:
    """'{{name}}' as a plain string -- avoids f-string brace-escaping bugs."""
    return "{{" + name + "}}"


def validate_skill_steps(
    steps: List[Dict[str, Any]],
    declared_inputs: Optional[List[str]] = None,
    exposed_handlers: Optional[List[str]] = None,
) -> List[ValidationError]:
    """Validate a skill's step list. Returns [] when everything checks out.

    Rules (see this module's docstring for the step shape):
    - Every `{{read}}` resolves to an earlier step's write or a declared
      skill input.
    - No two steps declare the same output var.
    - A write clause names a valid Python-identifier-shaped variable.
    - (warning, not error) a write that no later step reads.
    - A `kind="function"` step (P3) names a handler in `exposed_handlers`.

    `declared_inputs` are the skill's own input names (Phase 3 concept --
    pass `[]`/omit until that exists; every read then must come from an
    earlier step's write).

    `exposed_handlers` is step_registry.get_step_registry().builder_exposed_handlers()
    -- omit (the default, None) to skip handler-name checking entirely
    (existing callers that predate function steps, and this module's own
    llm-only test suite, pass no handler list at all and must keep
    working).
    """
    errors: List[ValidationError] = []
    seen_output_vars: Dict[str, int] = {}  # name -> index of the step that wrote it

    ordered_steps = sorted(steps, key=lambda s: s.get("index", 0))

    # Pass 0: step kind and handler validity. Runs first -- a malformed
    # function step would otherwise be misdiagnosed as a bad llm step by
    # the passes below, which assume every step has an `instruction`.
    for step in ordered_steps:
        index = step.get("index", 0)
        name = step.get("name") or step.get("handler") or f"step_{index}"
        kind = step.get("kind") or "llm"

        if kind not in VALID_STEP_KINDS:
            errors.append(
                ValidationError(
                    index,
                    name,
                    f"unknown step kind {kind!r}; expected one of "
                    f"{', '.join(VALID_STEP_KINDS)}",
                )
            )
            continue

        if kind != "function":
            continue

        handler = step.get("handler")
        if not handler:
            errors.append(
                ValidationError(index, name, "a function step must name a handler")
            )
            continue

        if exposed_handlers is not None and handler not in exposed_handlers:
            errors.append(
                ValidationError(
                    index,
                    name,
                    f"handler {handler!r} is not available to the skill builder; "
                    f"available: {', '.join(exposed_handlers) or '(none)'}",
                )
            )

    # Pass 1: each step's own write clause is well-formed and unique.
    for step in ordered_steps:
        index = step.get("index", 0)
        name = step.get("name") or f"step_{index}"
        instruction = step.get("instruction") or ""
        stored_output_var = step.get("output_var")
        is_function_step = (step.get("kind") or "llm") == "function"

        if is_function_step:
            # The write comes from the handler's return value, not a
            # '-> {{var}}' clause in an instruction -- nothing to parse, and
            # no write-clause-vs-output_var consistency to check. It still
            # goes through the same identifier/uniqueness checks below as
            # an llm step's write, though: a bad or colliding output_var is
            # exactly as broken coming from a handler as from a clause.
            effective_output_var = stored_output_var
        else:
            _read_text, parsed_output_var = parse_output_binding(instruction)

            if stored_output_var and not parsed_output_var:
                errors.append(
                    ValidationError(
                        index,
                        name,
                        f"declares output_var {stored_output_var!r} but its instruction has no "
                        f"'-> {_var_display(stored_output_var)}' write clause",
                    )
                )
            elif (
                stored_output_var
                and parsed_output_var
                and stored_output_var != parsed_output_var
            ):
                errors.append(
                    ValidationError(
                        index,
                        name,
                        f"instruction's write clause names {_var_display(parsed_output_var)} "
                        f"but the stored output_var is {_var_display(stored_output_var)}",
                    )
                )

            effective_output_var = stored_output_var or parsed_output_var

        if not effective_output_var:
            continue

        if not _IDENTIFIER_RE.match(effective_output_var):
            errors.append(
                ValidationError(
                    index,
                    name,
                    f"output var {_var_display(effective_output_var)} is not a valid identifier",
                )
            )
            continue

        if effective_output_var in seen_output_vars:
            first_index = seen_output_vars[effective_output_var]
            errors.append(
                ValidationError(
                    index,
                    name,
                    f"output var {_var_display(effective_output_var)} is already written by "
                    f"step {first_index}",
                )
            )
            continue

        seen_output_vars[effective_output_var] = index

    # Pass 2: every read resolves to an earlier write or a declared input.
    # Order matters here -- "earlier" is enforced by walking steps in index
    # order and only adding a step's own output var to `available` *after*
    # checking that step's reads.
    available = set(declared_inputs or [])
    used_vars: set = set()
    for step in ordered_steps:
        index = step.get("index", 0)
        name = step.get("name") or f"step_{index}"
        instruction = step.get("instruction") or ""
        read_text, parsed_output_var = parse_output_binding(instruction)

        for match in _READ_VAR_RE.finditer(read_text):
            var_name = match.group(1)
            used_vars.add(var_name)
            if var_name not in available:
                errors.append(
                    ValidationError(
                        index,
                        name,
                        f"reads {_var_display(var_name)} but no earlier step writes it and "
                        "it isn't a declared skill input",
                    )
                )

        effective_output_var = step.get("output_var") or parsed_output_var
        if effective_output_var and _IDENTIFIER_RE.match(effective_output_var):
            available.add(effective_output_var)

    # Pass 3 (warning): a write nothing downstream reads. Note this can miss
    # a genuine "unused write" when its name collides with a declared input
    # of the same name (the input's read satisfies pass 2 and also marks
    # the name "used" here) -- a rare enough edge case to accept rather than
    # add a second used-vars set to disambiguate for a warning-level rule.
    for out_var, index in seen_output_vars.items():
        if out_var in used_vars:
            continue
        step = next((s for s in ordered_steps if s.get("index") == index), {})
        name = step.get("name") or f"step_{index}"
        errors.append(
            ValidationError(
                index,
                name,
                f"writes {_var_display(out_var)} but no later step reads it",
                severity="warning",
            )
        )

    return errors


__all__ = ["ValidationError", "validate_skill_steps"]
