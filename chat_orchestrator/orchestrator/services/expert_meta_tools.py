"""Meta-tools exposing expert workflow introspection AND execution to the main LLM.

Phase D of the agentic expert workflows effort. Phase C added `StepContract`
dataclasses to package_generator (LPP) step handlers and
`WorkflowExecutor.run_single_step()` / `validate_step_prerequisites()` for
out-of-order single-step execution -- but nothing called them yet. This
module exposes that machinery to the main conversation LLM as ordinary tool
calls, so staff users can say things like "regenerate the map for Foo" in
plain language instead of only via the rigid `/lpp` slash command.

Three of the four functions here (`list_steps`/`find_packet`/
`get_packet_state`) are read-only introspection. The fourth, `run_steps`, is
the highest-risk piece of this effort: it actually EXECUTES workflow steps
(including, transitively via `run_missing_prerequisites`, steps the caller
never explicitly named) on the LLM's/user's say-so. See `run_steps`'s own
docstring for the two-phase dry-run-preview-then-confirm design that keeps
that auto-execution from ever happening silently.

All four are dispatched from
`orchestrator.graphs.conversation_graph._handle_expert_meta_tool_call` as
in-process "virtual tools" (see `orchestrator.graphs.nodes.prepare_tools`'s
`EXPERT_LIST_STEPS_TOOL_DEF` / `EXPERT_FIND_PACKET_TOOL_DEF` /
`EXPERT_GET_PACKET_STATE_TOOL_DEF` / `EXPERT_RUN_STEPS_TOOL_DEF`) -- not
backed by an mcp_servers server. Each is awaited directly and returns its
result synchronously within the tool-call turn; there is no fire-and-poll
background-task mechanism here (that pattern, used by
`expert_tool_runner.py` for persistent agents invoking a FULL workflow run,
does not apply here -- run_steps executes in-line and returns synchronously,
same as the three read-only lookups).

None of these functions raise to their caller -- every public function wraps
its body in a broad try/except and returns an `{"error": ...}`-shaped dict on
an UNEXPECTED failure, matching `expert_tool_runner.py`'s own defensive
style. This is deliberate: the caller is a tool-call dispatcher that will
relay whatever comes back to the LLM (and from there, potentially the
user), so an unhandled exception here would surface as an ugly 500 instead
of a clean, actionable tool result. Expected/structured outcomes --
`run_steps`'s `blocked`/`needs_confirmation`/`needs_user_input` payloads --
are NOT run through `sanitize_error_for_user`; they are normal, informative
results with their own clear messages, not opaque internal errors.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Set

from orchestrator.experts.step_registry import get_step_contract, get_step_handler
from orchestrator.experts.workflow_executor import WorkflowExecutor
from orchestrator.services.expert_instructions_provider import ExpertInstructionsProvider
from orchestrator.services.work_packet_service import WorkPacketService
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Packet_state keys ending in this suffix hold a Google Drive file ID for a
# produced artifact (see shared.grid_design.artifact_log / workflow_executor.py's
# own _DRIVE_ID_SUFFIX). Surfaced separately in find_packet's summary so the
# LLM can see what artifacts a packet has already produced without dumping
# the entire packet_state.
_DRIVE_ID_SUFFIX = "_drive_id"


async def list_steps(expert_id: str, packet_type: str) -> Dict[str, Any]:
    """Return an expert workflow's recipe merged with each step's data-dependency contract.

    Loads the expert config, parses the raw `### Workflow` lines for
    `packet_type` into `ParsedStep`s, and for each step looks up its
    `StepContract` (only meaningful for `[function:...]` steps -- `[llm]`
    steps typically have none, which is handled gracefully, not as an
    error).

    Args:
        expert_id: Expert identifier that owns this workflow, e.g. "lpp_expert".
        packet_type: Packet type whose workflow recipe to inspect.

    Returns:
        `{"expert_id", "packet_type", "steps": [...]}` on success, where
        each step dict always has `name`/`step_type`/`description`, plus
        `consumes_state`/`optional_consumes_state`/`produces_state`/
        `consumes_results`/`params`/`guard_keys`/`side_effects` when a
        `StepContract` is registered for that step.
        `{"error": ...}` if the expert or packet_type is not found -- never
        raises.
    """
    try:
        provider = ExpertInstructionsProvider()
        expert_config = await provider.get_expert_config(expert_id)
        if expert_config is None:
            return {"error": f"Unknown expert_id: {expert_id!r}"}

        if packet_type not in expert_config.workflows:
            return {
                "error": (
                    f"Unknown packet_type {packet_type!r} for expert {expert_id!r}. "
                    f"Available packet types: {sorted(expert_config.workflows.keys())}"
                )
            }

        workflow_lines = expert_config.get_workflow(packet_type)

        # A lightweight WorkflowExecutor instance used ONLY to reuse its
        # step-line parsing logic. _parse_step_line/parse_workflow are pure
        # string parsing -- they never touch self.gemini/self.packet_service/
        # self.mcp_executor -- so constructing a full executor with live
        # Gemini/MCP clients would be wasteful for what is, here, just a
        # string-parsing operation.
        executor = WorkflowExecutor(gemini_client=None, packet_service=None, mcp_executor=None)  # type: ignore[arg-type]
        parsed_steps = executor.parse_workflow(workflow_lines)

        steps_out: List[Dict[str, Any]] = []
        for step in parsed_steps:
            entry: Dict[str, Any] = {
                "name": step.name,
                "step_type": step.step_type,
                "description": step.description,
            }

            contract = get_step_contract(step.name)
            if contract is not None:
                entry["consumes_state"] = list(contract.consumes_state)
                entry["optional_consumes_state"] = list(contract.optional_consumes_state)
                entry["produces_state"] = list(contract.produces_state)
                entry["consumes_results"] = list(contract.consumes_results)
                entry["params"] = [
                    {
                        "name": param.name,
                        "param_type": param.param_type,
                        "description": param.description,
                        "synonyms": list(param.synonyms),
                        "required": param.required,
                        "default": param.default,
                    }
                    for param in contract.params
                ]
                entry["guard_keys"] = list(contract.guard_keys)
                entry["side_effects"] = contract.side_effects

            steps_out.append(entry)

        return {"expert_id": expert_id, "packet_type": packet_type, "steps": steps_out}

    except Exception as e:
        LOGGER.exception(f"expert_meta_tools.list_steps failed for {expert_id}/{packet_type}: {e}")
        return {"error": sanitize_error_for_user(str(e), context="listing workflow steps")}


async def find_packet(packet_type: str, key_entity: str, organization_id: int) -> Dict[str, Any]:
    """Find an existing packet for this key entity and summarize its progress.

    Uses `WorkPacketService.find_packets_by_entity`, which -- unlike
    `find_similar_completed` -- searches packets of ANY status (in-progress,
    awaiting_input, failed, blocked, completed), so the caller gets the most
    complete, accurate picture of whether work already exists for this
    entity, not just whether a COMPLETED run exists.

    Args:
        packet_type: Packet type to search for, e.g. "light_preliminary_package".
        key_entity: Site/grid name (or other subject) the packet was created for.
        organization_id: Org filter.

    Returns:
        On a match: `{"found": True, "packet_id", "packet_status",
        "steps_completed", "design_id", "artifact_drive_ids", "created_at",
        "updated_at"}`.
        No match: `{"found": False, "packet_type", "key_entity"}` -- this is
        a normal, expected outcome (not an error) the LLM should relay
        plainly, e.g. "no existing LPP found for Foo, want me to start one?"
        On an unexpected failure: `{"error": ...}` -- never raises.
    """
    try:
        packet_service = WorkPacketService()
        packets = await packet_service.find_packets_by_entity(
            packet_type=packet_type,
            key_entity=key_entity,
            organization_id=organization_id,
        )

        if not packets:
            return {"found": False, "packet_type": packet_type, "key_entity": key_entity}

        # Most recently updated match.
        packet = packets[0]
        state = packet.get("packet_state") or {}
        artifact_drive_ids = {
            key: value for key, value in state.items() if key.endswith(_DRIVE_ID_SUFFIX) and value
        }

        return {
            "found": True,
            "packet_id": packet.get("packet_id"),
            "packet_status": packet.get("packet_status"),
            "steps_completed": packet.get("steps_completed") or [],
            "design_id": state.get("design_id"),
            "artifact_drive_ids": artifact_drive_ids,
            "created_at": packet.get("created_at"),
            "updated_at": packet.get("updated_at"),
        }

    except Exception as e:
        LOGGER.exception(
            f"expert_meta_tools.find_packet failed for {packet_type}/{key_entity}: {e}"
        )
        return {"error": sanitize_error_for_user(str(e), context="looking up an existing packet")}


async def get_packet_state(packet_id: str, keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """Fetch a packet's state, optionally filtered to specific keys.

    Returns `packet_state` for a still-running/paused packet. For a
    completed packet, `packet_outputs` is merged on top of `packet_state`
    (outputs take precedence) since that's where a completed workflow's
    authoritative final values live.

    Args:
        packet_id: The packet's human-readable packet_id or UUID.
        keys: Optional list of specific state keys to fetch. Missing keys are
            simply absent from the result -- this never errors on a key that
            doesn't exist, since asking about a key that happens to be unset
            is a normal, expected outcome, not a caller mistake.

    Returns:
        `{"packet_id", "packet_status", "state": {...}}` on success.
        `{"error": "Packet not found"}` if `packet_id` doesn't resolve to a
        real packet. `{"error": ...}` on an unexpected failure. Never raises.
    """
    try:
        packet_service = WorkPacketService()
        packet = await packet_service.get_packet(packet_id)
        if not packet:
            return {"error": "Packet not found"}

        status = packet.get("packet_status")
        state = dict(packet.get("packet_state") or {})
        if status == "completed":
            state.update(packet.get("packet_outputs") or {})

        if keys:
            state = {key: state[key] for key in keys if key in state}

        return {
            "packet_id": packet.get("packet_id"),
            "packet_status": status,
            "state": state,
        }

    except Exception as e:
        LOGGER.exception(f"expert_meta_tools.get_packet_state failed for {packet_id}: {e}")
        return {"error": sanitize_error_for_user(str(e), context="fetching packet state")}


async def _walk_producers(
    executor: WorkflowExecutor,
    packet: Dict[str, Any],
    step_name: str,
    visited: Set[str],
    order: List[str],
    seen: Set[str],
    blocked: List[Dict[str, Any]],
    force: bool = False,
) -> None:
    """Recursively discover producer steps `step_name` would need, WITHOUT running anything.

    This mirrors `WorkflowExecutor.run_single_step`'s own recursive
    producer-walk (see `workflow_executor.py:2976-3017`) exactly, including
    its cycle guard AND its already-completed short-circuit, but never calls
    `run_single_step` and never mutates the packet -- it only calls the
    read-only `validate_step_prerequisites` against the SAME `packet`
    snapshot at every depth (there is nothing to re-fetch: nothing has
    actually executed).

    Args:
        executor: Used only for its `validate_step_prerequisites` method.
        packet: The packet snapshot to validate against (unchanged throughout
            the whole recursive walk -- a pure preview).
        step_name: The step whose prerequisites to check.
        visited: Cycle guard for THIS branch of the recursion -- mirrors
            `run_single_step`'s own `_producer_visited`. Two steps whose
            contracts claim to mutually produce each other's dependency stop
            recursing into one another rather than looping forever; this is
            intentionally NOT treated as `blocked` (a producer_chain entry
            genuinely exists) -- `run_single_step` would hit the exact same
            situation at execution time and surface it as a normal
            `needs_user_input` pause, not a hard refusal, so this preview
            doesn't second-guess that by inventing a stricter outcome here.
        order: Ordered, deduped list of producer step names discovered so
            far across the WHOLE preview (shared across all top-level
            requested steps) -- mutated in place. Never contains a step the
            caller explicitly requested (see `seen`'s docstring below).
        seen: Dedup set for `order`, seeded by the caller with the full set
            of explicitly-requested step names before the first call so that
            a producer this walk discovers, which happens to be one of the
            OTHER steps the caller already explicitly asked for, is treated
            as "already accounted for" (no extra confirmation needed for
            something the user already asked for) rather than re-added to
            `order`.
        blocked: Accumulates one entry per missing item, anywhere in the
            recursion, that has NO known producer at all -- mutated in
            place. A non-empty `blocked` at the end means the whole request
            must be refused outright (see `run_steps`).
        force: Only meaningful for the TOP-LEVEL call (the explicitly
            requested step) -- mirrors `run_single_step`'s Step 1
            already-completed check (`if step_name in steps_completed and
            not force`, `workflow_executor.py:2955-2956`), which only ever
            receives the caller's top-level `force` for the step being run
            directly. Every recursive call this function makes for an
            auto-discovered PRODUCER always passes `force=False` (the
            default), exactly matching `run_single_step`'s own
            producer-recursion call, which hardcodes `force=False`
            regardless of the outer `force` (`workflow_executor.py:3008`) --
            producers are never force-rerun, only the step that was actually
            requested can be.
    """
    if get_step_handler(step_name) is None:
        blocked.append(
            {
                "step": step_name,
                "missing_item": None,
                "reason": f"'{step_name}' is not a registered step.",
            }
        )
        return

    # Already-completed short-circuit -- mirrors run_single_step's Step 1
    # (workflow_executor.py:2954-2956) EXACTLY: an already-completed step is
    # treated as satisfied and its own prerequisites are never walked into,
    # so the preview can't report producers for a step that would actually
    # short-circuit before ever checking prerequisites at real execution
    # time. See the `force` arg docstring above for why only the top-level
    # call's `force` value matters here.
    steps_completed = packet.get("steps_completed") or []
    if step_name in steps_completed and not force:
        return

    report = await executor.validate_step_prerequisites(packet, step_name)
    if report.satisfied:
        return

    new_visited = visited | {step_name}
    for item in list(report.missing_state) + list(report.missing_results):
        producers = report.producer_chain.get(item)
        if not producers:
            blocked.append(
                {
                    "step": step_name,
                    "missing_item": item,
                    "reason": (
                        f"'{step_name}' needs '{item}', and no registered step can "
                        "automatically produce it."
                    ),
                }
            )
            continue

        producer_step_name = producers[0]
        if producer_step_name in new_visited:
            # Cycle guard -- see the `visited` arg docstring above.
            continue
        if producer_step_name in seen:
            # Already scheduled earlier in this preview (either an explicitly
            # requested step, or a producer some other branch already found).
            continue
        if producer_step_name in steps_completed:
            # The producer itself already ran (and, by definition, already
            # produced `item` previously -- unless `force`-cleared, which
            # never applies to an auto-inserted producer; see the `force`
            # arg docstring). Mirrors run_single_step's real behavior
            # EXACTLY: the recursive `run_single_step(producer_step_name,
            # force=False, ...)` call it makes for this exact producer would
            # short-circuit on ITS OWN Step 1 before ever checking
            # prerequisites, so there is nothing left to auto-run and
            # nothing to confirm -- don't recurse into it, and don't add it
            # to `order`/`auto_inserted_steps` (this is deliberately a
            # SEPARATE check from the top-of-function short-circuit above:
            # that one covers `step_name` itself being the already-completed
            # step passed INTO this call; this one covers a `step_name`
            # discovered here, mid-loop, AS a producer candidate -- callers
            # of `_walk_producers` always add whatever it's called with to
            # `order` once it returns, unless skipped up front like this).
            continue

        await _walk_producers(
            executor, packet, producer_step_name, new_visited, order, seen, blocked, force=False
        )
        if producer_step_name not in seen:
            seen.add(producer_step_name)
            order.append(producer_step_name)


def _compute_confirmation_token(
    packet_id: str,
    state_version: Any,
    steps: List[str],
    auto_inserted_names: List[str],
    param_overrides_json: Optional[str],
    force: bool,
) -> str:
    """Deterministic staleness/anti-hallucination guard for run_steps' confirmation gate.

    Hashes everything that defines an exact confirmation-needed plan AND the
    packet's current `state_version` (Phase C's optimistic-concurrency
    column -- see `work_packet_service.py`'s `update_state`, which bumps it
    on every `packet_state` write) so the resulting token is both:
    (a) impossible to guess/fabricate without having actually received a real
        Phase-1 `needs_confirmation` result from the server (it depends on
        server-computed `auto_inserted_names`, not just caller-supplied
        input), and
    (b) automatically invalidated the moment the packet's state changes
        underneath it (a bumped `state_version` changes the hash).

    NOT a cryptographic secret and NOT an auth boundary -- callers already
    have `packet_id` and complete visibility into their own `steps`/
    `param_overrides_json`/`force`. What matters is determinism (same inputs
    -> same token, always) and sensitivity to every input that could make
    "please confirm this plan" mean something different than what a real
    Phase-1 call actually computed and showed the user.

    Args:
        packet_id: The resolved packet's `packet_id`.
        state_version: The packet's `state_version` column value at the
            moment Phase 1 computed this plan (read via
            `packet.get("state_version")`, per Phase C's optimistic
            concurrency fix). Any concurrent `update_state` between two
            `run_steps` calls bumps this, so a token computed before that
            write will never match a token recomputed after it.
        steps: The caller's explicitly-requested step names (order-
            insensitive here -- sorted before hashing).
        auto_inserted_names: The producer step names Phase 1 discovered would
            need to auto-run (order-insensitive here -- sorted before
            hashing; the user-facing `auto_inserted_steps` list itself stays
            execution-ordered).
        param_overrides_json: The caller's raw per-step override JSON string
            (or None/empty), included verbatim so a change in overrides
            between calls invalidates a previously-issued token.
        force: The caller's top-level `force` flag.

    Returns:
        A 24-character hex digest (sha256, truncated) -- short enough to be
        a normal string LLM tool argument, long enough that it can't be
        practically guessed.
    """
    payload = (
        f"{packet_id}:{state_version}:{sorted(steps)}:{sorted(auto_inserted_names)}:"
        f"{param_overrides_json or ''}:{force}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


async def run_steps(
    steps: List[str],
    packet_id: Optional[str] = None,
    expert_id: Optional[str] = None,
    packet_type: Optional[str] = None,
    key_entity: Optional[str] = None,
    param_overrides_json: Optional[str] = None,
    force: bool = False,
    confirmation_token: Optional[str] = None,
    organization_id: Optional[int] = None,
    user_email: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one or more named expert workflow steps out of order, with a confirmation gate.

    This is the execution half of the Phase D meta-tools (the other three --
    `list_steps`/`find_packet`/`get_packet_state` -- are read-only). It is a
    thin, carefully-gated wrapper around `WorkflowExecutor.run_single_step`,
    which itself will happily auto-run "producer" steps to satisfy missing
    prerequisites with NO confirmation of any kind -- appropriate for that
    low-level primitive, but NOT appropriate here, where the caller is the
    main conversation LLM acting on a plain-language user request. So this
    function never lets a producer step run without either (a) the caller
    not needing one at all, or (b) the caller supplying a
    `confirmation_token` that this function itself, freshly, recomputes and
    verifies matches -- NOT a bare caller-supplied boolean. A bare
    `confirmed: bool` (this function's v1 shape) let a caller -- an LLM,
    possibly hallucinating, or influenced by content read via another tool --
    call this function with `confirmed=True` as its very first call for a
    packet that never went through the confirmation round-trip, executing
    every auto-inserted producer with zero human ever having seen the plan.
    The token closes that hole: it can only be produced by this function
    itself (via `_compute_confirmation_token`), from a real Phase-1 result,
    and it goes stale automatically the moment the packet's `state_version`
    changes underneath it.

    Two-phase design:

    Phase 1 (dry-run preview, always runs, never mutates anything): for each
    requested step, walks the exact same recursive producer-discovery logic
    `run_single_step` itself uses (see `_walk_producers`, mirroring
    `workflow_executor.py:2976-3017`) but only via the read-only
    `validate_step_prerequisites` -- never calling `run_single_step`. This
    produces two possible outcomes:
      - `blocked`: some missing item, anywhere in the recursive chain, has NO
        known producer. Refused outright -- no confirmation would help this,
        so none is offered.
      - `auto_inserted`: an ordered, deduped list of producer steps that
        would need to run before the caller's own requested steps.

    Phase 2 (confirm-or-execute): if `auto_inserted` is empty, execute
    immediately -- nothing to confirm, so no token is needed or checked, same
    as before this change. Otherwise, this function recomputes the expected
    confirmation token FRESH against the packet just (re-)fetched for THIS
    call (same function, same inputs, current `state_version`) and compares
    it to the caller-supplied `confirmation_token`:
      - Match: proceed to execute -- calling the real
        `run_single_step(..., run_missing_prerequisites=True)` once per
        requested step, IN ORDER, reusing its own already-hardened
        prerequisite-running logic rather than re-implementing execution
        here (Phase 1's walk exists purely to build an accurate confirmation
        preview). A match proves both that the plan is identical to what a
        real prior Phase-1 call would have shown a human, AND that the
        packet hasn't changed since (a bumped `state_version` changes the
        hash, so a token computed before a concurrent edit will not match).
      - No match (missing, wrong, fabricated, or stale because the packet
        changed since the token was issued) -- return a FRESH
        `needs_confirmation` payload (with a newly-computed token) and
        execute NOTHING. This deliberately does not try to distinguish
        "never confirmed" from "confirmed, but the packet changed since" in
        the message -- both need the same fix (relay the current plan, get
        the human to confirm THIS plan), and a fabricated token must not be
        able to distinguish which case it hit.
    Nothing is ever executed on a missing, mismatched, or fabricated token --
    the single most important property of this whole function.

    Packet resolution: pass `packet_id` to act on an existing packet, or
    leave it out and pass `expert_id`/`packet_type`/`key_entity` to create a
    new one (mirrors `expert_tool_runner.start_expert_workflow`'s
    packet-creation block, minus its fire-and-poll background-task
    machinery -- this function executes in-line and returns synchronously).

    v1 scope decision -- no live progress pushes: the `StepContext` built
    here uses `user_context=None`, exactly like
    `expert_tool_runner._execute_headless`'s headless pattern. This means
    `context.send_progress_to_user` is a silent no-op (see
    `step_context.py:327-352` -- it requires `user_context.chat_id` and does
    nothing without it), so a slow multi-step run triggered by this tool
    does NOT push interim Telegram messages; this function's own synchronous
    return value is the only feedback channel for now. A future enhancement
    could thread the real `UserContext` through `metadata` if steps prove
    slow enough that users need interim feedback, but `metadata` in
    `_execute_tool_calls`'s dispatch path doesn't currently carry the full
    object (only flattened fields) -- that's a separate, broader change.

    Args:
        steps: Ordered list of step names to run (in this order).
        packet_id: Existing packet to act on. If omitted, a new packet is
            created from `expert_id`/`packet_type`/`key_entity` (all three
            required in that case).
        expert_id: Expert owning the workflow (new-packet path only).
        packet_type: Packet type to create (new-packet path only).
        key_entity: Site/grid/subject name for the new packet (new-packet
            path only). Stored as BOTH `packet_inputs["key_entity"]` and
            `packet_inputs["site_name"]` -- this tool's schema only accepts a
            single entity-name string (unlike `start_expert_workflow`'s
            fully generic `inputs` dict), so both conventional keys are
            populated to match the `key_entity`/`site_name` resolution order
            used elsewhere in this module and in
            `validate_step_prerequisites`'s own Tier 2 lookup.
        param_overrides_json: Optional JSON string shaped as
            `{"step_name": {"param_name": value, ...}, ...}` -- a per-step
            parameter override dict. Parsed defensively: invalid JSON
            returns a clean `{"error": ...}` dict, never raises.
        force: Passed straight through to every `run_single_step` call --
            its existing "re-run even if already completed, clearing
            guard_keys" semantics. Does NOT change confirmation-gate
            behavior. Also folded into the confirmation token hash, so
            changing `force` between calls invalidates a previously-issued
            token.
        confirmation_token: The `confirmation_token` value from a prior
            `needs_confirmation` response, echoed back verbatim to confirm
            that exact plan. Only meaningful when Phase 1 finds
            auto-inserted producers; ignored (no token needed) when nothing
            needs auto-running. Must match what this function itself
            recomputes fresh for the CURRENT packet/plan -- a caller cannot
            fabricate or guess a valid value, and a value that was valid
            earlier stops matching the moment the packet's `state_version`
            changes.
        organization_id: Org context, threaded through from `metadata` in
            the dispatch handler.
        user_email: Requesting user's email, threaded through from
            `metadata`.
        session_id: Session context, threaded through from `metadata`.

    Returns:
        `{"success": False, "blocked": True, "details": [...]}` -- hard
        refusal, nothing executed, no confirmation offered.
        `{"needs_confirmation": True, "requested_steps", "auto_inserted_steps",
        "confirmation_token", "message"}` -- nothing executed yet; caller
        must relay `auto_inserted_steps` (with their `side_effects`) to the
        user and, if they agree, re-call with the exact same `steps`/
        `param_overrides_json`/`force` PLUS this `confirmation_token` value
        (never a fabricated one -- it will simply be re-rejected with a
        fresh token).
        `{"success": False, "needs_user_input": True, ...}` -- execution
        stopped because a step (requested or auto-inserted) paused for
        input.
        `{"success": False, "error": ..., "stopped_at_step", "executed_steps"}`
        -- execution stopped because a step failed or was refused;
        `executed_steps` lists whatever succeeded before the failure.
        `{"success": True, "executed_steps": [...]}` -- every requested step
        ran (or was already complete) without a stop condition.
        `{"error": ...}` on an unexpected failure. Never raises.
    """
    try:
        if not steps:
            return {"error": "steps must be a non-empty list of step names."}

        param_overrides: Dict[str, Dict[str, Any]] = {}
        if param_overrides_json:
            try:
                parsed_overrides = json.loads(param_overrides_json)
            except (TypeError, ValueError) as e:
                return {"error": f"param_overrides_json is not valid JSON: {e}"}
            if not isinstance(parsed_overrides, dict):
                return {"error": "param_overrides_json must decode to a JSON object."}
            param_overrides = parsed_overrides

        packet_service = WorkPacketService()
        provider = ExpertInstructionsProvider()

        # --- Resolve the packet: existing, or newly created ---------------------
        if packet_id:
            packet = await packet_service.get_packet(packet_id)
            if not packet:
                return {"error": f"Packet not found: {packet_id!r}"}
            resolved_expert_id = packet.get("assigned_expert")
            resolved_packet_type = packet.get("packet_type")
        else:
            if not expert_id or not packet_type or not key_entity:
                return {
                    "error": (
                        "packet_id was not given, so expert_id, packet_type, and "
                        "key_entity are all required to create a new packet."
                    )
                }

            expert_config_check = await provider.get_expert_config(expert_id)
            if expert_config_check is None:
                return {"error": f"Unknown expert_id: {expert_id!r}"}
            if not expert_config_check.get_workflow(packet_type):
                return {
                    "error": (
                        f"Unknown packet_type {packet_type!r} for expert {expert_id!r}. "
                        f"Available packet types: {sorted(expert_config_check.workflows.keys())}"
                    )
                }

            resolved_org_id = organization_id or int(os.getenv("STAFF_ORG_ID", "2"))
            packet = await packet_service.create_packet(
                packet_type=packet_type,
                packet_title=f"[expert_run_steps] {expert_id}: {packet_type} ({key_entity})",
                packet_goal=f"Run step(s) {steps} for {key_entity}",
                assigned_expert=expert_id,
                packet_inputs={"key_entity": key_entity, "site_name": key_entity},
                session_id=session_id or "",
                organization_id=resolved_org_id,
                requested_by_email=user_email,
            )
            resolved_expert_id = expert_id
            resolved_packet_type = packet_type

        expert_config = await provider.get_expert_config(resolved_expert_id)
        if expert_config is None:
            return {"error": f"Unknown expert_id: {resolved_expert_id!r}"}

        # --- Build StepContext/WorkflowExecutor (mirrors expert_tool_runner._execute_headless) --
        from orchestrator.clients.gemini import GeminiClient
        from orchestrator.config.settings import GeminiModelConfig, get_settings
        from orchestrator.experts.step_context import StepContext
        from orchestrator.services.tool_executor import ToolExecutor
        from orchestrator.services.tool_registry import ToolRegistry

        settings = get_settings()
        context = StepContext(
            packet_id=packet["packet_id"],
            packet_type=resolved_packet_type,
            packet_goal=packet.get("packet_goal", ""),
            packet_inputs=packet.get("packet_inputs") or {},
            packet_state=packet.get("packet_state") or {},
            current_step="",
            steps_completed=list(packet.get("steps_completed") or []),
            session_id=session_id or packet.get("requested_in_session") or "",
            user_email=user_email,
            organization_id=organization_id,
            # v1 scope: user_context=None suppresses Telegram progress pushes.
            # See this function's own docstring for the full rationale.
            user_context=None,
            call_depth=0,
        )
        registry = ToolRegistry()
        mcp_executor = ToolExecutor(registry=registry, settings=settings)
        context.mcp_executor = mcp_executor

        gemini_client = GeminiClient(
            api_key=settings.google_api_key,
            model_config=GeminiModelConfig(),
        )
        executor = WorkflowExecutor(
            gemini_client=gemini_client,
            packet_service=packet_service,
            mcp_executor=mcp_executor,
        )

        # =====================================================================
        # Phase 1: dry-run preview -- never executes or mutates anything.
        # =====================================================================
        order: List[str] = []
        seen: Set[str] = set(steps)
        blocked: List[Dict[str, Any]] = []
        for requested_step in steps:
            await _walk_producers(
                executor, packet, requested_step, set(), order, seen, blocked, force=force
            )

        if blocked:
            return {"success": False, "blocked": True, "details": blocked}

        if order:
            auto_inserted_steps = []
            for name in order:
                contract = get_step_contract(name)
                auto_inserted_steps.append(
                    {
                        "name": name,
                        "description": contract.description if contract else name,
                        "side_effects": contract.side_effects if contract else "",
                    }
                )

            # Recomputed FRESH against the packet just (re-)fetched for THIS
            # call -- current state_version, current order/auto_inserted
            # names. A caller-supplied token only ever proceeds to Phase 2 if
            # it matches this exactly, which proves both (a) it was actually
            # issued by a real prior Phase-1 call for THIS SAME plan (it
            # can't be guessed/fabricated -- see _compute_confirmation_token),
            # and (b) the packet hasn't changed since (a bumped
            # state_version changes the hash).
            expected_token = _compute_confirmation_token(
                packet_id=packet["packet_id"],
                state_version=packet.get("state_version", 0) or 0,
                steps=steps,
                auto_inserted_names=order,
                param_overrides_json=param_overrides_json,
                force=force,
            )

            if confirmation_token != expected_token:
                message_lines = [
                    f"Running {', '.join(steps)} requires first auto-running "
                    f"{len(order)} prerequisite step(s):"
                ]
                for entry in auto_inserted_steps:
                    side_effects_note = (
                        f" Side effects: {entry['side_effects']}" if entry["side_effects"] else ""
                    )
                    message_lines.append(
                        f"- {entry['name']}: {entry['description']}.{side_effects_note}"
                    )
                if confirmation_token:
                    message_lines.append(
                        "A confirmation_token was provided but does not match this plan "
                        "(it may be stale, mismatched, or the packet may have changed since "
                        "it was issued) -- please re-confirm."
                    )
                message_lines.append(
                    "Relay the above to the user and ask them to confirm before proceeding. "
                    "If they agree, call expert_run_steps again with the exact same "
                    "steps/param_overrides_json/force PLUS confirmation_token set to the "
                    "value in this response. Never fabricate a confirmation_token value."
                )

                return {
                    "needs_confirmation": True,
                    "packet_id": packet["packet_id"],
                    "requested_steps": list(steps),
                    "auto_inserted_steps": auto_inserted_steps,
                    "confirmation_token": expected_token,
                    "message": "\n".join(message_lines),
                }
            # else: confirmation_token matches the freshly-recomputed plan --
            # fall through to Phase 2 execution below.

        # =====================================================================
        # Phase 2: confirm-or-execute -- reuses run_single_step's own
        # already-hardened execution/producer-run logic.
        # =====================================================================
        executed_steps: List[Dict[str, Any]] = []
        for index, step_name in enumerate(steps):
            if index > 0:
                # Refresh packet/context so later steps in THIS loop see
                # completions/state written by earlier steps in this same
                # run -- run_single_step only refreshes internally around
                # its own force/producer-rerun paths, not across separate
                # top-level calls like this loop makes.
                packet = await packet_service.get_packet(packet["packet_id"])
                context.packet_state = packet.get("packet_state") or {}
                context.steps_completed = list(packet.get("steps_completed") or [])

            step_overrides = param_overrides.get(step_name)
            _msg, data = await executor.run_single_step(
                packet,
                step_name,
                context,
                expert_config,
                param_overrides=step_overrides,
                force=force,
                run_missing_prerequisites=True,
            )

            if data.get("needs_user_input"):
                return {
                    "success": False,
                    "needs_user_input": True,
                    "packet_id": packet["packet_id"],
                    "stopped_at_step": step_name,
                    "executed_steps": executed_steps,
                    "details": data,
                }

            if data.get("refused") or ("error" in data and not data.get("success")):
                return {
                    "success": False,
                    "packet_id": packet["packet_id"],
                    "stopped_at_step": step_name,
                    "executed_steps": executed_steps,
                    "error": data.get("error", f"Step '{step_name}' failed."),
                    "details": data,
                }

            executed_steps.append({"step_name": step_name, "result": data})

        return {
            "success": True,
            "packet_id": packet["packet_id"],
            "executed_steps": executed_steps,
        }

    except Exception as e:
        LOGGER.exception(f"expert_meta_tools.run_steps failed: {e}")
        return {"error": sanitize_error_for_user(str(e), context="running expert workflow steps")}


__all__ = ["list_steps", "find_packet", "get_packet_state", "run_steps"]
