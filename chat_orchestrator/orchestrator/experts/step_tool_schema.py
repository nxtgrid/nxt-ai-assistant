"""Derive LLM-callable tool declarations from registered `StepContract`s.

Phase 4 of docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md.
Before this module, a skill's `[llm]` step could only call MCP tools (see
`skill_step_bindings.filter_tools_for_step`); a registered step handler
(`@register_step`, `orchestrator/experts/handlers/**`) was invisible to that
tool-call loop no matter how rich its `StepContract` was. This module builds
the missing half: a Gemini-format function declaration for each contract-
bearing, permission-cleared handler, in the exact shape
`UserPermissionsService._convert_to_gemini_format` already produces for MCP
tools (`name`/`description`/`parameters` with `type: OBJECT`), so the two
lists can sit in the same `tools_payload` with no translation at the call
site. See `WorkflowExecutor._execute_skill_step_tool_call` for the routing
half (which name goes to a real handler vs. `context.mcp_executor`).

Two facts from the design spec shape everything below:

1. Contracts describe state, not arguments (see `step_contracts.py`).
   `consumes_state` and `params` are BOTH real inputs -- a schema built from
   `params` alone is empty for most steps (12 of LPP's 17 handlers declare
   `params=()`).
2. Not every `consumes_state` key is a caller-suppliable argument. A key
   another registered step's `produces_state` already claims (e.g.
   `populate_lpp_cells`'s `document_id`, produced by `copy_lpp_template`) is
   a PRECONDITION -- something `WorkflowExecutor._soft_failure_before_running_step`
   checks before the handler ever runs -- not a tool argument. Declaring it
   as an argument anyway would invite the model to invent a value for
   something only a prior call can legitimately produce.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from orchestrator.experts.step_contracts import OutputSpec, StepContract
from orchestrator.experts.step_registry import get_step_contract, get_step_registry

# StepContract/ParamSpec/OutputSpec's value_type strings map to Gemini's
# function-declaration type enum the same way UserPermissionsService's own
# _convert_property_type maps raw MCP JSON-Schema types -- mirrored here so a
# derived declaration is indistinguishable, format-wise, from an MCP one.
_GEMINI_TYPES: Dict[str, str] = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "object": "OBJECT",
    "array": "ARRAY",
}


def _gemini_type(value_type: str) -> str:
    """Map a ParamSpec.param_type/OutputSpec.value_type string to Gemini's enum.

    Falls back to STRING for anything unrecognized -- these type strings are
    informational only (see ParamSpec/OutputSpec's own docstrings), so an
    unrecognized value is a documentation slip, not something worth failing
    schema derivation over.
    """
    return _GEMINI_TYPES.get((value_type or "string").lower(), "STRING")


def _producible_state_keys(contracts: Dict[str, StepContract]) -> Set[str]:
    """Every packet_state key some registered contract's `produces_state` writes.

    Deliberately global across every registered contract, not scoped to one
    expert's steps -- this matches `WorkflowExecutor.validate_step_prerequisites`'s
    own `producer_chain` search exactly (it also scans `get_step_registry().
    list_handlers()` unfiltered by packet_type), so a key this function calls
    "has a producer" is precisely a key that check can already resolve or
    explain via `remediation`. Computing this per-expert instead would create
    a second, silently different notion of "has a producer" from the one the
    runtime precondition check actually uses.
    """
    producible: Set[str] = set()
    for contract in contracts.values():
        producible.update(contract.produces_state)
    return producible


def derive_tool_declaration(
    step_name: str,
    contract: StepContract,
    producible_state_keys: Set[str],
) -> Dict[str, Any]:
    """Build one Gemini-format function declaration for a contract-bearing step.

    Args:
        step_name: The registered handler's name (matches `[function:name]`
            and what `context.mcp_executor`-style tool calls carry as
            `call.name`).
        contract: That handler's `StepContract`.
        producible_state_keys: Output of `_producible_state_keys` (or an
            equivalent set) -- `consumes_state` keys in this set are
            preconditions, not arguments; see module docstring, fact 2.

    Returns:
        `{"name", "description", "parameters"}`, `parameters` always
        `{"type": "OBJECT", "properties": {...}, "required": [...]}` even
        when empty (matching `UserPermissionsService._convert_to_gemini_format`'s
        shape exactly, so an empty-argument tool is still well-formed).
    """
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for key in contract.consumes_state:
        if key in producible_state_keys:
            continue  # precondition, not an argument -- fact 2 above
        properties[key] = {
            "type": "STRING",
            "description": (
                f"Value for packet_state key '{key}'. Only needed if a prior "
                "call in this run hasn't already supplied it."
            ),
        }
        # Never added to `required`: every one of these keys, by definition
        # of reaching this branch, has no known producer step either -- some
        # resolve via an env-var/default fallback inside the handler itself
        # (e.g. copy_lpp_template's template_id), so omitting the argument
        # gets the same behavior a top-level recipe run gets today.

    for param in contract.params:
        properties[param.name] = {
            "type": _gemini_type(param.param_type),
            "description": param.description or f"'{param.name}' parameter.",
        }
        if param.required and param.default is None:
            required.append(param.name)

    description = contract.description or f"Run the '{step_name}' step."
    if contract.mutates:
        description += " This step has a real external side effect."
    if contract.outputs:
        # Gemini's function-declaration format has no separate "response
        # schema" slot the way e.g. OpenAPI does -- description is the only
        # place to tell the model what a call actually hands back, so
        # `contract.outputs` (Task 4.1: "outputs come from `outputs`") is
        # folded in here rather than left undeclared.
        def _describe_output(output: OutputSpec) -> str:
            note = f"{output.name} ({output.value_type})"
            return f"{note}: {output.description}" if output.description else note

        output_notes = "; ".join(_describe_output(output) for output in contract.outputs)
        description += f" Returns: {output_notes}."

    return {
        "name": step_name,
        "description": description,
        "parameters": {
            "type": "OBJECT",
            "properties": properties,
            "required": required,
        },
    }


def _is_offerable(contract: Optional[StepContract], *, allow_write: bool) -> bool:
    """Single predicate for "may this step be offered/called as a tool right now?"

    Shared by `function_step_tool_declarations` (what gets offered to the
    model) and `is_declared_function_step` (what
    `WorkflowExecutor._execute_skill_step_tool_call` will actually route to a
    real handler for) so the two can never drift -- a name never declared
    can never be routed either, even if a call for it arrives anyway (a
    hallucinated name, or a stale tools_payload from an earlier, more
    permissive round). See Task 4.3: "No contract => not offered."

    Two bars, both must clear:
    - No contract at all: excluded outright (nothing to validate an
      unknown-shaped call against).
    - `required_permission` set: excluded outright for now. This module has
      no caller/user context to check a specific permission grant against --
      Phase 6 of the skills plan is chartered to build that. Until it does,
      the conservative default is to withhold the declaration entirely
      rather than declare a gated tool and rely on a call-time check that
      doesn't exist yet.
    - `mutates=True` and `allow_write=False`: excluded. Mirrors
      `skill_step_bindings.filter_tools_for_step`'s existing read-only gate
      for MCP tools -- same intent (a step not explicitly marked
      `allow_write=True` gets no write-capable tools), applied here via the
      contract's actual `mutates` flag instead of a name-prefix heuristic
      (see step_contracts.py's MUTATION_KINDS docstring for why the prefix
      heuristic must never be reused for this).
    """
    if contract is None:
        return False
    if contract.required_permission:
        return False
    if contract.mutates and not allow_write:
        return False
    return True


def function_step_tool_declarations(*, allow_write: bool = False) -> List[Dict[str, Any]]:
    """All contract-bearing, permission-cleared registered steps, as tool declarations.

    Args:
        allow_write: Mirrors `ParsedStep.allow_write` for the calling skill
            step. `False` (the default, matching every step's own default)
            withholds every `mutates=True` step's declaration entirely --
            see `_is_offerable`.

    Returns:
        Declarations sorted by name for a stable, diffable order (tests and
        any future display of "tools available to this step" both benefit
        from this not reshuffling between calls).
    """
    registry = get_step_registry()
    all_contracts: Dict[str, StepContract] = {}
    for name in registry.list_handlers():
        contract = registry.get_contract(name)
        if contract is not None:
            all_contracts[name] = contract

    producible = _producible_state_keys(all_contracts)

    declarations = [
        derive_tool_declaration(name, contract, producible)
        for name, contract in all_contracts.items()
        if _is_offerable(contract, allow_write=allow_write)
    ]
    declarations.sort(key=lambda d: d["name"])
    return declarations


def is_declared_function_step(name: str, *, allow_write: bool = False) -> bool:
    """Whether `call.name` should route to a real step handler, not MCP.

    Used by `WorkflowExecutor._execute_skill_step_tool_call` for routing.
    Reuses `_is_offerable` -- the exact predicate that decided whether `name`
    was declared to the model in the first place -- so routing can never be
    more permissive than declaration. A name that fails this (no contract,
    permission-gated, or a mutating step called from a non-`allow_write`
    step) falls through to the existing `context.mcp_executor` path, which
    fails it cleanly as an unknown tool via that path's existing never-raise
    contract -- never silently runs an unvetted or ungated handler.
    """
    return _is_offerable(get_step_contract(name), allow_write=allow_write)


__all__ = [
    "derive_tool_declaration",
    "function_step_tool_declarations",
    "is_declared_function_step",
]
