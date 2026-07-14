"""Machine-readable step contracts for expert workflow step handlers.

A workflow's *recipe* (which steps run, in what order) lives in the Google Doc
expert definition and is parsed by `expert_instructions_provider.py`. That
recipe says nothing about *data dependencies* -- what packet_state keys or
prior-step results a given step actually reads, or what it produces. Today
that knowledge only exists implicitly, inside each handler's body.

`StepContract` externalizes that knowledge as a plain, inspectable dataclass
attached to a step handler at registration time (see
`StepHandlerRegistry.register()` / `register_step()` in `step_registry.py`).
This lets the workflow executor validate that a step's prerequisites are
satisfied *before* running it -- which matters once steps can be invoked out
of normal recipe order (Phase C's `run_single_step`, a later task) rather
than only ever executing sequentially from the top of the workflow.

This is NOT the deprecated `StepSchema` / `ParameterDefinition` pair still
defined in `step_registry.py` for backwards compatibility -- those were built
for a since-abandoned step-level parameter-confirmation UI and describe user-
facing parameters for a confirmation prompt. `StepContract` instead describes
a step's data-dependency shape (state read/written, prior results consumed,
guard conditions) for the executor's own bookkeeping. The two are unrelated
and this module does not touch or replace `StepSchema`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParamSpec:
    """Describes one parameter a step handler reads via context.get_parameter_value().

    Attributes:
        name: Parameter name, as passed to `context.get_parameter_value(name)`.
        param_type: Logical type of the parameter (e.g. "string", "integer",
            "number", "boolean"). Informational only -- not enforced here.
        description: Human-readable explanation of what the parameter is for.
        synonyms: Alternate names/phrasings a caller might use to refer to
            this parameter (e.g. for LLM-driven parameter resolution).
        required: Whether the step cannot meaningfully run without this
            parameter being set.
        default: Default value used when the parameter is not supplied.
    """

    name: str
    param_type: str = "string"
    description: str = ""
    synonyms: tuple[str, ...] = ()
    required: bool = False
    default: Any = None


@dataclass(frozen=True)
class StepContract:
    """Machine-readable contract for a step handler, attached at registration time.

    This is NOT the deprecated `StepSchema` (see `step_registry.py`) --
    `StepSchema` was for a since-abandoned step-level parameter-confirmation
    UI. `StepContract` instead describes a step's data dependencies (what
    packet_state/prior-step-results it reads, what it produces) so the
    workflow executor can validate prerequisites before running a step out
    of order (see Phase C's `run_single_step`, a later task).

    Recipe/ordering (which steps run, and when) stays in the Google Doc
    expert definition; this dataclass is the machine-readable data-dependency
    layer that sits alongside it.

    Attributes:
        description: Human-readable summary of what the step does.
        consumes_state: `packet_state` keys this step reads via
            `context.get_state(...)` and cannot meaningfully run without --
            absence means the step would crash, produce garbage, or (at
            best) have no path forward except pausing for user input.
            Contrast with `optional_consumes_state` below.
        optional_consumes_state: `packet_state` keys this step reads
            opportunistically via `context.get_state(...)`, where the
            handler body has genuine in-handler fallback/default logic for
            when the key is absent (e.g. `X = context.get_state(key) or
            default`, or an `if X: ... else: <legitimate alternate path>`).
            The step functions correctly without these -- they are not
            prerequisites. `validate_step_prerequisites` reports missing
            entries here informationally (`PrereqReport.missing_optional_state`)
            but they never block `satisfied` and are never fed into
            `producer_chain` auto-resolution, unlike `consumes_state`.
        produces_state: `packet_state` keys this step writes via its
            returned `StepResult.state_updates`.
        consumes_results: Names of previous steps whose results this step
            reads via `context.get_previous_result(...)`.
        params: Parameters this step reads via
            `context.get_parameter_value(...)`.
        guard_keys: `packet_state` keys used as idempotency/guard checks
            (e.g. a "*_generated" flag checked before doing real work).
        side_effects: Free-form description of external side effects this
            step has (e.g. "calls grid_design MCP server", "uploads to
            Google Drive") for operators/executors reasoning about safety
            of re-running or skipping the step.
    """

    description: str = ""
    consumes_state: tuple[str, ...] = ()
    optional_consumes_state: tuple[str, ...] = ()
    produces_state: tuple[str, ...] = ()
    consumes_results: tuple[str, ...] = ()
    params: tuple[ParamSpec, ...] = ()
    guard_keys: tuple[str, ...] = ()
    side_effects: str = ""


__all__ = ["ParamSpec", "StepContract"]
