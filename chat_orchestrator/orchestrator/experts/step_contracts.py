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

from dataclasses import dataclass, field
from typing import Any, Optional


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


# Machine-readable mutation categories a step's `mutation_kind` may declare.
# `side_effects` stays free-form prose for humans; this tuple is what code
# branches on to decide whether a step needs a MockSpec. Never infer mutation
# from `side_effects` text, and never reuse the READ_ONLY_TOOL_PREFIXES
# name-prefix heuristic in skill_step_bindings.py for this purpose -- that
# heuristic already mislabels some handlers (e.g. `store_module` and
# `process_doc_edits` both write and match no read-only prefix).
MUTATION_KINDS: tuple[str, ...] = (
    "external_write",  # writes to an external system: Drive, Sheets, Telegram, etc.
    "db_write",  # writes to a database this codebase owns
    "notification",  # sends a message/notification a human will see
    "control_action",  # commands external equipment or an external control system
)


@dataclass(frozen=True)
class OutputSpec:
    """Describes one value a step produces, so a caller can chain onto it.

    `StepContract.produces_state` names the `packet_state` keys a step
    writes, but not their type or meaning, and `StepResult.data` (the
    `accumulated_results` half of a step's output) is undeclared entirely.
    Without this, an LLM calling steps as tools has no way to know what a
    prior call actually returned, or whether it satisfies a later call's
    input -- only the step's name to guess from.

    Attributes:
        name: Key this value is published under (a `produces_state` key when
            `where="state"`, an `accumulated_results`/`StepResult.data` key
            when `where="data"`).
        value_type: Logical type of the value (e.g. "string", "integer",
            "number", "boolean", "object", "array"). Informational only,
            mirroring `ParamSpec.param_type` -- not enforced here.
        description: What this value means to a caller.
        where: Which half of `StepResult` carries this value -- `"state"`
            for a `state_updates` key (expected to also appear in the
            contract's `produces_state`), or `"data"` for a key nested
            under `StepResult.data`.
    """

    name: str
    value_type: str = "string"
    description: str = ""
    where: str = "state"


@dataclass
class MockSpec:
    """Stand-in result for a mutating step run in mock mode.

    Not `frozen`, unlike every other dataclass in this module -- it holds
    mutable dict fields (`state_updates`, `data`), and a dataclass-generated
    `__hash__` on mutable fields would raise at runtime. Nothing hashes a
    `StepContract` or `MockSpec` today; failing loudly the moment something
    tries beats silently making this type unhashable via `frozen=True`.

    A mock that returns nothing is worse than no mock at all: mock
    `copy_lpp_template` into an empty result and the next step
    (`populate_lpp_cells`) fails its `document_id` precondition, so a mocked
    run collapses at the first mutation and proves nothing end to end. A
    `MockSpec` therefore must populate the same keys the real step would --
    see `validate_mock_covers_outputs` below, which checks exactly that.

    Values should be self-evidently synthetic -- a `MOCK-` prefix, an
    obviously fake id -- so a mocked artefact is never mistaken for a real
    one on inspection. This convention is not enforced by the dataclass
    itself; it is on whoever writes the `MockSpec` for a given step.

    Attributes:
        state_updates: Stands in for the real step's
            `StepResult.state_updates`.
        data: Stands in for the real step's `StepResult.data`.
        message: Progress message shown for the mocked step. Should read as
            hypothetical (e.g. "Would have copied the LPP template.").
    """

    state_updates: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""


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
            of re-running or skipping the step. Prose for humans only --
            never branch code on this text; see `mutates`/`mutation_kind`.
        mutates: Whether this step has an external side effect that a mock
            run should not perform for real (writes to Drive/Sheets, sends a
            Telegram message, triggers BOM generation, controls equipment,
            etc.). Machine-readable and deliberately separate from the
            `side_effects` prose -- see that field's docstring for why
            nothing should infer this from text or from a name-prefix
            heuristic. Defaults to `False`, matching every step handler
            before this field existed: a step is presumed safe to run for
            real unless explicitly marked otherwise.
        mutation_kind: One of `MUTATION_KINDS` describing what kind of
            mutation this is, when `mutates=True`. Empty string when
            `mutates=False`. Purely descriptive -- not enforced here.
        outputs: Typed description of the values this step produces (see
            `OutputSpec`), complementing the bare key names in
            `produces_state`.
        mock: The stand-in result to return instead of actually running this
            step, when a skill run has mock mode enabled and `mutates=True`.
            `None` for a step that does not mutate anything, and for a
            mutating step that has not had a `MockSpec` written yet --
            `validate_mock_covers_outputs` below flags that second case.
        required_permission: Permission name a caller must hold to invoke
            this step as a tool. Empty string means no permission gate
            beyond whatever already applies to the caller generally.
        expected_latency_seconds: Roughly how long this step normally takes
            to run for real (e.g. a step that sleeps ~60s waiting on an
            external system). `0.0` means "fast, synchronous" -- the
            default, matching every step handler before this field existed.
    """

    description: str = ""
    consumes_state: tuple[str, ...] = ()
    optional_consumes_state: tuple[str, ...] = ()
    produces_state: tuple[str, ...] = ()
    consumes_results: tuple[str, ...] = ()
    params: tuple[ParamSpec, ...] = ()
    guard_keys: tuple[str, ...] = ()
    side_effects: str = ""
    mutates: bool = False
    mutation_kind: str = ""
    outputs: tuple[OutputSpec, ...] = ()
    mock: Optional[MockSpec] = None
    required_permission: str = ""
    expected_latency_seconds: float = 0.0


def validate_mock_covers_outputs(contract: StepContract) -> list[str]:
    """Check that a mutating contract's MockSpec covers its produces_state keys.

    Returns a list of human-readable findings; an empty list means the mock
    is adequate (or the contract doesn't mutate, so no mock is required at
    all). Never raises -- this is meant to be called from save-time
    validation (see `skill_validation.py`) and from ad-hoc audits, neither
    of which wants an exception for a data problem.

    A `MockSpec` that doesn't populate every `produces_state` key is the
    failure mode that makes a mocked run worthless: mock `copy_lpp_template`
    into an empty result and `populate_lpp_cells` fails its `document_id`
    precondition, so the run collapses at the first mutation and a mocked
    pass proves nothing. This only checks `produces_state` coverage in
    `mock.state_updates` -- it does not attempt to validate `outputs` entries
    whose `where="data"` against `mock.data`, since not every `produces_state`
    key necessarily has a matching `OutputSpec` yet.
    """
    if not contract.mutates:
        return []

    if contract.mock is None:
        return [
            "mutates=True but mock is None -- a mock run would have nothing "
            "to return for this step."
        ]

    findings: list[str] = []
    missing = [key for key in contract.produces_state if key not in contract.mock.state_updates]
    if missing:
        findings.append(
            "mock.state_updates is missing produces_state key(s): "
            f"{', '.join(missing)} -- a mocked run would fail any later "
            "step's precondition on these keys."
        )
    return findings


__all__ = [
    "MUTATION_KINDS",
    "MockSpec",
    "OutputSpec",
    "ParamSpec",
    "StepContract",
    "validate_mock_covers_outputs",
]
