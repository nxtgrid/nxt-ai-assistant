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

Phase 6 addendum -- declare-time and route-time permission checking are
DELIBERATELY two different functions, not one shared predicate the way
structural offerability (`_is_offerable`) is:
- `function_step_tool_declarations` calls `caller_holds_permission` itself,
  so a caller who doesn't hold a step's `required_permission` never even
  sees it declared -- a UX choice (don't dangle an unusable tool in front
  of the model) with no security weight of its own.
- `is_declared_function_step` does NOT check permission at all. A
  permission-gated call that clears the structural bar still routes to
  `WorkflowExecutor._execute_declared_function_step_call`, which calls
  `caller_holds_permission` itself and returns an explicit `not_permitted`
  soft failure (Task 6.1: "check required_permission at call time"). THIS
  is the actual enforcement boundary -- re-checked at the moment of
  execution regardless of what was declared, so a call for a name that
  looked fine in an earlier tools_payload (e.g. permissions changed
  mid-run, or the name was never legitimately declared at all) is still
  caught, rather than trusted because routing let it through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from orchestrator.experts.step_contracts import OutputSpec, StepContract
from orchestrator.experts.step_registry import get_step_contract, get_step_registry

if TYPE_CHECKING:
    from orchestrator.models.schemas import UserContext

# R5 / Task 9.1: a step whose expected_latency_seconds is at or above this
# gets a latency warning folded into its tool description (see
# derive_tool_declaration) -- chosen well below the 60-180s the three known
# long-running LPP steps actually declare, so it also catches anything
# smaller-but-still-tool-round-risky a future contract might declare.
LONG_RUNNING_THRESHOLD_SECONDS = 30.0

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
    if contract.expected_latency_seconds >= LONG_RUNNING_THRESHOLD_SECONDS:
        # R5 / Task 9.1 (docs/superpowers/plans/2026-08-20-expert-steps-as-
        # skill-tools.md): the honest, safe subset of "steps above a
        # threshold get different handling" that's actually built -- no
        # poll/resume execution path exists (see update_design_distances.py's
        # contract for why), so the call still blocks synchronously for the
        # full duration. This at least keeps the caller from mistaking a
        # slow response for a failure or a hang.
        description += (
            f" This step can take up to ~{int(contract.expected_latency_seconds)}s to "
            "complete -- a slow response is normal, not a failure."
        )
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
    """Single STRUCTURAL predicate for "does this step even exist as a
    routable tool right now?" -- deliberately says nothing about permission;
    see `caller_holds_permission` below for that, and this module's
    docstring addendum on why the two are checked separately.

    Shared by `function_step_tool_declarations` (what gets offered to the
    model) and `is_declared_function_step` (what
    `WorkflowExecutor._execute_skill_step_tool_call` will actually route to a
    real handler for) so the two can never drift on STRUCTURE -- a name with
    no contract, or a mutating name called from a non-allow_write step, is
    excluded identically at both surfaces. See Task 4.3: "No contract => not
    offered."

    Two bars, both must clear:
    - No contract at all: excluded outright (nothing to validate an
      unknown-shaped call against).
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
    if contract.mutates and not allow_write:
        return False
    return True


def caller_holds_permission(
    required_permission: str, user_context: Optional["UserContext"]
) -> bool:
    """Whether `user_context` clears `required_permission` (Phase 6 of
    docs/superpowers/plans/2026-08-20-expert-steps-as-skill-tools.md).

    `required_permission` names a role a caller must hold in
    `user_context.roles` -- reusing that existing, already-modeled field
    (see `orchestrator.models.schemas.UserContext`) rather than inventing a
    second permission-name vocabulary. `is_staff=True` always clears any
    gate regardless of `roles` content, matching the ONE authorization
    boundary actually enforced elsewhere in this codebase today
    (`UserPermissionsService._filter_and_convert_tools`'s customer-visible-
    tools filter) -- staff already sees everything non-staff doesn't, and a
    step-level permission gate that staff couldn't clear would be a new,
    narrower boundary than anything else in the app, not a consistent one.

    An empty `required_permission` always clears (nothing required). No
    `user_context` at all (None) never clears a non-empty requirement --
    absence of identity is not a permission grant.
    """
    if not required_permission:
        return True
    if user_context is None:
        return False
    if getattr(user_context, "is_staff", False):
        return True
    return required_permission in (getattr(user_context, "roles", None) or [])


def function_step_tool_declarations(
    *, allow_write: bool = False, user_context: Optional["UserContext"] = None
) -> List[Dict[str, Any]]:
    """All contract-bearing, structurally-offerable, permission-cleared
    registered steps this specific caller may see, as tool declarations.

    Args:
        allow_write: Mirrors `ParsedStep.allow_write` for the calling skill
            step. `False` (the default, matching every step's own default)
            withholds every `mutates=True` step's declaration entirely --
            see `_is_offerable`.
        user_context: The calling run's `StepContext.user_context`. `None`
            (the default) withholds every permission-gated step's
            declaration entirely -- same conservative default Phase 4 used
            before this parameter existed, now scoped specifically to
            "no identity to check" rather than "no permission checking
            exists yet". A permission-gated step this caller doesn't hold
            is simply never declared (hidden, not offered-then-rejected --
            declare-time is a UX choice, unlike the explicit `not_permitted`
            soft failure `WorkflowExecutor._execute_declared_function_step_call`
            returns for a call that reaches it anyway; see that method's
            own permission check, which is the actual enforcement boundary).

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
        and caller_holds_permission(contract.required_permission, user_context)
    ]
    declarations.sort(key=lambda d: d["name"])
    return declarations


def is_declared_function_step(name: str, *, allow_write: bool = False) -> bool:
    """Whether `call.name` should route to a real step handler, not MCP.

    Used by `WorkflowExecutor._execute_skill_step_tool_call` for routing.
    Reuses `_is_offerable` -- the STRUCTURAL half of the predicate that
    decided whether `name` was declared to the model in the first place --
    so routing can never be more permissive on contract/mutation shape than
    declaration. A name that fails this (no contract, or a mutating step
    called from a non-`allow_write` step) falls through to the existing
    `context.mcp_executor` path, which fails it cleanly as an unknown tool
    via that path's existing never-raise contract -- never silently runs an
    unvetted or ungated handler.

    Deliberately does NOT check permission (unlike Phase 4/5, this is no
    longer the same predicate `function_step_tool_declarations` uses in
    full) -- a permission-gated name that clears the structural bar still
    routes to `_execute_declared_function_step_call`, which checks
    `caller_holds_permission` itself and returns an explicit
    `not_permitted` soft failure. Routing a permission-gated call through to
    a real, actionable rejection (Task 6.1: "check required_permission at
    call time") is more useful to the model than falling through to MCP,
    which would fail it as a generic unknown-tool error with no indication
    of why.
    """
    return _is_offerable(get_step_contract(name), allow_write=allow_write)


__all__ = [
    "LONG_RUNNING_THRESHOLD_SECONDS",
    "caller_holds_permission",
    "derive_tool_declaration",
    "function_step_tool_declarations",
    "is_declared_function_step",
]
